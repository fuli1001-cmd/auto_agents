"""Adapters for the existing provider and verification executors.

Domain algorithms propose work. The kernel owns reservations, outcomes,
continuations and retries; a native process exit is never an engine diagnosis.
"""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from uuid import uuid4

from .authority import installed
from .executor import Executor, FunctionExecutor
from .model import Command, Contract, Event, Evidence, KernelError, Outcome, OutcomeKind, digest, require


def _apply(store, stream, kind, data, key):
    return store.apply(stream, store.load(stream)['revision'], Event(kind + ':' + digest(key), kind, data))


def context(owner, usage=None):
    usage = usage or {}
    location = (getattr(owner, '_kernel_project', None) or getattr(owner, '_custody_control_root', None)
                or getattr(owner, 'project_root', None))
    if location is None: return None
    root = Path(location)
    store = installed(root)
    if store is None: return None
    active = store.meta('active_runtime') or {}
    if active.get('path'):
        require(Path(active['path']).resolve() == Path(__file__).resolve().parents[3],
                'stale_runtime', 'This process belongs to an earlier engine generation; restart through the bootstrap')
    from ..config import load_run_state, load_session_state
    native = usage.get('subject_id')
    kind = usage.get('workflow_kind') or getattr(owner, 'mode', 'run')
    if kind == 'provider-resolve': kind = 'provider_resolve'
    if native and kind != 'run': state = load_session_state(root, native)
    elif getattr(owner, '_current_state', None) is not None:
        state = owner._current_state
        native = state.session_id; kind = state.mode
    else:
        state = load_run_state(root); native = state.run_id; kind = 'run'
    stream = store.binding(root, ('run:' if kind == 'run' else 'session:') + native)
    require(stream is not None, 'kernel_binding', 'Public entrypoint did not register its task')
    owner._kernel_subject = ('run:' if kind == 'run' else 'session:') + native
    if getattr(owner,'orch',None) is not None: owner.orch._kernel_subject = owner._kernel_subject
    return store, stream, root, kind, native, state


def _source(owner, state):
    from ..git_ops import worktree_fingerprint
    if getattr(owner, '_recovery_policy_active', False):
        from ..git_ops import head_ref
        return digest([head_ref(owner.project_root), worktree_fingerprint(owner.project_root)])
    return digest(worktree_fingerprint(owner.project_root))


def _contract(store, stream, root, kind, native, state, phase, key):
    from ..io_utils import read_json
    issue = read_json(root / '.auto-agents/state/sessions' / native / 'issue.json', default={}) if kind != 'run' else {}
    goal = getattr(state, 'goal', '') or getattr(state, 'spec_file', '')
    if kind == 'run' and not goal:
        spec = Path(getattr(state, 'resume_context', {}).get('spec_file') or 'spec.md')
        if not spec.is_absolute(): spec = root / spec
        require(spec.is_file() and spec.resolve().is_relative_to(root.resolve()),
                'task_contract', 'Run requires a retained specification')
        goal = spec.read_text()
    require(bool(goal) or kind == 'run', 'task_contract', 'A provider cannot execute without its original goal')
    command = getattr(state, 'fix_verify_command', '')
    checks = (command,) if command else tuple(getattr(state, 'verification_binding', {}).get('required_commands', []))
    if not checks:
        from ..config import load_project_config
        config = load_project_config(root)
        checks = tuple(target for step in config.gates.steps for target in step.targets)
    if phase == 'implement' and kind == 'fix':
        require(bool(checks), 'task_contract', 'Fix implementation requires retained verification obligations')
        require(bool(issue.get('summary') or issue.get('task_id')) or not getattr(state, 'parent_handoff_id', ''),
                'task_contract', 'A child writer must receive its classified issue')
    task_id = 'operation:' + digest([kind, native, phase, key])[:40]
    domain_id = kind + ':' + native
    domain = store.load(stream)['tasks'].get(domain_id)
    if domain is None:
        from .migration import _contracts
        phases, completion = _contracts(kind)
        business = Contract(store.load(stream)['goal_id'], domain_id, kind,
            store.put({'goal': goal, 'native': native}), store.put(issue or {'goal': goal}),
            tuple(issue.get('constraints', [])), checks or ('stage:' + phase,), (str(root),),
            store.put(getattr(state, 'authorization_policy', {}) or {'mode':'interactive'}), completion, phases)
        _apply(store, stream, 'task_bound', {'contract':business.to_dict()}, domain_id)
    else: business = Contract.read(domain['contract'])
    contract = Contract(business.goal_id, task_id, kind,
        business.goal_ref, business.issue_ref, business.constraints, business.required_checks,
        business.source_scope, business.authorization_ref,
        'phase_completed', (phase,), parent_task=domain_id)
    _apply(store, stream, 'task_bound', {'contract': contract.to_dict()}, [task_id, contract.identity])
    return contract


