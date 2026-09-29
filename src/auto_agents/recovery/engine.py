"""Kernel-owned engine repair using the existing isolated execution primitives.

The model can propose candidate code; only the kernel selects a next phase or
spends retry credit. Core adoption remains a separate release transaction.
"""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading

from .executor import Executor, FunctionExecutor
from .model import Command, Contract, Event, Evidence, KernelError, Outcome, OutcomeKind, digest, require


def previous_result(store, stream, task_id, phase):
    rows = [c for c in store.load(stream)['commands'].values() if c['task_id'] == task_id
            and c['phase'] == phase and c['status'] == 'finished' and c['outcome']['kind'] == 'success']
    latest = max(rows,key=lambda row:row['sequence']) if rows else None
    return store.read(latest['outcome']['details']['result_ref']) if latest else None


class EngineRunner:
    def __init__(self, store, stream, contract, effects, progress=None):
        self.store, self.stream, self.contract, self.effects = store, stream, contract, effects
        self.progress = progress

    def emit(self, kind, data, identity):
        return self.store.apply(self.stream, self.store.load(self.stream)['revision'], Event(identity, kind, data))

    def run(self, *, resume=False):
        self.emit('task_bound', {'contract':self.contract.to_dict()}, self.contract.task_id + ':bind')
        from .policy import automatic
        automatic(self.store, self.stream)
        initial = self.store.load(self.stream)['tasks'][self.contract.task_id]
        if resume and initial['status'] == 'blocked' and not initial['active_command'] and (initial.get('failure') or {}).get('kind') == 'environment_blocked':
            evidence = self.store.put({'environment':self.effects.environment,'failure':initial['failure'],'request':'explicit_resume'})
            self.emit('task_resumed',{'task_id':self.contract.task_id,'evidence_ref':evidence},
                      self.contract.task_id + ':retry:' + digest([evidence,self.store.load(self.stream)['revision']])[:32])
        while True:
            state = self.store.load(self.stream); task = state['tasks'][self.contract.task_id]
            if task['status'] == 'completed': return task
            if task['active_command']:
                command_id = task['active_command']
                current = state['commands'][command_id]
                if self.progress is not None:
                    self.progress.command(current, task)
                executor = Executor(self.store, {current['phase']:FunctionExecutor(self.effects.execute)})
                if current['status'] == 'reserved': executor.execute(self.stream, command_id)
                else: executor.reconcile(self.stream, command_id)
                task = self.store.load(self.stream)['tasks'][self.contract.task_id]
                if task['active_command']: return task
                continue
            if task['status'] == 'blocked':
                if self.contract.phases == ('diagnose',): return task
                failure = task.get('failure') or {}
                if 'recovery' in state and failure.get('kind') == 'candidate_rejected':
                    from .convergence import decision
                    selected = decision(state, self.contract.task_id, 'implement', self.effects.source())
                    if selected['allowed'] or selected['action'] == 'diagnose':
                        evidence = self.store.put(selected)
                        self.emit('task_resumed', {'task_id': self.contract.task_id, 'evidence_ref': evidence},
                                  self.contract.task_id + ':recovery:' + evidence)
                        continue
                    return task
                if failure.get('kind') == 'protocol_invalid':
                    prior = [c for c in state['commands'].values() if c['task_id'] == self.contract.task_id
                             and (c.get('outcome') or {}).get('kind') == 'protocol_invalid']
                    if len(prior) < 2:
                        evidence = self.store.put(failure)
                        self.emit('task_resumed', {'task_id':self.contract.task_id,'evidence_ref':evidence},
                                  self.contract.task_id + ':protocol:' + evidence)
                        continue
                if 'recovery' not in state and state['budget']['diagnosis_due']:
                    diagnosis = replace(self.contract, task_id=self.contract.task_id + ':diagnosis',
                        completion='phase_completed', phases=('diagnose',))
                    diagnostic = EngineRunner(self.store, self.stream, diagnosis, self.effects, self.progress).run()
                    if diagnostic['status'] != 'completed': return task
                    evidence = self.store.put(diagnostic['proofs'])
                    self.emit('task_resumed', {'task_id':self.contract.task_id,'evidence_ref':evidence},
                              self.contract.task_id + ':after-diagnosis:' + evidence)
                    continue
                return task
            phase = task['phase']
            if 'recovery' in state and phase == 'implement':
                from .convergence import decision, scope
                selected = decision(state, self.contract.task_id, phase, self.effects.source())
                if selected['action'] == 'diagnose':
                    item = scope(state, self.contract.task_id)
                    diagnosis = replace(self.contract,
                        task_id='diagnosis:' + digest([self.contract.task_id, item['latest'], item['diagnoses']])[:40],
                        parent_task=self.contract.task_id, completion='phase_completed', phases=('diagnose',))
                    result = EngineRunner(self.store, self.stream, diagnosis, self.effects, self.progress).run()
                    if result['status'] != 'completed': return result
                    continue
            number = sum(c['task_id'] == self.contract.task_id for c in state['commands'].values())
            identity = 'engine-command:' + digest([self.contract.task_id, phase, number])
            command = Command(identity, self.stream, self.contract.task_id, phase,
                self.effects.source(), self.contract.identity, self.effects.environment,
                self.store.meta('active_runtime')['source'], identity, phase in {'plan','implement','review','diagnose'})
            if 'recovery' in state:
                from .policy import reserve
                reserve(self.store, self.stream, command)
            else:
                self.emit('command_reserved', command.to_dict(), identity + ':reserve')


