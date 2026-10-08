import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
import pytest
from auto_agents.control import Store, Contract, ControlError, VerificationSpec
from auto_agents.control.effects import parse_reply, compile_checks, Verifier
from auto_agents.control.engine import Engine
from auto_agents.control.types import ExecutionContext
from auto_agents.control.workspace import git
from auto_agents.control.confinement import Boundary
from auto_agents.models import ProjectConfig
from test_control_engine import Worker, project


def test_native_commentary_does_not_become_authority():
    assert parse_reply('Inspecting workspace.\n{"decision":"pass"}') == {
        "decision": "pass"
    }
    with pytest.raises(ControlError):
        parse_reply('{"decision":"revise"}\n{"decision":"pass"}')
    with pytest.raises(ControlError):
        parse_reply('Inspecting\n{"status":"COMPLETED"}')


def test_contract_children_are_recursively_immutable_and_copies_are_independent():
    supplied = {"choice": {"options": ["one", "two"]}}
    c = Contract("workflow", "work", "fix", "Original goal", "source", inputs=supplied)
    supplied["choice"]["options"].append("forged")
    assert c.inputs["choice"]["options"] == ("one", "two")
    with pytest.raises(TypeError):
        c.inputs["choice"]["options"] += ("forged",)
    copy = c.to_dict()
    copy["inputs"]["choice"]["options"].append("forged")
    assert c.to_dict()["inputs"]["choice"]["options"] == ["one", "two"]


def test_bound_owner_cannot_replace_inputs_or_parent_identity(tmp_path):
    s = Store(tmp_path)
    w = s.create_workflow("fix", "goal", "source", inputs={"issue": "original"})
    c = s.contract(w["contract"])
    for changed in (
        replace(c, inputs={"issue": "another"}),
        replace(c, parent_contract="another"),
    ):
        with pytest.raises(ControlError):
            s.bind_contract(w, changed)


@pytest.mark.parametrize("flags", ["", "-B ", "-u -B "])
def test_unit_discovery_is_an_exact_preserved_launch(tmp_path, flags):
    command = f"python3 {flags}-m unittest discover -s tests -p test_behavior.py -v"
    checks = compile_checks([command], tmp_path)
    assert checks[0].command == command and checks[0].targets == (
        "tests/test_behavior.py",
    )


def test_all_skipped_unittest_is_not_proof(tmp_path):
    root = project(tmp_path)
    (root / "tests").mkdir()
    (root / "tests/test_skip.py").write_text(
        'import unittest\nclass T(unittest.TestCase):\n @unittest.skip("no behavior")\n def test_missing(self): pass\n'
    )
    ctx = ExecutionContext(
        root,
        root,
        "work",
        Contract("workflow", "work", "fix", "goal", "source"),
        "fixture",
    )
    with pytest.raises(ControlError) as err:
        Verifier().run(
            ctx,
            compile_checks(
                [sys.executable + " -m unittest discover -s tests -p test_skip.py"],
                root,
            ),
        )
    assert err.value.code == "proof"


@pytest.mark.parametrize("readonly", [False, True])
def test_every_provider_boundary_protects_shared_files_and_metadata(tmp_path, readonly):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch"
    outside = tmp_path / "shared"
    outside.write_text("retained")
    before = outside.stat().st_mode
    boundary = Boundary(workspace, scratch, readonly=readonly)
    boundary.check()
    script = """from pathlib import Path
import os,sys
outside=Path(sys.argv[1]);root=Path(sys.argv[2])
for action in (lambda:outside.write_text('forged'),lambda:outside.chmod(0o777)):
 try:action()
 except PermissionError:pass
 else:raise AssertionError('shared write was permitted')
try:(root/'produced').write_text('owned')
except PermissionError:
 assert sys.argv[3]=='True'
else:assert sys.argv[3]=='False'
"""
    command, env = boundary.dispatch(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(outside),
            str(workspace),
            str(readonly),
        ],
        os.environ,
        workspace,
    )
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert outside.read_text() == "retained" and outside.stat().st_mode == before


