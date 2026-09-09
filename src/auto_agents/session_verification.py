"""Durable session proof scope and candidate ownership.

The binding is captured before agent work. A resumed legacy child recovers its
contract from its recorded revision, never from a different pending run.
"""
from __future__ import annotations

import hashlib
import ast
import json
import shlex
import subprocess
from pathlib import Path
from copy import deepcopy

from .config import config_path, requirements_trace_path, run_state_path, task_plan_path
from .execution_binding import command_spans, executable_tokens, route_sources
from .git_ops import head_ref
from .io_utils import read_json


_PYTEST_CONFIG_NAMES = ('pytest.toml', '.pytest.toml', 'pytest.ini', '.pytest.ini',
                        'pyproject.toml', 'tox.ini', 'setup.cfg')


class SessionOwnershipError(RuntimeError):
    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


def ownership_error(state, message, **details):
    binding = state.verification_binding
    return SessionOwnershipError(message, diagnostic={
        'session_id': state.session_id, 'workflow_id': state.workflow_id,
        'handoff_id': binding.get('original_handoff_id', state.parent_handoff_id),
        'contract_fingerprint': binding.get('contract_fingerprint', ''),
        'task_scope': binding.get('task_scope', {}), 'retry_fix': False, **details,
    })


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def engine_verification_refs(command: str, project_root: Path, payload: dict) -> list[str]:
    """Find retained test references owned by the explicitly repaired engine."""
    roots = {(project_root / Path(str(source['target_repository'])).expanduser()).resolve()
             for source in route_sources(payload) if source.get('target_repository')}
    roots.discard(project_root.resolve())
    refs = []
    try:
        for start, end in command_spans(command):
            args = executable_tokens(command[start:end])
            if not args or not (Path(args[0]).name.startswith('python')
                                or Path(args[0]).name in {'pytest', 'py.test'}):
                continue
            for arg in args[1:]:
                path = Path(arg.split('::', 1)[0])
                if (path.is_absolute() and path.suffix == '.py'
                        and any(root in path.resolve().parents for root in roots)
                        and arg not in refs):
                    refs.append(arg)
    except ValueError:
        # Unknown shell syntax still goes through ordinary execution preflight.
        return []
    return refs


def bind_session(session, state) -> None:
    # Upgrades and initial sealing are transactional: a rejected authority must
    # not leave a partially rewritten inventory in the durable session.
    from .session_source import resolve_source
    had_source = hasattr(session, "_retained_source_root")
    previous_source = getattr(session, '_retained_source_root', session.project_root)
    control_root = getattr(session, '_custody_control_root', session.project_root)
    session._retained_source_root = resolve_source(control_root, state)
    original = state.verification_binding
    state.verification_binding = deepcopy(original)
    try:
        _bind_session(session, state)
    except Exception:
        state.verification_binding = original
        raise
    finally:
        if had_source:
            session._retained_source_root = previous_source
        else:
            del session._retained_source_root


def _bind_session(session, state) -> None:
    if state.verification_binding:
        validate_binding(session, state)
        scope = _task_scope(session, state)
        if 'task_scope' not in state.verification_binding:
            state.verification_binding['task_scope'] = scope
        elif any(scope.values()) and scope != state.verification_binding['task_scope']:
            raise ownership_error(state, 'retained task authority conflicts with session evidence')
        if state.verification_binding.get('schema_version', 1) < 12:
            _recover_retained_plan(session, state)
            _seal_inventory(session, state)
        if state.verification_binding.get('schema_version', 1) < 13:
            _seal_authority(session, state)
            session._save(state)
        return
    root = session.project_root
    source_root = getattr(session, "_retained_source_root", root)
    task_scope = _task_scope(session, state)
    gates = session.config.gates.to_dict()
    tasks = read_json(task_plan_path(root), default={})
    run = read_json(run_state_path(root), default={})
    revision = state.baseline_git_ref or state.baseline_head_ref or state.lineage_head_ref
    if state.source_descriptor:
        revision = state.source_descriptor['contract_revision']
    resumed = getattr(session, '_resumed_verification_state', None)
    if resumed is not None and resumed.session_id == state.session_id:
        # Workflow migration may populate lineage with today's HEAD. That is
        # not evidence of the contract authorized by a legacy session.
        revision = resumed.baseline_git_ref or resumed.baseline_head_ref or resumed.lineage_head_ref
        if not revision and head_ref(source_root):
            raise ownership_error(state, 'session verification contract revision is unavailable')
    if state.parent_handoff_id and not revision:
        raise ownership_error(state, 'session verification contract revision is unavailable')
    if (not state.parent_handoff_id and not state.baseline_head_ref
            and not state.lineage_head_ref and not head_ref(source_root)):
        # An unborn legacy standalone repository has no historical contract
        # to recover. Preserve its normal first-baseline capture behavior.
        revision = ""
    if revision and (resumed is not None or state.parent_handoff_id or state.baseline_git_ref or state.baseline_head_ref
                     or state.current_attempt or revision != head_ref(source_root)):
        # Baseline snapshot refs are disposable. Recover from the recorded
        # source HEAD (or workflow lineage) if the snapshot has disappeared,
        # before _ensure_baseline refreshes the comparison snapshot. Legacy
        # standalone resumes acquire their lineage during workflow migration;
        # routed children must retain their own recorded contract history.
        for candidate in dict.fromkeys(filter(None, (
            state.source_descriptor.get("contract_revision", ""),
            (resumed or state).baseline_git_ref, (resumed or state).baseline_head_ref,
            (resumed or state).lineage_head_ref,
        ))):
            probe = subprocess.run(
                ["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"],
                cwd=source_root, capture_output=True, text=True,
            )
            if probe.returncode == 0:
                revision = probe.stdout.strip()
                break
        else:
            raise ownership_error(state, "session verification contract revision is unavailable")
        payloads = []
        for path in (config_path(root), task_plan_path(root), run_state_path(root)):
            result = subprocess.run(
                ["git", "show", f"{revision}:{path.relative_to(root).as_posix()}"],
                cwd=source_root, capture_output=True, text=True,
            )
            if result.returncode and path == task_plan_path(root) and state.parent_handoff_id:
                raise ownership_error(state, 'retained task plan ownership is unavailable',
                                      task_scope=task_scope, contract_revision=revision)
            if result.returncode and path != config_path(root):
                payloads.append({})
                continue
            if result.returncode:
                raise ownership_error(state, "session verification contract revision is unavailable")
            payloads.append(json.loads(result.stdout))
        gates = payloads[0]["gates"]
        tasks = payloads[1]
        run = payloads[2]
    plan_workflow = run.get('resume_context', {}).get('workflow_id', run.get('workflow_id', ''))
    state.verification_binding = {
        "schema_version": 1,
        "session_id": state.session_id,
        "workflow_id": state.workflow_id,
        "authorization": deepcopy(state.authorization_policy),
        "gates": gates,
        "tasks": tasks.get("tasks", []),
        "plan": deepcopy(tasks),
        "task_scope": task_scope,
        "plan_workflow_id": plan_workflow,
        "contract_revision": revision or head_ref(source_root),
        "contract_fingerprint": fingerprint([gates, tasks, task_scope, plan_workflow]),
        "plan_fingerprint": fingerprint(tasks),
    }
    _seal_inventory(session, state)
    _seal_authority(session, state)
    session._save(state)


