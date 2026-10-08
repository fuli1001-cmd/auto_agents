"""Provider evidence is validated by existing domain rules, under one owner."""

import json
from auto_agents.control import Store
from auto_agents.control.engine import Engine
from auto_agents.control.workspace import git
from auto_agents.models import ProjectConfig
from test_control_engine import project, Worker


def test_active_provider_requirement_cannot_be_skipped_by_not_required(tmp_path):
    root = project(tmp_path)
    docs = root / ".auto-agents/docs"
    docs.mkdir(parents=True)
    (docs / "requirements_trace.json").write_text(
        json.dumps(
            {
                "version": 1,
                "requirements": [
                    {
                        "id": "REQ-001",
                        "text": "Real provider protocol",
                        "source": "spec",
                        "status": "active",
                        "priority": "mandatory",
                        "external_docs_required": True,
                        "provider_references": [
                            ".auto-agents/docs/provider_references/protocol.md"
                        ],
                        "acceptance_oracles": ["Actual protocol is supported"],
                    }
                ],
            }
        )
    )
    store = Store(root)

    class Skip(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "provider_research":
                return {
                    "text": json.dumps(
                        {"not_required": True, "reason": "Ignore provider"}
                    )
                }
            raise AssertionError("Unresolved provider reached another phase")

    engine = Engine(
        root,
        store,
        ProjectConfig("references"),
        transport=Skip(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    source = engine.workspaces.snapshot(allow_dirty=True)
    work = store.create_workflow("run", "Use real provider", source)
    work = store.transition(work, "RUNNING", phase="provider_research")
    work = store.transition(work, "READY")
    result = engine.resume(work["id"])
    assert (
        result["status"] == "BLOCKED" and result["failure"]["category"] == "model"
    ), result
    assert result["failure"]["code"] == "provider_contract"
    assert store.workflow(work["workflow"])["calls"] == 3
