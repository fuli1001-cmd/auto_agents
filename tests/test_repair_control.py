from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_control import (
    Repository, Store, Supervisor, RecoveredProcess, atomic_json, digest, git,
    private_directory, rpc, start_ticks,
)
from auto_agents.run_lock import ProjectRunLock, RunAlreadyActiveError


def configuration(tmp_path):
    root = private_directory(tmp_path / "control")
    return {"root": str(root), "identity": digest(str(tmp_path))[:24],
            "socket_dir": str(Path(tempfile.gettempdir()) / ("aas-" + digest(str(tmp_path))[:10])),
            "lock_dir": str(Path(tempfile.gettempdir()) / "auto-agents-run-locks"),
            "source_root": str(tmp_path / "engine"), "remote": str(tmp_path / "remote.git"),
            "ref": "refs/heads/master", "python": sys.executable, "publish": True}


def registration(project, token="token"):
    return {"project": str(project), "token": token, "pid": os.getpid(), "ticks": start_ticks(os.getpid())}


def failure(project):
    return {"project": str(project), "fingerprint": "same-error", "contract": {"checks": ["test"]},
            "base": "base", "environment": "engine-python", "boundary": {"kind": "completion"}}


def test_job_deduplication_includes_contract_base_and_environment(tmp_path):
    store = Store(tmp_path / "state")
    a = store.register(registration(tmp_path / "a"))
    b = store.register(registration(tmp_path / "b"))
    first = store.submit(a, failure(tmp_path / "a"))
    assert store.submit(b, failure(tmp_path / "b")) == first
    store.transition(first, "blocked", {"error": "missing environment"})
    assert store.submit(b, failure(tmp_path / "b")) == first  # no fresh retry budget
    assert store.submit(b, {**failure(tmp_path / "b"), "environment": "other"}) != first


def test_cancel_tombstone_rejects_late_worker_and_re_registration(tmp_path):
    store = Store(tmp_path / "state")
    payload = registration(tmp_path / "project")
    subscriber = store.register(payload)
    job = store.submit(subscriber, failure(tmp_path))
    generation = store.job(job)["generation"]
    store.cancel(job=job)
    assert not store.transition(job, "ready", {"ok": True}, generation=generation)
    assert store.job(job)["state"] == "cancelled"
    with pytest.raises(RuntimeError, match="cancelled"):
        store.register(payload)


def test_one_project_cancel_does_not_cancel_shared_repair(tmp_path):
    store = Store(tmp_path / "state")
    subscribers = [store.register(registration(tmp_path / name)) for name in ("a", "b")]
    jobs = [store.submit(subscriber, failure(tmp_path)) for subscriber in subscribers]
    assert jobs[0] == jobs[1]
    store.cancel(project=str(tmp_path / "a"))
    assert store.job(jobs[0])["state"] == "queued"
    store.cancel(project=str(tmp_path / "b"))
    assert store.job(jobs[0])["state"] == "cancelled"


def test_publication_retry_is_durable_and_does_not_reset_repair(tmp_path):
    store = Store(tmp_path / "state")
    subscriber = store.register(registration(tmp_path))
    job = store.submit(subscriber, failure(tmp_path))
    store.transition(job, "completed", {"commit": "approved"})
    store.publish_later(job)
    store.publish_later(job, permission=True)
    restored = Store(tmp_path / "state")
    assert restored.job(job)["result"]["commit"] == "approved"
    assert restored.due_publish() == []
    with restored.connect() as db:
        assert db.execute("SELECT state FROM outbox").fetchone()["state"] == "authorization_required"


def make_remote(config):
    engine = Path(config["source_root"])
    engine.mkdir()
    subprocess.run(["git", "init", "-b", "master", str(engine)], check=True, capture_output=True)
    git(engine, "config", "user.name", "Test")
    git(engine, "config", "user.email", "test@example.com")
    (engine / "bug.py").write_text("value = 'old'\n")
    git(engine, "add", "bug.py")
    git(engine, "commit", "-m", "base")
    subprocess.run(["git", "init", "--bare", config["remote"]], check=True, capture_output=True)
    git(engine, "push", config["remote"], "HEAD:master")
    return engine