class IsolatedEngineEffects:
    def __init__(self, store, stream, contract, payload, root, source, progress=None):
        from ..config import load_project_config
        from ..repair_v2.docker import DockerVerifier
        from ..repair_v2.providers import AgentSandbox, NativeDriver
        from ..repair_v2.workspace import Workspace
        from ..repair_v2.migration import request_from_payload
        from ..repair_v2.scope import ScopeGuard
        from ..repair_v2.diagnostic_evidence import prepare as prepare_evidence
        self.store, self.stream, self.contract = store, stream, contract
        self.progress = progress
        self.payload, self.root, self.base = payload, Path(root), Path(source)
        self.cancel = threading.Event()
        self.accepted = request_from_payload(payload, contract.task_id)
        config = load_project_config(Path(payload['project']))
        provider = config.providers[payload['provider']]
        from .environment import prepare as prepare_environment
        trusted = (store.meta('trusted_verifier_runtime') or {}).get('path') or str(self.base)
        self.verifier = DockerVerifier(store.root/'kernel-verification',python=prepare_environment(store,trusted),
                                      codex_binary=provider.binary if provider.kind == 'codex' else None)
        if progress is not None:
            self.verifier.callback = progress.check
        self.verifier.prepare()
        self.environment = digest({'verifier':self.verifier.runtime, 'provider':payload['provider']})
        self.workspace = Workspace(self.root/'workspace', self.base, payload['base'])
        self.candidate = self.workspace.prepare()
        evidence, self.evidence_context = prepare_evidence(self.root, payload)
        provider.provider_name = payload['provider']
        self.driver = NativeDriver(provider, AgentSandbox(self.root/'provider-state',self.verifier.image,evidence=evidence),
            effort=config.efforts.get('self_repair','deep'), review_effort=config.efforts.get('self_repair_review','max'))
        self.scope = ScopeGuard(self.root, payload, self.root/'target-evidence', self.base)
        self.scope.import_receipt(payload.get('scope_receipt'))

    def source(self):
        from ..repair_v2.workspace import source_identity
        return source_identity(self.candidate)

    def observed(self, command, result, kind=OutcomeKind.SUCCESS, reason='Phase completed'):
        if command.phase == 'verify' and 'recovery' in self.store.load(self.stream):
            result = {**result, 'ok': kind == OutcomeKind.SUCCESS, 'reason': reason}
        reference = self.store.put(result)
        predicate = 'preflight_recovered' if command.phase == 'review' and kind == OutcomeKind.SUCCESS else 'phase_completed'
        proof = Evidence(reference, command.task_id, command.source, command.contract, command.environment,
            digest('isolated-engine-executor-v1'),command.phase,predicate)
        from .policy import result_details, parse_diagnosis
        extra = result_details(self.store, self.stream, command, result)
        if result.get('scope_approval'): extra['scope_approval'] = result['scope_approval']
        if result.get('scope_amendment_required'):
            extra.update(scope_amendment_required=result['scope_amendment_required'], native_result=reference,
                         post_source=result['artifact']['source'])
        if command.phase == 'diagnose' and 'recovery' in self.store.load(self.stream) and kind == OutcomeKind.SUCCESS:
            try:
                extra['recovery_diagnosis'] = parse_diagnosis(self.store.load(self.stream), command, result.get('text', ''))
            except KernelError as error:
                kind, reason = OutcomeKind.PROTOCOL_INVALID, str(error)
        return Outcome(kind, reason, (proof,) if kind == OutcomeKind.SUCCESS else (), {'result_ref':reference, **extra})

    def _previous(self, phase):
        return previous_result(self.store,self.stream,self.contract.task_id,phase)

    def phase(self, name):
        if getattr(self, 'progress', None) is not None:
            self.progress.phase(name)

    def agent_progress(self, event):
        if getattr(self, 'progress', None) is not None:
            self.progress.agent(event)

    def rejected(self, failures):
        if getattr(self, 'progress', None) is not None:
            self.progress.rejected(failures)

    def execute(self, command):
        from ..repair_v2.types import RepairBlocked
        try: return self._execute(command)
        except RepairBlocked as error:
            return self.observed(command, {'code':error.code,'reason':str(error)},
                OutcomeKind.NEED_INPUT if error.code.startswith('scope_') else OutcomeKind.ENVIRONMENT_BLOCKED, str(error))
        except KernelError as error:
            return self.observed(command, {'code':error.code,'reason':str(error)},
                OutcomeKind.OWNERSHIP_CONFLICT if error.code == 'read_only_violation' else OutcomeKind.EVIDENCE_INVALID,str(error))

    def _execute(self, command):
        from ..repair_v2.types import RepairBlocked
        from ..repair_v2.workspace import source_identity
        from ..repair_v2.runtime_artifact import build
        from ..repair_v2.scope import INSTRUCTION, proposal, changes
        from ..repair_v2.controller import REVIEW_SCHEMA, review_result
        from .protocol import ReviewManifest
        from .prompt_evidence import PromptEvidence, READ_INSTRUCTION, result_summary
        phase = command.phase
        state = self.store.load(self.stream)
        ordered = sorted(state['commands'].values(),key=lambda row:row['sequence'])
        failures = [c['outcome'] for c in ordered if c['task_id'] == self.contract.task_id
                    and c.get('outcome') and c['outcome']['kind'] != 'success'][-3:]
        if phase != 'verify':
            evidence = PromptEvidence(self.root / 'prompt-evidence', self.driver)
            observations = []
            for failure in failures:
                reference = failure.get('details',{}).get('result_ref')
                if not reference: continue
                observed = self.store.read(reference)
                observations.append({**failure, 'observed': evidence.section(
                    observed, summary=result_summary(observed))})
            context = READ_INSTRUCTION + evidence.render({
                'contract':self.contract.to_dict(), 'goal':self.store.read(self.contract.goal_ref),
                'issue':self.store.read(self.contract.issue_ref),'acceptance':[asdict(a) for a in self.accepted.acceptance],
                'read_only_evidence':self.evidence_context,'failures':observations})
        if phase in {'plan','diagnose','implement'}:
            if phase == 'diagnose' and 'recovery' in state:
                from .policy import diagnosis_input, materialize_observation
                materialize_observation(self.candidate, state, self.contract.task_id, self.store)
                text, schema = diagnosis_input(state, self.contract.task_id, self.store)
                before = self.source()
                reply = self.driver.run('plan', text, self.candidate, schema=schema, progress=self.agent_progress, cancel=self.cancel)
                require(self.source() == before, 'read_only_violation', 'Diagnosis changed protected source')
                return self.observed(command, asdict(reply),
                    OutcomeKind.SUCCESS if reply.ok else OutcomeKind.ENVIRONMENT_BLOCKED,
                    'Bounded diagnosis completed' if reply.ok else reply.error)
            prefix = ('Implement only the authorized repair and preserve all existing test obligations.' if phase == 'implement'
                else 'Inspect the original failure and propose a bounded, falsifiable repair plan. Do not modify files or run a broad test suite.')
            if phase == 'implement': require(self.scope.current(), 'scope_missing', 'Implementation requires retained necessity evidence')
            prompt = prefix + '\n' + context + '\nPlan context:\n' + evidence.render(self._previous('plan'))
            if phase == 'implement' and 'recovery' in state:
                from .policy import correction_context, materialize_observation
                materialize_observation(self.candidate, state, self.contract.task_id, self.store)
                prompt += '\nController recovery evidence:\n' + correction_context(state, self.contract.task_id)
            if phase != 'implement': prompt += '\n' + INSTRUCTION
            before = self.source()
            from .runtime_source import inventory
            before_paths = inventory(self.candidate) if phase == 'implement' and 'recovery' in state else None
            reply = self.driver.run('implement' if phase == 'implement' else 'plan',prompt,self.candidate,
                                    progress=self.agent_progress,cancel=self.cancel)
            after = self.source()
            outside = []
            if before_paths is not None:
                from .policy import correction_paths
                outside = correction_paths(state, self.contract.task_id, before_paths, inventory(self.candidate))
                from .scope_amendments import product_paths
                require(not outside or product_paths(outside), 'read_only_violation',
                        'Correction crossed a protected source boundary', paths=outside)
            if phase != 'implement': require(after == before, 'read_only_violation','Diagnosis changed protected source')
            if not reply.ok:
                return self.observed(command, asdict(reply),
                    OutcomeKind.OUTCOME_UNKNOWN if after != before else OutcomeKind.ENVIRONMENT_BLOCKED,
                    reply.error or 'Provider did not finish')
            if phase != 'implement': self.scope.admit(proposal(reply.text))
            else:
                self.phase('artifact')
                self.workspace.checkpoint()
                artifact = build(self.root, self.candidate, self.source(), {'verifier':self.verifier.runtime})
                result = {'reply':asdict(reply),'artifact':artifact, 'ok': True,
                          **({'scope_amendment_required': outside} if outside else {})}
                candidate_id = digest(artifact)
                task = self.store.load(self.stream)['tasks'][self.contract.task_id]
                parent = (task.get('candidate') or {}).get('candidate_id')
                self.store.apply(self.stream, self.store.load(self.stream)['revision'], Event(
                    command.command_id + ':candidate', 'candidate_retained', {'task_id':self.contract.task_id,
                    'candidate_id':candidate_id, 'source':artifact['source'],'base':self.payload['base'],
                    'receipt':self.store.put(result), 'parent':parent}))
                return self.observed(command, result)
            return self.observed(command, asdict(reply))
        implementation = self._previous('implement')
        require(implementation is not None, 'candidate_missing','Engine candidate has no durable implementation outcome')
        artifact = implementation['artifact']; snapshot = Path(artifact['path'])
        require(source_identity(snapshot) == command.source == self.source(), 'candidate_changed','Phase inputs no longer match the retained candidate')
        if phase == 'verify':
            from ..repair_v2.audit import test_protection_findings
            findings = test_protection_findings(snapshot, self.payload['base'], snapshot)
            if findings:
                self.rejected(findings)
                return self.observed(command, {'findings':findings},OutcomeKind.CANDIDATE_REJECTED,'Candidate weakens retained tests')
            self.phase('full_suite')
            units = self.verifier.suite_units(snapshot, self.accepted)
            suite = self.verifier.validate_suite(command.source,snapshot,units,self.cancel)
            if not suite.ok:
                self.rejected(suite.failures)
                from ..verification_dependencies import detect_verification_dependencies
                missing = [dependency.to_dict() for failure in suite.failures
                           for dependency in detect_verification_dependencies(failure.get('excerpt',''))]
                return self.observed(command,{**asdict(suite),'missing_dependencies':missing},
                    OutcomeKind.ENVIRONMENT_BLOCKED if suite.infrastructure or missing else OutcomeKind.CANDIDATE_REJECTED,
                    'Verification prerequisites are unavailable' if missing else 'Mandatory verification failed')
            from ..repair_v2.integration import _boundaries
            self.phase('boundary')
            boundary = _boundaries(self.verifier,self.root,command.source,snapshot,self.payload,self.cancel)
            if not boundary['ok']:
                self.rejected([{'unit': 'original-boundary', 'infrastructure': boundary.get('infrastructure', False)}])
            return self.observed(command, {'suite':asdict(suite),'boundary':boundary},
                OutcomeKind.SUCCESS if boundary['ok'] else OutcomeKind.CANDIDATE_REJECTED,'Original failure recovery checked')
        require(phase == 'review', 'phase','Unknown engine phase')
        verification = self._previous('verify')
        require(verification and verification['boundary']['ok'] and verification['suite']['snapshot'] == command.source,
                'verification_required','Review needs a current recovery proof')
        manifest = ReviewManifest(command.source,self.payload['base'],self.contract.identity,
            tuple(a.identity for a in self.accepted.acceptance),changes(snapshot,self.payload['base'],[]))
        from .scope_amendments import pending, schema as scope_schema, approval as scope_approval
        scope_paths = pending(state, self.contract.task_id) if 'recovery' in state else []
        prompt = ('Independently review this immutable candidate and concrete test/recovery evidence against every requirement. '
            'Do not modify source. Reject only demonstrated violations with a counterexample.\n' + context + '\n' +
            evidence.render(verification, summary=result_summary(verification)) + '\n' + evidence.render(manifest.instruction()))
        if scope_paths:
            prompt += ('\nIndependently review whether these unplanned source paths are necessary for the original goal: '
                       + json.dumps(scope_paths) + '. Approval requires scope_coverage entries with path, reason and evidence '
                       'for every path. Reject unrelated scope expansion.')
        original_review_prompt = prompt
        invalid = [c for c in ordered if c['task_id'] == self.contract.task_id
                   and (c.get('outcome') or {}).get('kind') == 'protocol_invalid']
        original = None
        if invalid:
            prior = self.store.read(invalid[-1]['outcome']['details']['result_ref'])
            prompt = READ_INSTRUCTION + '\nResponse protocol correction:\n' + evidence.render(
                manifest.correction(invalid[-1]['command_id'],prior['reply'],prior['diagnostic']))
            prompt = original_review_prompt + '\n' + prompt
            try: original = json.loads(prior['reply'])
            except ValueError: pass
        reply = self.driver.run('review',prompt,snapshot,schema=scope_schema(manifest.schema(REVIEW_SCHEMA), scope_paths),
                                progress=self.agent_progress,cancel=self.cancel)
        if not reply.ok: return self.observed(command,asdict(reply),OutcomeKind.ENVIRONMENT_BLOCKED,reply.error)
        try:
            parsed = manifest.validate(reply.text)
            reviewed = review_result(reply.text,command.source,set(manifest.requirements),manifest.changes)
            grant = scope_approval(parsed, scope_paths, command.source)
            if original is not None:
                require(isinstance(original,dict) and original.get('decision') == parsed['decision']
                        and original.get('findings') == reviewed.findings,
                        'review_changed','Protocol correction changed its substantive judgment')
        except (KernelError, ValueError, TypeError, RepairBlocked) as error:
            return self.observed(command, {'reply':reply.text,'diagnostic':str(error)},OutcomeKind.PROTOCOL_INVALID,'Review protocol does not match the manifest')
        if not reviewed.ok: self.rejected(reviewed.findings)
        return self.observed(command,{**asdict(reviewed), 'review_requirements': list(manifest.requirements),
                                     **({'scope_approval': grant} if grant else {})},
                             OutcomeKind.SUCCESS if reviewed.ok else OutcomeKind.CANDIDATE_REJECTED,'Independent review completed')


