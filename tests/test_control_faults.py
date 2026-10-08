import json
from pathlib import Path
import pytest
from auto_agents.control import Store, ControlError
from auto_agents.control.engine import Engine
from auto_agents.models import ProjectConfig
from test_control_engine import Worker, project


@pytest.mark.parametrize(
    "bad",
    [
        None,
        0,
        False,
        [],
        {"command": "forged"},
        "conda run -p ./.conda pytest tests/foreign.py",
    ],
)
def test_bound_classification_cannot_change_execution(tmp_path, bad):
    from auto_agents.control.types import VerificationSpec

    root = project(tmp_path)
    store = Store(root)
    base = Worker()

    class BadWorker(Worker):
        def run(self, ctx, phase, prompt, output, **kwargs):
            if phase == "classify":
                return {
                    "text": json.dumps({"decision": "fix", "verification_command": bad})
                }
            raise AssertionError("Invalid classification reached implementation")

    engine = Engine(
        root,
        store,
        ProjectConfig("test"),
        transport=BadWorker(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    result = engine.start(
        "fix",
        "Required value",
        checks=(VerificationSpec("python -m pytest -q tests/test_value.py"),),
    )
    if bad is None:
        # Omission is legal, but an explicit JSON null is not a command.
        assert result["status"] == "BLOCKED"
    else:
        assert result["status"] == "BLOCKED"
    assert (
        store.contract(result["contract"]).checks[0].command
        == "python -m pytest -q tests/test_value.py"
    )
    assert (root / "value.py").read_text() == "VALUE = 0\n"


def test_unknown_result_never_fails_over_to_another_account(tmp_path):
    root = project(tmp_path)
    store = Store(root)

    class Unknown(Worker):
        def run(self, *args, **kwargs):
            raise ControlError(
                "outcome_unknown", "Unconfirmed request", category="reconciliation"
            )

    config = ProjectConfig("test")
    config.providers["codex-fuli0110"] = config.providers["codex"]
    engine = Engine(
        root,
        store,
        config,
        provider="codex-fuli0110",
        transport=Unknown(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    result = engine.start("fix", "Required value")
    assert (
        result["failure"]["category"] == "reconciliation"
        and engine.provider == "codex-fuli0110"
    )
    calls = store.workflow(result["workflow"])["calls"]
    assert engine.resume(result["id"])["status"] == "BLOCKED"
    assert store.workflow(result["workflow"])["calls"] == calls


def test_workspace_admission_fault_is_blocked_without_dispatch_or_retarget(
    tmp_path, monkeypatch
):
    root = project(tmp_path)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        ProjectConfig("admission"),
        transport=worker,
        print_fn=lambda *a: None,
    )
    source = engine.workspaces.snapshot()
    work = store.create_workflow(
        "fix", "Keep original selected goal", source, max_calls=4
    )

    def refused(*args):
        raise ControlError(
            "workspace_identity", "Registered workspace changed", category="state"
        )

    monkeypatch.setattr(engine.workspaces, "ensure", refused)
    result = engine.resume(work["id"])
    assert result["status"] == "BLOCKED" and result["failure"]["category"] == "state"
    assert result["id"] == work["id"] and result["contract"] == work["contract"]
    assert not worker.calls and store.workflow(work["workflow"])["calls"] == 0
    assert (root / "value.py").read_text() == "VALUE = 0\n"


@pytest.mark.parametrize("output", ["tests/test_value.py", "value.py", "package.json"])
def test_selector_and_program_sources_cannot_be_mutable_outputs(tmp_path, output):
    from auto_agents.control.effects import compile_checks

    with pytest.raises(
        ControlError, match="Test sources cannot be mutable output artifacts"
    ):
        compile_checks(
            [
                {
                    "command": "python -m pytest -q tests/test_value.py::test_value",
                    "outputs": [output],
                }
            ],
            tmp_path,
        )
