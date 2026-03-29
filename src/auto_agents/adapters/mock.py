from __future__ import annotations

from ..io_utils import write_text
from ..models import AgentRequest, AgentResult
from .base import AgentAdapter


class MockAdapter(AgentAdapter):
    def available(self) -> bool:
        return True

    def run(self, request: AgentRequest) -> AgentResult:
        content = f"MOCK stage={request.stage} effort={request.effort}\n"
        if request.stage == "review":
            content = "DECISION: pass\nMock review passed.\n"
        write_text(request.output_path, content)
        return AgentResult(
            ok=True,
            command=["mock"],
            output_path=request.output_path,
            summary=content.strip(),
            returncode=0,
        )

