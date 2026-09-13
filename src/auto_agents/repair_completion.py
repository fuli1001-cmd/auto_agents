"""Conditional component completion, distinct from final integration proof.

Only the controller seals a successful reviewed component. Opaque checks require
their original execution context; unknown closures never imply independence.
"""
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
import os
from types import SimpleNamespace

from .repair_control import digest
from .repair_memory import component_key, read_record as _read_record, save_record, dependency_manifest, dependencies_match

VERSION = 1


def read_record(runner, reference):
    try:
        value = _read_record(runner, reference)
        return value if isinstance(value, dict) else None
    except AttributeError:
        return None


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, SimpleNamespace):
        return _plain(vars(value))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError('unknown validation configuration value')


def definition(group):
    # Display/state/finding ownership bookkeeping is not a new acceptance scope.
    return {key: value for key, value in group.items() if key not in {
        'status', 'completed_at', 'completed_by', 'finding_ids', 'group_id', 'title'}}


def policy():
    from .gate_result_cache import execution_policy_fingerprint
    from .prompting.core import policy_fingerprint
    root = Path(__file__).parent
    names = sorted({path.name for pattern in ('repair_*.py', 'verification*.py') for path in root.glob(pattern)}
                   | {'self_repair.py', 'self_repair_search.py', 'models.py', 'config.py', 'git_ops.py',
                      'repository_guard.py', 'process_supervision.py'})
    names.extend(sorted(str(path.relative_to(root)) for directory in ('adapters', 'prompting')
                        for path in (root / directory).rglob('*.py')))
    return digest([VERSION, execution_policy_fingerprint(),
                   policy_fingerprint(),
                   [(name, (root / name).read_text()) for name in names]])


def plan_semantics(record):
    draft = (record or {}).get('draft')
    if not isinstance(draft, dict):
        return None
    value = {key: deepcopy(item) for key, item in draft.items() if key != 'mode'}
    scenarios = value.get('scenarios', [])
    if not isinstance(scenarios, list) or any(not isinstance(item, dict) for item in scenarios):
        return None
    for scenario in scenarios:
        for key in ('finding_ids', 'reference_ids'):
            scenario.pop(key, None)
    return digest(value)


def context(runner, workspace):
    return digest(context_parts(runner, workspace))


def context_parts(runner, workspace):
    from .planning_capabilities import planning_capabilities
    from .repository_guard import capture_repository_guard
    reviewer = _reviewer_context(runner, workspace)
    execution = getattr(getattr(runner.target_orchestrator, 'config', None), 'execution', None)
    settings = _plain(execution) if execution else {}
    production = None
    if getattr(runner, '_real_project_root', None) is not None:
        from .repair_capability_checks import production_capabilities
        production = production_capabilities(runner, workspace)
    return {'environment': runner._full_suite_environment_fingerprint(), 'reviewer': reviewer,
        'settings': settings, 'capabilities': planning_capabilities(runner),
        'production_capabilities': production,
        'gates': _plain(getattr(runner.target_orchestrator.config, 'gates', None)),
        'ledger': _ledger_state(runner),
        'invocation': getattr(runner, '_invocation_context', {}),
        'project': str(Path(getattr(runner, '_real_project_root', None) or runner.target_project_root).resolve()),
        'project_inputs': capture_repository_guard(
            Path(getattr(runner, '_real_project_root', None) or runner.target_project_root), ignore_run_artifacts=True),
        'sandbox': bool(os.environ.get('AUTO_AGENTS_VERIFICATION_SANDBOX')),
        'read_roots': list(map(str, getattr(runner, '_verification_read_roots', [])))}


def execution_binding(runner, workspace, *, parts=None):
    """Review/model changes do not change the meaning of an executed check."""
    from .gate_result_cache import execution_policy_fingerprint
    parts = parts if parts is not None else context_parts(runner, workspace)
    root = Path(__file__).parent
    files = {name: digest((root / name).read_text()) for name in (
        'self_repair.py', 'models.py', 'config.py', 'repository_guard.py', 'git_ops.py',
        'process_supervision.py', 'repair_completion.py', 'repair_schedule.py',
        'repair_verification.py', 'repair_verification_pool.py', 'repair_concurrent_validation.py',
        'repair_dependencies.py', 'repair_test_refs.py', 'gate_execution.py', 'gate_result_cache.py',
        'gates.py', 'workers.py', 'verification_sandbox.py', 'verification_metadata.py', 'verification_input_trace.py',
        'gate_verification.py', 'verification_inputs.py', 'verification_probes.py',
        'verification_manifest.py', 'verification_pytest.py', 'verification_trace.py')}
    components = {key: digest(value) for key, value in parts.items() if key != 'reviewer'}
    return {'policy': digest([execution_policy_fingerprint(), files]), 'files': files,
            'context': digest(components), 'components': components}


