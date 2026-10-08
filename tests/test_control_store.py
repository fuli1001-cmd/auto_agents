import pytest
from auto_agents.control import Store, Status, ControlError, Contract, VerificationSpec


def test_transitions_contracts_and_budget_are_transactional(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow(
        "fix", "Repair the owned behavior", "source", max_calls=2, node_limit=3
    )
    contract = store.contract(work["contract"])
    assert contract.identity == work["contract"]
    running = store.transition(work, Status.RUNNING)
    with pytest.raises(ControlError):
        store.transition(work, Status.RUNNING)
    op = store.reserve(running, "classify", 0, {"prompt": "one"}, model=True)
    with pytest.raises(ControlError):
        store.reserve(running, "classify", 0, {"prompt": "one"}, model=True)
    store.settle(op["id"], {"decision": "fix"})
    assert not store.reserve(running, "classify", 0, {"prompt": "one"}, model=True).get(
        "new"
    )
    store.consume(op["id"])
    second = store.reserve(running, "classify", 1, {"prompt": "two"}, model=True)
    store.settle(second["id"], {"ok": False}, state="FAILED")
    with pytest.raises(ControlError):
        store.reserve(running, "classify", 2, {"prompt": "three"}, model=True)
    assert store.workflow(work["workflow"])["calls"] == 2
    with pytest.raises(ControlError):
        store.transition(running, Status.COMPLETED, contract="forged")


def test_unknown_model_blocks_dispatch_and_preserves_counters(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("collab", "Goal", "source")
    work = store.transition(work, Status.RUNNING)
    op = store.reserve(work, "diagnose", 0, {}, model=True)
    store.orphan_calls()
    with pytest.raises(ControlError) as failure:
        store.reserve(work, "diagnose", 1, {}, model=True, provider="other")
    assert failure.value.category == "reconciliation"
    assert store.workflow(work["workflow"])["calls"] == 1


def test_resume_is_same_node_and_terminal_state_cannot_reopen(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("run", "Goal", "source")
    work = store.transition(work, Status.RUNNING)
    work = store.transition(work, Status.BLOCKED, failure={"code": "environment"})
    work = store.transition(work, Status.READY)
    work = store.transition(work, Status.RUNNING)
    work = store.transition(work, Status.COMPLETED)
    with pytest.raises(ControlError):
        store.transition(work, Status.READY)
    assert len(store.works()) == 1


@pytest.mark.parametrize("readonly", [False, True])
def test_future_schema_is_refused_before_any_schema_mutation(tmp_path, readonly):
    store = Store(tmp_path)
    with store.connect(True) as db:
        db.execute("UPDATE control_meta SET value='99' WHERE key='schema'")
        db.execute("CREATE TABLE future_receipts(value TEXT)")
    with pytest.raises(ControlError, match="Unsupported business database schema"):
        Store(tmp_path, readonly=readonly)
    import sqlite3

    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT value FROM control_meta WHERE key='schema'").fetchone()[
                0
            ]
            == "99"
        )
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='future_receipts'"
        ).fetchone()
