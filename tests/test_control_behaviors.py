"""Public safety contracts replacing tests of retired driver internals."""

import ast
import json
import os
import sys
from pathlib import Path
import pytest
from auto_agents.control import (
    Store,
    Contract,
    ExecutionContext,
    VerificationSpec,
    ControlError,
)
from auto_agents.control.engine import Engine
from auto_agents.control.effects import Verifier, compile_checks, parse_reply
from auto_agents.control.cli import main
from auto_agents.control.api import status, snapshot, checkpoint
from auto_agents.control.observer import milestones
from auto_agents.control import cleanup
from auto_agents.models import ProjectConfig
from auto_agents.config import save_project_config
from auto_agents.control.workspace import git
from test_control_engine import Worker, project


@pytest.mark.parametrize(
    "phase,payload",
    [
        ("classify", {"decision": "fix", "paths": None}),
        ("classify", {"decision": "fix", "checks": None}),
        ("classify", {"decision": 42}),
        ("classify", {"decision": "fix", "paths": "value.py"}),
        ("diagnose", {"action": "fix", "issue": None}),
        ("diagnose", {"action": True}),
        ("plan", {"tasks": [None]}),
        ("plan", {"tasks": [{"task_id": None, "goal": "x", "checks": []}]}),
        (
            "plan",
            {"tasks": [{"task_id": "T", "goal": "x", "paths": [None], "checks": []}]},
        ),
        ("review", {"decision": "pass", "test_changes_valid": 1}),
    ],
)
def test_bad_model_shapes_are_business_errors_not_engine_repairs(
    tmp_path, phase, payload
):
    root = project(tmp_path)
    store = Store(root)

    class Wrong(Worker):
        def run(self, ctx, current, *a, **kw):
            if current == phase:
                return {"text": json.dumps(payload)}
            return super().run(ctx, current, *a, **kw)

    mode = "collab" if phase == "diagnose" else "run" if phase == "plan" else "fix"
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Wrong(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    result = engine.start(mode, "Set value to one", max_calls=20)
    assert result["status"] == "BLOCKED"
    assert result["failure"]["category"] == "model", result["failure"]
    assert store.contract(result["contract"]).goal == "Set value to one"


@pytest.mark.parametrize(
    "text",
    [
        '{"decision":"fix","decision":"not_bug"}',
        '{"accepted":NaN}',
        '{"status":"COMPLETED"}',
        "{}\n{}",
    ],
)
def test_ambiguous_or_controller_owned_responses_are_refused(text):
    with pytest.raises(ControlError):
        parse_reply(text)


@pytest.mark.parametrize(
    "name",
    [
        ".auto-agents/config.json",
        ".auto-agents/operator/secrets.env",
        ".env",
        ".auto-agents/state/workspace-control.json",
    ],
)
def test_ignored_protected_mutations_cannot_become_a_candidate(tmp_path, name):
    root = project(tmp_path)
    (root / ".gitignore").write_text(".auto-agents/\n.env\n")
    git(root, "add", ".gitignore")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "ignore runtime",
    )

    class Tamper(Worker):
        def run(self, ctx, phase, *a, **kw):
            result = super().run(ctx, phase, *a, **kw)
            if phase == "implement":
                target = ctx.workspace_root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("forged")
            return result

    result = Engine(
        root,
        Store(root),
        ProjectConfig("fixture"),
        transport=Tamper(),
        auto_approve=True,
        print_fn=lambda *a: None,
    ).start("fix", "Set value")
    assert result["status"] == "BLOCKED" and result["failure"]["category"] == "model"
    assert (root / "value.py").read_text() == "VALUE = 0\n"
    assert not (root / name).exists()


@pytest.mark.parametrize("code", ["provider_quota", "provider_unavailable"])
def test_confirmed_failover_and_explicit_override_keep_the_original_budget(
    tmp_path, code
):
    root = project(tmp_path)
    store = Store(root)
    config = ProjectConfig("fixture")
    config.providers["codex-fuli0110"] = config.providers["codex"]

    class Fail(Worker):
        def __init__(self):
            super().__init__()
            self.aliases = []

        def run(self, ctx, phase, *a, **kw):
            self.aliases.append(ctx.provider)
            if ctx.provider == "codex-fuli0110":
                raise ControlError(code, "Confirmed unavailable", category="provider")
            return super().run(ctx, phase, *a, **kw)

    worker = Fail()
    engine = Engine(
        root,
        store,
        config,
        provider="codex-fuli0110",
        transport=worker,
        print_fn=lambda *a: None,
    )
    result = engine.start("fix", "Set value")
    assert result["status"] == "COMPLETED", result["failure"]
    assert (
        worker.aliases[0] == "codex-fuli0110"
        and worker.aliases[1:]
        and set(worker.aliases[1:]) == {"codex"}
    )
    assert store.workflow(result["workflow"])["calls"] == len(worker.aliases)
    assert store.contract(result["contract"]).goal == "Set value"


