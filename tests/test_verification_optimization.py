from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import time

import pytest

from auto_agents.self_repair import _VerificationResult, _FullSuiteShard
from auto_agents.repair_control import atomic_json, start_ticks, retire_idle_legacy_supervisor, Store
from auto_agents.verification_trace import resolve_file_trace
from test_self_repair_performance import _runner


def test_lazy_baseline_checks_only_failed_batches(tmp_path):
    (tmp_path / "tests").mkdir()
    runner = _runner(tmp_path)
    command = "python -m pytest tests/test_a.py"
    failed = _VerificationResult(False, "FAILED tests/test_a.py::test_a", returncodes=(1,),
                                 payload={"source_commands": [command]})
    with patch.object(runner, "_run_full_suite_shards", return_value=failed), \
         patch.object(runner, "_run_full_suite_at_ref", side_effect=AssertionError("unnecessary full baseline")), \
         patch.object(runner, "_run_verification_at_ref", return_value=failed) as baseline:
        assert runner._full_suite_differential("base", tmp_path).ok
        assert runner._full_suite_differential("base", tmp_path).ok
    baseline.assert_called_once_with([command], "base")


def test_failed_checkpoint_is_diagnostic_not_a_cached_failure(tmp_path):
    runner = _runner(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    atomic_json(checkpoint, {"schema_version": 1, "suite_key": "key", "completed": {
        "shard": _VerificationResult(False, "old transient failure").to_dict()}})
    shard = _FullSuiteShard("shard", "tests/test_a.py", ("tests/test_a.py",), True)
    with patch.object(runner, "_collect_full_suite_shards", return_value=[shard]), \
         patch.object(runner, "_full_suite_checkpoint_key", return_value="key"), \
         patch.object(runner, "_full_suite_checkpoint_path", return_value=checkpoint), \
         patch.object(runner, "_full_suite_proof_cache_lookup", return_value=None), \
         patch.object(runner, "_full_suite_proof_cache_store"), \
         patch.object(runner, "_execute_full_suite_shard", return_value=_VerificationResult(True, "passed")) as execute:
        assert runner._run_full_suite_shards(tmp_path).ok
    execute.assert_called_once()


def test_file_trace_tracks_fork_cwd_and_decoded_directory_fd(tmp_path):
    text = (
        '100 chdir("sub") = 0\n'
        '100 clone(child_stack=NULL, flags=SIGCHLD) = 101\n'
        '101 openat(AT_FDCWD, "input.txt", O_RDONLY) = 3\n'
        f'101 openat(4<{tmp_path}/other>, "second.txt", O_RDONLY) = 5\n'
    )
    resolved = resolve_file_trace(text, tmp_path)
    assert str(tmp_path / "sub/input.txt") in resolved
    assert str(tmp_path / "other/second.txt") in resolved


@pytest.mark.parametrize("trace", [
    '100 openat(7, "file", O_RDONLY) = 8\n',
    '100 clone(child_stack=NULL, flags=CLONE_FS|SIGCHLD) = 101\n101 chdir("sub") = 0\n',
    '100 unshare(CLONE_NEWNS) = 0\n',
])
def test_unresolved_trace_cannot_certify_inputs(tmp_path, trace):
    assert resolve_file_trace(trace, tmp_path) is None


def test_denied_read_is_not_retried_as_host_side_hash(tmp_path):
    from auto_agents.gate_execution import _observed_input_manifest
    protected = tmp_path / "protected"
    protected.write_text("sensitive")
    trace = tmp_path / "trace.log"
    trace.write_text(f'100 openat(AT_FDCWD, "{protected}", O_RDONLY) = -1 EACCES (Permission denied)\n')
    manifest = _observed_input_manifest(trace, tmp_path, {})[0]
    assert manifest == {"?protected": "13"}
    from auto_agents.gate_result_cache import GateResultCache
    assert not GateResultCache(tmp_path)._manifest_matches(manifest)


def test_local_probe_does_not_launch_unrequested_runtimes(tmp_path):
    from auto_agents.workers import worker_probe
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="Python 3.11", stderr="")
    with patch("auto_agents.workers.subprocess.run", side_effect=execute):
        probe = worker_probe(required_capabilities={"python"})
    assert probe["ok"]
    assert calls and all(command[-1] == "--version" and "python" in Path(command[0]).name for command in calls)
    assert "chrome" not in probe["capabilities"]
    assert "docker" not in probe["capabilities"]


def test_idle_upgrade_never_stops_registered_business_owner(tmp_path):
    store = Store(tmp_path)
    store.register({"project": str(tmp_path / "project"), "token": "token", "pid": os.getpid(), "ticks": start_ticks(os.getpid())})
    with patch("auto_agents.repair_control.alive", return_value=True), patch("auto_agents.repair_control.os.kill") as kill:
        assert not retire_idle_legacy_supervisor({"root": str(tmp_path)}, {"pid": os.getpid() + 1, "ticks": 1})
    kill.assert_not_called()


def test_aged_large_request_drains_capacity_instead_of_starving(tmp_path):
    from auto_agents.workers import WorkerSlotLease
    directory = tmp_path / "slots/worker/queue"
    with WorkerSlotLease(tmp_path, "worker", 2, 1):
        large = WorkerSlotLease(tmp_path, "worker", 2, 2)
        small = WorkerSlotLease(tmp_path, "worker", 2, 1)
        for lease, age in ((large, 120), (small, 0)):
            atomic_json(directory / (lease.lease_id + ".json"), {
                "pid": os.getpid(), "ticks": start_ticks(os.getpid()), "required": lease.required,
                "created": time.time() - age, "priority": 0, "project": lease.lease_id})
        assert not small._queue_turn(directory.parent)
    assert large._queue_turn(directory.parent)


def test_git_object_hash_403_is_not_an_authorization_failure(tmp_path):
    from auto_agents.repair_control import Repository
    from test_repair_control import configuration, make_remote
    config = configuration(tmp_path)
    make_remote(config)
    repository = Repository(config)
    result = SimpleNamespace(returncode=1, stderr="[rejected] abc403def -> master (fetch first)")
    with patch("auto_agents.repair_control.git", return_value=result):
        with pytest.raises(RuntimeError, match="publication failed"):
            repository.push("abc403def")
