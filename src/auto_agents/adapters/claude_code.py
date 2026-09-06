from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from typing import Callable, List, Optional, Tuple

from ..io_utils import read_text, write_text
from ..models import (
    AgentProgressEvent,
    AgentRequest,
    AgentResult,
    AgentUsage,
    ProviderConfig,
    SmartTimeoutConfig,
)
from .base import AgentAdapter, run_subprocess_with_optional_streaming
from ..prompting.runtime import observed_model_metadata
from ..supervision import ProgressDecoder


# Extra-arg flags that signal the caller has taken over Claude Code
# permission handling; the adapter must not inject its own defaults.
_CLAUDE_PERMISSION_FLAGS = {
    "--permission-mode",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--allowedTools",
    "--allowed-tools",
    "--disallowedTools",
    "--disallowed-tools",
}

_READ_ONLY_SANDBOX_MODES = {"read-only", "readonly"}

_ERROR_RESULT_SUBTYPES = {
    "error_during_execution",
    "error_max_budget_usd",
    "error_max_structured_output_retries",
    "error_max_turns",
}


def _claude_result_error_message(event: dict) -> str:
    result = event.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    errors = event.get("errors")
    if isinstance(errors, list):
        messages = [str(item).strip() for item in errors if str(item).strip()]
        if messages:
            return "\n".join(messages)
    return str(event.get("subtype") or event.get("type") or "claude code error")


def _claude_content_blocks(event: dict) -> List[object]:
    """Return the content blocks of a stream-json assistant/user event."""
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _block_fingerprint(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class ClaudeProgressDecoder(ProgressDecoder):
    """Decode Claude Code ``--output-format stream-json`` JSONL events.

    Event map (print mode with ``--verbose``):
    * ``system/init``    -> activity carrying the session id (resume handle)
    * ``assistant``      -> ``tool_started`` per ``tool_use`` block
    * ``user``           -> ``tool_completed`` per ``tool_result`` block
    * ``result`` (error) -> error event with the provider message
    * ``result`` (ok)    -> completed event
    """

    def feed(self, stream_name: str, chunk: str):
        if stream_name != "stdout":
            return ()
        try:
            event = json.loads(chunk.strip())
        except (json.JSONDecodeError, AttributeError):
            if chunk.strip():
                raise ValueError("Claude Code JSONL stream emitted invalid JSON")
            return ()
        if not isinstance(event, dict):
            raise ValueError("Claude Code JSONL event must be an object")
        event_type = str(event.get("type", ""))
        if not event_type:
            raise ValueError("Claude Code JSONL event is missing type")
        if event_type == "system":
            subtype = str(event.get("subtype", ""))
            detail = f"system.{subtype}" if subtype else event_type
            session_id = str(event.get("session_id", ""))
            return (
                AgentProgressEvent(
                    kind="activity",
                    session_id=session_id,
                    detail=detail,
                ),
            )
        if event_type == "assistant":
            return tuple(self._assistant_events(event))
        if event_type == "user":
            return tuple(self._tool_result_events(event))
        if event_type == "stream_event":
            # Partial message deltas; only emitted with
            # --include-partial-messages, which this adapter never enables.
            return ()
        if event_type == "result":
            subtype = str(event.get("subtype", ""))
            if bool(event.get("is_error")) or subtype in _ERROR_RESULT_SUBTYPES:
                message = _claude_result_error_message(event)
                return (AgentProgressEvent(kind="error", detail=message),)
            return (AgentProgressEvent(kind="completed", detail=event_type),)
        return (AgentProgressEvent(kind="activity", detail=event_type),)

    @staticmethod
    def _assistant_events(event: dict) -> List[AgentProgressEvent]:
        events: List[AgentProgressEvent] = []
        for block in _claude_content_blocks(event):
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))
            if block_type in {"tool_use", "server_tool_use"}:
                events.append(
                    AgentProgressEvent(
                        kind="tool_started",
                        tool_id=str(block.get("id", "")),
                        fingerprint=_block_fingerprint(
                            {"name": block.get("name", ""), "input": block.get("input", {})}
                        ),
                        detail=str(block.get("name") or "tool"),
                    )
                )
            elif block_type.endswith("_tool_result"):
                events.append(
                    ClaudeProgressDecoder._tool_completed_event(block, block_type)
                )
            elif block_type == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    events.append(AgentProgressEvent(kind="activity", detail="assistant.text"))
        return events

    @staticmethod
    def _tool_result_events(event: dict) -> List[AgentProgressEvent]:
        events: List[AgentProgressEvent] = []
        for block in _claude_content_blocks(event):
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")) != "tool_result":
                continue
            events.append(ClaudeProgressDecoder._tool_completed_event(block, "tool_result"))
        return events

    @staticmethod
    def _tool_completed_event(block: dict, block_type: str) -> AgentProgressEvent:
        return AgentProgressEvent(
            kind="tool_completed",
            tool_id=str(block.get("tool_use_id", "")),
            fingerprint=_block_fingerprint(
                {
                    "tool_use_id": block.get("tool_use_id", ""),
                    "content": block.get("content", ""),
                }
            ),
            detail=block_type,
            semantic=True,
        )


