"""Small public Python facades. All execution enters Engine exactly once."""

from pathlib import Path
from ..config import load_project_config, save_project_config, default_provider_config
from ..models import ProjectConfig, TaskSpec, AgentRequest
from .types import Contract, ExecutionContext, ControlError
from .store import Store
from .engine import Engine
from .cli import ready
from .prompts import build
from .effects import Provider


class Orchestrator:
    def __init__(self, project_root, *, agent_output_stream=None, **kwargs):
        self.project_root = Path(project_root).resolve()
        self.config = load_project_config(self.project_root)
        self.agent_output_stream = agent_output_stream
        self.store = ready(self.project_root)

    @staticmethod
    def init_project(
        project_root, name=None, provider="codex", doc_language="en", **kwargs
    ):
        root = Path(project_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        config = ProjectConfig(name or root.name)
        if provider not in config.providers:
            config.providers[provider] = default_provider_config(provider)
        config.active_provider = provider
        config.docs.language = doc_language
        save_project_config(root, config)
        Store(root)
        return root

    def engine(self, *, provider=None, auto_approve=False):
        return Engine(
            self.project_root,
            self.store,
            self.config,
            provider=provider,
            auto_approve=auto_approve,
        )

    def run(
        self, spec_file=None, *, auto_approve=False, allow_dirty_tree=False, **kwargs
    ):
        roots = [
            w
            for w in self.store.works()
            if w["mode"] == "run"
            and not w["parent"]
            and w["status"] not in {"COMPLETED", "CANCELLED"}
        ]
        engine = self.engine(auto_approve=auto_approve)
        if len(roots) == 1:
            return engine.resume(roots[0]["id"])
        if len(roots) > 1:
            raise ControlError("selection", "Choose --session or --workflow explicitly")
        path = Path(spec_file or self.project_root / "spec.md")
        return engine.start("run", path.read_text(), allow_dirty=allow_dirty_tree)

    def _build_adapter_for_provider(self, provider):
        return Provider(self.config, alias=provider).adapter(provider)

    def _build_task_prompt(self, task, purpose="implement"):
        goal = "\n".join(
            str(getattr(task, key, ""))
            for key in ("title", "description", "acceptance")
        )
        contract = Contract("prompt", "prompt", "fix", goal, "prompt-only", scope=())
        return build(
            ExecutionContext(
                self.project_root,
                self.project_root,
                "prompt",
                contract,
                self.config.active_provider,
            ),
            purpose,
        )


class Session:
    def __init__(self, orchestrator, *, mode="fix", **kwargs):
        self.orch = orchestrator
        self.mode = mode
        self.project_root = orchestrator.project_root

    def start(self, goal, *, session_id=None, auto_approve=False, provider=None):
        engine = self.orch.engine(provider=provider, auto_approve=auto_approve)
        return (
            engine.resume(session_id) if session_id else engine.start(self.mode, goal)
        )

    def resume(self, session_id):
        return self.orch.engine().resume(session_id)


class WorkflowCoordinator:
    def __init__(self, orchestrator, *, auto_approve=False, **kwargs):
        self.orch = orchestrator
        self.engine = orchestrator.engine(auto_approve=auto_approve)

    def resume(self, workflow_id):
        return self.engine.resume(workflow_id)

    def start(self, mode, goal):
        return self.engine.start(mode, goal)