def _ledger_state(runner):
    from .verification_ledger import ledger_root, repository_identity
    namespace = ledger_root() / digest(['engine', repository_identity(getattr(runner, '_engine_source_root', runner.repo_root))])
    revoked = namespace / 'revoked.json'
    return [str(namespace), digest(revoked.read_bytes().hex()) if revoked.exists() else None]


def _reviewer_context(runner, workspace):
    from .prompting.runtime import _settings_values, binary_identity
    config = runner.target_orchestrator.config
    provider = getattr(runner.target_orchestrator, '_current_provider', '') or getattr(config, 'active_provider', '')
    selected = getattr(config, 'providers', {}).get(provider)
    effort = runner._review_effort()
    if selected is None:
        return [provider, effort]
    root = Path(workspace).resolve()
    values = _settings_values(selected, SimpleNamespace(cwd=root, effort=effort), dict(os.environ))
    # Missing ancestor config paths carry no settings. Recreated checkout names
    # must not invalidate an identical local configuration; new ancestor files do.
    values['files'] = [(('<workspace>/' + str(Path(path).relative_to(root)))
                         if Path(path).is_relative_to(root) else path, fingerprint)
                       for path, fingerprint in values['files'] if Path(path).exists()]
    return [provider, selected.kind, binary_identity(selected.binary), values]


def snapshot(workspace):
    from .repository_guard import capture_repository_guard
    return digest(capture_repository_guard(workspace, ignore_run_artifacts=True))


def epoch(runner, workspace):
    return digest([getattr(runner, '_repair_control_binding', None), str(Path(workspace).resolve())])


def _memory(runner, group):
    # Component-key memory can be shared by overlapping groups. Completion is
    # owned by one group; another group's review cannot overwrite its status.
    key = 'completion:' + digest([component_key(group), group['group_id']])
    state = runner._experiment
    memory = state.component_memory.setdefault(key, {})
    if not memory.get('completion') and not memory.get('completion_history') and not memory.get('completion_assessment'):
        live = {g['group_id'] for g in state.finding_groups}
        matching = [g for g in state.finding_groups if definition(g) == definition(group)]
        if len(matching) == 1:
            for previous in list(state.component_memory.values()):
                proof = read_record(runner, previous.get('completion', {}))
                if proof and proof.get('component') not in live and proof.get('definition') == definition(group):
                    memory['completion'] = previous['completion']  # A rename, not cross-group approval.
                    break
    return memory


def _evidence_memory(runner, group):
    return runner._experiment.component_memory.setdefault(component_key(group), {})


def _paths(commands):
    from .repair_test_refs import pytest_targets
    return sorted({target.split('::', 1)[0] for command in commands for target in pytest_targets(command, prose=False)})


