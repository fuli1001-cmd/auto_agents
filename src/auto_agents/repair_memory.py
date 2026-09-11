"""Controller-owned repair memory. Model text is evidence, never a proof cache."""
from copy import deepcopy
import ast
import json
from pathlib import Path
import re
import subprocess
import uuid

from .repair_control import atomic_json, digest
from .verification_ledger import source_identity
from .repair_feedback import sanitize_evidence


def component_key(group):
    # Presentation labels and candidate/source revisions are not new work.
    from .repair_test_refs import pytest_targets
    checks = sorted({node for command in group.get('focused_tests', [])
                     for node in (pytest_targets(command, prose=False) or [command])})
    return digest([sorted(group.get('contract_obligation_ids', [])),
                   sorted(group.get('touched_paths', [])), checks])


def save_record(runner, kind, payload):
    identity = uuid.uuid4().hex
    record = {'kind': kind, **deepcopy(sanitize_evidence(payload)), 'id': identity}
    directory = runner._experiment_store.root / 'planning' / identity
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory / 'memory.json', record)
    return {'id': identity, 'digest': digest(record)}


def read_record(runner, reference):
    try:
        identity = reference['id']
        if not re.fullmatch('[a-f0-9]{32}', identity):
            return None
        directory = runner._experiment_store.root / 'planning' / identity
        path = directory / 'memory.json'
        if directory.parent.is_symlink() or directory.is_symlink() or path.is_symlink():
            return None
        value = json.loads(path.read_text())
        return value if value.get('id') == identity and digest(value) == reference['digest'] else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def remember_revision(runner, group, payload):
    payload = deepcopy(payload)
    parent = read_record(runner, runner._experiment.plan_revisions.get(payload.get('parent_revision'), {}))
    steps = (payload.get('draft') or {}).get('implementation_steps', []) if isinstance(payload.get('draft'), dict) else []
    prior_steps = (parent.get('draft') or {}).get('implementation_steps', []) if parent else []
    prior_ids = parent.get('step_ids', []) if parent else []
    used = set()
    identities = []
    for index, step in enumerate(steps):
        identity = None
        if len(steps) == len(prior_steps) and index < len(prior_ids):
            identity = prior_ids[index]
        else:
            for old_index, old in enumerate(prior_steps):
                if old == step and old_index < len(prior_ids) and prior_ids[old_index] not in used:
                    identity = prior_ids[old_index]
                    break
        identity = identity or 'step-' + uuid.uuid4().hex[:12]
        used.add(identity)
        identities.append(identity)
    payload['step_ids'] = identities
    reference = save_record(runner, 'plan_revision', payload)
    state = runner._experiment
    state.plan_revisions[reference['id']] = reference
    state.component_memory.setdefault(component_key(group), {})['latest_revision'] = reference
    runner._experiment_store.save(state)
    return reference


def latest_revision(runner, group):
    memory = runner._experiment.component_memory.get(component_key(group), {})
    return read_record(runner, memory.get('latest_revision', {}))


