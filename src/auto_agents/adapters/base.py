from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AgentRequest, AgentResult


class AgentAdapter(ABC):
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError

