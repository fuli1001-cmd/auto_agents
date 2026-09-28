"""Quiescent, independently verified adoption; never reopen an incident."""
import json
from pathlib import Path
import sqlite3
import time

from .model import canonical, checksum, digest, require

MANDATORY_CHECKS = frozenset({'journal_replay', 'all_entrypoints', 'migration', 'receipt_integrity',
    'closed_incident_upgrade', 'unknown_effect', 'host_confinement', 'review_protocol',
    'candidate_feedback', 'restart_matrix', 'rollback_compatibility'})


def check_receipt(store, receipt, runtime):
    """The stable verifier, not the upgrade candidate, issues this receipt."""
    from ..repair_v2.runtime_artifact import verify
    verify(runtime)
    require(receipt.get('schema') == 1 and receipt.get('runtime') == runtime['artifact_id']
            and receipt.get('source') == runtime['source'], 'upgrade_receipt', 'Upgrade receipt names another runtime')
    require(receipt.get('verifier') == store.meta('trusted_verifier'), 'upgrade_verifier', 'Untrusted upgrade verifier')
    checks = receipt.get('checks', {})
    require(set(checks) == MANDATORY_CHECKS and all(checks.values()), 'upgrade_checks', 'Core upgrade gates are incomplete')
    require(receipt.get('event_schemas') == [1] and receipt.get('rpc_protocol') == 2,
            'upgrade_protocol', 'Core cannot replay the installed protocol')
    for name in MANDATORY_CHECKS:
        proof = store.read(checks[name])
        require(proof.get('ok') is True and proof.get('check') == name
                and proof.get('source') == runtime['source']
                and proof.get('verifier') == receipt['verifier'], 'upgrade_proof', 'Upgrade gate has no matching evidence', check=name)
    reference = store.put(receipt)
    require(reference in store.meta('verified_upgrades', []), 'upgrade_receipt', 'Upgrade receipt was not sealed by the stable verifier')
    if receipt.get('trusted_runtime'):
        verify(receipt['trusted_runtime'])
        installed = store.meta('trusted_verifier_runtime')
        require(installed is None or installed == receipt['trusted_runtime'],
                'upgrade_verifier','A core candidate cannot replace the independent verifier')
    return reference


def activate(store, runtime, receipt, *, migration_manifests=()):
    """Commit one active runtime pointer after every control lane is idle."""
    reference = check_receipt(store, receipt, runtime)
    from .migration import inspect_project
    # The caller holds project/run locks. Reject even a benign state change
    # since validation rather than importing mixed-generation inputs.
    for manifest in migration_manifests:
        require(inspect_project(manifest['project'])['identity'] == manifest['identity'],
                'migration_changed', 'Migration input advanced before cutover')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT id FROM kernel_outbox WHERE state IN ('pending','running','unknown') LIMIT 1").fetchone()
        require(active is None, 'upgrade_busy', 'Unsettled kernel operation prevents cutover')
        try:
            live = db.execute("SELECT id FROM jobs WHERE state IN ('repairing','validating','resuming') LIMIT 1").fetchone()
        except sqlite3.OperationalError: live = None
        require(live is None, 'upgrade_busy', 'Legacy operations have not drained')
        frontier = digest({row['id']:row['revision'] for row in db.execute('SELECT id,revision FROM kernel_streams')})
        require(receipt.get('state_frontier') == frontier, 'upgrade_journal',
                'Current business history needs a fresh replay proof before adoption or rollback')
        for manifest in migration_manifests:
            row = db.execute("SELECT state FROM kernel_migrations WHERE id=?", (manifest['identity'],)).fetchone()
            require(row and row['state'] == 'imported', 'upgrade_migration', 'A project migration is not committed')
        def get(key, default=None):
            row = db.execute('SELECT value FROM kernel_meta WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default
        previous = get('active_runtime')
        generation = get('epoch', 0) + 1
        values = {'previous_runtime': previous, 'active_runtime': runtime, 'epoch': generation,
                  'trusted_verifier_runtime':get('trusted_verifier_runtime') or receipt.get('trusted_runtime'),
                  'mode': 'active', 'activation_receipt': reference,
                  'activation': {'at': time.time(), 'epoch': generation,
                                 'projects': sorted(set(get('activation', {}).get('projects', [])) |
                                                    {m['project'] for m in migration_manifests})}}
        if (Path(runtime['path']) / 'src/auto_agents/recovery/runtime_manager.py').is_file():
            values['runtime_manager_runtime'] = runtime
        for key, value in values.items():
            db.execute('INSERT INTO kernel_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, canonical(value)))
    return {'ok': True, 'epoch': generation, 'runtime': runtime['artifact_id']}


def rollback(store, receipt):
    """Select a compatible prior core, never roll back business events or usage."""
    previous = store.meta('previous_runtime')
    require(previous is not None, 'rollback_unavailable', 'No prior compatible core is retained')
    return activate(store, previous, receipt)


def independent_verify(store, runtime, checks, verifier):
    """Run stable, caller-installed probes; no candidate-selected commands."""
    from ..repair_v2.runtime_artifact import verify
    verify(runtime)
    require(set(checks) == MANDATORY_CHECKS, 'upgrade_checks', 'Stable verifier must supply the entire gate inventory')
    checksum(verifier)
    require(store.meta('trusted_verifier') == verifier, 'upgrade_verifier', 'Verifier differs from installed policy')
    refs = {}
    for name in sorted(MANDATORY_CHECKS):
        result = checks[name](runtime)
        require(isinstance(result, dict) and result.get('ok') is True,
                'upgrade_check_failed', 'Core remains unchanged after a failed gate', check=name)
        refs[name] = store.put({**result, 'ok': True, 'check': name, 'source': runtime['source'], 'verifier': verifier})
    receipt = {'schema': 1, 'runtime': runtime['artifact_id'], 'source': runtime['source'],
               'verifier': verifier, 'checks': refs, 'event_schemas': [1], 'rpc_protocol': 2,
               'state_frontier':store.frontier()}
    reference = store.put(receipt)
    store.set_meta('verified_upgrades', [*store.meta('verified_upgrades', []), reference])
    return receipt