def seal(runner, workspace, review, verification):
    """Called after both gates succeed, before the candidate checkout is released."""
    if not review.ok or not verification.ok:
        return None
    group = runner._candidate_group
    canonical = next((g for g in runner._experiment.finding_groups if g['group_id'] == group['group_id']), group)
    from .repair_planning import finding_key
    before = snapshot(workspace)
    fresh = getattr(runner, '_fresh_component_completions', {})
    fresh[group['group_id']] = {'source': before, 'environment': digest(runner._full_suite_environment_fingerprint()),
        'findings': {f.finding_id: finding_key(f) for f in runner._experiment.findings.values()},
        'resolved': review.payload.get('resolved_finding_ids', [])}
    runner._fresh_component_completions = fresh
    target = _memory(runner, canonical)
    previous = target.pop('completion', None)
    if previous:
        target.setdefault('completion_history', []).append(previous)
    target['completion_assessment'] = {'state': 'completed', 'reason': 'fresh gates passed; no portable receipt sealed'}
    if (not runner._acceleration_enabled()
            or review.payload.get('decision') != 'APPROVE' or review.payload.get('findings')
            or review.payload.get('deferred_findings') or verification.payload.get('cleanup_incomplete')):
        return None
    memory = _evidence_memory(runner, group)
    review_ref, checks_ref = memory.get('code_review', {}), memory.get('verification_schedule', {})
    reviewed, schedule = read_record(runner, review_ref), read_record(runner, checks_ref)
    from .verification_ledger import source_identity
    from .git_ops import head_ref
    source = source_identity(workspace)
    if (not reviewed or reviewed.get('source') != source or reviewed.get('result', {}).get('decision') != 'APPROVE'
            or reviewed.get('result', {}).get('findings') or reviewed.get('result', {}).get('deferred_findings')
            or reviewed.get('result', {}).get('resolved_finding_ids', []) != review.payload.get('resolved_finding_ids', [])
            or reviewed.get('component', {}).get('group_id') != group['group_id']
            or not schedule or not schedule.get('ok') or schedule.get('phase') != 'expanded'
            or schedule.get('source_commit') != head_ref(workspace)
            or schedule.get('candidate_id') != runner._candidate_id):
        return None  # A boolean/model statement alone is not a completion receipt.
    commands = schedule.get('plan', {}).get('commands', [])
    observed = verification.payload.get('source_commands', [])
    if (not commands or set(commands) != set(observed) or len(observed) != len(verification.returncodes)
            or any(verification.returncodes) or any(verification.termination_reasons)):
        return None
    from .repair_test_refs import pytest_targets
    def nodes(command):
        targets = pytest_targets(command, prose=False)
        return [node for node in verification.payload.get('executed_tests', [])
                if any(node == target or node.startswith(target + '[') or node.startswith(target + '::') for target in targets)]
    proof = {'version': VERSION, 'component': group['group_id'], 'definition': definition(canonical),
        'contract': runner._experiment.contract_fingerprint, 'context': context(runner, workspace), 'policy': policy(),
        'source': before, 'candidate_id': runner._candidate_id, 'plan': deepcopy(group),
        'epoch': epoch(runner, workspace), 'source_identity': source,
        'plan_revision': _evidence_memory(runner, canonical).get('latest_revision'),
        'plan_semantics': plan_semantics(read_record(runner, _evidence_memory(runner, canonical).get('latest_revision', {}))),
        'acceptance': memory.get('acceptance_inventory', []),
        'review': review_ref, 'verification': checks_ref, 'commands': commands,
        'dependencies': dependency_manifest(workspace, [*group.get('touched_paths', []), *_paths(commands)]),
        'findings': {f.finding_id: finding_key(f) for f in runner._experiment.findings.values()},
        'resolved': review.payload.get('resolved_finding_ids', []),
        'checks': [{'command': command, 'command_digest': digest(command), 'executed_tests': nodes(command),
                    'inputs': next((row for row in verification.payload.get('completion_inputs', []) if row['command'] == command), {}),
                    'dependencies': dependency_manifest(workspace, _paths([command]))}
                   for command in commands]}
    proof['execution_binding'] = execution_binding(runner, workspace)
    if snapshot(workspace) != before:
        return None
    reference = save_record(runner, 'component_completion', proof)
    target['completion'] = reference
    target.pop('completion_assessment', None)
    runner._experiment_store.save(runner._experiment)
    return reference


def retained_proof(runner, group):
    """Authenticate the old completion as data, separately from current validity."""
    memory = _memory(runner, group)
    proof = read_record(runner, memory.get('completion', {}))
    if not proof:
        return None, 'completion receipt missing or invalid'
    if (proof.get('kind') != 'component_completion' or proof.get('version') != VERSION
            or proof.get('contract') != runner._experiment.contract_fingerprint
            or proof.get('definition') != definition(group)):
        return None, 'component contract or acceptance changed'
    current_revision = _evidence_memory(runner, group).get('latest_revision')
    revision_record = read_record(runner, current_revision or {})
    plan_changed = (proof.get('plan_revision') != current_revision
                    and (not revision_record or revision_record.get('component', {}).get('group_id') == group['group_id'])
                    and (not proof.get('plan_semantics') or proof['plan_semantics'] != plan_semantics(revision_record)))
    if (plan_changed
            or proof.get('acceptance', []) != _evidence_memory(runner, proof.get('plan', group)).get('acceptance_inventory', [])):
        return None, 'approved plan or retained acceptance inventory changed'
    review, checks = read_record(runner, proof.get('review', {})), read_record(runner, proof.get('verification', {}))
    if (not review or not checks or review.get('result', {}).get('decision') != 'APPROVE'
            or review.get('kind') != 'code_review' or checks.get('kind') != 'verification_schedule'
            or review.get('contract') != proof.get('contract')
            or review.get('source') != proof.get('source_identity')
            or review.get('component', {}).get('group_id') != proof.get('component')
            or review.get('result', {}).get('resolved_finding_ids', []) != proof.get('resolved', [])
            or checks.get('phase') != 'expanded' or checks.get('candidate_id') != proof.get('candidate_id')
            or review.get('result', {}).get('findings') or review.get('result', {}).get('deferred_findings')
            or not checks.get('ok')
            or checks.get('plan', {}).get('commands') != proof.get('commands')):
        return None, 'original review or verification evidence is missing'
    if (not proof.get('commands') or len(proof.get('checks', [])) != len(proof['commands'])
            or any(row.get('command') != command or row.get('command_digest') != digest(command)
                   for command, row in zip(proof['commands'], proof['checks']))):
        return None, 'original executable acceptance evidence is incomplete or redacted'
    return proof, ''