def _seal_authority(session, state):
    binding = state.verification_binding
    binding.update({
        'schema_version': 13,
        'execution_environment': deepcopy(state.goal_execution_environment),
        'session_mode': state.mode,
        'source_provenance': {
            'repository': binding['repository'],
            'revision': binding['contract_revision'],
            'contract_fingerprint': binding['contract_fingerprint'],
            'plan_fingerprint': binding['plan_fingerprint'],
        },
    })
    binding['binding_fingerprint'] = fingerprint({key: value for key, value in binding.items()
                                                 if key != 'binding_fingerprint'})


def _recover_retained_plan(session, state):
    """Upgrade an old receipt from its historical revision, never ambient state."""
    binding = state.verification_binding
    if 'plan' in binding:
        return
    revision = binding.get('contract_revision')
    if not revision:
        raise ownership_error(state, 'retained plan history is unavailable for binding upgrade')
    path = task_plan_path(session.project_root).relative_to(session.project_root).as_posix()
    result = subprocess.run(['git', 'show', f'{revision}:{path}'],
                            cwd=getattr(session, "_retained_source_root", session.project_root), capture_output=True, text=True)
    if result.returncode:
        raise ownership_error(state, 'retained plan history is unavailable for binding upgrade')
    plan = json.loads(result.stdout)
    if plan.get('tasks', []) != binding.get('tasks', []):
        raise ownership_error(state, 'retained plan ownership conflicts with binding')
    binding['plan'] = plan
    binding['plan_fingerprint'] = fingerprint(plan)


def _complete_gates(state):
    """Union generated configuration with its original, unabridged plan."""
    from .models import VerificationStep
    binding = state.verification_binding
    gates = deepcopy(binding['gates'])
    indexed = {}
    for raw in [*gates.get('steps', []), *binding.get('plan', {}).get('verification_steps', [])]:
        step = VerificationStep.from_dict(raw).to_dict()
        key = step.get('proof_id') or 'session.legacy.' + fingerprint(step)[:16]
        step['proof_id'] = key
        if key in indexed:
            existing = indexed[key]
            # The same proof id cannot identify two different invocations.
            for field in ('runner', 'command', 'targets', 'args', 'cwd'):
                if existing.get(field) != step.get(field):
                    raise ownership_error(state, f'retained proof {key} has conflicting {field}', proof_id=key)
            for field in ('depends_on_proofs', 'levels', 'impact_paths'):
                existing[field] = list(dict.fromkeys([*existing.get(field, []), *step.get(field, [])]))
        else:
            indexed[key] = step
    gates['steps'] = list(indexed.values())
    return gates


def _step_affected(session, state, step):
    from .verification_selection import StaticDependencyIndex, _matches
    changed = set(state.candidate_paths) | set(state.lineage_changed_paths)
    if not changed:
        return False
    dependencies = StaticDependencyIndex(session.project_root).closure_for_targets(step.get('targets', []))
    return any(path in dependencies or any(_matches(path, pattern) for pattern in step.get('impact_paths', []))
               for path in changed)


def _future_foreign_step(session, state, step, excluded):
    """Recognize only absent Python selectors exclusively owned by future work.

    Existing or unclassifiable entries remain prerequisites. This observation
    uses the retained revision, so candidate edits cannot change projection.
    """
    targets = step.get('targets', [])
    if step.get('command') or step.get('args') or step.get('runner', 'pytest') != 'pytest':
        return False  # Declared targets alone cannot establish command coverage.
    if not targets or (step.get('proof_id') not in excluded
                       and any(target not in excluded for target in targets)):
        return False
    if _step_affected(session, state, step):
        return False
    revision = state.verification_binding.get('contract_revision')
    if not revision:
        return False
    for target in targets:
        parts = target.split('::')
        if len(parts) > 2 or not parts[0].endswith('.py'):
            return False
        result = subprocess.run(['git', 'show', f'{revision}:{parts[0]}'],
                                cwd=getattr(session, "_retained_source_root", session.project_root), capture_output=True, text=True)
        if result.returncode:
            continue
        if len(parts) == 1:
            return False  # Existing whole-file coverage is regression evidence.
        try:
            tree = ast.parse(result.stdout)
        except SyntaxError:
            return False
        # Parameter IDs are collection-time identities, not Python function
        # names. A retained function is regression evidence even when static
        # inspection cannot establish whether the requested parameter exists.
        function_name = parts[1].split('[', 1)[0]
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
               for node in ast.walk(tree)):
            return False
    return True


def validate_binding(session, state):
    binding = state.verification_binding
    for key, expected in (
        ('session_id', state.session_id), ('workflow_id', state.workflow_id),
        ('authorization', state.authorization_policy),
    ):
        if binding.get(key) != expected:
            raise ownership_error(state, f'session verification binding has conflicting {key}')
    # Check legacy identity before inventory migration can replace it.
    if ('original_handoff_id' in binding
            and binding['original_handoff_id'] != state.parent_handoff_id):
        raise ownership_error(state, 'session verification binding has conflicting handoff identity',
                              resumed_handoff_id=state.parent_handoff_id)
    if binding.get('schema_version', 1) >= 2:
        from .execution_binding import session_execution_error
        error = session_execution_error(session, state)
        if error:
            raise ownership_error(state, error)
        if binding.get('binding_fingerprint') != fingerprint({
            key: value for key, value in binding.items() if key != 'binding_fingerprint'
        }):
            raise ownership_error(state, 'session verification binding inventory changed')
        if binding.get('fix_verify_command') != state.fix_verify_command:
            raise ownership_error(state, 'explicit fix verification changed since binding')
    if binding.get('schema_version', 1) >= 13:
        for key, expected in (('execution_environment', state.goal_execution_environment),
                              ('session_mode', state.mode)):
            if binding.get(key) != expected:
                raise ownership_error(state, f'session verification binding has conflicting {key}')
    _validate_task_authority(state)


def _validate_task_authority(state):
    binding = state.verification_binding
    scope = binding.get('task_scope', {})
    task_ids = set(scope.get('task_ids', []))
    requirement_ids = set(scope.get('requirement_ids', []))
    if task_ids and requirement_ids:
        # These are two representations of the child's authority, not
        # independent grants. Resolve against retained tasks before taking
        # any union of their proof obligations, even within one workflow.
        requirement_task_ids = {task.get('task_id') for task in binding.get('tasks', [])
                                if requirement_ids.intersection(task.get('requirement_ids', []))}
        if requirement_task_ids != task_ids:
            raise ownership_error(state, 'task and requirement authority conflict in retained contract',
                                  retained_task_ids=sorted(task_ids),
                                  requirement_task_ids=sorted(requirement_task_ids, key=str),
                                  requirement_ids=sorted(requirement_ids))
    if (not any(scope.values()) and binding.get('plan_workflow_id')
            and binding['plan_workflow_id'] != state.workflow_id):
        raise ownership_error(state, 'retained plan belongs to another workflow without matching task authority')
    for task in binding.get('tasks', []):
        explicit = (task.get('task_id') in scope.get('task_ids', [])
                    or set(scope.get('requirement_ids', [])).intersection(task.get('requirement_ids', [])))
        owner = task.get('workflow_id') or binding.get('plan_workflow_id')
        if explicit and task.get('workflow_id') and owner != state.workflow_id:
            raise ownership_error(state, 'task authority conflicts with retained workflow owner',
                                  conflicting_task_id=task.get('task_id'), task_workflow_id=owner)


