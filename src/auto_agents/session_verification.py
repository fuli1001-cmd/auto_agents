"""Durable session proof scope and candidate ownership.

The binding is captured before agent work. A resumed legacy child recovers its
contract from its recorded revision, never from a different pending run.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from copy import deepcopy

from .config import config_path, requirements_trace_path, run_state_path, task_plan_path
from .execution_binding import command_spans, executable_tokens, route_sources
from .git_ops import head_ref
from .io_utils import read_json


class SessionOwnershipError(RuntimeError):
    pass


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
    if state.verification_binding:
        if 'task_scope' not in state.verification_binding:
            state.verification_binding['task_scope'] = _task_scope(session, state)
            session._save(state)
        return
    root = session.project_root
    gates = session.config.gates.to_dict()
    tasks = read_json(task_plan_path(root), default={})
    run = read_json(run_state_path(root), default={})
    revision = state.baseline_git_ref or state.baseline_head_ref or state.lineage_head_ref
    if (not state.parent_handoff_id and not state.baseline_head_ref
            and not state.lineage_head_ref and not head_ref(root)):
        # An unborn legacy standalone repository has no historical contract
        # to recover. Preserve its normal first-baseline capture behavior.
        revision = ""
    if revision and (state.baseline_git_ref or state.baseline_head_ref or state.current_attempt or revision != head_ref(root)):
        # Baseline snapshot refs are disposable. Recover from the recorded
        # source HEAD (or workflow lineage) if the snapshot has disappeared,
        # before _ensure_baseline refreshes the comparison snapshot. Legacy
        # standalone resumes acquire their lineage during workflow migration;
        # routed children must retain their own recorded contract history.
        for candidate in dict.fromkeys(filter(None, (
            state.baseline_git_ref, state.baseline_head_ref, state.lineage_head_ref,
        ))):
            probe = subprocess.run(
                ["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"],
                cwd=root, capture_output=True, text=True,
            )
            if probe.returncode == 0:
                revision = probe.stdout.strip()
                break
        else:
            raise SessionOwnershipError("session verification contract revision is unavailable")
        payloads = []
        for path in (config_path(root), task_plan_path(root), run_state_path(root)):
            result = subprocess.run(
                ["git", "show", f"{revision}:{path.relative_to(root).as_posix()}"],
                cwd=root, capture_output=True, text=True,
            )
            if result.returncode and path != config_path(root):
                payloads.append({})
                continue
            if result.returncode:
                raise SessionOwnershipError("session verification contract revision is unavailable")
            payloads.append(json.loads(result.stdout))
        gates = payloads[0]["gates"]
        tasks = payloads[1]
        run = payloads[2]
    task_scope = _task_scope(session, state)
    plan_workflow = run.get('resume_context', {}).get('workflow_id', run.get('workflow_id', ''))
    state.verification_binding = {
        "schema_version": 1,
        "session_id": state.session_id,
        "workflow_id": state.workflow_id,
        "authorization": dict(state.authorization_policy),
        "gates": gates,
        "tasks": tasks.get("tasks", []),
        "task_scope": task_scope,
        "plan_workflow_id": plan_workflow,
        "contract_revision": revision,
        "contract_fingerprint": fingerprint([gates, tasks, task_scope, plan_workflow]),
    }
    session._save(state)


def _task_scope(session, state):
    task_ids = set()
    requirement_ids = set()
    seeds = [read_json(session.project_root / '.auto-agents/state/sessions' / state.session_id / 'issue.json', default={})]
    if state.parent_handoff_id:
        handoff = read_json(session.project_root / '.auto-agents/state/handoffs' / (state.parent_handoff_id + '.json'), default={})
        seeds.append(handoff.get('payload', {}))
    for seed in seeds:
        for source in route_sources(seed):
            task_ids.update(source.get('task_ids', []))
            if source.get('task_id'):
                task_ids.add(source['task_id'])
            requirement_ids.update(source.get('requirement_ids', []))
    return {'task_ids': sorted(task_ids), 'requirement_ids': sorted(requirement_ids)}


def session_gates(session, state):
    """Project the retained plan onto the session's task ownership.

    Unfinished tasks explicitly owned by another run remain in the durable
    plan. Their planned selectors and ordering edges cannot become obligations
    of a fix merely because a shared regression escalates to release.
    """
    from .models import GateConfig

    binding = state.verification_binding
    gates = deepcopy(binding['gates'])
    task_ids = set(binding.get('task_scope', {}).get('task_ids', []))
    requirement_ids = set(binding.get('task_scope', {}).get('requirement_ids', []))
    owned_refs, foreign_refs = set(), set()
    for task in binding.get('tasks', []):
        refs = set(task.get('verification_refs', []))
        for proof in task.get('requirement_proofs', []):
            refs.update(proof.get('evidence_refs', []))
        owned = (task.get('task_id') in task_ids
                 or requirement_ids.intersection(task.get('requirement_ids', [])))
        unresolved = task.get('status') in {'pending', 'in_progress', 'blocked', 'failed'}
        owner = task.get('workflow_id') or binding.get('plan_workflow_id')
        foreign = bool(owner and owner != state.workflow_id)
        (foreign_refs if unresolved and foreign and not owned else owned_refs).update(refs)
    # An explicit targeted regression remains required, even for an unfinished
    # task. Missing entries must still produce a preflight failure.
    excluded = {ref for ref in foreign_refs - owned_refs if ref not in state.fix_verify_command}
    removed = set()
    selected = []
    for step in gates.get('steps', []):
        targets = step.get('targets', [])
        kept = [target for target in targets if target not in excluded]
        if targets and not kept:
            removed.add(step.get('proof_id'))
            continue
        step['targets'] = kept
        selected.append(step)
    if removed:
        gates['steps'] = selected
        for step in selected:
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
            gates['parallel_groups'] = []
    return GateConfig.from_dict(gates)


def owned_paths(orchestrator, state) -> list[str]:
    snapshot = candidate_snapshot(orchestrator)
    conflicts = [path for path, digest in state.candidate_paths.items()
                 if snapshot.get(path, "") != digest]
    if conflicts:
        raise SessionOwnershipError("candidate ownership changed: " + ", ".join(sorted(conflicts)))
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


def diagnostic_owners(state, command: str) -> list[dict[str, object]]:
    owners = []
    for task in state.verification_binding.get("tasks", []):
        refs = list(task.get("verification_refs", []))
        for proof in task.get("requirement_proofs", []):
            refs.extend(proof.get("evidence_refs", []))
        if any(str(ref).removeprefix("cmd:") in command for ref in refs):
            owners.append({"task_id": task.get("task_id", task.get("id", "")),
                           "requirement_ids": task.get("requirement_ids", []),
                           "verification_refs": refs})
    return owners


def validate_selected_contracts(session, state, commands: list[str]) -> None:
    """A frozen selector cannot attest a changed requirement contract."""
    from .requirements import requirement_contract_sha256

    task_ids = {owner['task_id'] for command in commands
                for owner in diagnostic_owners(state, command)}
    trace = read_json(requirements_trace_path(session.project_root), default={})
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
                raise SessionOwnershipError(
                    f"session {state.session_id} task {task['task_id']} requirement "
                    f"{requirement_id} no longer matches its bound verification contract"
                )
