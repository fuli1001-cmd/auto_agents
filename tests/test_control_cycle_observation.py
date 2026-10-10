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


@pytest.mark.parametrize("context", [{"operator_answers": ["New decision"]}, {"prototype_repairs": 1}])
def test_failed_decision_transition_does_not_reset_loop_history(tmp_path, monkeypatch, context):
    from auto_agents.control import ControlError

    store = Store(tmp_path / "project")
    work = running(store)
    for _ in range(3):
        step(store, work)
    work = store.transition(work, "BLOCKED")
    with pytest.raises(ControlError):
        store.transition(work, "READY", context=context, operation="missing")
    assert store.work(work["id"]) == work
    work = store.transition(work, "READY")
    store.transition(work, "RUNNING")
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    Observer(store.project).publish()
    assert cycle(json.loads(observation.read_text()))


@pytest.mark.parametrize("cancelled,consumed", [(True, True), (True, False), (False, True)])
def test_only_confirmed_consumed_cancellation_starts_a_fresh_window(tmp_path, monkeypatch, cancelled, consumed):
    store = Store(tmp_path / "project")
    work = running(store)
    for _ in range(3):
        step(store, work)
    op = store.reserve(work, "model:prototype", 0, {}, model=True)
    store.settle(op["id"], {
        "code": "provider_cancelled" if cancelled else "provider_failed",
        "category": "provider", "message": "Confirmed cancelled" if cancelled else "Confirmed failure",
    }, state="FAILED")
    if consumed:
        store.consume(op["id"])
    step(store, work)
    before = store.workflow(work["workflow"])
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    Observer(store.project).publish()
    value = json.loads(observation.read_text())
    assert cycle(value) is not (cancelled and consumed)
    assert len(value["steps"]) == (1 if cancelled and consumed else 4)
    assert value["progress_seq"] == 0 and not value["milestones"]
    assert store.workflow(work["workflow"]) == before and before["calls"] == 1


@pytest.mark.parametrize("counter", ["prototype_repairs", "protocol_retries"])
def test_bounded_retry_is_exempt_once_and_ordinary_reentry_still_detects_cycles(tmp_path, monkeypatch, counter):
    store = Store(tmp_path / "project")
    work = running(store)
    step(store, work)
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    for attempt in (1, 2):
        work = store.transition(work, "READY", context={counter: attempt})
        work = store.transition(work, "RUNNING")
        step(store, work)
        Observer(store.project).publish()
        value = json.loads(observation.read_text())
        assert not cycle(value)
        assert value["steps"][-1]["counted"] is False
    for _ in range(2):
        work = store.transition(work, "READY")
        work = store.transition(work, "RUNNING")
        step(store, work)
        Observer(store.project).publish()
    value = json.loads(observation.read_text())
    assert cycle(value) and len(value["steps"]) == 5
    assert value["progress_seq"] == 0 and not value["milestones"]


@pytest.mark.parametrize("always_invalid", [False, True])
def test_prototype_scope_corrections_finish_before_supervisor_stops_them(tmp_path, monkeypatch, always_invalid):
    from auto_agents.control.engine import Engine
    from auto_agents.models import ProjectConfig
    from test_control_domains import Frontend
    from test_control_engine import project

    root = project(tmp_path)
    store = Store(root)
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    attempts = []

    class Corrected(Frontend):
        def run(self, ctx, phase, *args, **kwargs):
            if phase == "prototype_prepare":
                Observer(root).publish()
                value = json.loads(observation.read_text())
                assert not cycle(value), "Supervisor would kill an admitted correction request"
                attempts.append(ctx.work_id)
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare" and (len(attempts) < 3 or always_invalid):
                reply = json.loads(result["text"])
                reply["scope"]["changes"][0]["requirement_ids"] = []
                result["text"] = json.dumps(reply)
            return result

    config = ProjectConfig("bounded corrections")
    config.frontend_design.mode = "off"
    engine = Engine(root, store, config, transport=Corrected(), auto_approve=True, print_fn=lambda *a: None)
    result = engine.start("run", "Set value")
    assert len(attempts) == 3
    if always_invalid:
        assert result["status"] == "BLOCKED" and result["failure"]["code"] == "prototype_scope"
    else:
        assert result["status"] == "WAITING" and result["context"]["approval"] == "prototype", result["failure"]
    operations = [o for o in store.operations() if o["kind"] == "model:prototype_prepare"]
    assert len(operations) == 3 and all(o["state"] == "CONFIRMED" for o in operations)
    assert store.workflow(result["workflow"])["calls"] == (4 if always_invalid else 7)


