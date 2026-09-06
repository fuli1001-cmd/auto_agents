import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import List, Optional

from ..io_utils import read_text, write_text
from ..models import (
    AgentProgressEvent,
    AgentRequest,
    AgentResult,
    ProviderConfig,
    SmartTimeoutConfig,
)
from .base import AgentAdapter, run_subprocess_with_optional_streaming
from ..prompting.runtime import cli_capabilities, last_option
from ..supervision import ProgressDecoder

SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
INLINE_PROMPT_MAX_BYTES = 96 * 1024
_CONVERSATION_PATTERN = re.compile(
    r"(?:Created conversation |conversation=)([0-9a-fA-F-]{36})"
)
_TOOL_STEP_TYPES = {8, 9, 21}


class AntigravityProgressDecoder(ProgressDecoder):
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.offset = 0
        self.session_id = ""
        self.step_snapshot: dict[int, tuple[int, int, str]] = {}

    def feed(self, stream_name: str, chunk: str):
        return ()

    def poll(self):
        events: List[AgentProgressEvent] = []
        if self.log_path.is_file():
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                lines = handle.readlines()
                self.offset = handle.tell()
            for line in lines:
                match = _CONVERSATION_PATTERN.search(line)
                if match:
                    self.session_id = match.group(1)
                events.append(
                    AgentProgressEvent(
                        kind="activity",
                        session_id=self.session_id,
                        detail=self._log_detail(line),
                    )
                )
        if self.session_id:
            events.extend(self._poll_steps())
        return events

    def _poll_steps(self) -> List[AgentProgressEvent]:
        database = SETTINGS_PATH.parent / "conversations" / f"{self.session_id}.db"
        if not database.is_file():
            return []
        try:
            connection = sqlite3.connect(
                f"file:{database}?mode=ro", uri=True, timeout=0.1
            )
            try:
                rows = connection.execute(
                    "select idx, step_type, status, step_payload from steps order by idx"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "unable to open" in str(exc).lower():
                return []
            raise RuntimeError(f"Antigravity progress schema error: {exc}") from exc

        events: List[AgentProgressEvent] = []
        next_snapshot: dict[int, tuple[int, int, str]] = {}
        for idx, step_type, status, payload in rows:
            payload_bytes = bytes(payload or b"")
            fingerprint = hashlib.sha256(payload_bytes).hexdigest()
            current = (int(step_type), int(status), fingerprint)
            next_snapshot[int(idx)] = current
            previous = self.step_snapshot.get(int(idx))
            if current == previous:
                continue
            is_tool = int(step_type) in _TOOL_STEP_TYPES
            if is_tool and int(status) == 3:
                kind = "tool_completed"
            elif is_tool and previous is None:
                kind = "tool_started"
            elif is_tool:
                kind = "tool_progress"
            else:
                kind = "milestone" if int(status) == 3 else "activity"
            events.append(
                AgentProgressEvent(
                    kind=kind,
                    session_id=self.session_id,
                    tool_id=f"step-{idx}" if is_tool else "",
                    fingerprint=fingerprint,
                    detail=f"antigravity step={idx} type={step_type} status={status}",
                    semantic=(kind == "tool_completed"),
                )
            )
        self.step_snapshot = next_snapshot
        return events

    @staticmethod
    def _log_detail(line: str) -> str:
        message = line.split("]", 1)[-1].strip()
        return message[:300]


def _is_prompt_transport_error(summary: str) -> bool:
    """Detect agy replying to one of its own flags instead of the request."""
    text = summary.strip().lower()
    if not text or len(text) > 4000:
        return False
    if not text.startswith(
        ("it looks like", "it seems like", "you entered", "you provided", "you passed")
    ):
        return False
    mentions_option = any(
        option in text
        for option in ("--dangerously-skip-permissions", "--print", "`-p`")
    )
    describes_flag = any(
        phrase in text
        for phrase in (
            "cli startup flag",
            "cli flag",
            "command-line flag",
            "command line flag",
        )
    )
    rejects_as_request = any(
        phrase in text
        for phrase in (
            "rather than a task",
            "rather than a request",
            "rather than a prompt",
            "instead of a task",
            "instead of a request",
            "instead of a prompt",
            "not a task",
            "not a request",
        )
    )
    return mentions_option and describes_flag and rejects_as_request


class AntigravityAdapter(AgentAdapter):
    def __init__(
        self,
        config: ProviderConfig,
        smart_timeout: Optional[SmartTimeoutConfig] = None,
    ) -> None:
        self.config = config
        self.smart_timeout = smart_timeout or SmartTimeoutConfig(enabled=False)

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, request: AgentRequest) -> AgentResult:
        request = self.prepare_request(request)
        log_path = self._progress_log_path(request) if self.smart_timeout.enabled else None
        if log_path is not None:
            write_text(log_path, "")
        command = self._build_command(request, log_path=log_path)
        write_text(request.output_path, "")

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort

        # 动态修改 settings.json 改变模型选择
        original_content: Optional[str] = None
        target_model = last_option(self.config.extra_args, "--model") or self.config.profile_map.get(request.effort)
        
        if target_model and not self._uses_native_model_flag() and SETTINGS_PATH.parent.exists():
            try:
                if SETTINGS_PATH.is_file():
                    original_content = read_text(SETTINGS_PATH)
                    try:
                        settings_data = json.loads(original_content)
                    except json.JSONDecodeError:
                        settings_data = {}
                else:
                    settings_data = {}
                
                settings_data["model"] = target_model
                SETTINGS_PATH.write_text(
                    json.dumps(settings_data, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )
            except Exception:
                # 即使修改配置失败，也允许继续使用默认配置运行
                pass

        try:
            process_result = run_subprocess_with_optional_streaming(
                command,
                request,
                env,
                timeout=request.timeout_seconds or self.config.timeout_seconds or None,
                # agy 1.1+ takes the print prompt as the flag value. Sending
                # it on stdin can make the next CLI option become the prompt.
                stdin_input="",
                idle_timeout=self.config.idle_timeout_seconds or None,
                smart_timeout=self.smart_timeout,
                progress_decoder=(
                    AntigravityProgressDecoder(log_path)
                    if log_path is not None
                    else None
                ),
                provider="antigravity",
            )
            stdout, stderr, returncode, streamed_stdout, streamed_stderr = process_result
        finally:
            # 还原 settings.json
            if original_content is not None:
                try:
                    SETTINGS_PATH.write_text(original_content, encoding="utf-8")
                except Exception:
                    pass

        summary = read_text(request.output_path).strip()
        if not summary and stdout:
            summary = stdout.strip()
            write_text(request.output_path, summary + "\n")

        stderr = stderr.strip()
        protocol_error = _is_prompt_transport_error(summary)
        if protocol_error:
            detail = (
                "provider protocol error: Antigravity CLI treated a startup option "
                "as the prompt"
            )
            stderr = f"{stderr}\n{detail}".strip() if stderr else detail

        return AgentResult(
            prompt_metadata=dict(request.prompt_metadata),
            ok=returncode == 0 and not protocol_error,
            command=command,
            output_path=request.output_path,
            summary=summary,
            model=target_model or "default",
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
            provider_session_id=getattr(process_result, "provider_session_id", ""),
            termination=getattr(process_result, "termination", None),
            supervision_report_path=(
                str(request.progress_report_path) if request.progress_report_path else ""
            ),
        )

    def _build_command(
        self,
        request: AgentRequest,
        *,
        log_path: Optional[Path] = None,
    ) -> List[str]:
        command = [self.config.binary]
        command.extend(["--dangerously-skip-permissions"])  # 跳过权限提示
        command.extend(["--add-dir", str(request.cwd)])  # 添加 workspace 目录
        
        # 注入 print-timeout
        timeout = (
            self.smart_timeout.safety_ceiling_seconds + 60
            if self.smart_timeout.enabled
            else self.config.timeout_seconds or 1800
        )
        command.extend(["--print-timeout", f"{timeout}s"])

        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            command.extend(["--log-file", str(log_path)])

        if request.resume_session_id:
            command.extend(["--conversation", request.resume_session_id])
        
        model = self.config.profile_map.get(request.effort)
        if model and not last_option(self.config.extra_args, "--model") and self._uses_native_model_flag():
            command.extend(["--model", model])
        command.extend(self.config.extra_args)
        # agy parses --print/-p as a string flag. Keep it last so another
        # option can never be consumed as the prompt value.
        command.extend(["--print", self._prompt_argument(request)])
        return command

    def _uses_native_model_flag(self) -> bool:
        return bool(last_option(self.config.extra_args, "--model")) or "--model" in cli_capabilities(self.config.binary)[1]

    @staticmethod
    def _progress_log_path(request: AgentRequest) -> Path:
        if request.progress_report_path is not None:
            return request.progress_report_path.with_suffix(".antigravity.log")
        return request.output_path.with_suffix(".antigravity.log")

    @staticmethod
    def _prompt_argument(request: AgentRequest) -> str:
        if len(request.prompt.encode("utf-8")) <= INLINE_PROMPT_MAX_BYTES:
            return request.prompt

        digest = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()[:16]
        prompt_path = (
            request.cwd
            / ".auto-agents"
            / "runs"
            / "provider-prompts"
            / f"antigravity-{digest}.txt"
        )
        write_text(prompt_path, request.prompt)
        return (
            "The complete authoritative task instructions are stored in this UTF-8 file:\n"
            f"{prompt_path}\n"
            "Read the file in full before doing anything else, then follow its instructions "
            "exactly. Do not answer until you have read it."
        )