def perform(owner, phase, key, function, classify, *, usage=None, model=False, bind=False, completion=False, read_only=False):
    selected = context(owner, usage)
    if selected is None: return function()
    store, stream, root, kind, native, state = selected
    from .policy import automatic
    automatic(store, stream)
    owner._recovery_policy_active = 'recovery' in store.load(stream)
    source = _source(owner, state)
    runtime = store.meta('active_runtime')
    require(isinstance(runtime, dict) and runtime.get('source'), 'runtime', 'Active runtime is not sealed')
    environment = digest({'python': os.sys.executable, 'path': os.environ.get('PATH', ''),
                          'proof_context': getattr(state, 'verification_binding', {}).get('proof_execution_context', {}),
                          'provider': getattr(owner.config, 'active_provider', ''),
                          'verifier_runtime':runtime['source'] if phase in {'verify','review','deliver','acceptance'} else None})
    operation_key = digest([kind,native,phase,key] if model and phase != 'review' else
                           [kind,native,phase,key,source,environment])
    prior_id = store.load(stream)['operations'].get(operation_key)
    # A dead verifier supplies an interruption receipt, not an outcome that
    # can satisfy this verification. Retry has its own durable operation ID.
    while prior_id and phase == 'verify' and not model:
        prior = store.load(stream)['commands'][prior_id]
        if prior['status'] != 'finished' or not (prior.get('outcome') or {}).get('details', {}).get('interrupted_verification'):
            break
        operation_key = digest(['verification-after-interruption', operation_key, prior_id])
        prior_id = store.load(stream)['operations'].get(operation_key)
    if model and prior_id is None and kind != 'run':
        snapshot = store.load(stream)
        pending = [c for c in snapshot['commands'].values() if c['status'] == 'reserved'
                   and c['model_call'] and c['phase'] == phase and c['source'] == source
                   and c['environment'] == environment and
                   snapshot['tasks'][c['task_id']]['contract'].get('parent_task') == kind + ':' + native]
        require(len(pending) <= 1,'pending_dispatch','More than one native reservation needs reconciliation')
        if pending:
            prior_id, operation_key = pending[0]['command_id'], pending[0]['operation_key']
    if prior_id:
        prior = store.load(stream)['commands'][prior_id]
        if prior['status'] in {'running', 'unknown'}:
            Executor(store, {}).reconcile(stream, prior_id)
            prior = store.load(stream)['commands'][prior_id]
        if prior['status'] != 'reserved':
            require(prior['status'] == 'finished', 'outcome_unknown', 'Prior native operation requires reconciliation', command_id=prior_id)
            require(not model or prior['outcome']['kind'] == 'success', prior['outcome']['kind'], prior['outcome']['reason'],
                    command_id=prior_id)
            require(not model or source in {prior['source'], prior['outcome']['details'].get('post_source')},
                    'reconciliation', 'Source changed after the retained model operation; reconcile its candidate before continuing')
            return store.read(prior['outcome']['details']['native_result'])
    require(not model or not any(c['status'] in {'running', 'unknown','reserved'} and c['command_id'] != prior_id
                                for c in store.load(stream)['commands'].values()),
            'outcome_unknown', 'An unconfirmed operation must be reconciled before another model call')
    if model and not owner._recovery_policy_active:
        from .model_progress import require_progress
        require_progress(store.load(stream), phase)
    contract = _contract(store, stream, root, kind, native, state, phase, operation_key)
    if kind == 'fix' and phase == 'deliver':
        parent = store.load(stream)['tasks'][contract.parent_task]
        require(all(parent['proofs'].get(stage) and all(p['source'] == source for p in parent['proofs'][stage])
                    for stage in ('verify','review')), 'delivery_evidence', 'Delivery requires verification and review of the same candidate')
        gates = getattr(state, 'verification_binding', {}).get('gates', {})
        if gates.get('verification_policy_version', 1) >= 5:
            required = {step['proof_id'] for step in gates.get('steps', []) if step.get('proof_id')}
            release = False
            snapshot = store.load(stream)
            for row in snapshot['commands'].values():
                outcome = row.get('outcome') or {}
                task = snapshot['tasks'][row['task_id']]
                if (row['phase'] != 'verify' or row['status'] != 'finished' or row['source'] != source
                        or row['environment'] != environment or outcome.get('kind') != 'success'
                        or task['contract'].get('parent_task') != contract.parent_task):
                    continue
                ref = outcome.get('details', {}).get('native_result')
                value = store.read(ref) if ref else {}
                if (value.get('ok') and value.get('scope') == 'final' and value.get('attestation_level') == 'release'
                        and required.issubset(value.get('proof_ids', []))):
                    release = True
                    break
            require(release, 'delivery_evidence', 'Delivery requires sealed complete release evidence')
        if owner._recovery_policy_active:
            from .scope_amendments import pending
            require(not pending(store.load(stream), contract.task_id), 'scope_review_required',
                    'Delivery requires independent approval of scope amendments')
    command = Command('cmd:' + operation_key, stream, contract.task_id, phase, source, contract.identity,
                      environment, runtime['source'], operation_key, model_call=model)
    if owner._recovery_policy_active:
        from .policy import reserve
        reserve(store, stream, command)
    else:
        _apply(store, stream, 'command_reserved', command.to_dict(), command.command_id)
    captured = []
    def execute(_command):
        correction_snapshot = store.load(stream)
        inspector = getattr(owner, 'orch', owner)
        before = (inspector._worktree_change_snapshot() if model and phase == 'implement'
                  and owner._recovery_policy_active else None)
        result = function(contract) if bind else function(); captured.append(result)
        plain = result if isinstance(result, (dict, list, str, int, float, bool)) or result is None else asdict(result)
        # Native AgentResult contains an output Path, but no executable callbacks.
        if isinstance(plain, dict) and isinstance(plain.get('output_path'), Path): plain['output_path'] = str(plain['output_path'])
        reference = store.put(plain)
        verdict, reason = classify(result)
        scope_paths = []
        if before is not None:
            from .policy import correction_paths
            outside = correction_paths(correction_snapshot, command.task_id, before, inspector._worktree_change_snapshot())
            if outside:
                from .scope_amendments import product_paths
                if product_paths(outside) and verdict == OutcomeKind.SUCCESS:
                    scope_paths = outside
                else:
                    verdict, reason = OutcomeKind.OWNERSHIP_CONFLICT, 'Correction crossed a protected boundary: ' + ', '.join(outside)
        extra = {}
        if scope_paths: extra['scope_amendment_required'] = scope_paths
        if isinstance(plain, dict) and plain.get('scope_approval'): extra['scope_approval'] = plain['scope_approval']
        if model and verdict == OutcomeKind.PROTOCOL_INVALID:
            from .rejections import request_rejection
            rejection = request_rejection(plain)
            if rejection and _source(owner, state) == source: extra['request_rejection'] = rejection
            elif rejection: verdict, reason = OutcomeKind.OUTCOME_UNKNOWN, 'Source changed after a rejected request'
        if model and verdict == OutcomeKind.ENVIRONMENT_BLOCKED:
            from .rejections import quota_rejection
            quota = quota_rejection(plain)
            if quota and _source(owner, state) == source: extra['provider_quota'] = quota
            elif quota: verdict, reason = OutcomeKind.OUTCOME_UNKNOWN, 'Source changed after a quota rejection'
        if owner._recovery_policy_active:
            from .policy import result_details, parse_diagnosis
            if phase in {'verify', 'review'}: extra.update(result_details(store, stream, command, plain))
            if phase == 'diagnose' and verdict == OutcomeKind.SUCCESS:
                try:
                    text = plain.get('summary') or plain.get('stdout') or plain.get('text', '')
                    extra['recovery_diagnosis'] = parse_diagnosis(store.load(stream), command, text)
                except KernelError as error:
                    verdict, reason = OutcomeKind.PROTOCOL_INVALID, str(error)
        if model and (phase in {'review', 'diagnose'} or read_only) and _source(owner, state) != source:
            verdict, reason = OutcomeKind.OWNERSHIP_CONFLICT, 'Read-only review changed candidate inputs'
        parent = store.load(stream)['tasks'][contract.parent_task]
        predicate = parent['contract']['completion'] if not model and (phase == 'deliver' or completion) else 'phase_completed'
        require(not completion or phase == parent['contract']['phases'][-1],
                'completion_boundary','Completion observation belongs to another stage')
        proof = Evidence(store.put({'command': command.command_id, 'result': reference}), contract.task_id,
                         source, contract.identity, environment, digest(['native-executor-v1',runtime['source']]), phase, predicate)
        evidence = (proof,) if predicate == 'phase_completed' else (proof, replace(proof, predicate='phase_completed'))
        return Outcome(verdict, reason, evidence if verdict == OutcomeKind.SUCCESS else (),
                       {'native_result': reference, 'subject': native, 'post_source': _source(owner, state), **extra})
    executor = Executor(store, {phase: FunctionExecutor(execute)}, owner='native:' + str(os.getpid()) + ':' + uuid4().hex)
    outcome = executor.execute(stream, command.command_id)
    if outcome.details.get('request_rejection'):
        from .rejections import note
        note(store, stream, command.command_id)
    if model and outcome.kind != OutcomeKind.SUCCESS:
        raise KernelError(outcome.kind.value, outcome.reason, command_id=command.command_id)
    return captured[0]


