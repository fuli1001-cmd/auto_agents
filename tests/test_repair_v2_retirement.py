import json
import time

import pytest

from auto_agents.repair_control import Store as ControlStore
from auto_agents.repair_v2 import images
from auto_agents.repair_v2.retirement import abandon, verified_abandonment
from auto_agents.repair_v2.store import Store
from auto_agents.repair_v2.transaction import transaction_lock, bind_controller
from auto_agents.repair_v2.types import RepairBlocked


@pytest.fixture
def retained(tmp_path):
    control = ControlStore(tmp_path / 'control')
    identity = 'a' * 64
    root = control.root / 'v2-transactions' / identity
    Store(root).save({'status': 'blocked', 'phase': 'validate', 'revision': 1})
    (root / 'candidate.py').write_text('retained code\n')
    directory = control.root / 'jobs' / ('b' * 24); directory.mkdir(parents=True)
    (directory / 'v2-transaction.json').write_text(json.dumps({'root': str(root)}))
    with control.connect() as db:
        db.execute('INSERT INTO jobs(id,dedup,state,payload,result,generation,updated) VALUES(?,?,?,?,?,?,?)',
                   (directory.name, 'key', 'blocked', '{}', '{}', 2, time.time()))
        db.execute('INSERT INTO subscribers VALUES(?,?,?,?,?,?,?)',
                   ('subscriber', directory.name, '/project', 'token', 'blocked', '{}', time.time()))
        db.execute('INSERT INTO outbox VALUES(?,?,?,?,?)', (directory.name, 'pending', 0, time.time(), ''))
    images.pin('sha256:0', root)
    return control, root, directory.name


def pin_active():
    return json.loads(next((images.registry() / 'pins').glob('*.json')).read_text())['active']


def test_explicit_abandonment_fences_resume_and_releases_only_pin(retained):
    control, root, job = retained
    assert pin_active()
    result = abandon(control.root, root.name, 'operator no longer needs this repair')
    assert result['ok'] and result['evidence_retained'] and not pin_active()
    with control.connect() as db:
        assert verified_abandonment(root, db)
        assert tuple(db.execute('SELECT state,generation FROM jobs WHERE id=?', (job,)).fetchone()) == ('cancelled', 3)
    assert (root / 'candidate.py').read_text() == 'retained code\n'
    with pytest.raises(RepairBlocked, match='explicitly abandoned'):
        with transaction_lock(root): pytest.fail('reopened abandoned transaction')
    with pytest.raises(RepairBlocked): bind_controller(root, {}, 'new-generation')
    with pytest.raises(ValueError): images.pin('sha256:1', root)
    assert abandon(control.root, root.name, 'retry')['ok']


def test_cancelled_flag_alone_does_not_release_pin(retained):
    control, root, job = retained
    with control.connect() as db: db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,))
    images.release_completed(images.registry(), float('inf'))
    assert pin_active()


@pytest.mark.parametrize('protection', ['active_job', 'transaction_lock', 'unsettled_kernel'])
def test_abandon_refuses_live_or_uncertain_work(retained, protection):
    control, root, job = retained
    if protection == 'active_job':
        with control.connect() as db: db.execute("UPDATE jobs SET state='repairing' WHERE id=?", (job,))
    if protection == 'unsettled_kernel':
        with control.connect() as db:
            db.executescript("CREATE TABLE kernel_outbox(state TEXT); INSERT INTO kernel_outbox VALUES('unknown');")
    if protection == 'transaction_lock':
        with transaction_lock(root), pytest.raises(RepairBlocked): abandon(control.root, root.name, 'stop')
    else:
        with pytest.raises(RepairBlocked): abandon(control.root, root.name, 'stop')
    assert not (root / 'abandonment.json').exists() and pin_active()


def test_interrupted_retirement_is_retryable_but_cannot_release_early(retained, monkeypatch):
    control, root, job = retained
    with monkeypatch.context() as patch:
        patch.setattr(Store, 'transition', lambda *a, **k: (_ for _ in ()).throw(OSError('power loss')))
        with pytest.raises(OSError): abandon(control.root, root.name, 'retire')
    assert (root / 'abandonment.json').exists() and pin_active()
    images.release_completed(images.registry(), float('inf'))
    assert pin_active()
    assert abandon(control.root, root.name, 'retry')['ok'] and not pin_active()


def test_abandon_cli_selects_transaction_without_resuming_work(retained, monkeypatch, capsys):
    control, root, _ = retained
    monkeypatch.setenv('AUTO_AGENTS_RECOVERY_CONTROL', str(control.root))
    from auto_agents.cli import main
    assert main(['repair', 'abandon', '--transaction', root.name, '--reason', 'retired']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'abandoned'