def test_delivery_preserves_unrelated_staged_user_changes(tmp_path):
    root = project(tmp_path)
    (root / "notes.txt").write_text("user work\n")
    git(root, "add", "notes.txt")
    engine = Engine(
        root,
        Store(root),
        ProjectConfig("fixture"),
        transport=Worker(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    result = engine.start("fix", "Set value to one", allow_dirty=True)
    assert result["status"] == "COMPLETED", result["failure"]
    assert git(root, "diff", "--cached", "--name-only") == "notes.txt"
    assert (
        "notes.txt"
        not in git(root, "show", "--format=", "--name-only", "HEAD").splitlines()
    )


def test_failed_child_resumes_the_same_work_and_budget(tmp_path):
    root = project(tmp_path)
    store = Store(root)

    class Unavailable(Worker):
        def run(self, ctx, phase, *args, **kwargs):
            if phase == "implement":
                raise ControlError(
                    "provider_failed", "Temporary account problem", category="provider"
                )
            return super().run(ctx, phase, *args, **kwargs)

    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Unavailable(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    failed = engine.start("collab", "Set value to one")
    child = failed["failure"]["child_id"]
    count = store.workflow(failed["workflow"])["calls"]
    # Explicit retry consumes a known failed receipt. It never refunds its call.
    for op in store.operations(child):
        if op["state"] == "FAILED":
            store.consume(op["id"])
    engine.transport = Worker()
    done = engine.resume(failed["id"])
    assert done["status"] == "COMPLETED", done["failure"]
    assert [w["id"] for w in store.works() if w["parent"]] == [child]
    assert store.workflow(done["workflow"])["calls"] > count


def test_workspace_same_inode_symlink_is_not_owned_execution(tmp_path):
    from test_control_engine import project, Worker
    from auto_agents.models import ProjectConfig

    root = project(tmp_path)
    store = Store(root)
    engine = Engine(
        root,
        store,
        ProjectConfig("identity"),
        transport=Worker(),
        print_fn=lambda *a: None,
    )
    source = engine.workspaces.snapshot()
    work = store.create_workflow("fix", "Preserve ownership", source)
    workspace = engine.context(work).workspace_root
    saved = tmp_path / "user-preserved"
    workspace.rename(saved)
    workspace.symlink_to(saved, target_is_directory=True)
    with pytest.raises(ControlError, match="workspace was replaced"):
        engine.context(work)
    assert (saved / "value.py").read_text() == "VALUE = 0\n"


def test_declared_test_outputs_survive_ignored_paths_and_terminal_cleanup(tmp_path):
    root = project(tmp_path)
    (root / ".gitignore").write_text("evidence/\n")
    git(root, "add", ".gitignore")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "ignore runtime outputs",
    )
    from test_control_engine import Worker
    from auto_agents.models import ProjectConfig

    class EvidenceWorker(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "classify":
                return {
                    "text": json.dumps(
                        {
                            "decision": "fix",
                            "paths": ["value.py", "tests"],
                            "checks": [
                                {
                                    "command": sys.executable
                                    + " -m pytest -q tests/test_value.py",
                                    "outputs": ["evidence/result.json"],
                                }
                            ],
                        }
                    )
                }
            if phase == "implement":
                (ctx.workspace_root / "value.py").write_text("VALUE = 1\n")
                tests = ctx.workspace_root / "tests"
                tests.mkdir(exist_ok=True)
                (tests / "test_value.py").write_text(
                    "from value import VALUE\nfrom pathlib import Path\n"
                    'def test_value():\n assert VALUE == 1\n p=Path("evidence/result.json")\n'
                    ' p.parent.mkdir(exist_ok=True)\n p.write_text("{\\"observed_value\\":1}")\n'
                )
                return {
                    "text": json.dumps(
                        {"summary": "Real check writes declared evidence"}
                    )
                }
            return super().run(ctx, phase, *a, **kw)

    store = Store(root)
    result = Engine(
        root,
        store,
        ProjectConfig("outputs"),
        transport=EvidenceWorker(),
        auto_approve=True,
        print_fn=lambda *a: None,
    ).start("fix", "Set value to one")
    assert result["status"] == "COMPLETED", result["failure"]
    assert json.loads((root / "evidence/result.json").read_text()) == {
        "observed_value": 1
    }
    assert "evidence/result.json" in git(root, "ls-files").splitlines()
    assert not (root / ".auto-agents/state/owned/workspaces" / result["id"]).exists()


@pytest.mark.parametrize("tracked", [False, True])
def test_verification_temp_files_do_not_relax_tracked_input_seals(tmp_path, tracked):
    root = project(tmp_path)
    (root / "tests").mkdir()
    (root / ".tmp-tests").mkdir()
    if tracked:
        (root / ".tmp-tests/input.json").write_text("original")
        git(root, "add", "-f", ".tmp-tests/input.json")
        git(
            root,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "admitted input",
        )
    program = (
        'from pathlib import Path\ndef test_temp():\n p=Path(".tmp-tests/input.json")\n'
        ' p.parent.mkdir(exist_ok=True)\n p.write_text("runtime")\n assert p.read_text()=="runtime"\n'
    )
    (root / "tests/test_temp.py").write_text(program)
    ctx = ExecutionContext(
        root,
        root,
        "work",
        Contract("wf", "work", "fix", "Temporary output", "source"),
        "fixture",
    )
    checks = compile_checks([sys.executable + " -m pytest -q tests/test_temp.py"], root)
    if tracked:
        with pytest.raises(ControlError, match="changed undeclared inputs"):
            Verifier().run(ctx, checks)
    else:
        assert Verifier().run(ctx, checks)["ok"]


def test_backend_assignment_summary_does_not_invent_failed_tests(tmp_path):
    root = project(tmp_path)
    (root / "tests").mkdir()
    (root / "tests/test_summary.py").write_text(
        'def test_summary():\n print("backend summary: tests=2 passed=2 failed=0 skipped=0")\n assert True\n'
    )
    ctx = ExecutionContext(
        root,
        root,
        "work",
        Contract("wf", "work", "fix", "Summary", "source"),
        "fixture",
    )
    report = Verifier().run(
        ctx,
        compile_checks(
            [sys.executable + " -m pytest -q -s tests/test_summary.py"], root
        ),
    )
    assert report["ok"] and report["checks"][0]["executed"] == 1


def test_porcelain_leading_column_preserves_dirty_paths_and_local_config(tmp_path):
    root = project(tmp_path)
    from auto_agents.models import ProjectConfig
    from auto_agents.config import save_project_config

    save_project_config(root, ProjectConfig("local"))
    git(root, "add", "-f", ".auto-agents/config.json")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "existing configuration",
    )
    (root / ".auto-agents/config.json").write_text('{"local":"modified"}')
    engine = Engine(root, Store(root), ProjectConfig("local"), print_fn=lambda *a: None)
    source = engine.workspaces.snapshot()
    assert engine.store.meta("source:" + source)["dirty"] == []
    (root / "value.py").write_text("VALUE = 9\n")
    with pytest.raises(ControlError, match="existing product changes"):
        engine.workspaces.snapshot()
    source = engine.workspaces.snapshot(allow_dirty=True)
    assert engine.store.meta("source:" + source)["dirty"] == ["value.py"]
