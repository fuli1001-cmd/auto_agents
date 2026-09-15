"""One plan, one implementation conversation, independent snapshot acceptance."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import json
import threading
import time

from .store import digest
from .types import RepairBlocked, ReviewResult
from .workspace import source_identity


REVIEW_SCHEMA = {'type': 'object', 'properties': {
    'decision': {'type': 'string', 'enum': ['APPROVE', 'REJECT']},
    'coverage': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'requirement': {'type': 'string'}, 'nodes': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['requirement', 'nodes'], 'additionalProperties': False}},
    'findings': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'requirement': {'type': 'string'}, 'reason': {'type': 'string'},
        'counterexample': {'type': 'string'}, 'check': {'type': 'string'}},
        'required': ['requirement', 'reason', 'counterexample', 'check'], 'additionalProperties': False}}},
    'required': ['decision', 'findings', 'coverage'], 'additionalProperties': False}


def review_result(text, snapshot, requirements):
    """Local envelope handling; a malformed verdict is never approval."""
    value = text.strip()
    if value.startswith('```') and value.endswith('```'):
        value = '\n'.join(value.splitlines()[1:-1])
    try: result = json.loads(value)
    except ValueError as error: raise RepairBlocked('review_format', 'independent review lacks an unambiguous result') from error
    if not isinstance(result, dict):
        raise RepairBlocked('review_format', 'independent review must return an object')
    findings = result.get('findings')
    if result.get('decision') not in ('APPROVE', 'REJECT') or not isinstance(findings, list):
        raise RepairBlocked('review_format', 'independent review has an invalid result envelope')
    for row in findings:
        if (not isinstance(row, dict) or row.get('requirement') not in requirements
                or any(not isinstance(row.get(k), str) or not row[k].strip()
                       for k in ('reason', 'counterexample', 'check'))):
            raise RepairBlocked('review_format', 'blocking findings need a requirement, counterexample and check')
    if result['decision'] == 'REJECT' and not findings:
        raise RepairBlocked('review_format', 'rejection needs actionable findings')
    coverage = result.get('coverage', [])
    if result['decision'] == 'APPROVE' and not findings:
        if (not isinstance(coverage, list) or len(coverage) != len(requirements)
                or any(not isinstance(row, dict) or row.get('requirement') not in requirements
                       or not isinstance(row.get('nodes'), list) or not row['nodes']
                       or any(not isinstance(n, str) or not n.startswith('tests/') or '..' in n.split('/')
                              for n in row['nodes']) for row in coverage)
                or {row['requirement'] for row in coverage} != requirements):
            raise RepairBlocked('review_format', 'approval needs concrete test coverage for every requirement')
    return ReviewResult(result['decision'] == 'APPROVE' and not findings, snapshot, findings, text, coverage)


class Controller:
    def __init__(self, request, store, workspace, driver, verifier, *, units, max_stagnant=2, max_replans=1):
        self.request, self.store, self.workspace = request, store, workspace
        self.driver, self.verifier, self.units = driver, verifier, units
        self.max_stagnant, self.max_replans = max_stagnant, max_replans
        self.cancel = threading.Event()
        self.state_lock = threading.RLock()

    def checkpoint(self, **updates):
        with self.state_lock:
            self.state.update(updates)
            self.store.save(self.state)

    def phase(self, name):
        self.checkpoint(phase=name)
        self.store.event('phase_started', phase=name)

    def agent(self, role, prompt, root, *, schema=None):
        if self.cancel.is_set(): raise KeyboardInterrupt()
        before = source_identity(root)
        with self.state_lock:
            call = self.state['calls'] + 1
            self.state['calls'] = call
            self.store.save(self.state)
        reference = self.store.artifact('agent-input', {'role': role, 'prompt': prompt, 'source': before})
        self.store.event('agent_started', role=role, call=call, input=reference)
        started = time.monotonic()

        def progress(event):
            if event.get('session'):
                with self.state_lock:
                    if self.state['sessions'].get(role) != event['session']:
                        self.state['sessions'][role] = event['session']
                        self.store.save(self.state)
            if event.get('event', '').endswith(('/delta', '.delta')):
                return  # Native transcripts hold deltas; journal records transitions.
            self.store.event('agent_progress', role=role, **event)

        session = self.state['sessions'].get(role, '')
        if role == 'implement': session = session or self.state['sessions'].get('plan', '')
        reply = self.driver.run(role, prompt, root, session=session, schema=schema,
                                progress=progress, cancel=self.cancel)
        with self.state_lock:
            if reply.session: self.state['sessions'][role] = reply.session
            self.store.save(self.state)
        self.store.event('agent_finished', role=role, call=call, seconds=time.monotonic() - started,
                         ok=reply.ok, usage=reply.usage)
        self.store.artifact('agent-output', asdict(reply))
        if role != 'implement' and source_identity(root) != before:
            raise RepairBlocked('read_only_violation', role + ' modified protected source')
        if reply.interrupted or self.cancel.is_set(): raise KeyboardInterrupt()
        if not reply.ok: raise RepairBlocked('provider_failed', reply.error or 'provider did not complete the turn')
        return reply

    def context(self):
        return json.dumps({'goal': self.request.goal,
            'requirements': [asdict(item) for item in self.request.acceptance],
            'evidence': self.request.evidence}, ensure_ascii=False)

    def plan(self, root, *, rediagnose=False):
        self.phase('plan')
        prompt = ('Produce one implementation plan for the complete repair. Inspect the source and evidence. '
            'Return Markdown with root causes, proposed changes and coverage of each requirement. '
            'Do not edit source, execute a test suite or create separately approved component groups. '
            'Internal steps are only an implementation checklist. Preserve the requested scope and all mandatory tests. '
            'Use only supplied authorization; resolve implementation details from the repository.\n' + self.context())
        if rediagnose:
            prompt += '\nTwo implementations made no verified progress. Reconsider the root cause using these concrete failures:\n'
            prompt += json.dumps(self.state['failures'], ensure_ascii=False)
        reply = self.agent('plan', prompt, root)
        if not reply.text.strip(): raise RepairBlocked('plan_missing', 'provider returned no implementation plan')
        reference = self.store.artifact('plan', {'text': reply.text, 'request': self.state['request_digest']})
        self.checkpoint(plan=reference, phase='implement')

    def implement(self, root):
        self.phase('implement')
        prompt = ('Implement the complete repair plan in this private candidate. Resolve cross-module issues together. '
            'Preserve unrelated work and existing tests. Do not modify Git metadata or weaken verification. '
            'Use small diagnostics only when necessary; the controller runs formal acceptance after this turn. '
            'A previous passing check is not permission to skip a changed requirement. Finish with a concise change summary.\n'
            + self.context() + '\nPLAN:\n' + self.store.read(self.state['plan'])['text'])
        if self.state['failures']:
            prompt += '\nCorrect these current failures without rebuilding unchanged planning history:\n'
            prompt += json.dumps(self.state['failures'], ensure_ascii=False)
        self.agent('implement', prompt, root)
        from .audit import protect_tests
        protect_tests(self.workspace.source, self.request.engine_base, root)
        identity, snapshot = self.workspace.freeze()
        self.checkpoint(snapshot=identity, snapshot_path=str(snapshot), attempts=self.state['attempts'] + 1,
                        phase='validate')

    def review(self, identity, snapshot):
        prompt = ('Independently review this immutable candidate against every frozen requirement and the original baseline. '
            'Inspect the diff, relevant source and coverage. Do not modify files or run a broad test suite. '
            'Block only demonstrated violations or introduced regressions, with a concrete counterexample and check. '
            'Editorial preferences and unrelated improvements are not blockers. '
            'Return JSON {decision:APPROVE|REJECT, findings:[{requirement,reason,counterexample,check}], '
            'coverage:[{requirement,nodes:["tests/test_file.py::test_behavior"]}]}. '
            'An approval maps every requirement to actual behavioral tests; generic passing tests are not coverage. '
            'Every finding must name one supplied requirement identity. APPROVE requires no findings.\n'
            + self.context() + '\nOriginal baseline: ' + self.request.engine_base
            + '\nPlan:\n' + self.store.read(self.state['plan'])['text'])
        if self.state.get('review'):
            prompt += '\nPrevious independent review (recheck the current delta):\n' + json.dumps(
                self.store.read(self.state['review']), ensure_ascii=False)
        reply = self.agent('review', prompt, snapshot, schema=REVIEW_SCHEMA)
        try: result = review_result(reply.text, identity, {r.identity for r in self.request.acceptance})
        except RepairBlocked as error:
            if self.state.get('review_format_retries', 0) >= 1: raise
            original = reply.text.strip()
            if original.startswith('```') and original.endswith('```'):
                original = '\n'.join(original.splitlines()[1:-1])
            try: original = json.loads(original)
            except ValueError: raise error
            if (not isinstance(original, dict) or original.get('decision') not in ('APPROVE', 'REJECT')
                    or not isinstance(original.get('findings'), list)):
                raise error
            self.checkpoint(review_format_retries=1)
            reply = self.agent('review', 'Correct only the result envelope of the previous review; preserve all '
                'substantive decisions and evidence. Return the requested JSON. Problem: ' + str(error)
                + '\nOriginal response:\n' + reply.text, snapshot, schema=REVIEW_SCHEMA)
            result = review_result(reply.text, identity, {r.identity for r in self.request.acceptance})
            if ((result.ok and original['decision'] != 'APPROVE')
                    or digest(result.findings) != digest(original['findings'])):
                raise RepairBlocked('review_semantics_changed',
                    'format correction changed the independent verdict or blocking evidence')
        self.checkpoint(review=self.store.artifact('review', asdict(result)))
        return result

    def validate(self):
        from pathlib import Path
        identity, snapshot = self.state['snapshot'], Path(self.state['snapshot_path'])
        if source_identity(snapshot) != identity:
            raise RepairBlocked('snapshot_changed', 'verification snapshot no longer matches its checkpoint')
        self.phase('validate')
        self.checkpoint(verification_runtime=getattr(self.verifier, 'runtime', ''))
        with ThreadPoolExecutor(max_workers=2) as pool:
            tests = pool.submit(self.verifier.validate, identity, snapshot, self.units(snapshot), self.cancel)
            reviewed = pool.submit(self.review, identity, snapshot)
            try:
                # Observe either side's infrastructure failure immediately;
                # waiting on tests first can leave a failed reviewer unnoticed.
                for completed in as_completed((tests, reviewed)):
                    completed.result()
                validation, review = tests.result(), reviewed.result()
            except BaseException:
                self.cancel.set()
                raise
        if validation.snapshot != identity or review.snapshot != identity or source_identity(snapshot) != identity:
            raise RepairBlocked('snapshot_changed', 'acceptance evidence belongs to a different source snapshot')
        if validation.cancelled:
            self.checkpoint(validation=self.store.artifact('validation', asdict(validation)))
            raise KeyboardInterrupt()
        if validation.infrastructure:
            self.checkpoint(validation=self.store.artifact('validation', asdict(validation)))
            raise RepairBlocked('verification_infrastructure', json.dumps(validation.failures, ensure_ascii=False))
        executed = {node for check in validation.checks for node in check.get('passed', [])}
        missing = [node for row in review.coverage for node in row['nodes'] if not any(
            actual == node or actual.startswith(node + '[') or actual.startswith(node + '::')
            for actual in executed)] if review.ok else []
        if missing:
            validation.ok = False
            validation.failures.append({'reason': 'reviewed behavioral coverage was not executed', 'missing': missing})
        proof = self.store.artifact('validation', asdict(validation))
        self.checkpoint(validation=proof)
        if validation.ok and review.ok:
            self.checkpoint(status='ready', phase='deliver', failures=[],
                receipt=self.store.artifact('acceptance', {'request': self.state['request_digest'],
                    'snapshot': identity, 'validation': proof, 'review': self.state['review']}))
            return True
        failures = [*validation.failures, *review.findings]
        if not failures: raise RepairBlocked('invalid_validation', 'incomplete acceptance has no actionable failure')
        old = {digest(f) for f in self.state['failures']}
        current = {digest(f) for f in failures}
        progressed = bool(old and current < old)
        self.checkpoint(failures=failures, stagnant=0 if progressed else self.state['stagnant'] + 1,
                        phase='implement')
        self.store.event('acceptance_failed', snapshot=identity, failures=failures, progressed=progressed)
        return False

    def run(self):
        with self.store.locked():
            self.state = self.store.load() or {'version': 2, 'request_digest': digest(self.request.to_dict()),
                'status': 'active', 'phase': 'prepare', 'plan': None, 'sessions': {}, 'attempts': 0,
                'calls': 0, 'failures': [], 'stagnant': 0, 'replans': 0}
            if self.state['request_digest'] != digest(self.request.to_dict()):
                raise RepairBlocked('request_changed', 'resume request differs from the frozen repair contract')
            if self.state['status'] in ('ready', 'complete'):
                from pathlib import Path
                saved = self.store.read(self.state['receipt'])
                validation, review = self.store.read(saved['validation']), self.store.read(saved['review'])
                if (saved['request'] != self.state['request_digest']
                        or saved['snapshot'] != self.state['snapshot']
                        or not validation['ok'] or not review['ok'] or review['findings']
                        or source_identity(Path(self.state['snapshot_path'])) != saved['snapshot']):
                    raise RepairBlocked('invalid_acceptance', 'saved acceptance or source no longer matches')
                self.verifier.prepare()
                if self.state.get('verification_runtime', '') != getattr(self.verifier, 'runtime', ''):
                    self.checkpoint(status='active', phase='validate')
                else: return self.state
            # An unchanged failure does not acquire a new recovery allowance on restart.
            if self.state['status'] == 'blocked': return self.state
            self.checkpoint(status='active')
            try:
                root = self.workspace.prepare()
                self.verifier.prepare()
                if not self.state.get('plan'): self.plan(root)
                while True:
                    if self.cancel.is_set(): raise KeyboardInterrupt()
                    if self.state['phase'] != 'validate':
                        if self.state['stagnant'] >= self.max_stagnant:
                            if self.state['replans'] >= self.max_replans:
                                raise RepairBlocked('no_progress', 'repair made no verified progress after its bounded rediagnosis')
                            self.checkpoint(replans=self.state['replans'] + 1, stagnant=0)
                            self.plan(root, rediagnose=True)
                        self.implement(root)
                    if self.validate(): return self.state
            except KeyboardInterrupt:
                self.cancel.set()
                self.checkpoint(status='stopped')
                self.store.event('stopped', phase=self.state['phase'])
                raise
            except RepairBlocked as error:
                self.checkpoint(status='blocked', blocker={'code': error.code, 'message': str(error)})
                self.store.event('blocked', **self.state['blocker'])
                return self.state