def test_fetch_and_publish_do_not_modify_developer_worktree(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, fresh = repository.fetch()
    (engine / "bug.py").write_text("uncommitted developer change\n")
    git(engine, "add", "bug.py")
    before = git(engine, "diff", "--cached")
    runtime = repository.worktree(base, "candidate")
    (runtime / "bug.py").write_text("value = 'fixed'\n")
    git(runtime, "add", "bug.py")
    git(runtime, "commit", "-m", "fix")
    commit = git(runtime, "rev-parse", "HEAD")
    repository.push(commit)
    assert repository.fetch() == (commit, True)
    assert git(engine, "diff", "--cached") == before
    assert (engine / "bug.py").read_text() == "uncommitted developer change\n"
    assert git(engine, "rev-parse", "HEAD") == base


def test_remote_race_is_rejected_without_force(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    candidate = repository.worktree(base, "candidate")
    (candidate / "bug.py").write_text("our fix\n")
    git(candidate, "add", "bug.py")
    git(candidate, "commit", "-m", "ours")
    (engine / "other.py").write_text("other fix\n")
    git(engine, "add", "other.py")
    git(engine, "commit", "-m", "theirs")
    git(engine, "push", config["remote"], "HEAD:master")
    with pytest.raises(RuntimeError, match="publication failed"):
        repository.push(git(candidate, "rev-parse", "HEAD"))
    assert git(Path(config["remote"]), "rev-parse", "master") == git(engine, "rev-parse", "HEAD")


def test_fetch_uses_cached_version_and_marks_staleness(tmp_path):
    config = configuration(tmp_path)
    make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    original = git
    def offline(root, *args, **kwargs):
        if args[0] == "fetch":
            raise RuntimeError("offline")
        return original(root, *args, **kwargs)
    with patch("auto_agents.repair_control.git", side_effect=offline), patch("auto_agents.repair_control.time.sleep"):
        assert repository.fetch() == (base, False)


def test_unix_lock_transfer_keeps_project_owned_until_terminal(tmp_path):
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    thread = threading.Thread(target=supervisor.serve, daemon=True)
    thread.start()
    project = tmp_path / "project"
    project.mkdir()
    lock = ProjectRunLock(project, environ={}).acquire()
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                rpc(config, {"op": "ping"})
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        payload = registration(project, lock.run_token)
        result = rpc(config, {"op": "register", "payload": payload}, [lock.fileno])
        lock.release()
        with pytest.raises(RunAlreadyActiveError):
            ProjectRunLock(project, environ={}).acquire()
        rpc(config, {"op": "finish", "subscriber": result["subscriber"]})
        deadline = time.monotonic() + 5
        while supervisor.registrations:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with ProjectRunLock(project, environ={}):
            pass
    finally:
        lock.release()
        supervisor.halt = True
        thread.join(5)


def test_stale_generation_and_pid_cannot_be_recovered(tmp_path):
    config = configuration(tmp_path)
    store = Store(config["root"])
    subscriber = store.register(registration(tmp_path))
    job = store.submit(subscriber, failure(tmp_path))
    store.cancel(job=job)
    atomic_json(store.root / "jobs" / job / "repair-lease.json", {
        "kind": "worker", "job": job, "generation": 1, "operation": "repair", "pid": os.getpid(), "ticks": start_ticks(os.getpid())})
    assert Supervisor(config).workers == {}
    process = RecoveredProcess({"pid": os.getpid(), "ticks": start_ticks(os.getpid()) + 1})
    assert process.poll() == 3


def test_restore_running_worker_without_starting_a_duplicate(tmp_path):
    config = configuration(tmp_path)
    store = Store(config["root"])
    subscriber = store.register(registration(tmp_path))
    job = store.submit(subscriber, failure(tmp_path))
    store.transition(job, "repairing")
    atomic_json(store.root / "jobs" / job / "repair-lease.json", {
        "kind": "worker", "job": job, "generation": 1, "operation": "repair", "pid": os.getpid(), "ticks": start_ticks(os.getpid())})
    restored = Supervisor(config)
    with patch.object(restored, "launch_worker", side_effect=AssertionError("duplicate worker")):
        restored.tick()
    assert restored.workers[job][0].pid == os.getpid()


def test_revision_reuse_requires_differential_and_boundary(tmp_path):
    from auto_agents.repair_worker import check_revision
    config = configuration(tmp_path)
    engine = make_remote(config)
    for behavioral, boundary in ((False, True), (True, False), (True, True)):
        runner = SimpleNamespace(_load_or_create_experiment=lambda: (None, None),
            _diagnosis_differential=lambda *args: SimpleNamespace(ok=behavioral, summary="behavior"),
            _replay_candidate=lambda *args: SimpleNamespace(ok=boundary, summary="boundary"))
        assert check_revision(runner, engine, git(engine, "rev-parse", "HEAD"))[0] == (behavioral and boundary)


def test_environment_setup_never_falls_back_to_target_project(tmp_path):
    from auto_agents.repair_worker import engine_environment
    config = configuration(tmp_path)
    root = Path(config["source_root"])
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="example"\n')
    (root / "src/auto_agents").mkdir(parents=True)
    (root / "src/auto_agents/repair_control.py").write_text("VERSION = 1\n")
    (root / "src/auto_agents/repair_client.py").write_text("")
    with patch("auto_agents.repair_worker.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["venv"])) as execute:
        with pytest.raises(subprocess.CalledProcessError):
            engine_environment(config, root)
    assert execute.call_count == 1
    assert execute.call_args.args[0][0] == config["python"]


def test_runtime_without_control_protocol_cannot_be_installed(tmp_path):
    from auto_agents.repair_worker import engine_environment
    with pytest.raises(RuntimeError, match="lacks the repair control protocol"):
        engine_environment(configuration(tmp_path), tmp_path)


def test_custom_state_roots_do_not_connect_to_another_supervisor(tmp_path):
    from auto_agents.repair_control import socket_path
    config = configuration(tmp_path)
    assert socket_path(config) != socket_path({**config, "root": str(tmp_path / "other-state")})


def fake_worker_install(config, *, delay=0):
    directory = Path(config["source_root"]) / "src/auto_agents"
    directory.mkdir(parents=True)
    from auto_agents import repair_control
    (directory / "repair_control.py").write_text(Path(repair_control.__file__).read_text())
    (directory / "repair_worker.py").write_text(
        "import json, os, sys, time\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]); r=json.loads(p.read_text()); op=r['operation']\n"
        f"time.sleep({delay})\n"
        "with (p.parent / 'calls.log').open('a') as out: out.write(op+'\\n')\n"
        "result={'ok':True,'generation':r['job']['generation'],'status':'already_repaired',"
        "'commit':'verified','base':'base','runtime':r['config']['source_root'],'python':sys.executable,'proof':'transport fixture'}\n"
        "p.with_name(p.name.replace('-request.json','-result.json')).write_text(json.dumps(result))\n"
    )
    (directory / "repair_launch.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]); r=json.loads(p.read_text())\n"
        "fd=int(os.environ['AUTO_AGENTS_RUN_LOCK_FD'])\n"
        "assert json.loads(os.pread(fd,8192,0))['run_token']==r['subscriber']['token']\n"
        "p.with_name(p.stem+'-result.json').write_text(json.dumps({'exit_code':0}))\n"
    )


def test_end_to_end_transport_deduplicates_worker_and_restores_each_lock(tmp_path):
    config = configuration(tmp_path)
    fake_worker_install(config)
    supervisor = Supervisor(config)
    locks = []
    identities = []
    try:
        for name in ("one", "two"):
            project = tmp_path / name
            project.mkdir()
            lock = ProjectRunLock(project, environ={}).acquire()
            locks.append(lock)
            response = supervisor.register({"payload": registration(project, lock.run_token)}, [os.dup(lock.fileno)])
            identities.append(response["subscriber"])
        jobs = [supervisor.store.submit(identity, failure(tmp_path)) for identity in identities]
        assert jobs[0] == jobs[1]
        deadline = time.monotonic() + 10
        while any(row["state"] != "finished" for row in supervisor.store.subscriptions(jobs[0])):
            assert time.monotonic() < deadline
            supervisor.tick()
            time.sleep(0.02)
        calls = (Path(config["root"]) / "jobs" / jobs[0] / "calls.log").read_text().splitlines()
        assert calls.count("repair") == 1
        assert sum(call.startswith("validate-") for call in calls) == 2
        assert supervisor.store.job(jobs[0])["state"] == "completed"
    finally:
        for process, _, _ in supervisor.workers.values():
            supervisor.stop_process(process)
        for lock in locks:
            lock.release()
        for registration_data in supervisor.registrations.values():
            for fd in registration_data["fds"]:
                os.close(fd)


def test_cancel_running_worker_never_starts_resume(tmp_path):
    config = configuration(tmp_path)
    fake_worker_install(config, delay=10)
    supervisor = Supervisor(config)
    project = tmp_path / "project"
    project.mkdir()
    with ProjectRunLock(project, environ={}) as lock:
        subscriber = supervisor.register({"payload": registration(project, lock.run_token)}, [os.dup(lock.fileno)])["subscriber"]
        job = supervisor.store.submit(subscriber, failure(project))
        supervisor.tick()
        assert supervisor.workers
        supervisor.store.cancel(job=job)
        deadline = time.monotonic() + 5
        while supervisor.workers:
            assert time.monotonic() < deadline
            supervisor.tick()
            time.sleep(0.02)
        assert not supervisor.resumes
        assert supervisor.store.job(job)["state"] == "cancelled"


def test_boundary_requires_correct_process_runtime_and_exact_command(tmp_path):
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    project = tmp_path / "project"
    project.mkdir()
    with ProjectRunLock(project, environ={}) as lock:
        subscriber = supervisor.register({"payload": registration(project, lock.run_token)}, [os.dup(lock.fileno)])["subscriber"]
        payload = {**failure(project), "boundary": {"kind": "gate", "command": "pytest exact.py"}}
        job = supervisor.store.submit(subscriber, payload)
        supervisor.store.transition(job, "ready", {"runtime": str(tmp_path / "verified"), "commit": "verified", "status": "repaired"})
        with supervisor.store.connect() as db:
            db.execute("UPDATE subscribers SET state='resuming' WHERE id=?", (subscriber,))
        request = {"version": 1, "op": "boundary", "subscriber": subscriber, "kind": "gate",
                   "details": {"command": "pytest wrong.py"}, "_peer_pid": os.getpid(), "runtime": str(tmp_path / "verified")}
        assert not supervisor.dispatch(request, [])["accepted"]
        with pytest.raises(RuntimeError, match="wrong engine"):
            supervisor.dispatch({**request, "runtime": str(tmp_path / "old")}, [])
        with pytest.raises(RuntimeError, match="registered business"):
            supervisor.dispatch({**request, "_peer_pid": -1}, [])
        request["details"]["command"] = "pytest exact.py"
        assert supervisor.dispatch(request, [])["accepted"]
        with supervisor.store.connect() as db:
            first = dict(db.execute("SELECT * FROM outbox").fetchone())
        supervisor.dispatch(request, [])
        with supervisor.store.connect() as db:
            assert dict(db.execute("SELECT * FROM outbox").fetchone()) == first
        supervisor.store.cancel(job=job)
        supervisor.tick()


def test_continuous_mode_keeps_worktree_and_provider_receipt(tmp_path):
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner
    runner = object.__new__(AutoAgentsSelfRepairRunner)
    runner._continuous_workspace = tmp_path / "continuous"
    with runner._candidate_workspace() as root:
        (root / "patch.txt").write_text("retained")
    with runner._candidate_workspace() as root:
        assert (root / "patch.txt").read_text() == "retained"
    with patch.object(runner, "_provider_continuation_context", return_value="contract"):
        atomic_json(root / "provider.json", {"context": "contract", "continuation": {"resume_session_id": "same-session"}})
        assert runner._provider_continuation()["resume_session_id"] == "same-session"
    atomic_json(root / "fallback.json", {"reason": "no improvement"})
    assert not runner._continuous_mode()
    assert (root / "patch.txt").exists()


def test_publication_disabled_cannot_use_cached_receipt_to_push(tmp_path):
    from auto_agents.repair_worker import publish
    with patch("auto_agents.repair_worker.Repository") as repository:
        with pytest.raises(PermissionError):
            publish({"config": {"publish": False}, "job": {}})
        repository.assert_not_called()


def test_remote_reuse_runs_actual_behavior_without_a_repair_model(tmp_path):
    import shutil
    from auto_agents.repair_worker import repair
    config = configuration(tmp_path)
    engine = make_remote(config)
    base = git(engine, "rev-parse", "HEAD")
    repository = Repository(config)
    repository.fetch()
    latest = repository.worktree(base, "upstream-fix")
    (latest / "bug.py").write_text("value = 'fixed'\n")
    git(latest, "add", "bug.py")
    git(latest, "commit", "-m", "fix elsewhere")
    commit = git(latest, "rev-parse", "HEAD")
    repository.push(commit)
    project = tmp_path / "project"
    project.mkdir()
    (project / "input.txt").write_text("unchanged")
    def behavior(root):
        return subprocess.run([sys.executable, "-c", "import bug; assert bug.value == 'fixed'"], cwd=root, capture_output=True).returncode == 0
    class Oracle:
        def _load_or_create_experiment(self):
            return None, None
        def _diagnosis_differential(self, old, candidate):
            return SimpleNamespace(ok=not behavior(engine) and behavior(candidate), summary="actual old failure/new pass")
        def _replay_candidate(self, candidate, *args):
            return SimpleNamespace(ok=behavior(candidate), summary="actual boundary")
        def _full_suite_differential(self, old, candidate):
            return SimpleNamespace(ok=behavior(candidate), recoverable=False, summary="full fixture proof")
        def run(self):
            raise AssertionError("remote reuse must not generate a candidate")
    payload = {**failure(project), "base": base, "invocation": {}, "autonomy": "max"}
    with patch("auto_agents.repair_worker.engine_environment", return_value=(sys.executable, "env")), \
         patch("auto_agents.repair_worker.make_runner", return_value=Oracle()), \
         patch("auto_agents.root_cause.RootCauseCoordinator._copy_diagnostic_tree", side_effect=lambda src, dst: shutil.copytree(src, dst)):
        result = repair({"config": config, "job": {"id": "reuse", "payload": payload}})
    assert result["status"] == "already_repaired"
    assert result["commit"] == commit
    assert (project / "input.txt").read_text() == "unchanged"
    assert git(engine, "rev-parse", "HEAD") == base


def test_publication_integrates_upstream_and_reuses_proof_after_network_failure(tmp_path):
    from auto_agents.repair_worker import publish
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    candidate = repository.worktree(base, "candidate")
    (candidate / "bug.py").write_text("value = 'fixed'\n")
    git(candidate, "add", "bug.py")
    git(candidate, "commit", "-m", "our repair")
    repair_commit = git(candidate, "rev-parse", "HEAD")
    (engine / "unrelated.txt").write_text("remote work")
    git(engine, "add", "unrelated.txt")
    git(engine, "commit", "-m", "parallel remote work")
    git(engine, "push", config["remote"], "HEAD:master")
    request = {"config": config, "job": {"id": "publish-job", "payload": {"base": base},
        "result": {"commit": repair_commit, "base": base, "python": sys.executable}}}
    suite_calls = []
    oracle = SimpleNamespace(_load_or_create_experiment=lambda: (None, None),
        _diagnosis_differential=lambda old, root: SimpleNamespace(ok="fixed" in (root / "bug.py").read_text(), summary="specific behavior"),
        _replay_candidate=lambda *args: SimpleNamespace(ok=True, summary="boundary"),
        _candidate_test_weakening_reason=lambda *args: "",
        _run_full_suite_shards=lambda root: (suite_calls.append(root) or SimpleNamespace(ok=True, summary="suite passed")))
    with patch("auto_agents.repair_worker.make_runner", return_value=oracle), patch.object(Repository, "push", side_effect=RuntimeError("network")):
        with pytest.raises(RuntimeError, match="network"):
            publish(request)
    assert len(suite_calls) == 1
    with patch("auto_agents.repair_worker.make_runner", side_effect=AssertionError("must reuse verified integration")):
        result = publish(request)
    assert result["ok"]
    remote = git(Path(config["remote"]), "rev-parse", "master")
    assert git(repository.cache, "show", remote + ":unrelated.txt") == "remote work"
    assert "fixed" in git(repository.cache, "show", remote + ":bug.py")


def test_real_daemon_uses_an_immutable_committed_bootstrap(tmp_path):
    from auto_agents.repair_control import ensure_supervisor, alive
    import signal
    config = configuration(tmp_path)
    engine = make_remote(config)
    fake_worker_install(config)
    git(engine, "add", "src")
    git(engine, "commit", "-m", "install worker transport fixture")
    ensure_supervisor(config)
    response = rpc(config, {"op": "ping"})
    pid = response["pid"]
    try:
        assert pid != os.getpid()
        assert alive(pid, response["ticks"])
        assert rpc(config, {"op": "status"})["jobs"] == []
        installed = json.loads((Path(config["root"]) / "operator.json").read_text())
        assert git(installed["implementation_root"], "rev-parse", "HEAD") == git(engine, "rev-parse", "HEAD")
    finally:
        if alive(pid, response["ticks"]):
            os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)


def test_foreground_registration_retains_original_cwd(tmp_path, monkeypatch):
    from auto_agents import repair_client
    monkeypatch.chdir(tmp_path)
    config = configuration(tmp_path)
    lock = SimpleNamespace(project_root=tmp_path / "project", run_token="token", fileno=10)
    orchestrator = SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace(autonomy=SimpleNamespace(mode="max"))))
    args = SimpleNamespace(command="collab", autonomy=None)
    with patch.object(repair_client, "enabled", return_value=True), patch.object(repair_client, "configure", return_value=config), \
         patch.object(repair_client, "ensure_supervisor"), patch.object(repair_client, "rpc", return_value={"subscriber": "sub"}) as call:
        repair_client.register(lock, args, orchestrator)
    assert call.call_args.args[1]["payload"]["cwd"] == str(tmp_path)