def test_retry_before_oldest_retained_step_is_exempt_once(tmp_path):
    from auto_agents.control.observer import step_history

    store = Store(tmp_path / "project")
    work = running(store)
    step(store, work)
    work = store.transition(work, "READY", context={"prototype_repairs": 1})
    work = store.transition(work, "RUNNING")
    step(store, work)
    for _ in range(31):
        work = store.transition(work, "READY")
        work = store.transition(work, "RUNNING")
        step(store, work)
    steps = step_history(store, work["workflow"])
    assert len(steps) == 32 and steps[0]["counted"] is False
    assert sum(not row.get("counted", True) for row in steps) == 1
    assert cycle({"steps": steps, "progress_seq": 0})


def test_unrelated_workflow_cancellation_keeps_current_loop_history(tmp_path, monkeypatch):
    store = Store(tmp_path / "project")
    work = running(store)
    for _ in range(3):
        step(store, work)
    other = store.create_workflow("run", "Other goal", "other-source")
    other = store.transition(other, "RUNNING")
    op = store.reserve(other, "model:clarify", 0, {}, model=True)
    store.settle(op["id"], {"code": "provider_cancelled"}, state="FAILED")
    store.consume(op["id"])
    observation = tmp_path / "observation.json"
    monkeypatch.setenv("AUTO_AGENTS_OBSERVATION_FILE", str(observation))
    Observer(store.project).publish()
    assert cycle(json.loads(observation.read_text()))


def test_native_supervisor_keeps_the_final_admitted_correction_alive(tmp_path):
    from test_control_engine import project

    root = project(tmp_path)
    engine = Path(__file__).resolve().parents[1]
    script = tmp_path / "corrected_provider.py"
    script.write_text('''import json,sys,time
from pathlib import Path
sys.path[:0]=[sys.argv[1]+'/src',sys.argv[1]+'/tests']
from auto_agents.control import Store
from auto_agents.control.engine import Engine
from auto_agents.control.observer import Observer
from auto_agents.models import ProjectConfig
from test_control_domains import Frontend
root=Path(sys.argv[2])
attempts=[]
class Corrected(Frontend):
    def run(self,ctx,phase,*args,**kwargs):
        if phase=='prototype_prepare':
            observer.publish()
            time.sleep(.3)
            attempts.append(ctx.work_id)
        result=super().run(ctx,phase,*args,**kwargs)
        if phase=='prototype_prepare' and len(attempts)<3:
            reply=json.loads(result['text'])
            reply['scope']['changes'][0]['requirement_ids']=[]
            result['text']=json.dumps(reply)
        return result
config=ProjectConfig('native corrections')
config.frontend_design.mode='off'
store=Store(root)
with Observer(root) as observer:
    result=Engine(root,store,config,transport=Corrected(),auto_approve=True,
                  print_fn=lambda *a:None).start('run','Set value')
Path(sys.argv[3]).write_text(json.dumps({'work':result,'attempts':len(attempts)}))
''')
    report = tmp_path / "result.json"
    observation = tmp_path / "observation.json"
    result = run([sys.executable, str(script), str(engine), str(root), str(report)],
        cwd=tmp_path, env={**os.environ, "AUTO_AGENTS_OBSERVATION_FILE": str(observation)},
        log=tmp_path / "process.log", observation=observation, timeout=30)
    assert result["ok"], result
    value = json.loads(report.read_text())
    assert value["attempts"] == 3 and value["work"]["status"] == "WAITING", value["work"]["failure"]
    assert value["work"]["context"]["approval"] == "prototype"
    assert not any(o["state"] in {"UNKNOWN", "DISPATCHED"} for o in Store(root).operations())