def assess(runner, workspace, group, *, observations=None):
    """No model/test execution, no progress credit, and no mutable receipt import."""
    from .repair_planning import finding_key, _retained_scope
    memory = _memory(runner, group)
    proof, reason = retained_proof(runner, group)
    result = {'state': 'needs_revalidation', 'reason': reason,
              'reusable_checks': [], 'affected_checks': []}
    if not proof:
        return result
    result['affected_checks'] = proof['commands']
    if getattr(runner, '_verification_fresh', False):
        return {**result, 'reason': 'fresh verification requested'}
    related_paths = set(proof['plan'].get('touched_paths', [])) | set(proof.get('dependencies', {}).get('files', {}))
    historical_scope = False
    for finding in runner._experiment.blocking_findings():
        owned = finding.finding_id in group.get('finding_ids', []) or finding.repair_group_id == group['group_id']
        related = bool(set(finding.affected_paths) & related_paths)
        unchanged = proof.get('findings', {}).get(finding.finding_id) == finding_key(finding)
        if owned and (finding.finding_id not in proof.get('resolved', []) or not unchanged or finding.status == 'reopened'):
            old_scope = runner._experiment.scope_decisions.get(finding.finding_id, {})
            if (unchanged and finding.status != 'reopened'
                    and old_scope.get('verdict') in {'not_applicable', 'follow_up'}
                    and _retained_scope(runner, old_scope, finding.to_dict())):
                historical_scope = True  # Reassess the old disproof, not a newly confirmed defect.
            else:
                return {**result, 'state': 'pending', 'reason': 'confirmed component defect: ' + finding.finding_id}
        if related and (not unchanged or finding.status == 'reopened'):
            return {**result, 'state': 'pending', 'reason': 'new evidence affects a component dependency: ' + finding.finding_id}
        if (not unchanged or finding.status == 'reopened') and not proof.get('dependencies', {}).get('complete'):
            return {**result, 'reason': 'new evidence has unproved dependency independence: ' + finding.finding_id}
    observations = observations or {'policy': policy(), 'context': context(runner, workspace), 'source': snapshot(workspace)}
    changed_context = proof.get('policy') != observations['policy'] or proof.get('context') != observations['context']
    if changed_context:
        if not proof.get('execution_binding'):
            return {**result, 'reason': 'verification policy or runtime context changed'}
        binding = observations.get('execution_binding') or execution_binding(runner, workspace)
        if proof['execution_binding'] != binding:
            return {**result, 'reason': 'verification policy or runtime context changed'}
    exact = proof.get('source') == observations['source']
    reusable = [item['command'] for item in proof.get('checks', [])
                if item.get('command_digest') == digest(item['command'])
                and _check_matches(runner, workspace, proof, item, exact)]
    result.update(reusable_checks=reusable, affected_checks=[c for c in proof['commands'] if c not in reusable])
    if historical_scope:
        return {**result, 'reason': 'historical scope conclusions require delta review'}
    if changed_context:
        return {**result, 'reason': 'review context changed; execution evidence checked separately'}
    if not result['affected_checks'] and (exact or dependencies_match(workspace, proof.get('dependencies'))):
        return {**result, 'state': 'completed', 'reason': 'completion evidence remains valid', 'receipt': memory['completion']}
    return {**result, 'reason': ('execution context changed; check dependencies could not be fully validated' if exact
                                else 'source changed; only proven-independent checks may be retained')}