class ClaudeCodeAdapter(AgentAdapter):
    """Headless Claude Code adapter.

    Mirrors the Codex adapter contract:
    * ``claude -p --output-format stream-json --verbose`` for a JSONL event stream
    * ``profile_map`` maps effort levels to ``--model`` values
    * ``--resume <session_id>`` restores a prior conversation
    * the final ``result`` event carries usage and the answer text
    """

    def __init__(
        self,
        config: ProviderConfig,
        smart_timeout: Optional[SmartTimeoutConfig] = None,
    ) -> None:
        self.config = config
        self.smart_timeout = smart_timeout or SmartTimeoutConfig(enabled=False)

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def supports_image_attachments(self) -> bool:
        return True

    def run(self, request: AgentRequest) -> AgentResult:
        request = self.prepare_request(request)
        command = self._build_command(request)

        # Clear stale output so a reused output_path cannot mask fresh results.
        write_text(request.output_path, "")

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort
        timeout = request.timeout_seconds or self.config.timeout_seconds or None

        prompt = self._effective_prompt(request)
        if self.config.prompt_via_stdin:
            stdin_input: Optional[str] = prompt
        else:
            stdin_input = ""

        # Wrap the stream callback to parse the JSONL event stream in
        # real-time, forwarding only visible assistant messages and errors.
        filtered_request = request
        if request.stream_output is not None:
            filtered_request = replace(
                request,
                stream_output=self._make_json_stream_filter(request.stream_output),
            )

        process_result = run_subprocess_with_optional_streaming(
            command,
            filtered_request,
            env,
            timeout=timeout,
            stdin_input=stdin_input,
            idle_timeout=self.config.idle_timeout_seconds or None,
            smart_timeout=self.smart_timeout,
            progress_decoder=ClaudeProgressDecoder(),
            provider="claude-code",
        )
        stdout_raw, stderr, returncode, streamed_stdout, streamed_stderr = process_result

        visible_stdout, usage, error_messages, session_id, final_text = (
            self._parse_json_stdout(stdout_raw)
        )
        stderr = stderr.strip()
        if error_messages:
            err_text = "\n".join(error_messages)
            stderr = f"{stderr}\n{err_text}".strip() if stderr else err_text

        summary = final_text if not error_messages else ""
        if not summary and visible_stdout:
            summary = visible_stdout.strip()
        if summary:
            write_text(request.output_path, summary + "\n")
        else:
            summary = read_text(request.output_path).strip()

        provider_session_id = (
            getattr(process_result, "provider_session_id", "") or session_id
        )

        return AgentResult(
            prompt_metadata=observed_model_metadata(request, stdout_raw),
            ok=returncode == 0 and not error_messages,
            command=command,
            output_path=request.output_path,
            summary=summary,
            model=self._model_label(request),
            usage=usage,
            stdout=visible_stdout,
            stderr=stderr,
            returncode=returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
            provider_session_id=provider_session_id,
            termination=getattr(process_result, "termination", None),
            cleanup_incomplete=getattr(process_result, "cleanup_incomplete", False),
            supervision_report_path=(
                str(request.progress_report_path) if request.progress_report_path else ""
            ),
        )

    # -- internal helpers --------------------------------------------------

    def _build_command(self, request: AgentRequest) -> List[str]:
        command: List[str] = [
            self.config.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        if request.resume_session_id:
            command.extend(["--resume", request.resume_session_id])

        profile = self.config.profile_map.get(request.effort)
        if profile and not self._has_model_flag():
            command.extend(["--model", profile])

        command.extend(self._permission_args(request))

        command.extend(self.config.extra_args)

        if not self.config.prompt_via_stdin:
            command.append(self._effective_prompt(request))

        return command

    def _permission_args(self, request: AgentRequest) -> List[str]:
        if self._has_permission_flag():
            return []
        sandbox = str(request.sandbox_mode or "").strip().lower()
        if sandbox in _READ_ONLY_SANDBOX_MODES:
            return ["--permission-mode", "dontAsk"]
        # Headless default. Codex's ``workspace-write`` sandbox lets the agent
        # edit files and run commands without prompting; non-interactive
        # Claude Code denies unapproved tools instead, which would stall an
        # autonomous run at its first Edit/Bash call. Skipping permission
        # prompts is the closest functional headless equivalent. Callers must
        # run this default in a trusted or externally sandboxed environment; a
        # Git worktree scopes repository changes but is not a security boundary.
        return ["--dangerously-skip-permissions"]

    def _has_permission_flag(self) -> bool:
        return any(
            arg in _CLAUDE_PERMISSION_FLAGS
            or any(arg.startswith(flag + "=") for flag in _CLAUDE_PERMISSION_FLAGS)
            for arg in self.config.extra_args
        )

    def _has_model_flag(self) -> bool:
        return any(
            arg in {"--model", "-m"} or arg.startswith("--model=")
            for arg in self.config.extra_args
        )

    def _effective_prompt(self, request: AgentRequest) -> str:
        """Return the prompt, with image attachments referenced explicitly.

        Claude Code has no ``--image`` flag; it reads image files through its
        Read tool, so attachments are listed as file paths in the prompt.
        """
        if not request.attachments or request.prompt_spec is not None:
            return request.prompt
        lines = [
            request.prompt.rstrip("\n"),
            "",
            "Attached image files (inspect them with your Read tool):",
        ]
        lines.extend(f"- {attachment}" for attachment in request.attachments)
        return "\n".join(lines)

    def _model_label(self, request: AgentRequest) -> str:
        explicit_model = self._explicit_model_arg()
        if explicit_model:
            return explicit_model

        profile = self.config.profile_map.get(request.effort)
        if profile:
            return profile
        return "default"

    def _explicit_model_arg(self) -> str:
        extra_args = list(self.config.extra_args)
        for index, value in enumerate(extra_args):
            if value in {"--model", "-m"} and index + 1 < len(extra_args):
                return extra_args[index + 1]
            if value.startswith("--model="):
                return value.partition("=")[2]
        return ""

    @staticmethod
    def _make_json_stream_filter(
        callback: Callable[[str, str], None],
    ) -> Callable[[str, str], None]:
        """Wrap a stream callback to parse Claude Code JSONL in real-time.

        Only assistant text blocks and terminal errors are forwarded; raw JSON
        protocol lines are silently consumed.
        """

        def filtered(stream_name: str, chunk: str) -> None:
            if stream_name != "stdout":
                callback(stream_name, chunk)
                return
            line = chunk.strip()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                callback(stream_name, chunk)
                return
            if not isinstance(event, dict):
                return
            event_type = str(event.get("type", ""))
            if event_type == "assistant":
                for block in _claude_content_blocks(event):
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("type", "")) != "text":
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        callback("stdout", text if text.endswith("\n") else text + "\n")
            elif event_type == "result":
                subtype = str(event.get("subtype", ""))
                if bool(event.get("is_error")) or subtype in _ERROR_RESULT_SUBTYPES:
                    message = _claude_result_error_message(event)
                    if message:
                        callback("stderr", message + "\n")

        return filtered

    def _parse_json_stdout(
        self, stdout: str
    ) -> Tuple[str, Optional[AgentUsage], List[str], str, str]:
        """Parse Claude Code JSON-line stdout.

        Returns (visible_text, usage, error_messages, session_id, final_text).
        """
        visible_chunks: List[str] = []
        error_messages: List[str] = []
        usage: Optional[AgentUsage] = None
        session_id = ""
        final_text = ""

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                visible_chunks.append(raw_line + "\n")
                continue
            if not isinstance(event, dict):
                visible_chunks.append(raw_line + "\n")
                continue

            event_type = str(event.get("type", ""))
            if event_type == "system":
                candidate = str(event.get("session_id", ""))
                if candidate:
                    session_id = candidate
            elif event_type == "assistant":
                for block in _claude_content_blocks(event):
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("type", "")) != "text":
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        visible_chunks.append(
                            text if text.endswith("\n") else text + "\n"
                        )
            elif event_type == "result":
                candidate = str(event.get("session_id", ""))
                if candidate:
                    session_id = candidate
                subtype = str(event.get("subtype", ""))
                result_text = event.get("result")
                is_error = bool(event.get("is_error")) or subtype in _ERROR_RESULT_SUBTYPES
                if is_error:
                    error_messages.append(_claude_result_error_message(event))
                elif isinstance(result_text, str) and result_text.strip():
                    final_text = result_text.strip()
                usage_payload = event.get("usage")
                if isinstance(usage_payload, dict) and all(usage_payload.get(key) is not None for key in ("input_tokens", "output_tokens")):
                    uncached_input_tokens = int(
                        usage_payload.get("input_tokens", 0) or 0
                    )
                    cache_creation_input_tokens = int(
                        usage_payload.get("cache_creation_input_tokens", 0) or 0
                    )
                    cache_read_input_tokens = int(
                        usage_payload.get("cache_read_input_tokens", 0) or 0
                    )
                    usage = AgentUsage(
                        input_tokens=(
                            uncached_input_tokens
                            + cache_creation_input_tokens
                            + cache_read_input_tokens
                        ),
                        cached_input_tokens=cache_read_input_tokens,
                        output_tokens=int(usage_payload.get("output_tokens", 0) or 0),
                    )

        return "".join(visible_chunks), usage, error_messages, session_id, final_text