@pytest.mark.parametrize("mode", ["run", "fix", "collab", "provider_resolve"])
def test_call_ceiling_is_not_replenished_by_resume_or_provider_change(tmp_path, mode):
    class Research(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "research":
                path = ctx.workspace_root / ".auto-agents/docs/provider_references.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Primary source claim")
                return {
                    "text": json.dumps(
                        {
                            "artifacts": [".auto-agents/docs/provider_references.md"],
                            "references": [
                                {
                                    "url": "https://provider.example/docs",
                                    "claim": "configured usage",
                                }
                            ],
                        }
                    )
                }
            return super().run(ctx, phase, *a, **kw)

    root = project(tmp_path)
    store = Store(root)
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Research(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    result = engine.start(mode, "Set value", max_calls=1)
    assert (
        result["status"] == "BLOCKED" and result["failure"]["category"] == "budget"
    ), result["failure"]
    calls = store.workflow(result["workflow"])["calls"]
    assert engine.resume(result["id"])["status"] == "BLOCKED"
    assert store.workflow(result["workflow"])["calls"] == calls == 1


def test_shared_files_are_write_protected_even_without_gate_worktrees(tmp_path):
    root = project(tmp_path)
    outside = tmp_path / "user-data"
    outside.write_text("preserved")
    (root / "tests").mkdir()
    (root / "tests/test_write.py").write_text(
        "from pathlib import Path\ndef test_write():\n try: Path("
        + repr(str(outside))
        + ').write_text("forged")\n except PermissionError: return\n raise AssertionError("shared write allowed")\n'
    )
    ctx = ExecutionContext(
        root,
        root,
        "work",
        Contract("wf", "work", "fix", "Test writes", "source"),
        "fixture",
    )
    report = Verifier().run(
        ctx,
        compile_checks([sys.executable + " -m pytest -q tests/test_write.py"], root),
    )
    assert report["ok"] and outside.read_text() == "preserved"


def test_failed_tests_cannot_be_hidden_by_shell_exit_status(tmp_path):
    root = project(tmp_path)
    (root / "tests").mkdir()
    (root / "tests/test_mask.py").write_text(
        "def test_good(): pass\ndef test_bad(): assert False\n"
    )
    ctx = ExecutionContext(
        root,
        root,
        "work",
        Contract("wf", "work", "fix", "required assertion", "source"),
        "fixture",
    )
    with pytest.raises(ControlError) as failure:
        Verifier().run(
            ctx,
            compile_checks(
                [sys.executable + " -m pytest -q tests/test_mask.py || true"], root
            ),
        )
    assert failure.value.code == "proof"


def test_cancel_preserves_unknown_receipts_and_workspaces(tmp_path, capsys):
    root = project(tmp_path)
    save_project_config(root, ProjectConfig("fixture"))
    store = Store(root)

    class Unknown(Worker):
        def run(self, *a, **kw):
            raise ControlError(
                "outcome_unknown", "Request outcome unknown", category="reconciliation"
            )

    result = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Unknown(),
        print_fn=lambda *a: None,
    ).start("fix", "Set value")
    workspace = root / ".auto-agents/state/owned/workspaces" / result["id"]
    before = store.workflow(result["workflow"])["calls"]
    assert (
        main(["cancel", "--project", str(root), "--session", result["id"], "--json"])
        == 0
    )
    capsys.readouterr()
    cleanup.collect(store, git_gc=True)
    assert workspace.exists() and store.operations()[0]["state"] == "UNKNOWN"
    assert store.workflow(result["workflow"])["calls"] == before