def _task_refs(task):
    refs = set(task.get('verification_refs', []))
    for proof in task.get('requirement_proofs', []):
        refs.update(proof.get('evidence_refs', []))
    return refs


def _ref_covered(ref, step):
    if ref.startswith('cmd:'):
        from .gates import command_from_verification_step
        return ref[4:].strip() == (step.command.strip() or command_from_verification_step(step))
    if ref == step.proof_id:
        return True
    for target in _effective_targets(step):
        if ref == target or ref.startswith(target.rstrip('/') + '::'):
            return True
        if '::' not in target and not Path(target).suffix and ref.startswith(target.rstrip('/') + '/'):
            return True
    return False


def _effective_targets(step):
    # Match the runner compiler and directory expansion, including their
    # implicit pytest target. An empty declaration still executes tests.
    targets = [target.strip() for target in step.targets if target.strip()]
    if (not targets and step.runner.strip().lower() == 'pytest'
            and (step.kind.strip().lower() or 'test') == 'test'):
        return ['tests']
    return targets


def _command_covers(command, ref):
    if ref.startswith('cmd:'):
        return command.strip() == ref[4:].strip()
    try:
        from .models import VerificationStep
        from .orchestrator import Orchestrator
        for start, end in command_spans(command):
            args = executable_tokens(command[start:end])
            if ref in args:
                return True
            targets = Orchestrator._pytest_targets_from_command(shlex.join(args))
            if targets and _ref_covered(ref, VerificationStep(targets=targets)):
                # This establishes ownership, not selection evidence. The
                # mandatory node guard validates actual selection separately.
                return True
        return False
    except ValueError:
        return False


def _legacy_commands(gates):
    return list(dict.fromkeys([
        *(gates.commands if not gates.steps else []),
        *(command for group in gates.parallel_groups if not group.name.startswith('steps-')
          for command in group.commands),
    ]))


def _mandatory_refs(state):
    binding = state.verification_binding
    _validate_task_authority(state)
    scope = binding.get('task_scope', {})
    task_ids, requirement_ids = set(scope.get('task_ids', [])), set(scope.get('requirement_ids', []))
    tasks = binding.get('tasks', [])
    missing = task_ids - {task.get('task_id') for task in tasks}
    if missing:
        raise ownership_error(state, 'owned tasks are absent from retained contract history', missing_task_ids=sorted(missing))
    missing_requirements = requirement_ids - {key for task in tasks for key in task.get('requirement_ids', [])}
    if missing_requirements:
        raise ownership_error(state, 'owned requirements are absent from retained contract history',
                              missing_requirement_ids=sorted(missing_requirements))
    if state.parent_handoff_id and not (task_ids or requirement_ids) and not any(
        not (task.get('workflow_id') or binding.get('plan_workflow_id'))
        or (task.get('workflow_id') or binding.get('plan_workflow_id')) == state.workflow_id
        for task in tasks
    ):
        raise ownership_error(state, 'retained child contract ownership is unresolved')
    refs = set()
    for task in tasks:
        task_refs = _task_refs(task)
        explicit = task.get('task_id') in task_ids or requirement_ids.intersection(task.get('requirement_ids', []))
        owner = task.get('workflow_id') or binding.get('plan_workflow_id')
        retained = not (task_ids or requirement_ids) and (not owner or owner == state.workflow_id)
        if explicit or retained:
            refs.update(task_refs)
        refs.update(ref for ref in task_refs if _command_covers(state.fix_verify_command, ref))
    return refs


def _owned_inventory(state, gates):
    """Task refs are mandatory; unlabelled gates remain impact regressions."""
    refs = _mandatory_refs(state)
    required = []
    owners = {}
    for ref in sorted(refs):
        matches = [step for step in gates.steps if _ref_covered(ref, step)]
        # Supporting artifacts are not executable references. They remain in
        # the retained task proof, whose runner must produce them.
        # Only recognizable artifact paths are supporting evidence. An opaque
        # identifier is a required proof, even when its definition is missing.
        artifact = (Path(ref).suffix in {
            '.md', '.json', '.jsonl', '.txt', '.log', '.xml', '.html', '.png', '.jpg', '.pdf', '.csv',
        } and not ref.startswith('cmd:') and '::' not in ref)
        executable = not artifact
        if (not matches and executable and not _command_covers(state.fix_verify_command, ref)
                and not any(_command_covers(command, ref) for command in _legacy_commands(gates))):
            raise ownership_error(state, f'required verification reference has no executable proof: {ref}',
                                  verification_ref=ref, owners=diagnostic_owners(state, ref))
        for step in matches:
            if not step.proof_id:
                raise ownership_error(state, f'required verification reference has no proof identity: {ref}')
            if step.proof_id not in required:
                required.append(step.proof_id)
            owners.setdefault(step.proof_id, [])
            for owner in diagnostic_owners(state, ref):
                if owner not in owners[step.proof_id]:
                    owners[step.proof_id].append(owner)
    # Explicit fix checks are also mandatory even if the task has no refs.
    for step in gates.steps:
        if any(_command_covers(state.fix_verify_command, target) for target in step.targets):
            if step.proof_id and step.proof_id not in required:
                required.append(step.proof_id)
    indexed = {step.proof_id: step for step in gates.steps}
    cursor = 0
    while cursor < len(required):
        key = required[cursor]
        cursor += 1
        for dependency in indexed[key].depends_on_proofs:
            if dependency not in indexed:
                raise ownership_error(state, f'proof {key} requires unavailable prerequisite {dependency}',
                                      proof_id=key, missing_prerequisite=dependency, owners=owners.get(key, []))
            if dependency not in required:
                required.append(dependency)
    return required, owners