def recover_draft_output(runner, group, context, request_id):
    """A completed draft file after cancellation is data, never an approval."""
    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
        return None
    directory = runner._experiment_store.root / 'planning' / request_id
    try:
        paths = [directory / name for name in ('request.json', 'input.json', 'output.json')]
        if directory.parent.is_symlink() or directory.is_symlink() or any(p.is_symlink() for p in paths):
            return None
        request, incoming = [json.loads(p.read_text()) for p in paths[:2]]
        if (request.get('request_id') != request_id
                or request.get('stage') not in {'self_repair_component_plan', 'self_repair_plan_format'}
                or incoming.get('source') != context.get('source')
                or incoming.get('environment') != context.get('environment')
                or incoming.get('contract_fingerprint') != context.get('contract_fingerprint')
                or component_key(incoming.get('component', {})) != component_key(group)):
            return None
        from .self_repair import _extract_json_object
        from .repair_planning import _format_semantics_changed, _materialize_draft
        draft = _materialize_draft(_extract_json_object(paths[2].read_text()), incoming.get('previous_revision'))
        if not isinstance(draft, dict):
            return None
        if request.get('stage') == 'self_repair_plan_format' and _format_semantics_changed(
                incoming.get('previous_revision', {}).get('draft'), draft):
            return None
        parent = latest_revision(runner, group)
        ref = remember_revision(runner, group, {'parent_revision': parent.get('id') if parent else None,
            'draft': draft, 'source': incoming['source'], 'source_commit': incoming.get('source_commit'),
            'environment': incoming['environment'], 'planner_request': request_id, 'component': group,
            'status': 'recovered_draft', 'requires_independent_review': True})
        return read_record(runner, ref)
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def import_legacy_draft(runner, group, context):
    """Recover an old draft as data, never import its flag as a v2 approval.

    Only matching immutable input/request artifacts can reconstruct counters.
    Interrupted final rounds retain their slot; completed rejections remain spent.
    """
    from .repair_planning import finding_key
    matches = []
    for path in (runner._experiment_store.root / 'planning').glob('*/request.json'):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            request = json.loads(path.read_text())
            if request.get('policy') != 1 or request.get('stage') != 'self_repair_component_plan':
                continue
            incoming = json.loads(path.with_name('input.json').read_text())
            component = incoming.get('component', {})
            if (component_key(component) != component_key(group)
                    or component.get('implementation_steps') != group.get('implementation_steps')
                    or incoming.get('contract_fingerprint') != context.get('contract_fingerprint')
                    or sorted(finding_key(f) for f in incoming.get('findings', []))
                       != sorted(finding_key(f) for f in context.get('findings', []))):
                continue
            matches.append((path.stat().st_mtime_ns, request, incoming, path.parent))
        except (OSError, ValueError, TypeError):
            continue
    if not matches:
        return None
    _, request, incoming, directory = max(matches, key=lambda row: row[0])
    revision = incoming.get('revision', 1)
    if type(revision) is not int or not 1 <= revision <= 3:
        return None
    payload = None
    try:
        output = directory / 'result.json'
        if not output.is_symlink():
            payload = json.loads(output.read_text())
    except (OSError, ValueError):
        pass
    completed_review = False
    for path in (runner._experiment_store.root / 'planning').glob('*/input.json'):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            value = json.loads(path.read_text())
            result = path.with_name('result.json')
            if (value.get('planner_request') == request.get('request_id') and not result.is_symlink()
                    and json.loads(result.read_text()).get('decision') in {'APPROVE', 'REVISE'}):
                completed_review = True
        except (OSError, ValueError, TypeError):
            pass
    reference = remember_revision(runner, group, {'draft': payload, 'planner_request': request['request_id'],
        'component': group, 'source': incoming.get('source'), 'source_commit': incoming.get('source_commit'),
        'environment': incoming.get('environment'), 'feedback': incoming.get('feedback', []),
        'status': 'legacy_draft', 'legacy_completed_review': completed_review})
    return {'semantic_attempts': revision, 'pending_round': not completed_review,
            'phase': 'review' if isinstance(payload, dict) and not completed_review else 'draft',
            'latest_revision': reference, 'legacy_request': request['request_id'],
            'feedback': incoming.get('feedback', [])}


def compact_context(runner, context):
    """Keep complete background immutable, but do not make every turn reread it."""
    history = context.get('history')
    if not isinstance(history, dict):
        return context
    group = context.get('component', {})
    state = runner._experiment
    background_key = digest(history)
    memory = state.component_memory.setdefault(component_key(group), {})
    background = memory.get('background', {})
    if background.get('content') != background_key or not read_record(runner, background):
        background = {**save_record(runner, 'background', {'history': history}), 'content': background_key}
        memory['background'] = background
    ids = set(group.get('finding_ids', []))
    # Outstanding observations in other groups are indexed, not silently lost.
    active = [f for f in history.get('open_contract_findings', [])
              if f.get('finding_id') in ids or f.get('repair_group_id') == group.get('group_id')]
    current = {key: history[key] for key in (
        'experiment_id', 'contract_fingerprint', 'contract_obligations',
        'verified_progress_count', 'consecutive_non_improvements', 'next_action') if key in history}
    current.update(open_contract_findings=active,
                   historical_completed_groups=history.get('historical_completed_groups', {}),
                   scope_decisions={key: value for key, value in history.get('scope_decisions', {}).items() if key in ids},
                   pending_components=[{'group_id': item.get('group_id'), 'depends_on': item.get('depends_on', []),
                                        'finding_ids': item.get('finding_ids', [])}
                                       for item in state.finding_groups if item.get('status') != 'completed'])
    previous = latest_revision(runner, group)
    context = {**context, 'history': current, 'background_ref': str(
        runner._experiment_store.root / 'planning' / background['id'] / 'memory.json'),
        'background_policy': 'Retrieve indexed background when relevant or missing; do not reconstruct unchanged history.',
        'previous_revision': previous,
        'previous_code_review': read_record(runner, memory.get('code_review', {}))}
    runner._experiment_store.save(state)
    return context


def _file_value(root, name):
    path = root / name
    # Do not dereference a provider-supplied link into another workflow.
    if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
        raise ValueError('symbolic dependency')
    return digest([path.read_bytes().hex(), path.stat().st_mode & 0o777]) if path.is_file() else 'absent'