def provider_outcome(orchestrator, result):
    from .rejections import request_rejection, quota_rejection, quota_reason
    if request_rejection(result):
        return OutcomeKind.PROTOCOL_INVALID, 'Provider rejected the request schema before model execution'
    quota = quota_rejection(result)
    if quota:
        return OutcomeKind.ENVIRONMENT_BLOCKED, quota_reason(quota)
    reason = getattr(result.termination,'reason','') if result.termination is not None else ''
    uncertain = result.cleanup_incomplete or (not result.ok and
        (reason and reason not in {'execution_budget_exhausted','verification_environment_blocked'} or
         result.returncode < 0 and not reason))
    if not result.ok and orchestrator._failover_error_category(result) == 'connection': uncertain = True
    return (OutcomeKind.OUTCOME_UNKNOWN if uncertain else OutcomeKind.SUCCESS if result.ok else OutcomeKind.ENVIRONMENT_BLOCKED,
            'Provider outcome requires reconciliation' if uncertain else 'Provider response received' if result.ok else 'Provider call failed')


def provider(orchestrator, request, execute):
    # Root-cause consensus has its own bounded, read-only coordinator. It is
    # investigating a stopped workflow, not performing a native business phase.
    # In particular, an empty usage context must not bind its parallel roles to
    # an ambient run and publish plan proofs (or consume that run's retry credit).
    if (request.purpose == 'diagnosis'
            and request.stage in {'self_repair_investigator', 'self_repair_reviewer', 'self_repair_arbiter'}
            and request.sandbox_mode == 'read-only'
            and not request.record_execution_incidents):
        from .policy import auxiliary_call
        return auxiliary_call(orchestrator, request, execute)
    # Test amendments likewise reserve their independent review in the
    # proof-review store. They must not become business reviews of the parent
    # collab task, nor manufacture implementation credit in a stopped search.
    if (request.stage == request.purpose == 'proof_review'
            and request.sandbox_mode == 'read-only'
            and request.usage_context.get('workflow_kind') == 'proof_review'
            and request.logical_call_id.startswith('proof-review:')):
        from .policy import auxiliary_call
        return auxiliary_call(orchestrator, request, execute)
    selected = context(orchestrator, request.usage_context)
    if selected is None: return execute(request)
    store, stream, root, kind, native, state = selected
    phase = ('acceptance' if 'acceptance' in request.purpose else 'review' if 'review' in request.purpose
             else 'implement' if request.purpose in {'fix', 'implement'}
             else 'route' if request.purpose == 'collab' else 'research' if kind == 'provider_resolve' else 'plan')
    attachment_refs = []
    for path in request.attachments:
        require(Path(path).is_file(),'input_unavailable','A required provider attachment is unavailable',path=str(path))
        attachment_refs.append(store.put_file(path))
    key = digest([request.attempt_id or str(request.output_path),str(request.prompt),request.purpose,
                  request.response_schema,attachment_refs])
    from ..prompting import append_context
    from .policy import automatic
    automatic(store, stream)
    if 'recovery' in store.load(stream):
        orchestrator._recovery_policy_active = True
        from .convergence import decision, scope
        from .policy import diagnosis_input, correction_context, materialize_observation
        domain = kind + ':' + native
        if domain not in store.load(stream)['tasks']:
            _contract(store, stream, root, kind, native, state, phase, digest([kind, native, phase, key]))
        snapshot = store.load(stream)
        materialize_observation(orchestrator.project_root, snapshot, domain, store)
        selected_action = decision(snapshot, domain, phase, _source(orchestrator, state))
        if phase == 'implement' and selected_action['action'] == 'diagnose':
            from ..prompting import compose_prompt
            for correction in range(2):
                snapshot = store.load(stream)
                text, schema = diagnosis_input(snapshot, domain, store)
                item = scope(snapshot, domain)
                prompt = compose_prompt([text], purpose='review')
                identity = digest([domain, item['latest'], item['diagnoses']])
                diagnostic = replace(request, prompt=prompt, prompt_spec=prompt.spec,
                    stage='diagnose', purpose='review', sandbox_mode='read-only', response_schema=schema, writer_boundary=None,
                    resume_session_id='', resume_provider='', record_execution_incidents=False,
                    attempt_id='recovery-diagnose:' + identity,
                    output_path=request.output_path.with_name('recovery-diagnose-' + identity + '.json'),
                    usage_context={**request.usage_context, 'kernel_owned': '1'})
                try:
                    perform(orchestrator, 'diagnose', identity, lambda: execute(diagnostic),
                            lambda result: provider_outcome(orchestrator, result),
                            usage=request.usage_context, model=True)
                    break
                except KernelError as error:
                    if error.code != 'protocol_invalid': raise
                    if correction:
                        raise KernelError('no_progress', 'Bounded diagnosis did not produce a valid correction',
                                          diagnosis_error=str(error)) from error
        if phase == 'implement':
            enriched = append_context(request.prompt, correction_context(store.load(stream), domain),
                                      'Controller recovery evidence')
            request = replace(request, prompt=enriched, prompt_spec=getattr(enriched, 'spec', None))
    elif store.load(stream)['budget']['diagnosis_due']:
        from ..prompting import compose_prompt
        failures = [c['outcome'] for c in sorted(store.load(stream)['commands'].values(),key=lambda c:c['sequence'])
                    if (c.get('outcome') or {}).get('kind') == 'candidate_rejected'][-2:]
        diagnosis_prompt = compose_prompt([
            'Reassess why the last two candidates did not satisfy the original task. Do not edit files. '
            'Identify a falsifiable cause and the smallest next implementation and verification step.',
            str(request.prompt), json.dumps(failures, ensure_ascii=False)], purpose='review')
        diagnosis_request = replace(request, prompt=diagnosis_prompt, prompt_spec=diagnosis_prompt.spec,
            purpose='review', sandbox_mode='read-only', attempt_id='diagnose:' + digest([stream,
                store.load(stream)['budget']['implementations'], len(store.load(stream)['budget']['progress'])]),
            usage_context={**request.usage_context,'kernel_owned':'1'},
            output_path=request.output_path.with_name(request.output_path.name + '.diagnosis'))
        diagnosis = perform(orchestrator, 'diagnose', diagnosis_request.attempt_id,
            lambda: execute(diagnosis_request),
            lambda r: provider_outcome(orchestrator,r),
            usage=request.usage_context, model=True)
        summary = diagnosis.get('summary', '') if isinstance(diagnosis, dict) else diagnosis.summary
        enriched = append_context(request.prompt, summary, 'Bounded recovery diagnosis')
        request = replace(request, prompt=enriched, prompt_spec=getattr(enriched, 'spec', None))
    def bound_call(contract):
        parent = store.load(stream)['tasks'][contract.parent_task]
        prompt = append_context(request.prompt, json.dumps({'contract_id': parent['contract_id'],
            'execution_contract': contract.identity, 'task_id': contract.parent_task,
            'goal': store.read(contract.goal_ref), 'issue': store.read(contract.issue_ref),
            'constraints': contract.constraints, 'required_checks': contract.required_checks}, ensure_ascii=False),
            'Controller task contract')
        bound = replace(request, prompt=prompt, prompt_spec=getattr(prompt, 'spec', None),
                        logical_call_id=request.logical_call_id or 'kernel:' + digest([stream, contract.task_id]),
                        usage_context={**request.usage_context,'kernel_owned':'1'})
        return execute(bound)
    result = perform(orchestrator, phase, key, bound_call,
        lambda r: provider_outcome(orchestrator,r), usage=request.usage_context, model=True, bind=True,
        read_only=request.sandbox_mode == 'read-only')
    if isinstance(result, dict):
        from ..models import AgentResult, AgentUsage, AgentTermination
        result = {**result, 'output_path': request.output_path,
            'usage': AgentUsage(**result['usage']) if result.get('usage') else None,
            'termination': AgentTermination(**result['termination']) if result.get('termination') else None}
        return AgentResult(**result)
    return result