def _seal_inventory(session, state):
    from .gates import command_from_verification_step

    binding = state.verification_binding
    binding.setdefault('plan_fingerprint', fingerprint(binding.get('tasks', [])))
    gates = session_gates(session, state)
    required, owners = _owned_inventory(state, gates)
    owned_refs = _mandatory_refs(state)
    required_commands = {command: [owner for ref in sorted(owned_refs) if _command_covers(command, ref)
                                   for owner in diagnostic_owners(state, ref)]
                         for command in _legacy_commands(gates)
                         if any(_command_covers(command, ref) for ref in owned_refs)}
    binding.update({
        'schema_version': 12, 'repository': str(session.project_root.resolve()),
        'original_handoff_id': state.parent_handoff_id,
        'required_proof_ids': required, 'proof_owners': owners,
        'required_commands': required_commands,
        'task_ids': sorted({owner['task_id'] for rows in [*owners.values(), *required_commands.values()]
                            for owner in rows}),
        'requirement_ids': sorted({key for rows in [*owners.values(), *required_commands.values()]
                                   for owner in rows for key in owner['requirement_ids']}),
        'required_proofs': [step.to_dict() for step in gates.steps if step.proof_id in required],
        'regression_dependencies': {step['proof_id']: list(step.get('depends_on_proofs', []))
                                    for step in _complete_gates(state)['steps']},
        'verification_policy': {key: value for key, value in binding['gates'].items()
                                if key not in {'steps', 'commands', 'parallel_groups'}},
        'baseline_identity': {'git_ref': state.baseline_git_ref, 'head_ref': state.baseline_head_ref,
                              'lineage_head_ref': state.lineage_head_ref},
        'fix_verify_command': state.fix_verify_command,
    })
    proof_sources = {}
    targets = {(target.split('::', 1)[0], ()) for step in gates.steps
               for target in _effective_targets(step)}
    # Command-only structured proofs, manual checks and explicit fix checks
    # execute the same pytest discovery rules as ordinary steps.
    config_controls = set()
    revision = binding.get('contract_revision') or 'HEAD'
    for command in [*(step.command or command_from_verification_step(step, session.project_root)
                      for step in gates.steps), *_legacy_commands(gates), state.fix_verify_command]:
        targets.update(_command_source_targets(command, session=session, revision=revision,
                                               config_paths=config_controls))
    controls = set()
    for target, patterns in sorted(targets):
        try:
            relative = (session.project_root / target).resolve().relative_to(session.project_root.resolve()).as_posix()
        except ValueError:
            continue
        # Pytest searches from its invocation/target ancestors. Preserve both
        # existing selection configuration and the absence of overrides.
        directory = Path(relative) if not Path(relative).suffix else Path(relative).parent
        config_controls.update((parent / name).as_posix()
                               for parent in (directory, *directory.parents)
                               for name in _PYTEST_CONFIG_NAMES)
        entries = subprocess.run(['git', 'ls-tree', '-r', '--name-only', revision, '--', relative],
                                 cwd=getattr(session, "_retained_source_root", session.project_root), capture_output=True, text=True)
        for path in entries.stdout.splitlines():
            # Implicit pytest discovery starts at cwd. Protect its retained
            # tests without turning ordinary implementation files into proofs.
            if patterns and not _pytest_source_name(path, patterns):
                continue
            source = _historical_source(session, revision, path)
            if source is not None:
                proof_sources[path] = source
            if path.endswith('.py'):
                controls.update((parent / 'conftest.py').as_posix() for parent in Path(path).parents)
    # A newly introduced ancestor hook can weaken an unchanged test too.
    # Seal absence as well as contents, using the historical contract source.
    config_controls = {path.relative_to(session.project_root.resolve()).as_posix()
                       for value in config_controls
                       for path in [(session.project_root / value).resolve()]
                       if path.is_relative_to(session.project_root.resolve())}
    controls.update(config_controls)
    for path in controls:
        proof_sources[path] = _historical_source(session, revision, path)
    binding['proof_control_paths'] = sorted(controls)
    binding['proof_config_paths'] = sorted(config_controls)
    binding['proof_sources'] = proof_sources
    _validate_required_node_selection(session, state, [
        *(step.command or command_from_verification_step(step, session.project_root) for step in gates.steps),
        *_legacy_commands(gates), state.fix_verify_command,
    ])
    binding['binding_fingerprint'] = fingerprint({key: value for key, value in binding.items()
                                                  if key != 'binding_fingerprint'})


def _validate_required_node_selection(session, state, commands):
    """Path containment and proof labels cannot attest a deselected node.

    Require an invocation without selection restrictions for every mandatory
    pytest node. Name/marker expressions depend on collection-time metadata;
    without that evidence they cannot establish coverage. Keep the retained
    command intact and report ambiguity instead of stripping its options.
    """
    from .orchestrator import Orchestrator

    refs = sorted(ref for ref in _mandatory_refs(state)
                  if not ref.startswith('cmd:') and '.py::' in ref)
    if not refs:
        return
    binding = state.verification_binding
    configurations = {}
    for path in binding.get('proof_config_paths', []):
        source = binding.get('proof_sources', {}).get(path)
        if source is None:
            continue
        try:
            configurations[path] = _pytest_config_options(path, source)
        except (ValueError, TypeError):
            raise ownership_error(state, 'retained pytest selection configuration is unreadable',
                                  verification_ref=path)
    covered = set()
    for command in commands:
        cwd = session.project_root
        try:
            for start, end in command_spans(command):
                raw = command[start:end]
                args = executable_tokens(raw)
                if not args:
                    continue
                if args[0] == 'cd' and len(args) == 2:
                    cwd = (cwd / args[1]).resolve()
                    continue
                if Path(args[0]).name in {'pytest', 'py.test'}:
                    options = args[1:]
                elif Path(args[0]).name.startswith('python') and args[1:3] == ['-m', 'pytest']:
                    options = args[3:]
                else:
                    continue
                # Inline addopts are runner selection inputs too, even though
                # executable_tokens removes environment assignment wrappers.
                env_args = _inline_pytest_addopts(raw, args)
                targets = Orchestrator._pytest_targets_from_command(shlex.join(args))
                settings = _pytest_selection_config(session.project_root, cwd, [*env_args, *options],
                                                    targets, configurations)
                addopts = settings.get('addopts', [])
                config_args = shlex.split(addopts) if isinstance(addopts, str) else list(addopts)
                for key in ('python_functions', 'python_classes', 'python_files', 'norecursedirs'):
                    if key in settings:
                        value = settings[key]
                        config_args.extend(['-o', key + '=' + (value if isinstance(value, str) else ' '.join(value))])
                for ref in refs:
                    path, _, node = ref.partition('::')
                    absolute = (session.project_root / path).resolve()
                    for target in targets or ['.']:
                        target_path, _, target_node = target.partition('::')
                        selected = (cwd / target_path).resolve()
                        contains = (absolute == selected and (not target_node or node == target_node
                                    or node.startswith(target_node + '::')))
                        contains |= (not target_node and not selected.suffix and selected in absolute.parents)
                        selection_args = [*config_args, *env_args, *options]
                        if (contains and not _pytest_selection_restricted(selection_args)
                                and not _pytest_discovery_excludes(selection_args, ref,
                                                                 directory=absolute != selected,
                                                                 collection_root=selected,
                                                                 source_path=absolute)):
                            covered.add(ref)
        except ValueError:
            continue  # Unparseable commands cannot attest required nodes.
    for ref in refs:
        if ref not in covered:
            raise ownership_error(state, 'required pytest node lacks unfiltered executable evidence: ' + ref,
                                  verification_ref=ref, owners=diagnostic_owners(state, ref),
                                  commands=[command for command in commands if command])


