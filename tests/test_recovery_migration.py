import json
from pathlib import Path

import pytest

from auto_agents.recovery import KernelStore, KernelError
from auto_agents.recovery.migration import apply_project, inspect_project, check, import_repairs
from auto_agents.recovery.authority import activate_project


def legacy(project, kind='fix', attempt=4):
    directory = project/'.auto-agents/state'
    path = directory/'run_state.json' if kind == 'run' else directory/f'sessions/{kind}/session_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {'mode': kind, 'status': 'failed', 'goal': 'Resume the original goal',
             'current_attempt': attempt, 'fix_verify_command': 'python -m pytest tests/test_original.py'}
    value['run_id' if kind == 'run' else 'session_id'] = kind
    path.write_text(json.dumps(value))
    return path


def test_all_modes_import_read_only_idempotently_and_preserve_identity(tmp_path):
    project = tmp_path/'project'
    paths = [legacy(project, kind) for kind in ('collab','fix','run','provider_resolve')]
    before = {p: p.read_bytes() for p in paths}
    store = KernelStore(tmp_path/'control')
    manifest = inspect_project(project)
    first = apply_project(store, manifest)
    snapshots = [store.replay(row['stream']) for row in first]
    assert apply_project(store, manifest) == first
    assert [store.replay(row['stream']) for row in first] == snapshots
    assert {p: p.read_bytes() for p in paths} == before
    assert all(next(iter(s['tasks'].values()))['attempts'] == 4 for s in snapshots)


def test_changed_input_and_escaping_symlink_never_import(tmp_path):
    project = tmp_path/'project'
    path = legacy(project)
    manifest = inspect_project(project)
    path.write_text('{}')
    with pytest.raises(KernelError, match='changed'): apply_project(KernelStore(tmp_path/'control'), manifest)
    outside = tmp_path/'outside.json'; outside.write_text('{}')
    path.unlink(); path.symlink_to(outside)
    assert inspect_project(project)['ok'] is False


def test_managed_project_upgrade_replays_authority_without_importing_thin_views(tmp_path):
    project = tmp_path/'project'; path = legacy(project)
    store = KernelStore(tmp_path/'control')
    first = apply_project(store, inspect_project(project))
    store.set_meta('mode', 'active'); store.set_meta('activation', {'projects': [str(project)]})
    activate_project(store, project)
    path.write_text('{"kernel_schema": 1}')
    before = store.load(first[0]['stream'])
    assert apply_project(store, inspect_project(project))[0]['already_managed']
    assert store.replay(first[0]['stream']) == before


def test_version_jobs_alias_one_closed_incident(tmp_path, monkeypatch):
    project = tmp_path/'project'; legacy(project)
    store = KernelStore(tmp_path/'control')
    first = apply_project(store, inspect_project(project))
    txn = store.root/'transaction'; txn.mkdir()
    artifact = {'artifact_id': 'artifact'}
    result = {'v2_transaction': str(txn), 'commit':'commit', 'runtime_artifact':artifact,
              'engine_full_proof': {'recovered': True}}
    (txn/'live-recovery.json').write_text(json.dumps({'job':'old','generation':1,'commit':'commit','artifact_id':'artifact'}))
    monkeypatch.setattr('auto_agents.repair_v2.integration.verify_receipt', lambda *a, **k: None)
    payload = {'project':str(project),'invocation': {'session_id':'fix'}, 'boundary': {'route_digest':'route'}}
    with store.connect() as db:
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,payload TEXT,result TEXT,generation INTEGER,updated REAL)')
        for name, generation in [('old',1),('new',2)]:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',(name,json.dumps(payload),json.dumps(result),generation,generation))
    imported = import_repairs(store, store.root, project)
    assert all(r['resolved'] for r in imported)
    snapshot = store.replay(first[0]['stream'])
    assert len(snapshot['incidents']) == 1
    assert next(iter(snapshot['incidents'].values()))['legacy_jobs'] == ['old','new']
    import_repairs(store, store.root, project)
    assert store.replay(first[0]['stream']) == snapshot


def test_explicit_chain_limits_and_review_usage_survive_migration(tmp_path):
    from auto_agents.recovery import Command, Contract, Event
    from auto_agents.recovery.migration import import_budgets
    from auto_agents.repair_v2.store import atomic_json, digest
    project = tmp_path/'project'; legacy(project)
    store = KernelStore(tmp_path/'control')
    imported = apply_project(store,inspect_project(project)); stream = imported[0]['stream']
    saved = {'identity':{'project':str(project),'subject':'session:fix'},'transactions':{
        'one':{'canonical':'one','model_calls':2,'implementations':1}}, 'proof_reviews':{'proof':1},
        'explicit_limits':True,'limits':{'model_calls':3,'implementations':2,'transactions':1}}
    atomic_json(store.root/'repair-chains/chain/state.json',{'state':saved,'digest':digest(saved)})
    import_budgets(store,store.root,project)
    snapshot = store.replay(stream)
    assert snapshot['budget']['model_calls'] == 7
    assert snapshot['budget']['repair_model_calls'] == 3
    assert snapshot['budget']['repair_limits']['model_calls'] == 3
    ref = store.put({'counterexample':'retained'})
    contract = Contract(snapshot['goal_id'],'new-engine','engine_repair',ref,ref,(),('original check',),
                        (str(project),),ref,'phase_completed',('plan',))
    store.apply(stream,snapshot['revision'],Event('engine-bind','task_bound',{'contract':contract.to_dict()}))
    command = Command('attempt',stream,contract.task_id,'plan','a'*64,contract.identity,'b'*64,'c'*64,'attempt',True)
    with pytest.raises(KernelError,match='repair call limit'):
        store.apply(stream,store.load(stream)['revision'],Event('attempt','command_reserved',command.to_dict()))
    import_budgets(store,store.root,project)
    assert store.load(stream)['budget'] == snapshot['budget']