def verification(owner, scope, execute):
    def classify(result):
        if result.get('ok'): return OutcomeKind.SUCCESS, 'Candidate verification completed'
        kind = result.get('failure_kind', '')
        if result.get('retry_fix') is True: return OutcomeKind.CANDIDATE_REJECTED, str(result.get('reason', 'Candidate rejected'))
        if kind == 'proof_review_required': return OutcomeKind.NEED_INPUT, 'Independent proof review required'
        return OutcomeKind.ENVIRONMENT_BLOCKED, str(result.get('reason', 'Verification could not run'))
    return perform(owner, 'verify', scope, execute, classify)


def delivery(owner, state, execute):
    if state.candidate_custody and owner.project_root != Path(state.candidate_custody['checkout']):
        return execute()
    receipt = state.candidate_custody.get('receipt', {})
    return perform(owner, 'deliver', receipt.get('fingerprint') or str(state.current_attempt), execute,
                   lambda result: (OutcomeKind.SUCCESS if result else OutcomeKind.EVIDENCE_INVALID,
                                   'Candidate delivered' if result else 'Candidate delivery failed'))


def acceptance(owner, state):
    from ..session_acceptance import completed
    if state.status != 'completed' or not state.acceptance_execution: return
    perform(owner,'acceptance',state.acceptance_execution['identity'],
        lambda: {'ok':completed(owner,state),'acceptance':state.acceptance_execution},
        lambda result: (OutcomeKind.SUCCESS if result['ok'] else OutcomeKind.EVIDENCE_INVALID,
                        'Business acceptance evidence checked'),completion=True)