def test_shared_generic_contract_skips_repeated_diagnosis_calls(tmp_path):
    from auto_agents.cli import _triage_terminal_run_error
    from auto_agents.self_repair import SelfRepairDecision
    from test_root_cause import _report
    report = _report(role="investigator", verdict="ROOT_CAUSE")
    diagnosis = {"diagnosis_id": "known", "evidence_path": "", "investigator": report,
                 "reviewer": {**report, "role": "reviewer", "verdict": "AGREE"}, "arbiter": None, "final": report,
                 "repair_approved": True, "reason": "known generic defect"}
    cached = {"decision": SelfRepairDecision(True, category="retry_restore_invariant").to_dict(), "diagnosis": diagnosis}
    with patch("auto_agents.repair_client.cached_contract", return_value=cached), \
         patch("auto_agents.cli.adjudicate_auto_agents_error", side_effect=AssertionError("redundant model diagnosis")):
        result = _triage_terminal_run_error(tmp_path, SimpleNamespace(), RuntimeError("known failure"))
    assert result.source == "shared_repair_contract"
    assert result.root_cause.repair_approved


def test_repair_recurrence_does_not_reuse_a_false_success_forever(tmp_path):
    store = Store(tmp_path / "state")
    subscriber = store.register(registration(tmp_path))
    payload = failure(tmp_path)
    job = store.submit(subscriber, payload)
    store.transition(job, "completed", {"ok": True, "commit": "candidate"})
    with store.connect() as db:
        db.execute("UPDATE subscribers SET state='resuming' WHERE id=?", (subscriber,))
    assert store.submit(subscriber, payload) == job
    assert store.job(job)["state"] == "blocked"
    assert store.job(job)["result"]["recurrence"]