def dependency_manifest(root, paths):
    """Conservative file/import closure, including directory membership.

    Unknown dynamic Python access declines narrow reuse. Symbols only help locate
    a review; they never allow a changed file to reuse a safety decision.
    """
    root = Path(root).resolve()
    pending = list(paths)
    files, directories = {}, {}
    complete = bool(pending)
    pending += ['pyproject.toml', 'pytest.ini', 'conftest.py', 'tests/conftest.py']
    try:
        while pending:
            name = pending.pop()
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or not relative.parts:
                raise ValueError('invalid dependency')
            if name in files:
                continue
            path = root / name
            if path.is_dir():
                raise ValueError('unbounded directory dependency')
            files[name] = _file_value(root, name)
            if files[name] == 'absent':
                continue
            if path.suffix != '.py':
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    called = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                    if called not in {'len', 'str', 'int', 'bool', 'tuple', 'list', 'dict', 'set',
                                      'sorted', 'range', 'isinstance', 'enumerate', 'zip', 'min',
                                      'max', 'sum', 'abs', 'all', 'any'}:
                        complete = False
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                    complete = False
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        node.args.args or node.args.posonlyargs or node.args.kwonlyargs
                        or node.args.vararg or node.args.kwarg):
                    complete = False  # Caller-supplied objects/fixtures are not a proved static input closure.
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        parent = path.parent
                        for _ in range(node.level - 1):
                            parent = parent.parent
                        base = parent.relative_to(root)
                        modules = [str(base / (node.module or '').replace('.', '/'))]
                        modules.extend(str(base / (node.module or '').replace('.', '/') / alias.name)
                                       for alias in node.names if alias.name != '*')
                    else:
                        modules = [node.module or '']
                for module in modules:
                    stem = module.replace('.', '/') if '/' not in module else module
                    resolved = False
                    for prefix in ('', 'src/'):
                        for candidate in (prefix + stem + '.py', prefix + stem + '/__init__.py'):
                            if (root / candidate).is_file():
                                pending.append(candidate)
                                resolved = True
                    if not resolved:
                        complete = False
            parent = path.parent
            directories[str(parent.relative_to(root))] = sorted(p.name for p in parent.iterdir()
                if p.suffix == '.py' or p.is_dir() and p.name.isidentifier() and p.name != '__pycache__')
        return {'complete': complete, 'files': files, 'directories': directories,
                'source': source_identity(root)}
    except (OSError, ValueError, SyntaxError):
        return {'complete': False, 'source': source_identity(root)}


def dependencies_match(root, manifest):
    if not manifest:
        return False
    if not manifest.get('complete'):
        return False
    try:
        if any(_file_value(Path(root), name) != value for name, value in manifest['files'].items()):
            return False
        for name, entries in manifest.get('directories', {}).items():
            observed = sorted(p.name for p in (Path(root) / name).iterdir()
                if p.suffix == '.py' or p.is_dir() and p.name.isidentifier() and p.name != '__pycache__')
            if observed != entries:
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def remember_check_timings(runner, workspace, group, plan, result, *, phase):
    """Keep actual execution costs separate from certificate lookup latency."""
    from .git_ops import head_ref
    memory = runner._experiment.component_memory.setdefault(component_key(group), {})
    timings = result.payload.get('command_timings', [])
    known = memory.setdefault('check_timings', {})
    for row in timings:
        if not row.get('cache_hit'):
            known[row['command']] = row
    record = save_record(runner, 'verification_schedule', {
        'phase': phase, 'plan': plan, 'timings': timings, 'ok': result.ok,
        'certificate_hits': result.payload.get('certificate_hits', 0),
        'source_commit': head_ref(workspace),
        'slow_commands': [row for row in timings if not row.get('cache_hit') and row.get('seconds', 0) > 180],
    })
    memory['verification_schedule'] = record
    memory.setdefault('verification_history', []).append(record)
    runner._experiment_store.save(runner._experiment)


def review_context(runner, root, group):
    memory = runner._experiment.component_memory.get(component_key(group), {})
    previous = read_record(runner, memory.get('code_review', {}))
    if not previous:
        return {'mode': 'initial'}
    commit = previous.get('source_commit', '')
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', commit, 'HEAD'],
                              cwd=root, capture_output=True)
    if ancestor.returncode:
        return {'mode': 'initial', 'invalidated': 'previous reviewed source is not an ancestor'}
    diff = subprocess.check_output(['git', 'diff', commit, '--'], cwd=root, text=True)
    reference = save_record(runner, 'review_delta', {'diff': diff, 'parent_review': memory['code_review']})
    return {'mode': 'incremental', 'previous_review': {key: previous.get(key) for key in ('id', 'source_commit', 'contract', 'result')},
            'complete_previous_review': str(runner._experiment_store.root / 'planning' / memory['code_review']['id'] / 'memory.json'),
            'delta_ref': str(runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json'),
            'instruction': 'Review every changed behavior and its dependencies; retain unchanged conclusions. '
                           'The cumulative diff and frozen contract remain available. Missing evidence requires inspection.'}


def remember_review(runner, root, group, payload):
    from .git_ops import head_ref
    reference = save_record(runner, 'code_review', {'source_commit': head_ref(root),
        'source': source_identity(root), 'contract': runner._experiment.contract_fingerprint,
        'component': deepcopy(group), 'result': deepcopy(payload)})
    runner._experiment.component_memory.setdefault(component_key(group), {})['code_review'] = reference
    runner._experiment.review_facts[reference['id']] = reference
    runner._experiment_store.save(runner._experiment)