def review_candidate(owner, state, verification):
    """Independent native fix review shares the writer's business contract."""
    selected = context(owner)
    if selected is None: return {'ok':True}
    store, stream, root, kind, native, _ = selected
    from ..models import AgentRequest
    from ..prompting import compose_prompt
    from ..repair_v2.scope import changes
    from ..repair_v2.controller import REVIEW_SCHEMA
    from .protocol import ReviewManifest
    receipt = state.candidate_custody.get('receipt') or {}
    require(receipt and verification.get('ok'),'review_evidence','Review requires a verified candidate')
    parent = store.load(stream)['tasks'][kind + ':' + native]
    business = Contract.read(parent['contract'])
    from .scope_amendments import pending, schema as scope_schema, approval as scope_approval
    scope_paths = pending(store.load(stream), business.task_id) if 'recovery' in store.load(stream) else []
    if 'recovery' in store.load(stream):
        from .policy import materialize_observation, observation_summary
        from .convergence import scope
        snapshot = store.load(stream)
        materialize_observation(owner.project_root, snapshot, business.task_id, store)
        verification = {key: value for key, value in verification.items()
                        if key not in {'verification_checks', 'progress_checks', 'baseline_failures'}}
        verification['observation'] = observation_summary(scope(snapshot, business.task_id))
        # A successful full suite can contain megabytes of pre-existing failure
        # traces in its prose reason. The complete executor observation remains
        # available at the sealed evidence path, without duplicating it in a
        # provider request that has its own context and transport limits.
        reason = verification.get('reason', '')
        if isinstance(reason, str) and len(reason) > 4000:
            verification['reason'] = verification['observation']['reason']
            verification['reason_full_evidence'] = verification['observation']['full_evidence']
    manifest = ReviewManifest(_source(owner,state),receipt['base_revision'],business.identity,
        business.required_checks,changes(owner.project_root,receipt['base_revision'], revision=receipt.get('source_revision')))
    text = ('Independently review the verified candidate against the original task contract. Do not modify files. '
        'Return decision, findings, coverage and change_coverage using the supplied schema. Reject only '
        'demonstrated violations and provide a concrete counterexample and check for each finding.\n' +
        json.dumps({'contract_id':business.identity,'candidate_revision':receipt.get('source_revision'),
            'goal':store.read(business.goal_ref),
            'issue':store.read(business.issue_ref),'required_checks':business.required_checks,
            'verification':verification},ensure_ascii=False) + '\n' + manifest.instruction())
    if receipt.get('source_revision'):
        text += ('\nDelivery uses the immutable candidate_revision, including newly added files. '
                 'The working index is restored after freezing; an untracked working file can still be in the '
                 'delivery tree. Inspect git show candidate_revision:path before claiming a file is omitted.')
    if scope_paths:
        text += ('\nThese paths exceeded the diagnostic plan but remain inside the original repository: '
                 + json.dumps(scope_paths) + '. Independently assess whether each is necessary for the original goal '
                 'and preserves its constraints. Approval requires scope_coverage with path, reason and concrete evidence '
                 'for every listed path; reject unrelated expansion. This does not authorize new requirements.')
    if verification.get('reason_full_evidence'):
        text += '\nThe verification reason was abbreviated. Read reason_full_evidence for the complete test output when needed.'
    original_review_text = text
    usage = {'workflow_kind':kind,'subject_id':native,'kernel_owned':'1'}
    previous = None
    for correction in range(2):
        key = receipt['fingerprint'] + (':format' if correction else '')
        prompt = compose_prompt([text],purpose='review')
        output = owner.project_root/'.auto-agents/recovery-reviews'/ (key + '.json')
        output.parent.mkdir(parents=True,exist_ok=True)
        request = AgentRequest('review','max',prompt,owner.project_root,output,
            purpose='review',sandbox_mode='read-only',response_schema=scope_schema(manifest.schema(REVIEW_SCHEMA), scope_paths),usage_context=usage)
        def execute():
            reply = owner.orch._call_with_failover_owned(request)
            raw = reply.summary or reply.stdout
            if not reply.ok:
                outcome, reason = provider_outcome(owner.orch,reply)
                from ..diagnostic_output import redact
                return {'kind':outcome.value,'reason':reason,'text':raw,
                        'provider_receipt': {'returncode':reply.returncode,
                            'termination':asdict(reply.termination) if reply.termination else None,
                            'usage':asdict(reply.usage) if reply.usage else None,
                            'cleanup_incomplete':reply.cleanup_incomplete,
                            'stdout_observed':bool(reply.stdout), 'summary_observed':bool(reply.summary),
                            'stderr_excerpt':redact(reply.stderr)[-4000:]}}
            try:
                result = manifest.validate(raw)
                require(result.get('decision') in {'APPROVE','REJECT'} and isinstance(result.get('findings'),list),
                        'protocol_invalid','Review decision and findings are required')
                findings = result['findings']
                require(all(isinstance(f,dict) and f.get('counterexample') and f.get('check') for f in findings),
                        'protocol_invalid','Review findings need executable counterexamples')
                approved = result['decision'] == 'APPROVE' and not findings
                if approved:
                    coverage = result.get('coverage',[])
                    require(isinstance(coverage,list) and len(coverage) == len(business.required_checks)
                            and {r.get('requirement') for r in coverage if isinstance(r,dict)} == set(business.required_checks)
                            and all(isinstance(r,dict) and r.get('nodes') for r in coverage),
                            'protocol_invalid','Approval needs evidence for every required check')
                else: require(bool(findings),'protocol_invalid','Rejection needs concrete findings')
                if 'recovery' in store.load(stream):
                    from ..repair_v2.controller import review_result
                    from ..repair_v2.types import RepairBlocked
                    try:
                        review_result(raw, manifest.source, set(manifest.requirements), manifest.changes)
                    except RepairBlocked as error:
                        raise KernelError('protocol_invalid', str(error)) from error
                if previous is not None:
                    require(manifest.same_judgment(previous, result),
                            'protocol_invalid','Format correction changed the substantive judgment')
                grant = scope_approval(result, scope_paths, manifest.source)
                return {'kind':'success' if approved else 'candidate_rejected','ok':approved,'review':result,'text':raw,
                        'review_requirements': list(business.required_checks),
                        **({'scope_approval': grant} if grant else {}),
                        'reason':'Independent candidate review completed' if approved else
                                 'Candidate review rejected: ' + json.dumps(findings,ensure_ascii=False)}
            except (KernelError,TypeError,ValueError) as error:
                return {'kind':'protocol_invalid','reason':str(error),'text':raw}
        try:
            return perform(owner,'review',key,execute,
                lambda result:(OutcomeKind(result['kind']),result['reason']),model=True)
        except KernelError as error:
            if error.code == 'candidate_rejected':
                return {'ok':False,'reason':str(error)}
            if error.code != 'protocol_invalid' or correction: raise
            command = store.load(stream)['commands'].get(error.details.get('command_id'))
            require(command is not None,'protocol_invalid','Review correction has no retained response')
            response = store.read(command['outcome']['details']['native_result'])['text']
            try: previous = json.loads(response)
            except ValueError: previous = None
            if not isinstance(previous,dict): previous = None
            text = original_review_text + '\n' + manifest.correction(command['command_id'],response,error)
    raise KernelError('protocol_invalid','Review correction exhausted')


