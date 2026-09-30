"""Explicit operator migration; never rewrite an existing session binding."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .models import GateConfig


UPDATE_FIELDS = frozenset({'impact_symbols', 'release_trigger', 'coalesce_safe', 'node_replay_safe',
    'parallel_safe', 'depends_on_proofs', 'cache_scope', 'cpu_slots', 'memory_mb', 'memory_guard',
    'memory_reserve_mb', 'serial_reason'})


def preserve_operator_metadata(gates, generated):
    """An older task-plan format must not erase reviewed v5 execution policy."""
    from dataclasses import replace
    existing = {step.proof_id: step for step in gates.steps}
    result = []
    for step in generated:
        prior = existing.get(step.proof_id)
        if prior and all(getattr(prior, key) == getattr(step, key) for key in ('runner', 'targets', 'args', 'levels')):
            step = replace(step, **{key:getattr(prior,key) for key in UPDATE_FIELDS})
        result.append(step)
    return result


def migrate_config(config, updates=None):
    """Keep exact proof identities/targets/options while changing reviewed policy."""
    candidate = deepcopy(config)
    gates = candidate['gates']
    steps = {step['proof_id']: step for step in gates['steps'] if step.get('proof_id')}
    if len(steps) != len(gates['steps']):
        raise ValueError('v5 migration requires unique explicit proof IDs')
    for identity, changes in (updates or {}).items():
        if identity not in steps or not set(changes).issubset(UPDATE_FIELDS):
            raise ValueError('migration may only update reviewed execution/impact metadata')
        step = steps[identity]
        dropped = set(step.get('depends_on_proofs', [])) - set(changes.get('depends_on_proofs', step.get('depends_on_proofs', [])))
        if dropped and any(steps.get(key, {}).get('artifact_globs') for key in dropped):
            raise ValueError('artifact-producing prerequisites must be retained')
        step.update(deepcopy(changes))
    gates.update(verification_policy_version=5, release_verification_mode='blocking',
                 parallel_workers=2, max_auto_workers=2)
    # Unknown business changes cannot be certified by a small smoke fallback.
    gates['unmapped_change_policy'] = 'release'
    gates['fallback_proof_ids'] = []
    parsed = GateConfig.from_dict(gates)
    from .verification_selection import select_verification_steps
    selected = select_verification_steps(parsed.steps, Path('.'), parsed, level='release', preserve_release_targets=True)
    if set(selected.proof_ids) != set(steps):
        raise ValueError('migration must preserve complete final proof coverage')
    return candidate


def prepare_migration(root, manifest=None):
    root = Path(root).resolve()
    config_path = root / '.auto-agents/config.json'
    if config_path.is_symlink():
        raise ValueError('migration requires a regular project config')
    original = config_path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if manifest and manifest.get('config_sha256') != digest:
        raise ValueError('config changed since the reviewed migration manifest')
    candidate = migrate_config(json.loads(original), (manifest or {}).get('updates'))
    from .validation import validate_project_config_payload
    # The public parser and project schema must agree before writing anything.
    errors = validate_project_config_payload(candidate)
    if errors:
        raise ValueError('invalid migrated config: ' + str(errors))
    return config_path, original, candidate


def apply_migration(root, manifest=None):
    path, original, candidate = prepare_migration(root, manifest)
    if path.read_bytes() != original:
        raise ValueError('config changed while preparing migration')
    from .io_utils import write_json
    write_json(path, candidate)
    return candidate
