from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
import time

import pytest

from auto_agents.models import CommandResult
from auto_agents.verification_ledger import VerificationLedger, source_identity


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_AGENTS_VERIFICATION_ROOT", str(tmp_path / "proofs"))
    root = tmp_path / "source"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "Test"], ["config", "user.email", "t@example.invalid"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "code.py").write_text("VALUE=1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def passed():
    return CommandResult("pytest", True, 0, stdout="1 passed", executed_tests=["test_a"])


def test_same_source_is_executed_once_across_callers(repository):
    calls = []
    barrier = threading.Barrier(3)
    def run():
        calls.append(1)
        time.sleep(0.05)
        return passed()
    def request():
        ledger = VerificationLedger(repository, environment="same")
        barrier.wait(timeout=2)
        return ledger.execute("pytest", run)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: request(), range(3)))
    assert len(calls) == 1
    assert sum(result.cached for result in results) == 2
    assert len({result.proof_ref for result in results}) == 1


@pytest.mark.parametrize("change", ["source", "untracked", "environment", "context", "policy", "fresh"])
def test_proof_identity_invalidates_actual_inputs(repository, change):
    first = VerificationLedger(repository, environment="one", context="a")
    first.execute("pytest", passed, metadata="policy")
    if change == "source":
        (repository / "code.py").write_text("VALUE=2\n")
    if change == "untracked":
        (repository / "new.py").write_text("VALUE=3\n")
    ledger = VerificationLedger(repository, environment="two" if change == "environment" else "one",
                                context="b" if change == "context" else "a")
    result = ledger.execute("pytest", passed, metadata="other" if change == "policy" else "policy", fresh=change == "fresh")
    assert not result.cached


@pytest.mark.parametrize("kind", ["failure", "timeout", "cleanup", "mutation"])
def test_uncertain_and_failed_results_are_not_cached(repository, kind):
    ledger = VerificationLedger(repository)
    def run():
        result = passed()
        if kind == "failure":
            result.ok, result.returncode = False, 1
        if kind == "timeout":
            result.termination_reason = "timeout"
        if kind == "cleanup":
            result.cleanup_incomplete = True
        if kind == "mutation":
            (repository / "code.py").write_text("changed")
        return result
    ledger.execute("pytest", run)
    assert not ledger.execute("pytest", passed).cached


def test_audit_disagreement_revokes_namespace(repository):
    ledger = VerificationLedger(repository)
    ledger.execute("pytest", passed)
    result = ledger.execute("pytest", lambda: CommandResult("pytest", False, 1), audit_rate=1)
    assert not result.ok
    assert (ledger.root / "revoked.json").exists()
    assert not ledger.execute("pytest", passed).cached
