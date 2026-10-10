"""An operator can reject a proposal and carry its feedback into a new attempt."""

import pytest

from auto_agents.control import ControlError
from auto_agents.control import prototypes
from auto_agents.control.cli import main
from test_control_prototype_preview import frontend


@pytest.mark.parametrize("alternatives", [False, True])
def test_rejected_prototype_is_regenerated_with_feedback_and_original_contract(
    frontend, capsys, alternatives
):
    root, engine, work = frontend
    if alternatives:
        work = prototypes.generate(engine, work, "Alternative layout")
        assert work["status"] == "WAITING", work["failure"]
    reason = "保留智能推荐时长、时长确认、三种时长、分页、创建后新增卡片和播放进度拖动。"
    before = engine.store.work(work["id"])
    calls = engine.store.workflow(work["workflow"])["calls"]
    old = next(o for o in engine.store.operations(work["id"]) if o["kind"] == "model:prototype")
    native = engine.store.outcome(old["id"])
    assert main(["reject", "--project", str(root), "--session", work["id"],
                 "--gate", "prototype", "--reason", reason]) == 0
    capsys.readouterr()
    ready = engine.store.work(work["id"])
    assert ready["status"] == "READY" and ready["phase"] == "prototype_prepare"
    assert ready["context"]["feedback"] == reason
    assert "approval" not in ready["context"] and "waiting_for" not in ready["context"]
    assert ready["contract"] == before["contract"]
    assert engine.store.workflow(work["workflow"])["calls"] == calls

    worker = engine.transport
    prompts = []

    class Revised:
        def run(self, ctx, phase, prompt, output, **kwargs):
            prompts.append((phase, prompt))
            result = worker.run(ctx, phase, prompt, output, **kwargs)
            if phase == "prototype":
                path = ctx.workspace_root / ".auto-agents/docs/frontend_prototype/home.html"
                path.write_text(path.read_text().replace("Current prototype", "Revised prototype"))
            return result

    engine.transport = Revised()
    revised = engine.resume(work["id"])
    assert revised["status"] == "WAITING", revised["failure"]
    assert revised["context"]["approval"] == "prototype"
    assert [p for p, _ in prompts] == ["prototype_prepare", "prototype_scope_review", "prototype", "prototype_review"]
    assert all(reason in prompt for _, prompt in prompts)
    assert "Preserve existing product behavior" in next(prompt for phase, prompt in prompts if phase == "prototype")
    assert revised["contract"] == before["contract"] and revised["max_calls"] == before["max_calls"]
    assert engine.store.workflow(work["workflow"])["calls"] == calls + 4
    operations = [o for o in engine.store.operations(work["id"]) if o["kind"] == "model:prototype"]
    assert len(operations) == 2 and operations[1]["ordinal"] == operations[0]["ordinal"] + 1
    assert engine.store.outcome(old["id"])["payload"] == native["payload"]
    candidates = [row for row in prototypes.candidates(engine, revised) if row["status"] == "candidate"]
    assert len(candidates) == 1
    assert b"Revised prototype" in prototypes.preview_file(engine.context(revised).workspace_root, candidates[0]["path"])
    prototypes.approve(engine, revised, candidates[0]["id"])


def test_rejection_of_a_different_gate_preserves_the_pending_prototype(frontend, capsys):
    root, engine, work = frontend
    before = engine.store.work(work["id"])
    assert main(["reject", "--project", str(root), "--session", work["id"],
                 "--gate", "architecture", "--reason", "Revise the prototype"]) == 3
    capsys.readouterr()
    assert engine.store.work(work["id"]) == before


def test_stage_rollback_cannot_bypass_the_owned_approval_phase(frontend):
    _, engine, work = frontend
    context = {**work["context"], "feedback": "Do not bypass ownership"}
    context.pop("waiting_for")
    context.pop("approval")
    context.pop("approval_phase")
    with pytest.raises(ControlError, match="phase transition"):
        engine.store.transition(work, "READY", phase="clarify", context=context)
    assert engine.store.work(work["id"]) == work


@pytest.mark.parametrize("reason", ["", " \n "])
def test_rejection_requires_meaningful_feedback(frontend, capsys, reason):
    root, engine, work = frontend
    assert main(["reject", "--project", str(root), "--session", work["id"],
                 "--gate", "prototype", "--reason", reason]) == 3
    capsys.readouterr()
    assert engine.store.work(work["id"]) == work


def test_preservation_inputs_include_product_source_and_historical_prototypes(frontend):
    from auto_agents.control.domain import context_inputs
    from auto_agents.control.prompts import build

    _, engine, work = frontend
    context = engine.context(work)
    page = context.workspace_root / "workbench/src/app/page.tsx"
    page.parent.mkdir(parents=True)
    page.write_text("export default function Home() { return <main>Existing flow</main>; }")
    inputs = context_inputs(context, "prototype", engine.config, engine.store)
    preservation = inputs["prototype_preservation"]
    assert "workbench/src/app/page.tsx" in preservation["frontend_source_refs"]
    assert ".auto-agents/docs/frontend_prototype/manifest.json" in preservation["prototype_manifest_refs"]
    historical = ".auto-agents/docs/frontend_prototype_variants/legacy-old/frontend_prototype/manifest.json"
    assert historical in preservation["prototype_manifest_refs"]
    prompt = build(context, "prototype", inputs=inputs, feedback="Restore automatic duration")
    assert historical in prompt and "workbench/src/app/page.tsx" in prompt
    assert "Restore automatic duration" in prompt


@pytest.mark.parametrize("gate,phase", [("requirements", "clarify"), ("architecture", "design")])
def test_other_owned_manual_gates_can_also_return_for_revision(tmp_path, capsys, gate, phase):
    from auto_agents.config import save_project_config
    from auto_agents.control import Store
    from auto_agents.control.engine import Engine
    from auto_agents.models import ProjectConfig
    from test_control_engine import Worker, project

    root = project(tmp_path)
    config = ProjectConfig("manual gates")
    save_project_config(root, config)
    engine = Engine(root, Store(root), config, transport=Worker(), print_fn=lambda *args: None)
    work = engine.start("run", "Set value to one")
    if gate == "architecture":
        assert main(["approve", "--project", str(root), "--session", work["id"], "--gate", "requirements"]) == 0
        work = engine.resume(work["id"])
    assert work["status"] == "WAITING" and work["context"]["approval"] == gate
    calls = engine.store.workflow(work["workflow"])["calls"]
    assert main(["reject", "--project", str(root), "--session", work["id"],
                 "--gate", gate, "--reason", "Revise this proposal"]) == 0
    capsys.readouterr()
    ready = engine.store.work(work["id"])
    assert ready["status"] == "READY" and ready["phase"] == phase
    revised = engine.resume(work["id"])
    assert revised["status"] == "WAITING" and revised["context"]["approval"] == gate
    assert engine.store.workflow(work["workflow"])["calls"] == calls + 1
