"""Human decisions start a fresh loop window; process restarts do not."""

import json
import os
from pathlib import Path
import sys

import pytest

from auto_agents.control import Store
from auto_agents.control.observer import Observer
from auto_agents_watch.process import cycle, run


def step(store, work, phase="prototype"):
    store.event(work["id"], "step_entered", {
        "step_id": work["id"] + ":" + phase,
        "work_id": work["id"], "phase": phase, "progress_seq": 0,
    })


def running(store):
    work = store.create_workflow("run", "Revise the prototype", "source", max_calls=5)
    store.set_meta("active_root", work["id"])
    return store.transition(work, "RUNNING", phase="prototype")


@pytest.mark.parametrize("legacy", [False, True])
def test_rejected_prototype_history_does_not_kill_the_revised_run(tmp_path, legacy):
    store = Store(tmp_path / "project")
    work = running(store)
    for _ in range(3):
        step(store, work)
    work = store.transition(work, "WAITING", phase="design", context={
        "waiting_for": "approval", "approval": "prototype", "approval_phase": "prototype",
    })
    work = store.transition(work, "READY", phase="prototype", context={"feedback": "Restore existing flows"})
    if legacy:
        # Existing schema-2 histories predate explicit decision metadata.
        with store.connect(True) as db:
            db.execute("UPDATE events SET data=json_remove(data,'$.operator_decision') WHERE event='transition'")
    work = store.transition(work, "RUNNING")
    step(store, work)
    work = store.transition(work, "READY", phase="prototype_prepare")
    work = store.transition(work, "RUNNING")
    step(store, work, "prototype_prepare")
    op = store.reserve(work, "model:prototype_prepare", 0, {}, model=True)
    before = store.workflow(work["workflow"])
    observation = tmp_path / "observation.json"
    source = str(Path(__file__).resolve().parents[1] / "src")
    result = run([
        sys.executable, "-c",
        "import sys,time; sys.path.insert(0,sys.argv[1]); "
        "from auto_agents.control.observer import Observer; "
        "Observer(sys.argv[2]).publish(); time.sleep(.3); print('continued',flush=True)",
        source, str(store.project),
    ], cwd=tmp_path, env={**os.environ, "AUTO_AGENTS_OBSERVATION_FILE": str(observation)},
        log=tmp_path / "process.log", observation=observation, timeout=10)
    assert result["ok"], result
    value = json.loads(observation.read_text())
    assert [row["phase"] for row in value["steps"]] == ["prototype", "prototype_prepare"]
    assert value["progress_seq"] == 0 and not value["milestones"]
    assert store.workflow(work["workflow"]) == before
    assert next(o for o in store.operations() if o["id"] == op["id"])["state"] == "DISPATCHED"


def test_automatic_retry_keeps_loop_history_across_observer_restarts(tmp_path, monkeypatch):
    store = Store(tmp_path / "project")
    work = running(store)
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    for _ in range(3):
        step(store, work)
        work = store.transition(work, "BLOCKED")
        work = store.transition(work, "READY")
        work = store.transition(work, "RUNNING")
        Observer(store.project).publish()
    assert cycle(json.loads(observation.read_text()))


def test_answer_to_blocked_work_starts_a_new_window_without_refunding_calls(tmp_path, monkeypatch):
    store = Store(tmp_path / "project")
    work = running(store)
    for _ in range(3):
        step(store, work)
    op = store.reserve(work, "model:prototype", 0, {}, model=True)
    store.settle(op["id"], {"text": "invalid proposal"}, state="FAILED")
    store.consume(op["id"])
    work = store.transition(work, "BLOCKED")
    work = store.transition(work, "READY", context={"operator_answers": ["Use the retained design"]})
    work = store.transition(work, "RUNNING")
    step(store, work)
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    Observer(store.project).publish()
    value = json.loads(observation.read_text())
    assert len(value["steps"]) == 1 and not cycle(value)
    assert store.workflow(work["workflow"])["calls"] == 1


def test_repetition_of_an_earlier_phase_does_not_block_a_new_phase():
    steps = [{"step_id": "work:prototype", "progress_seq": 0} for _ in range(3)]
    steps.append({"step_id": "work:prototype_prepare", "progress_seq": 0})
    assert not cycle({"step_id": "work:prototype_prepare", "progress_seq": 0, "steps": steps})
    assert cycle({"step_id": "work:prototype", "progress_seq": 0, "steps": steps})


def test_failed_decision_transition_does_not_reset_loop_history(tmp_path, monkeypatch):
    from auto_agents.control import ControlError

    store = Store(tmp_path / "project")
    work = running(store)
    for _ in range(3):
        step(store, work)
    work = store.transition(work, "BLOCKED")
    with pytest.raises(ControlError):
        store.transition(work, "READY", context={"operator_answers": ["New decision"]}, operation="missing")
    assert store.work(work["id"]) == work
    work = store.transition(work, "READY")
    store.transition(work, "RUNNING")
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    Observer(store.project).publish()
    assert cycle(json.loads(observation.read_text()))
