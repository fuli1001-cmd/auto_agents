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


def perform(owner, phase, key, function, classify, *, usage=None, model=False, bind=False, completion=False):
    selected = context(owner, usage)
    if selected is None: return function()
    store, stream, root, kind, native, state = selected
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
    budget = store.load(stream)['budget']
    require(not model or not any(c['status'] in {'running', 'unknown','reserved'} and c['command_id'] != prior_id
                                for c in store.load(stream)['commands'].values()),
            'outcome_unknown', 'An unconfirmed operation must be reconciled before another model call')
    require(not model or budget['stagnant'] < 2 or phase == 'diagnose' and budget['diagnosis_due'],
            'no_progress', 'No verified progress; preserve the candidate before another model call')
    contract = _contract(store, stream, root, kind, native, state, phase, operation_key)
    if kind == 'fix' and phase == 'deliver':
        parent = store.load(stream)['tasks'][contract.parent_task]
        require(all(parent['proofs'].get(stage) and all(p['source'] == source for p in parent['proofs'][stage])
                    for stage in ('verify','review')), 'delivery_evidence', 'Delivery requires verification and review of the same candidate')
    command = Command('cmd:' + operation_key, stream, contract.task_id, phase, source, contract.identity,
                      environment, runtime['source'], operation_key, model_call=model)
    _apply(store, stream, 'command_reserved', command.to_dict(), command.command_id)
    captured = []
    def execute(_command):
        result = function(contract) if bind else function(); captured.append(result)
        plain = result if isinstance(result, (dict, list, str, int, float, bool)) or result is None else asdict(result)
        # Native AgentResult contains an output Path, but no executable callbacks.
        if isinstance(plain, dict) and isinstance(plain.get('output_path'), Path): plain['output_path'] = str(plain['output_path'])
        reference = store.put(plain)
        verdict, reason = classify(result)
        if model and phase == 'review' and _source(owner, state) != source:
            verdict, reason = OutcomeKind.OWNERSHIP_CONFLICT, 'Read-only review changed candidate inputs'
        parent = store.load(stream)['tasks'][contract.parent_task]
        predicate = parent['contract']['completion'] if not model and (phase == 'deliver' or completion) else 'phase_completed'
        require(not completion or phase == parent['contract']['phases'][-1],
                'completion_boundary','Completion observation belongs to another stage')
        proof = Evidence(store.put({'command': command.command_id, 'result': reference}), contract.task_id,
                         source, contract.identity, environment, digest(['native-executor-v1',runtime['source']]), phase, predicate)
        evidence = (proof,) if predicate == 'phase_completed' else (proof, replace(proof, predicate='phase_completed'))
        return Outcome(verdict, reason, evidence if verdict == OutcomeKind.SUCCESS else (),
                       {'native_result': reference, 'subject': native, 'post_source': _source(owner, state)})
    executor = Executor(store, {phase: FunctionExecutor(execute)}, owner='native:' + str(os.getpid()) + ':' + uuid4().hex)
    outcome = executor.execute(stream, command.command_id)
    if model and outcome.kind != OutcomeKind.SUCCESS:
        raise KernelError(outcome.kind.value, outcome.reason, command_id=command.command_id)
    return captured[0]


def provider_outcome(orchestrator, result):
    reason = getattr(result.termination,'reason','') if result.termination is not None else ''
    uncertain = result.cleanup_incomplete or (not result.ok and
        (reason and reason not in {'execution_budget_exhausted','verification_environment_blocked'} or
         result.returncode < 0 and not reason))
    if not result.ok and orchestrator._failover_error_category(result) == 'connection': uncertain = True
    return (OutcomeKind.OUTCOME_UNKNOWN if uncertain else OutcomeKind.SUCCESS if result.ok else OutcomeKind.ENVIRONMENT_BLOCKED,
            'Provider outcome requires reconciliation' if uncertain else 'Provider response received' if result.ok else 'Provider call failed')


def provider(orchestrator, request, execute):
    selected = context(orchestrator, request.usage_context)
    if selected is None: return execute(request)
    store, stream, root, kind, native, state = selected
    phase = ('review' if 'review' in request.purpose else 'acceptance' if 'acceptance' in request.purpose
             else 'implement' if request.purpose in {'fix', 'implement'}
             else 'route' if request.purpose == 'collab' else 'research' if kind == 'provider_resolve' else 'plan')
    attachment_refs = []
    for path in request.attachments:
        require(Path(path).is_file(),'input_unavailable','A required provider attachment is unavailable',path=str(path))
        attachment_refs.append(store.put_file(path))
    key = digest([request.attempt_id or str(request.output_path),str(request.prompt),request.purpose,
                  request.response_schema,attachment_refs])
    from ..prompting import append_context
    if store.load(stream)['budget']['diagnosis_due']:
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
        lambda r: provider_outcome(orchestrator,r), usage=request.usage_context, model=True, bind=True)
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
    manifest = ReviewManifest(_source(owner,state),receipt['base_revision'],business.identity,
        business.required_checks,changes(owner.project_root,receipt['base_revision']))
    text = ('Independently review the verified candidate against the original task contract. Do not modify files. '
        'Return decision, findings, coverage and change_coverage using the supplied schema. Reject only '
        'demonstrated violations and provide a concrete counterexample and check for each finding.\n' +
        json.dumps({'contract_id':business.identity,'goal':store.read(business.goal_ref),
            'issue':store.read(business.issue_ref),'required_checks':business.required_checks,
            'verification':verification},ensure_ascii=False) + '\n' + manifest.instruction())
    usage = {'workflow_kind':kind,'subject_id':native,'kernel_owned':'1'}
    previous = None
    for correction in range(2):
        key = receipt['fingerprint'] + (':format' if correction else '')
        prompt = compose_prompt([text],purpose='review')
        output = owner.project_root/'.auto-agents/recovery-reviews'/ (key + '.json')
        output.parent.mkdir(parents=True,exist_ok=True)
        request = AgentRequest('review','max',prompt,owner.project_root,output,
            purpose='review',sandbox_mode='read-only',response_schema=manifest.schema(REVIEW_SCHEMA),usage_context=usage)
        def execute():
            reply = owner.orch._call_with_failover_owned(request)
            raw = reply.summary or reply.stdout
            if not reply.ok:
                outcome, reason = provider_outcome(owner.orch,reply)
                return {'kind':outcome.value,'reason':reason,'text':raw}
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
                if previous is not None:
                    require(previous.get('decision') == result['decision'] and previous.get('findings') == findings,
                            'protocol_invalid','Format correction changed the substantive judgment')
                return {'kind':'success' if approved else 'candidate_rejected','ok':approved,'review':result,'text':raw,
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
            text = manifest.correction(command['command_id'],response,error)
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
    require(not incident.get('required_runtime') or incident['required_runtime'] == artifact['source'],
            'adoption_required', 'The resolved engine candidate still requires independent runtime adoption')
    from ..repair_client import _remember_engine_receipt
    return _remember_engine_receipt(orchestrator, payload, {'incident': incident['incident_id'],
        'resolution': incident['resolution_ref'], 'commit': artifact['commit'],
        'adoption': store.meta('activation_receipt'), 'route_digest': expected})