def run_completion(owner, state):
    perform(owner,'acceptance',state.run_id,
        lambda: {'ok':state.status == 'completed' and not owner._verify_stage_failed(state),
                 'stages':state.stage_summaries,'approved_gates':state.approved_gates},
        lambda result: (OutcomeKind.SUCCESS if result['ok'] else OutcomeKind.EVIDENCE_INVALID,
                        'Run completion checked against retained stage results'),
        usage={'workflow_kind':'run','subject_id':state.run_id},completion=True)


def recovered_route(orchestrator, payload):
    """A cold CLI reads a durable resolved incident, not an ephemeral subscriber."""
    if getattr(orchestrator, 'project_root', None) is None: return None
    project = Path(getattr(orchestrator,'_kernel_project',None) or orchestrator.project_root).resolve()
    store = installed(project)
    if store is None: return None
    from ..execution_binding import repository_binding_error, route_sources
    operator = store.root/'operator.json'
    policy = json.loads(operator.read_text()) if operator.is_file() else {}
    source_root = Path(policy.get('source_root') or Path(__file__).resolve().parents[3])
    if (not any(s.get('target_repository') for s in route_sources(payload))
            or repository_binding_error(source_root,payload)):
        return False
    from ..repair_control import digest as route_digest
    expected = route_digest(payload)
    invocation = getattr(orchestrator,'_invocation_context',{}) or {}
    subject = getattr(orchestrator,'_kernel_subject',None)
    if not subject and invocation.get('session_id'): subject = 'session:' + invocation['session_id']
    if not subject and invocation.get('run_id'): subject = 'run:' + invocation['run_id']
    current = store.binding(project,subject) if subject else None
    if current is None: return False
    with store.connect() as db:
        states = [json.loads(r['snapshot']) for r in db.execute('SELECT snapshot FROM kernel_streams')]
    matches = [(state, row) for state in states if state['project'] == str(project) and state['workflow_id'] == current
               for row in state['incidents'].values() if row.get('route_digest') == expected and row['status'] == 'resolved']
    if not matches:
        from ..repair_client import EngineRepairRequired
        raise EngineRepairRequired(payload)
    state, incident = matches[-1]
    artifact = store.meta('active_runtime')
    require(artifact and artifact.get('source'), 'adoption_required', 'Current engine has no adoption proof')
    from .engine_adoption import adopted
    pending = any(row['incident_id'] == incident['incident_id'] and row['status'] == 'ready'
                  for row in state['continuations'].values())
    accepted = adopted(store, current, incident, artifact)
    if incident.get('payload_ref') and (pending or not accepted):
        from ..repair_client import EngineRepairRequired
        raise EngineRepairRequired(payload, resume_incident=(current, incident['incident_id']))
    require(accepted, 'adoption_required',
            'The resolved engine candidate still requires independent runtime adoption')
    from ..repair_client import _remember_engine_receipt
    return _remember_engine_receipt(orchestrator, payload, {'incident': incident['incident_id'],
        'resolution': incident['resolution_ref'], 'commit': artifact['commit'],
        'adoption': store.meta('activation_receipt'), 'route_digest': expected})