def _pytest_selection_config(root, cwd, args, targets, configurations):
    """Resolve retained discovery settings for this invocation only.

    Another command's configuration must never override this command's node
    exclusions. Explicit configuration takes precedence over ancestor search.
    """
    from os.path import commonpath

    explicit = ''
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            break
        if arg in {'-c', '--config-file'} and index + 1 < len(args):
            index += 1
            explicit = args[index]
        elif arg.startswith('--config-file='):
            explicit = arg.partition('=')[2]
        elif arg.startswith('-c') and len(arg) > 2:
            explicit = arg[2:]
        index += 1

    def retained(path):
        try:
            return configurations.get(path.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            return None

    if explicit:
        return retained(cwd / explicit) or {}
    directories = [(cwd / target.split('::', 1)[0]).resolve() for target in targets]
    directories = [path.parent if path.suffix else path for path in directories]
    base = Path(commonpath(directories)) if directories else cwd
    for start in dict.fromkeys([base, cwd]):
        for parent in (start, *start.parents):
            for name in _PYTEST_CONFIG_NAMES:
                settings = retained(parent / name)
                if settings is not None:
                    return settings
    return {}


def _pytest_discovery_excludes(args, ref, *, directory, collection_root, source_path):
    """A containing path cannot cover names excluded by pytest discovery.

    Respect override precedence and pytest's prefix-or-glob name matching.
    File patterns apply to directory discovery, not explicit file arguments.
    """
    from fnmatch import fnmatch
    import os

    overrides = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            break
        value = ''
        if arg in {'-o', '--override-ini'} and index + 1 < len(args):
            index += 1
            value = args[index]
        elif arg.startswith('--override-ini='):
            value = arg.partition('=')[2]
        elif arg.startswith('-o'):
            value = arg[2:]
        key, separator, pattern = value.partition('=')
        if separator:
            if key.strip() == 'addopts' and _pytest_discovery_excludes(
                    shlex.split(pattern), ref, directory=directory,
                    collection_root=collection_root, source_path=source_path):
                return True
            overrides[key.strip()] = shlex.split(pattern) if key.strip() == 'norecursedirs' else pattern.split()
        index += 1
    path, *nodes = ref.split('::')
    checks = [('python_functions', nodes[-1].split('[', 1)[0])]
    checks.extend(('python_classes', name) for name in nodes[:-1])
    for key, name in checks:
        if key in overrides and not any(name.startswith(pattern) or fnmatch(name, pattern)
                                        for pattern in overrides[key]):
            return True
    if directory:
        patterns = overrides.get('norecursedirs',
                                 ['*.egg', '.*', '_darcs', 'build', 'CVS', 'dist',
                                  'node_modules', 'venv', '{arch}'])
        # Pytest bypasses recursion exclusions for explicit initial paths.
        # Only directories traversed below this invocation's target matter.
        for parent in source_path.parents:
            if parent == collection_root:
                break
            for pattern in patterns:
                pattern = pattern.replace('/', os.sep)
                name = parent.name
                if os.sep in pattern:
                    name = str(parent)
                    if not os.path.isabs(pattern):
                        pattern = '*' + os.sep + pattern
                if fnmatch(name, pattern):
                    return True
    return bool(directory and 'python_files' in overrides
                and not any(fnmatch(Path(path).name, pattern) for pattern in overrides['python_files']))


def _pytest_selection_restricted(args):
    # Fail closed on selectors whose actual coverage requires collection data.
    # These remain valid CLI options; a bound child needs independent evidence
    # before using them to discharge an exact mandatory node obligation.
    for arg in args:
        if arg == '--':
            break
        option = arg.split('=', 1)[0]
        if (option in {'--deselect', '--ignore', '--ignore-glob', '--lf', '--last-failed',
                       '--collect-only', '--co', '--stepwise', '--sw'}
                or arg.startswith(('-k', '-m'))):
            return True
        if 'addopts=' in arg and _pytest_selection_restricted(shlex.split(arg.partition('addopts=')[2])):
            return True
    return False


def _historical_source(session, revision, path):
    result = subprocess.run(['git', 'show', f'{revision}:{path}'],
                            cwd=getattr(session, "_retained_source_root", session.project_root), capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _inline_pytest_addopts(raw, args):
    """Retain runner environment options stripped by executable_tokens.

    Only launcher assignments are environment inputs; a test argument that
    happens to contain the same text is not. The final assignment wins.
    """
    tokens = shlex.split(raw)
    value = ''
    for token in tokens[:len(tokens) - len(args)]:
        if token.startswith('PYTEST_ADDOPTS='):
            value = token.partition('=')[2]
    return shlex.split(value)


def _command_source_targets(command, *, session, revision, config_paths):
    """Inventory command inputs without running candidate configuration/hooks."""
    from .orchestrator import Orchestrator

    targets = set()
    cwd = Path('.')
    try:
        for start, end in command_spans(command):
            args = executable_tokens(command[start:end])
            if not args:
                continue
            if args[0] == 'cd' and len(args) == 2:
                cwd = cwd / args[1]
                continue
            pytest = Path(args[0]).name in {'pytest', 'py.test'} or (
                Path(args[0]).name.startswith('python') and args[1:3] == ['-m', 'pytest'])
            if pytest:
                # Pytest parses config addopts, environment addopts, then CLI
                # options. Preserve that order for discovery and explicit
                # targets without changing the retained execution command.
                prefix = 1 if Path(args[0]).name in {'pytest', 'py.test'} else 3
                args = [*args[:prefix], *_inline_pytest_addopts(command[start:end], args), *args[prefix:]]
                patterns = _pytest_discovery_patterns(session, revision, cwd, args, config_paths)
                paths = Orchestrator._pytest_targets_from_command(shlex.join(args))
                targets.update(((cwd / path.split('::', 1)[0]).as_posix(), ()) for path in paths)
                if not paths:
                    targets.add((cwd.as_posix(), patterns))
            else:
                targets.update(((cwd / arg.split('::', 1)[0]).as_posix(), ()) for arg in args
                               if arg.split('::', 1)[0].endswith(('.py', '.js', '.ts', '.tsx', '.jsx')))
    except ValueError:
        pass  # Unsupported commands still face execution preflight.
    return targets


def _pytest_config_options(path, source):
    import configparser
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    name = Path(path).name
    if name.endswith('.toml'):
        document = tomllib.loads(source)
        if name in {'pytest.toml', '.pytest.toml'}:
            return document.get('pytest', {})
        options = document.get('tool', {}).get('pytest', {})
        native = {key: value for key, value in options.items() if key != 'ini_options'}
        return native or options.get('ini_options')
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(source)
    section = 'tool:pytest' if name == 'setup.cfg' else 'pytest'
    if config.has_section(section):
        return dict(config[section])
    return {} if name in {'pytest.ini', '.pytest.ini'} else None


def _pytest_discovery_patterns(session, revision, cwd, args, config_paths):
    """Read discovery inputs from the retained revision, never candidate hooks.

    Implicit commands may use an arbitrary -c file or override python_files
    with -o. Their inputs belong to each invocation (including its cwd), not
    just to the repository's default pytest configuration.
    """
    import configparser

    explicit = ''
    overrides = []
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == '--':
            break
        if arg in {'-c', '--config-file', '-o', '--override-ini'} and index + 1 < len(args):
            index += 1
            value = args[index]
            if arg in {'-c', '--config-file'}:
                explicit = value
            else:
                overrides.append(value)
        elif arg.startswith('--config-file='):
            explicit = arg.split('=', 1)[1]
        elif arg.startswith('-c') and len(arg) > 2:
            explicit = arg[2:]
        elif arg.startswith('--override-ini='):
            overrides.append(arg.split('=', 1)[1])
        elif arg.startswith('-o') and len(arg) > 2:
            overrides.append(arg[2:])
        index += 1
    if explicit:
        candidates = [cwd / explicit]
    else:
        candidates = [parent / name for parent in (cwd, *cwd.parents)
                      for name in _PYTEST_CONFIG_NAMES]
    options = {}
    for candidate in candidates:
        try:
            path = (session.project_root / candidate).resolve().relative_to(session.project_root.resolve()).as_posix()
        except ValueError:
            continue
        config_paths.add(path)
        source = _historical_source(session, revision, path)
        if source is None:
            continue
        try:
            found = _pytest_config_options(path, source)
        except (ValueError, configparser.Error):
            # An invalid retained config cannot safely narrow the inventory;
            # ordinary runner preflight will diagnose the configuration.
            return ('*.py',)
        if found is not None:
            options = found
            break
    patterns = options.get('python_files', ['test_*.py', '*_test.py'])
    # Pytest prepends retained addopts before parsing the invocation. Discovery
    # overrides there must protect the same sources as direct CLI overrides;
    # later CLI values retain their normal precedence.
    addopts = options.get('addopts', [])
    addopts_args = shlex.split(addopts) if isinstance(addopts, str) else list(addopts)
    retained_overrides = []
    index = 0
    while index < len(addopts_args):
        arg = addopts_args[index]
        if arg == '--':
            break
        if arg in {'-o', '--override-ini'} and index + 1 < len(addopts_args):
            index += 1
            retained_overrides.append(addopts_args[index])
        elif arg.startswith('--override-ini='):
            retained_overrides.append(arg.split('=', 1)[1])
        elif arg.startswith('-o') and len(arg) > 2:
            retained_overrides.append(arg[2:])
        index += 1
    for override in [*retained_overrides, *overrides]:
        key, separator, value = override.partition('=')
        if separator and key.strip() == 'python_files':
            patterns = value
    return tuple(shlex.split(patterns) if isinstance(patterns, str) else patterns)


def _pytest_source_name(path, patterns):
    from fnmatch import fnmatch
    return Path(path).name == 'conftest.py' or any(fnmatch(Path(path).name, pattern) for pattern in patterns)


def validate_plan(state, gates, plan):
    from .gates import GateCommandMetadata

    # Legacy commands and manual groups are part of the same mandatory union.
    # Give coalesced commands durable proof labels without changing shell text.
    for command in state.verification_binding.get('required_commands', {}):
        if command not in plan.commands and not any(command in group.commands for group in plan.parallel_groups):
            plan.commands.append(command)
        key = 'session.command.' + fingerprint(command)[:16]
        metadata = plan.metadata.setdefault(command, GateCommandMetadata())
        if isinstance(metadata, dict):
            metadata = plan.metadata[command] = GateCommandMetadata(**metadata)
        if key not in metadata.proof_ids:
            metadata.proof_ids.append(key)
        if key not in plan.proof_ids:
            plan.proof_ids.append(key)
    required = set(state.verification_binding.get('required_proof_ids', []))
    expected = {step['proof_id']: step for step in state.verification_binding.get('required_proofs', [])}
    actual = {step.proof_id: step.to_dict() for step in gates.steps}
    for key in required:
        if actual.get(key) != expected.get(key):
            raise ownership_error(state, f'required proof {key} changed since binding', proof_id=key)
    commands = [*plan.commands, *(cmd for group in plan.parallel_groups for cmd in group.commands)]
    covered = {key for command in commands if command.strip()
               for key in getattr(plan.metadata.get(command), 'proof_ids', [])}
    missing = (required | set(plan.proof_ids)) - covered
    for step in gates.steps:
        if step.proof_id in covered:
            missing.update(set(step.depends_on_proofs) - covered)
    if missing:
        raise ownership_error(state, 'required proofs lost executable evidence: ' + ', '.join(sorted(missing)),
                              missing_proof_ids=sorted(missing))


def _task_scope(session, state):
    control_root = getattr(session, '_custody_control_root', session.project_root)
    seeds = [read_json(control_root / '.auto-agents/state/sessions' / state.session_id / 'issue.json', default={})]
    if state.parent_handoff_id:
        handoff = read_json(control_root / '.auto-agents/state/handoffs' / (state.parent_handoff_id + '.json'), default={})
        child = handoff.get('child', {}) or {}
        child_id = child.get('native_id') or handoff.get('payload', {}).get('child_session_id')
        if (not handoff or (child_id and child_id != state.session_id)
                or (handoff.get('workflow_id') and handoff['workflow_id'] != state.workflow_id)
                or (handoff.get('handoff_id') and handoff['handoff_id'] != state.parent_handoff_id)):
            raise ownership_error(state, 'original child handoff ownership is unavailable or conflicting')
        for key, expected in (('authorization_policy', state.authorization_policy),
                              ('goal_execution_environment', state.goal_execution_environment)):
            if key in handoff.get('payload', {}) and handoff['payload'][key] != expected:
                raise ownership_error(state, f'original child handoff has conflicting {key}')
        seeds.append(handoff.get('payload', {}))
    task_ids, requirement_ids = set(), set()
    for seed in seeds:
        seed_tasks, seed_requirements = set(), set()
        for source in route_sources(seed):
            source_tasks = set(source.get('task_ids', []))
            if source.get('task_id'):
                source_tasks.add(source['task_id'])
            source_requirements = set(source.get('requirement_ids', []))
            for existing, incoming, field in ((seed_tasks, source_tasks, 'task_ids'),
                                               (seed_requirements, source_requirements, 'requirement_ids')):
                if existing and incoming and existing != incoming:
                    raise ownership_error(state, f'conflicting {field} in session authority evidence')
                existing.update(incoming)
        for existing, incoming, field in ((task_ids, seed_tasks, 'task_ids'),
                                           (requirement_ids, seed_requirements, 'requirement_ids')):
            if existing and incoming and existing != incoming:
                raise ownership_error(state, f'conflicting issue and handoff {field}',
                                      retained_scope=sorted(existing), conflicting_scope=sorted(incoming))
            existing.update(incoming)
    return {'task_ids': sorted(task_ids), 'requirement_ids': sorted(requirement_ids)}


def _foreign_refs(state):
    binding = state.verification_binding
    task_ids = set(binding.get('task_scope', {}).get('task_ids', []))
    requirement_ids = set(binding.get('task_scope', {}).get('requirement_ids', []))
    owned_refs, foreign_refs = set(), set()
    for task in binding.get('tasks', []):
        refs = _task_refs(task)
        owned = (task.get('task_id') in task_ids
                 or requirement_ids.intersection(task.get('requirement_ids', [])))
        unresolved = task.get('status') in {'pending', 'in_progress', 'blocked', 'failed'}
        owner = task.get('workflow_id') or binding.get('plan_workflow_id')
        foreign = bool(owner and owner != state.workflow_id)
        (foreign_refs if unresolved and foreign and not owned else owned_refs).update(refs)
    # An explicit targeted regression remains required, even for an unfinished
    # task. Missing entries must still produce a preflight failure.
    return {ref for ref in foreign_refs - owned_refs if not _command_covers(state.fix_verify_command, ref)}


def session_gates(session, state):
    """Project proven foreign pending obligations out of a retained copy only."""
    from .models import GateConfig, VerificationStep

    binding = state.verification_binding
    gates = _complete_gates(state)
    excluded = _foreign_refs(state)
    # An owned proof's prerequisite is acceptance evidence, even when another
    # workflow owns it. Default-target regressions have dependencies too.
    refs = _mandatory_refs(state)
    def keep_command(command):
        from .orchestrator import Orchestrator

        # Containment proves coverage, not exclusive ownership. A file or
        # directory command still runs existing regressions when a foreign
        # workflow has planned an additional node inside it. Remove only a
        # command consisting entirely of proven absent foreign selectors.
        if any(_command_covers(command, ref) for ref in refs):
            return True
        foreign_targets = excluded | {target for step in gates.get('steps', [])
                                      if step.get('proof_id') in excluded
                                      for target in step.get('targets', [])}
        seen = False
        try:
            for start, end in command_spans(command):
                args = executable_tokens(command[start:end])
                targets = Orchestrator._pytest_targets_from_command(shlex.join(args))
                if not targets or not _future_foreign_step(session, state, {'targets': targets}, foreign_targets):
                    return True
                seen = True
        except ValueError:
            return True  # Unknown commands require execution evidence.
        return not seen

    if not gates.get('steps'):
        gates['commands'] = [command for command in gates.get('commands', []) if keep_command(command)]
    for group in gates.get('parallel_groups', []):
        if not group.get('name', '').startswith('steps-'):
            group['commands'] = [command for command in group.get('commands', []) if keep_command(command)]
    indexed = {}
    for step in gates.get('steps', []):
        if not step.get('proof_id'):
            step['proof_id'] = 'session.legacy.' + fingerprint(step)[:16]
        indexed[step['proof_id']] = step
    protected = {key for key, step in indexed.items()
                 if any(_ref_covered(ref, VerificationStep.from_dict(step)) for ref in refs)
                 or _step_affected(session, state, step)}
    # Existing regression prerequisites remain evidence regardless of task
    # ownership. Only a demonstrably unimplemented foreign selector can be a
    # future ordering edge, and never on a mandatory owned proof.
    for key, step in indexed.items():
        if not _future_foreign_step(session, state, step, excluded):
            for dependency in step.get('depends_on_proofs', []):
                if dependency in indexed and not _future_foreign_step(session, state, indexed[dependency], excluded):
                    protected.add(dependency)
    pending = list(protected)
    while pending:
        key = pending.pop()
        for dependency in indexed[key].get('depends_on_proofs', []):
            if dependency in indexed and dependency not in protected:
                protected.add(dependency)
                pending.append(dependency)
    removed = set()
    selected = []
    for step in gates.get('steps', []):
        if (step['proof_id'] not in protected
                and _future_foreign_step(session, state, step, excluded)):
            removed.add(step['proof_id'])
            continue
        targets = step.get('targets', [])
        kept = [target for target in targets if step['proof_id'] in protected
                or not _future_foreign_step(session, state, {**step, 'targets': [target]}, excluded)]
        if targets and not kept:
            removed.add(step.get('proof_id'))
            continue
        step['targets'] = kept
        selected.append(step)
    gates['steps'] = selected
    if removed:
        for step in selected:
            if step['proof_id'] not in protected:
                step['depends_on_proofs'] = [ref for ref in step.get('depends_on_proofs', []) if ref not in removed]
        gates['fallback_proof_ids'] = [ref for ref in gates.get('fallback_proof_ids', []) if ref not in removed]
        if gates.get('unmapped_change_policy') == 'fallback' and not gates['fallback_proof_ids']:
            # Removing another workflow's fallback cannot turn an unmapped fix
            # into an empty successful check. Use the retained release
            # regressions, including their remaining prerequisite closure.
            gates['unmapped_change_policy'] = 'release'
        if not selected:
            # Do not resurrect excluded generated commands through the legacy
            # fallback when this plan contains only another run's future work.
            gates['commands'] = []
            gates['parallel_groups'] = [group for group in gates.get('parallel_groups', [])
                                       if not group.get('name', '').startswith('steps-')]
    return GateConfig.from_dict(gates)


def owned_paths(orchestrator, state) -> list[str]:
    if state.candidate_custody:
        from .session_candidate import validate_receipt
        validate_receipt(state)
        return sorted(state.candidate_paths)
    snapshot = candidate_snapshot(orchestrator)
    conflicts = [path for path, digest in state.candidate_paths.items()
                 if snapshot.get(path, "") != digest]
    if conflicts:
        raise ownership_error(state, "candidate ownership changed: " + ", ".join(sorted(conflicts)),
                              conflicting_paths=sorted(conflicts))
    return sorted(state.candidate_paths)


def candidate_snapshot(orchestrator) -> dict[str, str]:
    snapshot = orchestrator._worktree_change_snapshot()
    result = subprocess.run(['git', 'ls-files', '--stage', '-z'],
                            cwd=orchestrator.project_root, capture_output=True, text=True, check=True)
    index = {}
    for entry in result.stdout.split('\0'):
        if '\t' in entry:
            identity, path = entry.split('\t', 1)
            index.setdefault(path, []).append(identity)
    return {path: fingerprint([value, index.get(path, [])]) for path, value in snapshot.items()}


def product_path(path: str) -> bool:
    return (not path.startswith((".auto-agents/", ".antigravitycli/"))
            or path.startswith(".auto-agents/docs/provider_references/"))


def record_candidate(session, state, before: dict[str, str]) -> None:
    if state.candidate_custody:
        from .session_candidate import record_receipt
        record_receipt(session, state)
        return
    after = session.orch._worktree_change_snapshot()
    delta = session.orch._snapshot_delta_paths(before, after)
    protected = set(state.protected_preexisting_paths) | (set(before) - set(state.candidate_paths))
    attributed = getattr(session, '_candidate_attempt_paths', None)
    session._candidate_attempt_paths = None
    if attributed is None and any(product_path(path) for path in delta):
        raise SessionOwnershipError('candidate ownership is ambiguous without an isolated writer receipt')
    product = list(attributed or [])
    overlap = sorted(set(product) & protected)
    fingerprints = candidate_snapshot(session.orch)
    for path in product:
        if path not in protected:
            if path in after:
                state.candidate_paths[path] = fingerprints[path]
            else:
                state.candidate_paths.pop(path, None)
    session._save(state)
    if overlap:
        raise SessionOwnershipError("candidate overlaps preexisting work: " + ", ".join(overlap))


def collection_command(command: str) -> str:
    """Preserve shell/launcher arguments, adding collection only to pytest."""
    try:
        spans = command_spans(command)
        collected = []
        has_pytest = False
        for start, end in spans:
            raw = command[start:end].strip()
            args = executable_tokens(command[start:end])
            if not args:
                continue
            if args[0] == 'cd':
                collected.append(raw)
                continue
            is_pytest = Path(args[0]).name in {"pytest", "py.test"} or (
                len(args) > 2 and Path(args[0]).name.startswith("python") and args[1:3] == ["-m", "pytest"]
            )
            if not is_pytest:
                # A different runner or shell action does not waive pytest
                # entries elsewhere in the chain. Do not execute that action
                # during preflight; retain it in the original execution only.
                continue
            has_pytest = True
            # Place before the explicit end-of-options delimiter, if present.
            tokens = shlex.split(raw)
            if "--" in tokens:
                tokens.insert(tokens.index("--"), "--collect-only")
                collected.append(shlex.join(tokens))
            else:
                collected.append(raw + " --collect-only")
        if has_pytest:
            # Each referenced required entry must collect, even when the
            # execution command uses ';' or a conditional fallback. The
            # original command and its execution semantics remain unchanged.
            return ' && '.join(collected)
    except ValueError:
        pass
    return ""


def diagnostic_owners(state, command: str, *, proof_ids=()) -> list[dict[str, object]]:
    from .models import VerificationStep

    owners = []
    for key in proof_ids:
        for owner in state.verification_binding.get('proof_owners', {}).get(key, []):
            if owner not in owners:
                owners.append(owner)
    # Broader file/directory invocations still carry the node's owner. This
    # also supplies diagnostics for legacy commands without proof metadata.
    try:
        targets = [arg for start, end in command_spans(command)
                   for arg in executable_tokens(command[start:end]) if not arg.startswith('-')]
    except ValueError:
        targets = []
    evidence = VerificationStep(targets=targets)
    for task in state.verification_binding.get("tasks", []):
        refs = list(task.get("verification_refs", []))
        for proof in task.get("requirement_proofs", []):
            refs.extend(proof.get("evidence_refs", []))
        if any(str(ref) == command or _command_covers(command, str(ref))
               or (not str(ref).startswith('cmd:') and _ref_covered(str(ref), evidence)) for ref in refs):
            owner = {"task_id": task.get("task_id", task.get("id", "")),
                     "requirement_ids": task.get("requirement_ids", []),
                     "verification_refs": refs}
            if owner not in owners:
                owners.append(owner)
    return owners


def validate_selected_contracts(session, state, commands: list[str], *, metadata=None) -> None:
    """A frozen selector cannot attest a changed requirement contract."""
    from .requirements import requirement_contract_sha256
    from .models import VerificationStep

    _validate_required_node_selection(session, state, commands)
    for path, source in state.verification_binding.get('proof_sources', {}).items():
        if path not in state.candidate_paths:
            continue
        candidate = session.project_root / path
        try:
            current = candidate.read_text()
            preserved = current == source or (
                path in state.verification_binding.get('proof_config_paths', [])
                and _preserves_pytest_config(path, source, current)
            ) or (
                source is not None and path not in state.verification_binding.get('proof_control_paths', [])
                and path.endswith('.py') and _preserves_python_checks(ast.parse(source), ast.parse(current))
            )
        except (OSError, SyntaxError, UnicodeError):
            preserved = False
        if not preserved:
            proof_ids = [step['proof_id'] for step in state.verification_binding.get('required_proofs', [])
                         if _ref_covered(path, VerificationStep.from_dict(step))]
            raise ownership_error(state, f'required proof source was removed or changed: {path}',
                                  owners=(diagnostic_owners(state, path, proof_ids=proof_ids)
                                          or [owner for rows in state.verification_binding.get('proof_owners', {}).values()
                                              for owner in rows]
                                          or diagnostic_owners(state, state.fix_verify_command)
                                          or [owner for rows in state.verification_binding.get('required_commands', {}).values()
                                              for owner in rows]), verification_ref=path)

    # Mandatory ownership comes from the sealed inventory, never reconstructed
    # only from shell substrings. Expansion and command coalescing may remove
    # the literal node reference while retaining its full contract obligation.
    task_ids = set(state.verification_binding.get('task_ids', []))
    for command in commands:
        item = (metadata or {}).get(command)
        proof_ids = item.get('proof_ids', []) if isinstance(item, dict) else getattr(item, 'proof_ids', [])
        task_ids.update(owner['task_id'] for owner in diagnostic_owners(state, command, proof_ids=proof_ids))
    trace = read_json(requirements_trace_path(
        getattr(session, '_custody_control_root', session.project_root)), default={})
    requirements = {row.get('id'): row for row in trace.get('requirements', [])}
    for task in state.verification_binding.get('tasks', []):
        if task.get('task_id') not in task_ids:
            continue
        for proof in task.get('requirement_proofs', []):
            expected = proof.get('requirement_contract_sha256')
            if not expected:
                continue  # Legacy contracts predate proof hashes.
            requirement_id = proof.get('requirement_id')
            row = requirements.get(requirement_id)
            if row is None or requirement_contract_sha256(row) != expected:
                raise ownership_error(state,
                    f"session {state.session_id} task {task['task_id']} requirement "
                    f"{requirement_id} no longer matches its bound verification contract",
                    task_id=task['task_id'], requirement_id=requirement_id,
                    owners=[{'task_id': task['task_id'], 'requirement_ids': task.get('requirement_ids', []),
                             'verification_refs': sorted(_task_refs(task))}],
                )


def _preserves_pytest_config(path, before, after):
    """Allow unrelated build configuration edits, keeping pytest policy fixed."""
    import configparser
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    name = Path(path).name
    if name in {'pytest.toml', '.pytest.toml', 'pytest.ini', '.pytest.ini'}:
        return False  # Even an empty new ini file overrides ancestor policy.
    if before is None:
        return False  # New configuration can change root/config discovery.
    try:
        if name == 'pyproject.toml':
            def options(source):
                return tomllib.loads(source).get('tool', {}).get('pytest')
        else:
            def options(source):
                config = configparser.ConfigParser(interpolation=None)
                config.read_string(source)
                section = 'tool:pytest' if name == 'setup.cfg' else 'pytest'
                return dict(config[section]) if config.has_section(section) else None
        return options(before) == options(after)
    except (ValueError, configparser.Error, TypeError):
        return False


def _preserves_python_checks(before, after):
    """Existing executable bodies are immutable; allow independent test additions.

    Subsequence matching inside a body admits early returns, and matching only
    the first definition admits a later replacement of the collected test.
    """
    if type(before) is not type(after):
        return False
    if not isinstance(before, (ast.Module, ast.ClassDef)):
        return ast.dump(before) == ast.dump(after)
    old, new = deepcopy(before), deepcopy(after)
    old.body, new.body = [], []
    if ast.dump(old) != ast.dump(new):
        return False
    definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    names = [node.name for node in after.body if isinstance(node, definitions)]
    if len(names) != len(set(names)):
        return False
    old_names = {node.name for node in before.body if isinstance(node, definitions)}
    old_names.update(node.id for node in ast.walk(before) if isinstance(node, ast.Name))
    cursor = 0
    for node in after.body:
        if cursor < len(before.body) and _preserves_python_checks(before.body[cursor], node):
            cursor += 1
            continue
        # Only new definitions may be inserted at module/class scope. An
        # assignment, import, decorator or replacement could disable checks.
        if (not isinstance(node, definitions) or node.name in old_names
                or not _independent_python_test(node)):
            return False
    return cursor == len(before.body)


def _independent_python_test(node):
    """Admit additions whose independence can be established without execution.

    Definitions are executable Python: class bodies, defaults, annotations and
    setup hooks can replace a retained test even when its AST is unchanged.
    Arbitrary new code in a retained proof module therefore needs a separate
    contract review. Permit literal/local assertion tests here; never execute
    a candidate to decide whether it preserved the original assertions.
    """
    if not isinstance(node, ast.FunctionDef) or not node.name.startswith('test_'):
        return False
    if (node.decorator_list or node.returns or getattr(node, 'type_params', [])
            or node.args.posonlyargs or node.args.args or node.args.kwonlyargs
            or node.args.vararg or node.args.kwarg or node.args.defaults
            or node.args.kw_defaults):
        return False
    local_names = set()
    expressions = (ast.Constant, ast.Tuple, ast.List, ast.Set, ast.Dict,
                   ast.UnaryOp, ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp)

    def pure(value):
        if isinstance(value, ast.Name):
            return isinstance(value.ctx, ast.Load) and value.id in local_names
        if isinstance(value, (ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.Load)):
            return True
        return isinstance(value, expressions) and all(pure(child) for child in ast.iter_child_nodes(value))

    for statement in node.body:
        if isinstance(statement, ast.Assert):
            if not pure(statement.test) or (statement.msg is not None and not pure(statement.msg)):
                return False
        elif isinstance(statement, ast.Assign):
            if not pure(statement.value) or any(not isinstance(target, ast.Name) for target in statement.targets):
                return False
            local_names.update(target.id for target in statement.targets)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue  # A docstring has no effect on retained checks.
        else:
            return False
    return True