def test_secret_answer_is_bound_without_plaintext_in_business_state(
    tmp_path, capsys, monkeypatch
):
    root = project(tmp_path)
    config = ProjectConfig("fixture")
    save_project_config(root, config)
    store = Store(root)

    class Question(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "classify" and not ctx.environment.get("SERVICE_TEST_KEY"):
                return {
                    "text": json.dumps(
                        {
                            "decision": "need_user",
                            "question": {
                                "key": "service.test_key",
                                "kind": "secret",
                                "question": "Test key",
                                "purpose": "Configured service test",
                                "why_required": "Test service access",
                                "sensitivity": "secret",
                                "bindings": [
                                    {
                                        "env": "SERVICE_TEST_KEY",
                                        "input_key": "service.test_key",
                                        "projection": "value",
                                    }
                                ],
                            },
                        }
                    )
                }
            return super().run(ctx, phase, *a, **kw)

    engine = Engine(
        root,
        store,
        config,
        transport=Question(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    waiting = engine.start("fix", "Set value")
    assert waiting["status"] == "WAITING"
    monkeypatch.setenv("ANSWER_SECRET", "opaque-user-answer")
    assert (
        main(
            [
                "answer",
                "--project",
                str(root),
                "--session",
                waiting["id"],
                "--from-env",
                "ANSWER_SECRET",
                "--json",
                "--no-resume",
            ]
        )
        == 0
    )
    assert "opaque-user-answer" not in capsys.readouterr().out
    assert "opaque-user-answer" not in json.dumps(status(root))
    done = engine.resume(waiting["id"])
    assert done["status"] == "COMPLETED", done["failure"]


def test_snapshot_keeps_shared_head_and_excludes_protected_git_blobs(tmp_path):
    root = project(tmp_path)
    (root / ".env").write_text("PRIVATE=toy-secret\n")
    git(root, "add", ".env")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "fixture protected input",
    )
    git(root, "switch", "-c", "shared-branch")
    engine = Engine(
        root,
        Store(root),
        ProjectConfig("fixture"),
        transport=Worker(),
        print_fn=lambda *a: None,
    )
    work = engine.start("run", "Set value")
    copy = tmp_path / "copy"
    snapshot(root, copy)
    assert git(root, "rev-parse", "HEAD") == git(copy, "rev-parse", "HEAD")
    assert git(copy, "symbolic-ref", "HEAD") == "refs/heads/shared-branch"
    assert not (copy / ".env").exists()
    with pytest.raises(ControlError):
        git(copy, "show", "HEAD:.env")
    assert status(root)["identity"] == status(copy)["identity"]


def test_public_status_changes_when_live_candidate_bytes_change(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Worker(),
        print_fn=lambda *a: None,
    )
    work = engine.start("run", "Set value")
    before = status(root)["identity"]
    (engine.context(work).workspace_root / "value.py").write_text("VALUE = 7\n")
    assert status(root)["identity"] != before


def test_not_bug_still_has_independent_review(tmp_path):
    root = project(tmp_path)

    class NotBug(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "classify":
                self.calls.append((ctx.work_id, phase))
                return {
                    "text": json.dumps(
                        {
                            "decision": "not_bug",
                            "summary": "No implementation requested",
                        }
                    )
                }
            return super().run(ctx, phase, *a, **kw)

    worker = NotBug()
    result = Engine(
        root,
        Store(root),
        ProjectConfig("fixture"),
        transport=worker,
        print_fn=lambda *a: None,
    ).start("fix", "Inspect the behavior")
    assert (
        result["status"] == "COMPLETED" and result["result"]["resolution"] == "not_bug"
    )
    assert [p for _, p in worker.calls] == ["classify", "review"]
    assert (root / "value.py").read_text() == "VALUE = 0\n"


def test_provider_resolution_reviews_and_delivers_real_docs(tmp_path):
    root = project(tmp_path)

    class Research(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "research":
                self.calls.append((ctx.work_id, phase))
                path = ctx.workspace_root / ".auto-agents/docs/provider_references.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Primary source claim and usage.\n")
                return {
                    "text": json.dumps(
                        {
                            "artifacts": [".auto-agents/docs/provider_references.md"],
                            "references": [
                                {
                                    "url": "https://provider.example/docs",
                                    "claim": "configured usage",
                                }
                            ],
                        }
                    )
                }
            return super().run(ctx, phase, *a, **kw)

    worker = Research()
    result = Engine(
        root,
        Store(root),
        ProjectConfig("fixture"),
        transport=worker,
        print_fn=lambda *a: None,
    ).start("provider_resolve", "Repair provider docs")
    assert result["status"] == "COMPLETED", result["failure"]
    assert (root / ".auto-agents/docs/provider_references.md").is_file()
    assert [p for _, p in worker.calls] == ["research", "review"]


def test_retired_facades_cannot_dispatch_old_execution(tmp_path):
    package = Path(__file__).resolve().parents[1] / "src/auto_agents"
    for name in (
        "orchestrator",
        "session",
        "workflow_runtime",
        "cli_impl",
        "release_worker",
    ):
        text = (package / (name + ".py")).read_text()
        tree = ast.parse(text)
        assert not any(
            isinstance(n, (ast.ClassDef, ast.FunctionDef)) for n in tree.body
        )
        assert len(text.splitlines()) < 15
    for path in (package / "control").glob("*.py"):
        if path.name in {"migration.py", "projection.py", "compat.py"}:
            continue
        assert "from ..orchestrator" not in path.read_text()
        assert "from ..session import" not in path.read_text()
