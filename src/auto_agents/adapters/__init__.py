from .base import AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .copilot_cli import CopilotCliAdapter
from .antigravity import AntigravityAdapter
from .mock import MockAdapter
from .shell import ShellAdapter

__all__ = [
    "AgentAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CopilotCliAdapter",
    "AntigravityAdapter",
    "MockAdapter",
    "ShellAdapter",
]
