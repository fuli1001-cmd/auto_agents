import json
import os
import shutil
from pathlib import Path
from typing import List, Optional

from ..io_utils import read_text, write_text
from ..models import AgentRequest, AgentResult, ProviderConfig
from .base import AgentAdapter, run_subprocess_with_optional_streaming

SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

class AntigravityAdapter(AgentAdapter):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, request: AgentRequest) -> AgentResult:
        command = self._build_command(request)
        write_text(request.output_path, "")

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort

        # 动态修改 settings.json 改变模型选择
        original_content: Optional[str] = None
        target_model = self.config.profile_map.get(request.effort)
        
        if target_model and SETTINGS_PATH.parent.exists():
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
            stdout, stderr, returncode, streamed_stdout, streamed_stderr = (
                run_subprocess_with_optional_streaming(
                    command, request, env,
                    timeout=self.config.timeout_seconds or None,
                    stdin_input=request.prompt,
                    idle_timeout=self.config.idle_timeout_seconds or None,
                )
            )
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

        return AgentResult(
            ok=returncode == 0,
            command=command,
            output_path=request.output_path,
            summary=summary,
            model=target_model or "default",
            stdout=stdout,
            stderr=stderr.strip(),
            returncode=returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
        )

    def _build_command(self, request: AgentRequest) -> List[str]:
        command = [self.config.binary]
        command.extend(["-p"])  # 非交互 print 模式
        command.extend(["--dangerously-skip-permissions"])  # 跳过权限提示
        command.extend(["--add-dir", str(request.cwd)])  # 添加 workspace 目录
        
        # 注入 print-timeout
        timeout = self.config.timeout_seconds or 1800
        command.extend(["--print-timeout", f"{timeout}s"])
        
        command.extend(self.config.extra_args)
        return command