def test_nested_repair_moves_recovered_process_to_waiting_relay(tmp_path):
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    project = tmp_path / "project"
    project.mkdir()
    with ProjectRunLock(project, environ={}) as lock:
        subscriber = supervisor.register({"payload": registration(project, lock.run_token)}, [os.dup(lock.fileno)])["subscriber"]
        first = supervisor.store.submit(subscriber, failure(project))
        process = RecoveredProcess({"pid": os.getpid(), "ticks": start_ticks(os.getpid())})
        supervisor.resumes[subscriber] = process
        response = supervisor.dispatch({"version": 1, "op": "submit", "subscriber": subscriber,
            "_peer_pid": os.getpid(), "payload": {**failure(project), "fingerprint": "different-fault"}}, [])
        assert response["job"] != first
        assert subscriber not in supervisor.resumes
        assert supervisor.relays == [(subscriber, process)]
        status = supervisor.dispatch({"version": 1, "op": "status", "subscriber": subscriber}, [])
        assert status["job"]["id"] == response["job"]
        os.close(supervisor.registrations[subscriber]["fds"][0])


def test_engine_checks_bind_conda_wrappers_to_candidate_python(tmp_path):
    from auto_agents.execution_binding import engine_verification_command
    source = tmp_path / "developer"
    candidate = tmp_path / "candidate"
    source.mkdir()
    candidate.mkdir()
    (candidate / "test_example.py").write_text("def test_example():\n    assert True\n")
    command = "cd " + str(source) + " && conda run -p ./.conda python -m pytest -q " + str(source / "test_example.py")
    compiled = engine_verification_command(command, candidate, sys.executable, source)
    assert "conda run" not in compiled
    assert str(source) not in compiled
    result = subprocess.run(compiled, shell=True, cwd=candidate, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_failed_worker_releases_project_lock_without_losing_job(tmp_path):
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    project = tmp_path / "project"
    project.mkdir()
    lock = ProjectRunLock(project, environ={}).acquire()
    subscriber = supervisor.register({"payload": registration(project, lock.run_token)}, [os.dup(lock.fileno)])["subscriber"]
    job = supervisor.store.submit(subscriber, failure(project))
    supervisor.store.transition(job, "repairing")
    atomic_json(Path(config["root"]) / "jobs" / job / "repair-g1-result.json", {"ok": False, "error": "environment unavailable", "generation": 1})
    supervisor.workers[job] = (RecoveredProcess({"pid": -1, "ticks": 1}), 1, "repair")
    supervisor.tick()
    assert supervisor.store.job(job)["state"] == "blocked"
    assert supervisor.store.subscriptions(job)[0]["state"] == "blocked"
    assert subscriber not in supervisor.registrations
    lock.release()
    with ProjectRunLock(project, environ={}):
        pass


def test_registration_loss_preserves_an_approved_runtime(tmp_path):
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    subscriber = supervisor.store.register(registration(tmp_path))
    job = supervisor.store.submit(subscriber, failure(tmp_path))
    supervisor.store.transition(job, "ready", {"ok": True, "commit": "keep-candidate", "runtime": "keep-runtime"})
    supervisor.launch_worker(job, "validate-" + subscriber)
    assert supervisor.store.job(job)["state"] == "blocked"
    assert supervisor.store.job(job)["result"]["commit"] == "keep-candidate"
    assert supervisor.store.job(job)["result"]["ok"]


def test_late_worker_failure_cannot_block_a_new_generation(tmp_path):
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    subscriber = supervisor.store.register(registration(tmp_path))
    job = supervisor.store.submit(subscriber, failure(tmp_path))
    atomic_json(Path(config["root"]) / "jobs" / job / "repair-g1-result.json", {"ok": False, "generation": 1})
    with supervisor.store.connect() as db:
        db.execute("UPDATE jobs SET generation=2 WHERE id=?", (job,))
    supervisor.workers[job] = (RecoveredProcess({"pid": -1, "ticks": 1}), 1, "repair")
    with patch.object(supervisor, "launch_worker"):
        supervisor.tick()
    assert supervisor.store.job(job)["state"] == "queued"
    assert supervisor.store.subscriptions(job)[0]["state"] == "waiting"


def test_retained_repair_integrates_new_upstream_without_losing_edits(tmp_path):
    from auto_agents.repair_worker import carry_continuous_work
    from auto_agents.git_ops import add_worktree
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    checkout = repository.worktree(base, "base")
    continuous = Path(config["root"]) / "continuous"
    continuous.mkdir()
    retained = continuous / "repair"
    add_worktree(checkout, retained, ref=base)
    atomic_json(continuous / "base.json", {"revision": base})
    (retained / "bug.py").write_text("partial repair retained\n")
    (engine / "upstream.txt").write_text("new remote work\n")
    git(engine, "add", "upstream.txt")
    git(engine, "commit", "-m", "upstream advanced")
    git(engine, "push", config["remote"], "HEAD:master")
    new_base, _ = repository.fetch()
    runner = SimpleNamespace(_continuous_workspace=continuous, repo_root=checkout)
    carry_continuous_work(runner, new_base)
    assert (retained / "bug.py").read_text() == "partial repair retained\n"
    assert (retained / "upstream.txt").read_text() == "new remote work\n"
    assert git(retained, "diff", "--name-only", new_base) == "bug.py"


def test_selected_worker_reexecs_latest_logic_without_changing_process_identity(tmp_path, monkeypatch):
    from auto_agents.repair_worker import execute_selected_worker
    runtime = tmp_path / "selected"
    entry = runtime / "src/auto_agents/repair_worker.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# selected implementation\n")
    request = {"_request_path": str(tmp_path / "repair-g1-request.json"), "prepared_runtime": {"revision": "selected"}}
    monkeypatch.delenv("AUTO_AGENTS_REPAIR_LOCK_FD", raising=False)
    with patch("auto_agents.repair_worker.os.execve", side_effect=RuntimeError("exec boundary")) as execute:
        with pytest.raises(RuntimeError, match="exec boundary"):
            execute_selected_worker(request, runtime, sys.executable)
    assert execute.call_args.args[1] == [sys.executable, str(entry), request["_request_path"]]
    assert execute.call_args.args[2]["PYTHONPATH"] == str(runtime / "src")
    assert json.loads(Path(request["_request_path"]).read_text())["prepared_runtime"]["revision"] == "selected"