def _check_matches(runner, workspace, proof, check, exact):
    from .verification_ledger import source_identity
    inputs = check.get('inputs', {})
    same_epoch = exact and proof.get('epoch') == epoch(runner, workspace)
    static = bool(check.get('executed_tests')) and dependencies_match(workspace, check.get('dependencies'))
    acceleration = getattr(runner.target_orchestrator.config.execution, 'acceleration', None)
    runtime = (inputs.get('complete') and not inputs.get('network')
               and getattr(acceleration, 'verification_input_mode', 'observe') == 'on')
    if not (same_epoch or static or runtime):
        return False
    manifest = inputs.get('manifest', {})
    if runtime and not manifest:
        return False
    if manifest:
        root = Path(workspace)
        recorded_root = Path(inputs.get('root', ''))
        if same_epoch and recorded_root.is_dir() and source_identity(recorded_root) == proof.get('source_identity'):
            root = recorded_root
        from .repair_dependencies import verification_dependency_state
        return runner._revalidate_verification_manifest(root, manifest, verification_dependency_state(runner._verification_python()))
    return same_epoch or static


def refresh(runner, workspace):
    """Refresh statuses against the selected retained source before scheduling."""
    state = runner._experiment
    if not state.planning_receipts and not any(m.get('completion') for m in state.component_memory.values()):
        return  # Legacy direct calls have no component planning gate.
    changed = False
    previous = {g['group_id']: (g.get('status'), deepcopy(_memory(runner, g).get('completion_assessment')))
                for g in state.finding_groups}
    observations = None
    if any(memory.get('completion') for memory in state.component_memory.values()):
        try:
            parts = context_parts(runner, workspace)
            observations = {'policy': policy(), 'context': digest(parts), 'source': snapshot(workspace),
                            'execution_binding': execution_binding(runner, workspace, parts=parts)}
        except (OSError, RuntimeError, ValueError, TypeError):
            observations = {'policy': '', 'context': '', 'source': ''}
    for group in state.finding_groups:
        memory = _memory(runner, group)
        if not memory.get('completion') and group.get('status') != 'needs_revalidation':
            if group.get('status') != 'completed':
                continue
            from .repair_planning import finding_key
            fresh = getattr(runner, '_fresh_component_completions', {}).get(group['group_id'])
            if (fresh and fresh['source'] == snapshot(workspace)
                    and fresh['environment'] == digest(runner._full_suite_environment_fingerprint())
                    and all(fresh['findings'].get(f.finding_id) == finding_key(f)
                            and f.finding_id not in fresh['resolved'] for f in state.blocking_findings())):
                continue  # Fresh gates stand within this process; no cross-job receipt is inferred.
        try:
            decision = assess(runner, workspace, group, observations=observations) if runner._acceleration_enabled() else {
                'state': 'needs_revalidation', 'reason': 'acceleration disabled', 'reusable_checks': []}
        except (OSError, ValueError, TypeError, RuntimeError):
            decision = {'state': 'needs_revalidation', 'reason': 'completion dependencies could not be verified', 'reusable_checks': []}
        if memory.get('completion_assessment') != decision:
            memory['completion_assessment'] = decision
            changed = True
        if group.get('status') != decision['state']:
            group['status'] = decision['state']
            changed = True
    for _ in state.finding_groups:
        completed_ids = {g['group_id'] for g in state.finding_groups if g.get('status') == 'completed'}
        invalidated = False
        for group in state.finding_groups:
            if group.get('status') == 'completed' and not set(group.get('depends_on', [])).issubset(completed_ids):
                group['status'] = 'needs_revalidation'
                _memory(runner, group)['completion_assessment'] = {
                    'state': 'needs_revalidation', 'reason': 'prerequisite component needs revalidation',
                    'reusable_checks': []}
                invalidated = changed = True
        if not invalidated:
            break
    if observations and observations['source'] and snapshot(workspace) != observations['source']:
        for group in state.finding_groups:
            if _memory(runner, group).get('completion'):
                group['status'] = 'needs_revalidation'
                _memory(runner, group)['completion_assessment'] = {
                    'state': 'needs_revalidation', 'reason': 'source changed while checking completion', 'reusable_checks': []}
        changed = True
    # A component receipt can never substitute for a final integrated delivery.
    if state.finding_groups and all(g.get('status') == 'completed' for g in state.finding_groups):
        final = state.finding_groups[-1]
        final['status'] = 'needs_revalidation'
        _memory(runner, final)['completion_assessment'] = {
            'state': 'needs_revalidation', 'reason': 'final integration remains required', 'reusable_checks': []}
        changed = True
    completed = [g for g in state.finding_groups if g.get('status') == 'completed']
    obligations = sorted({o for g in completed for o in g.get('contract_obligation_ids', [])})
    findings = sorted({f for g in completed for f in g.get('finding_ids', [])})
    if state.completed_contract_obligation_ids != obligations or state.completed_finding_ids != findings:
        state.completed_contract_obligation_ids, state.completed_finding_ids = obligations, findings
        changed = True
    if changed:
        runner._experiment_store.save(state)
    callback = getattr(runner, '_control_phase_callback', None)
    if callback:
        for group in state.finding_groups:
            old_status, old_assessment = previous[group['group_id']]
            decision = _memory(runner, group).get('completion_assessment')
            if decision and (old_assessment != decision or old_status != group.get('status')):
                callback('component_completion_checked', {'component': group['group_id'], **decision,
                    'previous_state': old_status,
                    'restored_completion': old_status != 'completed' and group.get('status') == 'completed'})


