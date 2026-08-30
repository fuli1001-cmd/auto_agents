from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Mapping, Optional, Protocol

from .models import RunState


@dataclass(frozen=True)
class ReplaySpec:
    adapter: str
    task_id: str = ""
    command: str = ""
    expected_root_fingerprint: str = ""
    timeout_seconds: int = 1200

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PlaybookProbe:
    applicable: bool
    name: str
    category: str = ""
    reason: str = ""
    replay: Optional[ReplaySpec] = None
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["replay"] = self.replay.to_dict() if self.replay else None
        return payload


@dataclass(frozen=True)
class PlaybookResult:
    ok: bool
    changed: bool
    name: str
    category: str
    reason: str
    task_id: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class SelfRepairPlaybook(Protocol):
    name: str
    categories: frozenset[str]

    def probe(self, state: RunState) -> PlaybookProbe:
        ...

    def apply(self, orchestrator: object, state: RunState) -> PlaybookResult:
        ...


class RetainedVerifyBaselinePlaybook:
    name = "retained_verify_baseline_reconstruction"
    categories = frozenset({"retained_verify_baseline_snapshot_lost"})

    def probe(self, state: RunState) -> PlaybookProbe:
        blocker = state.active_blocker if isinstance(state.active_blocker, dict) else {}
        category = str(blocker.get("category", "")).strip()
        raw_detail = blocker.get("retained_verify_baseline_snapshot", {})
        detail = dict(raw_detail) if isinstance(raw_detail, Mapping) else {}
        task_id = str(detail.get("task_id", "")).strip()
        applicable = category in self.categories and bool(task_id)
        return PlaybookProbe(
            applicable=applicable,
            name=self.name,
            category=category,
            reason=(
                "legacy retained verification baseline can be reconstructed "
                "through the bounded resume migration"
                if applicable
                else "blocker is not a retained verification baseline loss"
            ),
            replay=(
                ReplaySpec(
                    adapter="blocked_resume",
                    task_id=task_id,
                    expected_root_fingerprint=str(
                        blocker.get("fingerprint", "")
                    ).strip(),
                )
                if applicable
                else None
            ),
            evidence=(f"task_id={task_id}",) if task_id else (),
        )

    def apply(self, orchestrator: object, state: RunState) -> PlaybookResult:
        probe = self.probe(state)
        if not probe.applicable:
            return PlaybookResult(
                ok=False,
                changed=False,
                name=self.name,
                category=probe.category,
                reason=probe.reason,
            )
        resume = getattr(orchestrator, "_resume_lost_retained_verify_baseline", None)
        if not callable(resume):
            return PlaybookResult(
                ok=False,
                changed=False,
                name=self.name,
                category=probe.category,
                reason="installed auto_agents runtime lacks the migration hook",
                task_id=probe.replay.task_id if probe.replay else "",
            )
        changed = bool(resume(state, state.active_blocker))
        return PlaybookResult(
            ok=changed,
            changed=changed,
            name=self.name,
            category=probe.category,
            reason=(
                "retained verification baseline reconstructed"
                if changed
                else "retained verification baseline reconstruction found no proof"
            ),
            task_id=probe.replay.task_id if probe.replay else "",
        )


class SelfRepairPlaybookRegistry:
    def __init__(self, playbooks: Optional[Iterable[SelfRepairPlaybook]] = None) -> None:
        self.playbooks = list(
            playbooks if playbooks is not None else [RetainedVerifyBaselinePlaybook()]
        )

    def probe(self, state: RunState) -> Optional[PlaybookProbe]:
        for playbook in self.playbooks:
            probe = playbook.probe(state)
            if probe.applicable:
                return probe
        return None

    def attempt(self, orchestrator: object, state: RunState) -> Optional[PlaybookResult]:
        for playbook in self.playbooks:
            probe = playbook.probe(state)
            if probe.applicable:
                return playbook.apply(orchestrator, state)
        return None