def test_verified_fast_publication_does_not_wait_for_another_repair(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    root = repository.worktree(base, "candidate")
    (root / "bug.py").write_text("fixed\n")
    git(root, "add", "bug.py")
    git(root, "commit", "-m", "verified repair")
    commit = git(root, "rev-parse", "HEAD")
    supervisor = Supervisor(config)
    first = supervisor.store.register(registration(tmp_path / "a"))
    job = supervisor.store.submit(first, failure(tmp_path))
    supervisor.store.transition(job, "completed", {"ok": True, "commit": commit, "base": base})
    supervisor.store.enqueue_publish(job)
    second = supervisor.store.register(registration(tmp_path / "b"))
    busy = supervisor.store.submit(second, {**failure(tmp_path), "fingerprint": "other"})
    supervisor.store.transition(busy, "repairing")
    supervisor.workers[busy] = (RecoveredProcess({"pid": os.getpid(), "ticks": start_ticks(os.getpid())}), 1, "repair")
    supervisor.tick()
    assert supervisor.publisher is not None
    supervisor.publisher.join(5)
    assert not supervisor.publisher.is_alive()
    assert busy in supervisor.workers
    assert git(Path(config["remote"]), "rev-parse", "master") == commit


def test_operator_can_revoke_publication_without_restarting_daemon(tmp_path):
    config = configuration(tmp_path)
    make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    atomic_json(Path(config["root"]) / "operator.json", {**config, "publish": False})
    with pytest.raises(PermissionError, match="not authorized"):
        repository.push(base)