def retained_checks(runner, workspace, group, commands):
    """An affected component can keep independent whole command cohorts."""
    from .repair_schedule import pytest_parts
    if not all(pytest_parts(command) for command in commands):
        return []  # Do not move retained commands across shell preparation barriers.
    canonical = next((g for g in runner._experiment.finding_groups if g['group_id'] == group['group_id']), group)
    try:
        decision = assess(runner, workspace, canonical)
    except (OSError, RuntimeError, ValueError, TypeError):
        return []
    if not runner._acceleration_enabled() or getattr(runner, '_verification_fresh', False):
        return []
    return [command for command in commands if command in decision['reusable_checks']]


def combine_retained(runner, group, retained, result):
    """Keep original cohorts and record historical proof rather than a fake run."""
    if not retained:
        return result
    canonical = next((g for g in runner._experiment.finding_groups if g['group_id'] == group['group_id']), group)
    reference = _memory(runner, canonical)['completion']
    proof = read_record(runner, reference)
    nodes = [node for check in proof['checks'] if check['command'] in retained for node in check['executed_tests']]
    result.commands = (*retained, *result.commands)
    result.returncodes = (*(0 for _ in retained), *result.returncodes)
    result.termination_reasons = (*('' for _ in retained), *result.termination_reasons)
    result.payload['source_commands'] = [*retained, *result.payload.get('source_commands', [])]
    result.payload['executed_tests'] = list(dict.fromkeys([*nodes, *result.payload.get('executed_tests', [])]))
    result.payload['retained_completion'] = reference
    result.payload['retained_commands'] = retained
    result.payload['completion_inputs'] = [
        *[check['inputs'] for check in proof['checks'] if check['command'] in retained and check.get('inputs')],
        *result.payload.get('completion_inputs', [])]
    result.payload['command_timings'] = [
        *[{'command': c, 'seconds': 0, 'cache_hit': True, 'origin': 'component_completion', 'receipt': reference['id']}
          for c in retained], *result.payload.get('command_timings', [])]
    result.summary = 'Reused unchanged completed command cohorts: ' + str(len(retained)) + '\n' + result.summary
    runner._candidate_verified_check_ids = set(getattr(runner, '_candidate_verified_check_ids', set())) | set(nodes)
    return result


def execute_checks(runner, workspace, commands, *, parallel=False):
    from .self_repair import _VerificationResult
    group = runner._candidate_group
    retained = (retained_checks(runner, workspace, group, commands)
                if not getattr(runner, '_candidate_is_final_group', True) else [])
    environment = runner._full_suite_environment_fingerprint() if retained else None
    pending = [command for command in commands if command not in retained]
    result = (runner._guarded_component_checks(pending, workspace, parallel=parallel) if pending
              else _VerificationResult(True, 'all selected checks retained', payload={'source_commands': []}))
    result = combine_retained(runner, group, retained, result)
    if retained:
        if runner._full_suite_environment_fingerprint() != environment:
            result.ok = False
            result.payload['outcome'] = 'invalid'
            result.summary += '\nretained completion environment changed; revalidation required'
        callback = getattr(runner, '_control_phase_callback', None)
        if callback:
            callback('component_checks_reused', {'candidate_id': runner._candidate_id,
                'component': group['group_id'], 'commands': retained, 'receipt': result.payload['retained_completion']})
    return result