def deliver_runtime(store, base, candidate, progress):
    """Verify and adopt the exact delivered tree, including concurrent edits."""
    from .runtime_delivery import deliver
    from .runtime_manager import adopt_source, bound_source
    from .runtime_source import capture
    from . import runtime_lifecycle

    source = bound_source(store)
    progress.phase('deliver')
    delivery = deliver(store, source, base, Path(candidate['path']))
    expected = delivery['after'] if delivery else candidate['source']
    merged = None
    try:
        runtime = candidate
        if expected != candidate['source']:
            merged = capture(store, source, expected=expected)
            runtime = merged
        if store.meta('active_runtime')['source'] != runtime['source']:
            progress.phase('activation')
            adopt_source(store.root, runtime['path'])
        return store.meta('active_runtime')
    finally:
        if merged: runtime_lifecycle.release_produced(store, merged)


def submit(store, project, orchestrator, payload, args, run_lock):
    """Admit a concrete failure, repair privately, adopt independently, resume."""
    from ..reporting import find_reporter
    from .progress import RepairProgress
    reporter = getattr(orchestrator, 'reporter', None) or find_reporter(project)
    with RepairProgress(reporter, payload) as progress:
        return _submit(store, project, orchestrator, payload, args, run_lock, progress)


def _submit(store, project, orchestrator, payload, args, run_lock, progress):
    from ..repair_v2.migration import request_from_payload
    from ..root_cause import RootCauseCoordinator
    from ..repair_v2.store import atomic_json, Store as ArtifactStore
    from ..repair_v2.evidence import dissociate
    from ..repair_v2.diagnostic_replay import copy_submission_evidence
    from .native import context
    invocation = payload['invocation']
    native = invocation.get('session_id') or invocation.get('run_id')
    kind = invocation.get('command') or args.command
    kind = kind.replace('-','_')
    if kind not in {'collab','fix','provider_resolve'}: kind = 'run'
    selected = context(orchestrator, {'workflow_kind':kind,'subject_id':native})
    require(selected is not None, 'kernel_binding','Engine recovery requires an admitted original task')
    _, stream, root, kind, native, business = selected
    from ..repair_v2.scope import context as scope_context
    domain = (scope_context(Path(project),payload).get('incident') or {}).get('domain')
    require(domain not in {'product','environment','proof_review','protocol','reconciliation','ownership','evidence','budget','input','controller_state'},
            'failure_owner','Current blocker requires its own recovery path, not an engine implementation',domain=domain)
    accepted = request_from_payload(payload, 'engine-input')
    identity = 'engine:' + digest([stream,payload.get('symptom_key') or payload['fingerprint'],payload['contract']])[:40]
    task_id = identity + ':repair'
    incident_id = identity + ':incident'
    state = store.load(stream)
    existing = state['tasks'].get(task_id)
    # Check before preparing an isolated environment. Existing verification and
    # reconciliation must remain available even when model work is exhausted.
    if 'recovery' not in state and (existing is None or (existing['status'] == 'ready' and not existing['active_command']
                            and existing['phase'] in {'plan', 'implement', 'review', 'diagnose'})):
        from .model_progress import require_progress
        require_progress(state, existing['phase'] if existing else 'plan')
    working = store.root/'kernel-engine'/identity
    working.mkdir(parents=True,exist_ok=True)
    if not (working/'original-payload.json').exists():
        RootCauseCoordinator._copy_diagnostic_tree(Path(project),working/'target.preparing')
        copy_submission_evidence(Path(project),working/'target.preparing',payload)
        dissociate(working/'target.preparing')
        (working/'target.preparing').rename(working/'target-evidence')
        atomic_json(working/'original-payload.json',payload)
        from ..repair_v2.budget_recovery import anchors
        atomic_json(working/'budget-anchors.json',ArtifactStore(working).artifact('budget-anchors',anchors(working/'target-evidence',invocation)))
        from ..repair_v2.evidence import identity as evidence_identity
        atomic_json(working/'target.json',{'digest':evidence_identity(working/'target-evidence')})
    retained = json.loads((working/'original-payload.json').read_text())
    source = Path(store.meta('active_runtime')['path'])
    contract = Contract(store.load(stream)['goal_id'],task_id,'engine_repair',
        store.put({'goal':getattr(business,'goal','') or accepted.goal}),store.put(retained),
        ('Preserve original task and all existing verification obligations',),
        tuple(a.description for a in request_from_payload(retained,'engine-input').acceptance),
        (str(source),),store.put({'autonomy':retained['autonomy']}),'preflight_recovered',('plan','implement','verify','review'))
    existing = store.load(stream)['tasks'].get(task_id)
    if existing:
        contract = Contract.read(existing['contract'])
        source = Path(contract.source_scope[0])
    require(store.read(contract.issue_ref) == retained,'engine_evidence','Retained engine input changed')
    from ..repair_v2.evidence import identity as evidence_identity
    require((working/'target.json').is_file() and
            json.loads((working/'target.json').read_text())['digest'] == evidence_identity(working/'target-evidence'),
            'engine_evidence','Original engine counterexample changed')
    def emit(event_kind,data,suffix):
        return store.apply(stream,store.load(stream)['revision'],Event(identity + ':' + suffix,event_kind,data))
    emit('task_bound',{'contract':contract.to_dict()},'bind')
    state = store.load(stream)
    if incident_id not in state['incidents']:
        occurrence = store.put({'failure':retained['error'],'diagnosis':retained.get('diagnosis'),'boundary':retained['boundary']})
        emit('incident_opened', {'incident_id':incident_id,'task_id':task_id,'occurrence_ref':occurrence,
            'route_digest':retained['boundary'].get('route_digest',''),'payload_ref':contract.issue_ref,
            'failure':Outcome(OutcomeKind.ENGINE_DEFECT,retained['error'],details={'counterexample':occurrence}).to_dict()},'incident')
    require(retained['autonomy'] == 'max', 'engine_authorization','Automatic implementation is not authorized by the retained autonomy policy')
    result = store.load(stream)['tasks'][task_id]
    if result['status'] != 'completed':
        try:
            effects = IsolatedEngineEffects(store,stream,contract,retained,working,source,progress)
        except (OSError,RuntimeError,ValueError) as error:
            from ..repair_environment_log import sanitize
            detail = {'code':getattr(error,'code','environment_preparation'),'reason':sanitize(str(error))}
            reference = store.put(detail)
            failure = Outcome(OutcomeKind.NEED_INPUT if detail['code'].startswith('scope_') else OutcomeKind.ENVIRONMENT_BLOCKED,
                              detail['reason'],details={'observation_ref':reference})
            emit('task_preparation_blocked',{'task_id':task_id,'failure':failure.to_dict(),'observation_ref':reference},
                 'prepare-blocked:' + reference[:32])
            progress.blocked(failure.to_dict(), incident_id)
            if progress.reporter is None:
                print(json.dumps({'ok':False,'incident':incident_id,'failure':failure.to_dict()},ensure_ascii=False))
            return 3
        result = EngineRunner(store,stream,contract,effects,progress).run(resume=True)
    if result['status'] != 'completed':
        failure = result.get('failure') or {'kind':'outcome_unknown','reason':'Unsettled engine operation'}
        progress.blocked(failure, incident_id)
        if progress.reporter is None:
            print(json.dumps({'ok':False,'incident':incident_id,'failure':failure},ensure_ascii=False))
        return 3
    proof = result['proofs']['review'][0]
    implementation = previous_result(store,stream,contract.task_id,'implement')
    candidate = implementation['artifact']
    state = store.load(stream)
    continuation = identity + ':continue'
    if state['incidents'][incident_id]['status'] != 'resolved':
        next_task = kind + ':' + native
        require(next_task in state['tasks'],'continuation','Original business contract is unavailable')
        emit('incident_resolved',{'incident_id':incident_id,'proof':proof,'continuation_id':continuation,
            'next_task_id':next_task,'required_runtime':candidate['source']},'resolved')
    # The foreground is suspended at this boundary and holds no model command.
    # Release project custody before the independent all-project adoption.
    run_lock.release()
    from .runtime_manager import suspend_business, adoption_lock
    from . import runtime_lifecycle
    suspend_business(store)
    artifact = deliver_runtime(store, source, candidate, progress)
    state = store.load(stream)
    from .engine_adoption import record as record_adoption
    record_adoption(store, stream, state['incidents'][incident_id], source, candidate, artifact)
    emit('continuation_consumed',{'continuation_id':continuation,'task_id':kind + ':' + native,
        'operation':'resume:' + candidate['source']},'continued')
    from ..cli import _run_command_for_self_repair_resume
    with adoption_lock(store):
        runtime_lifecycle.register(store, artifact)
        runtime_token = runtime_lifecycle.acquire(store, artifact, 'business')
    command = _run_command_for_self_repair_resume(args,repo_root=Path(artifact['path']))
    environment = {**os.environ,'PYTHONPATH':str(Path(artifact['path'])/'src'),'AUTO_AGENTS_RECOVERY_CONTROL':str(store.root),
                   'AUTO_AGENTS_RUNTIME_USE':runtime_token,'AUTO_AGENTS_RUNTIME_ID':artifact['artifact_id']}
    for key in ('AUTO_AGENTS_RUN_LOCK_FD','AUTO_AGENTS_RUN_LOCK_KEY','AUTO_AGENTS_RUN_TOKEN','AUTO_AGENTS_REPAIR_SUBSCRIBER'):
        environment.pop(key,None)
    progress.handoff()
    os.execve(command[0],command,environment)
