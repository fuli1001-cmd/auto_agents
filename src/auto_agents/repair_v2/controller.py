"""One plan, one implementation conversation, independent snapshot acceptance."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import json
import threading
import time

from .store import digest
from .types import Cancellation, RepairBlocked, ReviewResult
from .workspace import source_identity
from .feedback import assess_progress, diagnose, diagnostic_units, retain_unchecked


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
    def __init__(self, request, store, workspace, driver, verifier, *, units, max_stagnant=2, max_replans=1, boundary=None, resume_token='', allow_implementation=True, regression=None):
        self.request, self.store, self.workspace = request, store, workspace
        self.driver, self.verifier, self.units = driver, verifier, units
        self.max_stagnant, self.max_replans = max_stagnant, max_replans
        self.regression = regression
        self.allow_implementation = allow_implementation
        self.boundary = boundary
        self.resume_token = resume_token
        self.cancel = threading.Event()
        self.state_lock = threading.RLock()

    def checkpoint(self, **updates):
        with self.state_lock:
            self.state.update(updates)
            self.store.save(self.state)

    def phase(self, name):
        self.checkpoint(phase=name)
        self.store.event('phase_started', phase=name, attempt=self.state.get('attempts', 0))

    def agent(self, role, prompt, root, *, schema=None, cancel=None, fallback_prompt=None):
        if self.cancel.is_set(): raise KeyboardInterrupt()
        before = source_identity(root)
        with self.state_lock:
            call = self.state['calls'] + 1
            self.state['calls'] = call
            self.store.save(self.state)
        reference = self.store.artifact('agent-input', {'role': role, 'prompt': prompt, 'source': before})
        self.checkpoint(active_call={'call': call, 'role': role, 'source': before, 'input': reference})
        self.store.event('agent_started', role=role, call=call, input=reference)
        started = time.monotonic()

        def progress(event):
            if event.get('session'):
                with self.state_lock:
                    if self.state['sessions'].get(role) != event['session']:
                        self.state['sessions'][role] = event['session']
                        self.store.save(self.state)
            if event.get('event', '').lower().endswith('delta'):
                return  # Native transcripts hold deltas; journal records transitions.
            self.store.event('agent_progress', role=role, **event)

        session = self.state['sessions'].get(role, '')
        if role == 'implement': session = session or self.state['sessions'].get('plan', '')
        reply = self.driver.run(role, prompt, root, session=session, schema=schema,
                                progress=progress, cancel=cancel or self.cancel)
        with self.state_lock:
            if reply.session: self.state['sessions'][role] = reply.session
            self.store.save(self.state)
        self.store.event('agent_finished', role=role, call=call, seconds=time.monotonic() - started,
                         ok=reply.ok, usage=reply.usage)
        self.store.artifact('agent-output', asdict(reply))
        if role != 'implement' and source_identity(root) != before:
            raise RepairBlocked('read_only_violation', role + ' modified protected source')
        if self.cancel.is_set(): raise KeyboardInterrupt()
        if reply.interrupted or cancel is not None and cancel.is_set():
            if cancel is not None: raise InterruptedError('acceptance cancelled after a concrete failure')
            raise KeyboardInterrupt()
        if reply.missing_session and session:
            recovered = dict(self.state.get('session_recoveries', {}))
            if recovered.get(role, 0) < 1:
                recovered[role] = 1
                sessions = dict(self.state['sessions'])
                sessions.pop(role, None)
                if role in ('plan', 'implement'):
                    sessions.pop('plan', None); sessions.pop('implement', None)
                self.checkpoint(sessions=sessions, session_recoveries=recovered)
                self.store.event('native_session_recovered', role=role, old_session=session)
                return self.agent(role, fallback_prompt or prompt, root, schema=schema, cancel=cancel)
        if reply.timed_out and role == 'implement' and source_identity(root) != before:
            # A model's final message is not acceptance evidence. A stopped
            # time slice can submit its actual edits to the same mandatory
            # audit/tests/review; partial code never becomes approved here.
            self.checkpoint(implementation_timebox={'call': call, 'source_before': before,
                                                   'source_after': source_identity(root)})
            self.store.event('implementation_timeboxed', **self.state['implementation_timebox'])
            return reply
        if not reply.ok:
            code = 'provider_timeout' if reply.timed_out else 'provider_failed'
            raise RepairBlocked(code, reply.error or 'provider did not complete the turn')
        return reply

    def context(self):
        return json.dumps({'goal': self.request.goal,
            'requirements': [asdict(item) for item in self.request.acceptance],
            'evidence': self.request.evidence,
            'test_preservation_findings': self.state.get('test_preservation_findings', []),
            'failure_diagnosis': diagnose(self.state.get('failures', []))}, ensure_ascii=False)

    def plan(self, root, *, rediagnose=False):
        self.phase('plan')
        prompt = ('Produce one implementation plan for the complete repair. Inspect the source and evidence. '
            'Return Markdown with root causes, proposed changes and coverage of each requirement. '
            'Do not edit source, execute a test suite or create separately approved component groups. '
            'Internal steps are only an implementation checklist. Preserve the requested scope and all mandatory tests. '
            'Use only supplied authorization; resolve implementation details from the repository.\n' + self.context())
        from .context import source_context
        prompt += '\nCURRENT SOURCE:\n' + source_context(root, self.request.engine_base)
        prompt += ('\nUse the supplied source directly when sufficient; avoid repeating discovery or reading the same files. '
                   'Keep the plan concise, with concrete changes and acceptance coverage; do not restate the request.')
        if rediagnose:
            prompt += '\nTwo implementations made no verified progress. Reconsider the root cause using these concrete failures:\n'
            prompt += json.dumps(self.state['failures'], ensure_ascii=False)
        prompt += ('\nPreserve each original test entry, parameter case and assertion. Conjunctive strengthening '
                   'and literal parameter extensions retaining the old cases are supported. For other test '
                   'refactors, keep the original checks explicit and add separate new cases.')
        prompt += ('\nGrouped failures share an observed symptom, not necessarily a root cause. '
                   'Use a representative to distinguish candidate regressions, test/contract conflicts, '
                   'and environment failures before expanding the change. Tie any claimed conflict to '
                   'a frozen requirement; do not rewrite expected results merely to obtain a pass.')
        reply = self.agent('plan', prompt, root)
        if not reply.text.strip(): raise RepairBlocked('plan_missing', 'provider returned no implementation plan')
        reference = self.store.artifact('plan', {'text': reply.text, 'request': self.state['request_digest']})
        self.checkpoint(plan=reference, phase='implement')

    def implement(self, root):
        self.phase('implement')
        prompt = ('Implement the complete repair plan in this private candidate. Resolve cross-module issues together. '
            'Preserve unrelated work and existing tests. Do not modify Git metadata or weaken verification. '
            'Use small diagnostics only when necessary; the controller runs formal acceptance after this turn. '
            'Prioritize a testable correction for the supplied failures before exploring additional variants. '
            'Diagnose shared symptoms with their representative nodes first, then check all affected cases. '
            'Resolve behavior against the frozen requirements while preserving the original assertions. '
            'Use the image-provided python for diagnostics. At the execution deadline the controller may '
            'submit partial edits to formal acceptance, so keep changes coherent as you work. '
            'A previous passing check is not permission to skip a changed requirement. Finish with a concise change summary.\n'
            + self.context() + '\nPLAN:\n' + self.store.read(self.state['plan'])['text'])
        if self.state['failures']:
            prompt += '\nCorrect these current failures without rebuilding unchanged planning history:\n'
            prompt += json.dumps(self.state['failures'], ensure_ascii=False)
        self.agent('implement', prompt, root)
        self.workspace.checkpoint()
        # A completed implementation is durable even if its local audit fails.
        # Restarting at this boundary must not repeat the model turn.
        self.checkpoint(attempts=self.state['attempts'] + 1, phase='audit')

    def test_findings(self, root):
        from .audit import test_protection_findings
        from .workspace import git
        import subprocess
        findings = test_protection_findings(self.workspace.source, self.request.engine_base, root)
        for parent in self.state.get('integration_parents', []):
            # A lagging upstream is already represented by the frozen base.
            # Auditing its historical assertions again would reject test changes
            # that predate this repair. Never use candidate HEAD for this check:
            # new upstream tests remain protected even after their merge.
            try:
                git(root, 'merge-base', '--is-ancestor', parent, self.request.engine_base)
            except subprocess.CalledProcessError as error:
                if error.returncode != 1:
                    raise
            else:
                continue
            findings.extend(test_protection_findings(root, parent, root))
        return findings

    def audit(self, root):
        self.phase('audit')
        findings = self.test_findings(root)
        identity = source_identity(root)
        self.checkpoint(test_audit=self.store.artifact('test-audit', {'source': identity, 'findings': findings}),
                        test_preservation_findings=findings)
        if findings:
            self.record_failures(findings, identity)
            return False
        self.workspace.checkpoint()
        identity, snapshot = self.workspace.freeze()
        self.checkpoint(snapshot=identity, snapshot_path=str(snapshot), phase='validate')
        return True

    def review(self, identity, snapshot, cancel=None):
        try: return self._review(identity, snapshot, cancel)
        except InterruptedError:
            return ReviewResult(False, identity, text='Review cancelled after a concrete test failure.')

    def _review(self, identity, snapshot, cancel=None):
        key = digest([self.state['request_digest'], identity,
                      self.state.get('verification_runtime'), self.state['failures']])
        if self.state.get('review_input') == key and self.state.get('review'):
            saved = self.store.read(self.state['review'])
            if saved.get('snapshot') == identity:
                self.store.event('review_reused', snapshot=identity)
                return review_result(saved['text'], identity, {r.identity for r in self.request.acceptance})
        prompt = ('Independently review this immutable candidate against every frozen requirement and the original baseline. '
            'Inspect the diff, relevant source and coverage. Do not modify files or run a broad test suite. '
            'Block only demonstrated violations or introduced regressions, with a concrete counterexample and check. '
            'Editorial preferences and unrelated improvements are not blockers. '
            'Return JSON {decision:APPROVE|REJECT, findings:[{requirement,reason,counterexample,check}], '
            'coverage:[{requirement,nodes:["tests/test_file.py::test_behavior"]}]}. '
            'An approval maps every requirement to actual behavioral tests; generic passing tests are not coverage. '
            'Every finding must name one supplied requirement identity. APPROVE requires no findings.\n'
            + self.context() + '\nOriginal baseline: ' + self.request.engine_base
            + '\nPlan context (not acceptance evidence):\n' + self.store.read(self.state['plan'])['text'])
        from .context import source_context
        prompt += '\nCURRENT SOURCE AND DIFF:\n' + source_context(snapshot, self.request.engine_base)
        prompt += ('\nUse this exact source and diff for review; retrieve additional dependencies only as needed. '
                   'Do not repeat repository discovery when the supplied contents are complete.')
        if self.state.get('review'):
            prompt += '\nPrevious independent review (recheck the current delta):\n' + json.dumps(
                self.store.read(self.state['review']), ensure_ascii=False)
        reply = self.agent('review', prompt, snapshot, schema=REVIEW_SCHEMA, cancel=cancel)
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
                + '\nOriginal response:\n' + reply.text, snapshot, schema=REVIEW_SCHEMA, cancel=cancel)
            result = review_result(reply.text, identity, {r.identity for r in self.request.acceptance})
            if ((result.ok and original['decision'] != 'APPROVE')
                    or digest(result.findings) != digest(original['findings'])):
                raise RepairBlocked('review_semantics_changed',
                    'format correction changed the independent verdict or blocking evidence')
        self.checkpoint(review=self.store.artifact('review', asdict(result)), review_input=key)
        return result

    def validate(self):
        from pathlib import Path
        identity, snapshot = self.state['snapshot'], Path(self.state['snapshot_path'])
        if source_identity(snapshot) != identity:
            raise RepairBlocked('snapshot_changed', 'verification snapshot no longer matches its checkpoint')
        self.phase('validate')
        self.checkpoint(verification_runtime=getattr(self.verifier, 'runtime', ''))
        tests_cancel, review_cancel = threading.Event(), threading.Event()
        if self.state.get('validation') and hasattr(self.verifier, 'remember_timings'):
            self.verifier.remember_timings(self.store.read(self.state['validation'])['checks'])
        units = self.prioritize_failures(self.units(snapshot))
        if not self._diagnose_candidate(identity, snapshot, units):
            return False
        if self.state['phase'] == 'diagnose':
            self.phase('validate')
        with ThreadPoolExecutor(max_workers=2) as pool:
            validate = getattr(self.verifier, 'validate_suite', self.verifier.validate)
            tests = pool.submit(validate, identity, snapshot, units, Cancellation(self.cancel, tests_cancel))
            reviewed = pool.submit(self.review, identity, snapshot, Cancellation(self.cancel, review_cancel))
            try:
                # Observe either side's infrastructure failure immediately;
                # waiting on tests first can leave a failed reviewer unnoticed.
                for completed in as_completed((tests, reviewed)):
                    result = completed.result()
                    if completed is tests and not result.ok: review_cancel.set()
                    if completed is reviewed and result.findings: tests_cancel.set()
                validation, review = tests.result(), reviewed.result()
            except BaseException:
                self.cancel.set()
                raise
        if validation.snapshot != identity or review.snapshot != identity or source_identity(snapshot) != identity:
            raise RepairBlocked('snapshot_changed', 'acceptance evidence belongs to a different source snapshot')
        if validation.cancelled and not tests_cancel.is_set():
            self.checkpoint(validation=self.store.artifact('validation', asdict(validation)))
            raise KeyboardInterrupt()
        if validation.infrastructure:
            if not validation.failures:
                validation.failures = [{'unit': check.get('unit', 'verification'),
                    'reason': check.get('diagnostic') or check.get('excerpt') or 'verification infrastructure failed (exit ' + str(check.get('returncode')) + ')'}
                    for check in validation.checks if check.get('infrastructure')]
                if not validation.failures:
                    validation.failures = [{'unit': 'verification', 'reason': 'verification infrastructure failed without structured diagnostics'}]
            self.checkpoint(validation=self.store.artifact('validation', asdict(validation)))
            if review.findings:
                self.checkpoint(failures=[*validation.failures, *review.findings])
            raise RepairBlocked('verification_infrastructure', '; '.join(
                str(failure.get('reason') or 'verification infrastructure failure') for failure in validation.failures))
        executed = {node for check in validation.checks for node in check.get('passed', [])}
        missing = [node for row in review.coverage for node in row['nodes'] if not any(
            actual == node or actual.startswith(node + '[') or actual.startswith(node + '::')
            for actual in executed)] if review.ok else []
        if missing:
            validation.ok = False
            validation.failures.append({'reason': 'reviewed behavioral coverage was not executed', 'missing': missing})
        proof = self.store.artifact('validation', asdict(validation))
        self.checkpoint(validation=proof)
        self.workspace.collect_snapshots(identity)
        regression = None
        if validation.ok and review.ok and self.regression is not None:
            self.phase('regression')
            observed = self.regression(identity, snapshot, review.coverage, self.cancel)
            regression = self.store.artifact('regression', observed)
            self.checkpoint(regression=regression)
            if observed.get('infrastructure'):
                raise RepairBlocked('verification_infrastructure', observed.get('reason', 'baseline environment failed'))
            if not observed.get('ok') or observed.get('snapshot') != identity:
                validation.ok = False
                validation.failures.append({'unit': 'behavior-regression', 'reason': observed.get('reason', 'regression proof missing')})
                proof = self.store.artifact('validation', asdict(validation))
                self.checkpoint(validation=proof)
        boundary = None
        if validation.ok and review.ok and self.boundary is not None:
            self.phase('boundary')
            observed = self.boundary(identity, snapshot, self.cancel)
            boundary = self.store.artifact('boundary', observed)
            self.checkpoint(boundary=boundary)
            if not observed.get('ok') or observed.get('snapshot') != identity:
                validation.ok = False
                validation.failures.append({'unit': 'original-boundary', 'reason': 'original recovery boundary failed',
                                            'observed': observed.get('observed', observed)})
                proof = self.store.artifact('validation', asdict(validation))
                self.checkpoint(validation=proof)
        if validation.ok and review.ok:
            self.checkpoint(status='ready', phase='deliver', failures=[],
                receipt=self.store.artifact('acceptance', {'request': self.state['request_digest'],
                    'snapshot': identity, 'validation': proof, 'review': self.state['review'], 'boundary': boundary, 'regression': regression}))
            return True
        failures = [*validation.failures, *review.findings]
        if not failures: raise RepairBlocked('invalid_validation', 'incomplete acceptance has no actionable failure')
        self.record_failures(failures, identity,
            passed_tests={node for check in validation.checks if check.get('ok') for node in check.get('passed', [])},
            review=review)
        return False

    def _diagnose_candidate(self, identity, snapshot, units):
        selected = diagnostic_units(self.state.get('failures', []), units)
        if not selected:
            return True
        self.phase('diagnose')
        result = self.verifier.validate(identity, snapshot, selected, self.cancel)
        if result.snapshot != identity or source_identity(snapshot) != identity:
            raise RepairBlocked('snapshot_changed', 'diagnostic evidence belongs to a different source snapshot')
        self.checkpoint(diagnostic_validation=self.store.artifact('diagnostic-validation', asdict(result)))
        if result.infrastructure:
            raise RepairBlocked('verification_infrastructure', '; '.join(
                str(f.get('reason') or 'diagnostic infrastructure failed') for f in result.failures)
                or 'diagnostic verification infrastructure failed')
        if result.cancelled or self.cancel.is_set():
            raise KeyboardInterrupt()
        if result.ok:
            # Diagnostics are early feedback only. The complete mandatory suite,
            # independent review, regression and boundary checks still follow.
            return True
        if not result.failures:
            raise RepairBlocked('invalid_validation', 'failed diagnostic has no actionable evidence')
        passed = {node for check in result.checks if check.get('ok') for node in check.get('passed', [])}
        failures = retain_unchecked(self.state['failures'], result.failures, passed)
        self.record_failures(failures, identity, passed_tests=passed)
        return False

    def prioritize_failures(self, units):
        """Run retained counterexamples first without dropping any suite checks."""
        failures = self.state.get('failures', [])
        explicit = {node for row in failures for field in ('failed', 'missing') for node in row.get(field, [])}
        descriptions = '\n'.join(str(row.get(field, '')) for row in failures
                                 for field in ('reason', 'counterexample', 'check', 'command', 'unit'))
        def priority(unit):
            nodes = unit.expected_nodes
            # Reviews can name a pytest function without repeating its file.
            names = {name for node in nodes for qualified in (node, node.partition('::')[2])
                     for name in (qualified, qualified.split('[', 1)[0]) if name}
            if set(nodes) & explicit or any(name in descriptions for name in names): return 0
            if any(node.split('::', 1)[0] in descriptions for node in nodes): return 1
            return 2
        return sorted(units, key=priority)

    def record_failures(self, failures, identity, *, passed_tests=(), review=None):
        """All concrete candidate failures share the same persistent budget."""
        progress = assess_progress(self.state, failures, identity, passed_tests=passed_tests, review=review)
        self.checkpoint(failures=failures, best_failure_keys=progress['best_failure_keys'],
                        resolved_failure_keys=progress['resolved_failure_keys'],
                        failure_diagnosis=diagnose(failures), progress_policy_version=2,
                        stagnant=0 if progress['progressed'] else self.state['stagnant'] + 1, phase='implement')
        self.store.event('acceptance_failed', snapshot=identity, failures=failures,
                         progressed=progress['progressed'], newly_verified=progress['newly_verified'])

    def _recover_verified_progress(self):
        """Reinterpret an old stopped receipt once, without inventing new proof."""
        from pathlib import Path
        state = self.state
        if (not state or state.get('status') != 'blocked'
                or state.get('blocker', {}).get('code') != 'no_progress'
                or state.get('progress_policy_version', 0) >= 2 or state.get('external_correction')
                or not self.resume_token or self.resume_token == state.get('resume_token')
                or not all(state.get(k) for k in ('validation', 'review', 'snapshot', 'snapshot_path'))):
            return False
        if state['request_digest'] != digest(self.request.to_dict()):
            raise RepairBlocked('request_changed', 'progress recovery differs from the frozen repair contract')
        identity = state['snapshot']
        validation, saved_review = self.store.read(state['validation']), self.store.read(state['review'])
        if (validation.get('snapshot') != identity or saved_review.get('snapshot') != identity
                or validation.get('infrastructure') or validation.get('cancelled')
                or not saved_review.get('ok') or saved_review.get('findings')
                or source_identity(Path(state['snapshot_path'])) != identity):
            return False
        review = review_result(saved_review['text'], identity, {r.identity for r in self.request.acceptance})
        passed = {n for c in validation.get('checks', []) if c.get('ok') for n in c.get('passed', [])}
        progress = assess_progress(state, state['failures'], identity, passed_tests=passed, review=review)
        if not progress['newly_verified']:
            return False
        self.record_failures(state['failures'], identity, passed_tests=passed, review=review)
        self.checkpoint(status='active', blocker={}, resume_token=self.resume_token)
        self.store.event('verified_progress_recovered', snapshot=identity,
                         newly_verified=progress['newly_verified'])
        return True

    def recover_verified_progress(self):
        with self.store.locked():
            self.state = self.store.load()
            return self._recover_verified_progress()

    def run(self):
        with self.store.locked():
            recover_review = False
            recover_timeout = False
            self.state = self.store.load() or {'version': 2, 'request_digest': digest(self.request.to_dict()),
                'status': 'active', 'phase': 'prepare', 'plan': None, 'sessions': {}, 'attempts': 0,
                'calls': 0, 'failures': [], 'stagnant': 0, 'replans': 0}
            if self.state['request_digest'] != digest(self.request.to_dict()):
                raise RepairBlocked('request_changed', 'resume request differs from the frozen repair contract')
            self._recover_verified_progress()
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
            if self.state['status'] == 'blocked':
                # Earlier V2 controllers made a recoverable test audit terminal,
                # before counting/checkpointing the completed implementation.
                # One explicit resume re-audits its exact retained bytes under
                # the upgraded controller, preserving plan, session and budget.
                old_audit = (self.state.get('blocker', {}).get('code') in {'tests_weakened', 'tests_invalid'}
                             and not self.state.get('audit_recovery_version') and self.state.get('plan'))
                retryable = self.state.get('blocker', {}).get('code') in {
                    'provider_failed', 'provider_configuration', 'docker_unavailable', 'disk_space',
                    'verification_infrastructure', 'upstream_unavailable', 'disk_observation', 'execution_failed',
                    'provider_timeout', 'provider_cleanup_failed'}
                if not ((retryable or old_audit) and self.resume_token and self.resume_token != self.state.get('resume_token')):
                    return self.state
                if old_audit:
                    self.checkpoint(phase='audit', attempts=self.state['attempts'] + 1, audit_recovery_version=1)
                    self.store.event('test_audit_recovered', previous=self.state['blocker'])
                recover_review = self.state.get('blocker', {}).get('code') == 'verification_infrastructure'
                recover_timeout = (self.state.get('phase') == 'implement' and
                    self.state.get('blocker', {}).get('code') == 'provider_failed' and
                    self.state.get('blocker', {}).get('message') == 'provider call exceeded its configured time budget')
            self.checkpoint(resume_token=self.resume_token)
            self.checkpoint(status='active', blocker={})
            try:
                root = self.workspace.prepare()
                self.verifier.prepare()
                if hasattr(self.driver, 'preflight'): self.driver.preflight(root)
                provider = getattr(getattr(self.driver, 'config', None), 'kind', 'test')
                if self.state.get('session_provider') not in (None, provider):
                    self.checkpoint(sessions={})
                self.checkpoint(session_provider=provider)
                if recover_review:
                    self.recover_cancelled_review(root)
                if recover_timeout:
                    self.recover_timed_out_implementation(root)
                if not self.state.get('plan'):
                    self.checkpoint(test_preservation_findings=self.test_findings(root))
                    if self.allow_implementation:
                        self.plan(root)
                    else:
                        reference = self.store.artifact('plan', {'text': 'Verify the existing selected source without code generation.',
                                                               'request': self.state['request_digest']})
                        self.checkpoint(plan=reference, phase='audit')
                while True:
                    if self.cancel.is_set(): raise KeyboardInterrupt()
                    if self.state['phase'] not in ('audit', 'diagnose', 'validate', 'boundary', 'regression'):
                        if self.state.get('external_correction'):
                            raise RepairBlocked('no_progress',
                                'corrected source failed acceptance; implementation budget remains exhausted')
                        if not self.allow_implementation:
                            raise RepairBlocked('guarded_mode', 'existing source failed acceptance; code generation is not authorized')
                        if self.state['stagnant'] >= self.max_stagnant:
                            if self.state['replans'] >= self.max_replans:
                                raise RepairBlocked('no_progress', 'repair made no verified progress after its bounded rediagnosis')
                            self.checkpoint(replans=self.state['replans'] + 1, stagnant=0)
                            self.plan(root, rediagnose=True)
                        self.implement(root)
                    if self.state['phase'] == 'audit' and not self.audit(root): continue
                    if self.validate(): return self.state
            except KeyboardInterrupt:
                self.cancel.set()
                self.checkpoint(status='stopped')
                self.store.event('stopped', phase=self.state['phase'])
                raise
            except RepairBlocked as error:
                message = str(error)
                if error.code == 'no_progress' and self.state.get('failures'):
                    failure = self.state['failures'][0]
                    nodes = failure.get('failed') or failure.get('missing') or []
                    location = nodes[0] if nodes else failure.get('unit') or failure.get('path')
                    if not location and failure.get('requirement'):
                        from ..repair_environment_log import sanitize
                        location = 'independent review: ' + sanitize(str(failure.get('reason') or failure['requirement']))
                    if location:
                        message = 'Acceptance failed at ' + str(location)[:400] + '; ' + message
                self.checkpoint(status='blocked', blocker={'code': error.code, 'message': message})
                self.store.event('blocked', **self.state['blocker'])
                return self.state
            except Exception as error:
                from ..repair_environment_log import sanitize
                self.checkpoint(status='blocked', blocker={'code': 'execution_failed',
                    'message': sanitize(f'{type(error).__name__}: {error}')[:2000]})
                self.store.event('blocked', **self.state['blocker'])
                return self.state

    def recover_timed_out_implementation(self, root):
        """Recover older timeout checkpoints before spending another model turn."""
        call = self.state.get('active_call')
        if not call:
            events = self.store.root / 'events.jsonl'
            if not events.is_file(): return False
            for line in reversed(events.read_text().splitlines()):
                try: event = json.loads(line)
                except ValueError: continue  # A killed legacy writer may leave a partial final journal line.
                if not isinstance(event, dict): continue
                if event.get('kind') == 'agent_started' and event.get('call') == self.state['calls']:
                    call = event
                    break
        if not call or call.get('role') != 'implement' or call.get('call') != self.state['calls']: return False
        original = self.store.read(call['input'])
        if (original.get('role') != 'implement' or not isinstance(original.get('source'), str)
                or len(original['source']) != 64): return False
        after = source_identity(root)
        if original.get('source') == after: return False
        self.workspace.checkpoint()
        self.checkpoint(phase='audit', attempts=self.state['attempts'] + 1,
            implementation_timebox={'call': self.state['calls'], 'source_before': original['source'], 'source_after': after})
        self.store.event('implementation_timeout_recovered', **self.state['implementation_timebox'])
        return True

    def recover_cancelled_review(self, root):
        """Repair the old cancellation/infra mix-up using retained evidence only."""
        from pathlib import Path
        if not self.state.get('validation') or not self.state.get('review'): return False
        validation = self.store.read(self.state['validation'])
        saved = self.store.read(self.state['review'])
        identity = self.state.get('snapshot')
        bad = [c for c in validation.get('checks', []) if not c.get('ok')]
        if (not validation.get('cancelled') or validation.get('failures') or not bad
                or any(c.get('returncode') != 130 or 'execution' in c for c in bad)
                or validation.get('snapshot') != identity or saved.get('snapshot') != identity
                or self.state.get('review_input') != digest([self.state['request_digest'], identity,
                    self.state.get('verification_runtime'), self.state['failures']])):
            return False
        if source_identity(root) != identity or source_identity(Path(self.state['snapshot_path'])) != identity:
            return False
        review = review_result(saved['text'], identity, {r.identity for r in self.request.acceptance})
        if not review.findings: return False
        self.store.event('cancelled_review_recovered', snapshot=identity, findings=review.findings,
                         validation=self.state['validation'], review=self.state['review'])
        self.record_failures(review.findings, identity,
            passed_tests={node for check in validation['checks'] if check.get('ok') for node in check.get('passed', [])})
        return True

    def integrate(self, repository, parents):
        """Reconcile upstream on the same candidate, keeping original repair budget."""
        from .workspace import git
        with self.store.locked():
            self.state = self.store.load()
            if not self.state or self.state['status'] not in ('ready', 'active'):
                raise RepairBlocked('integration_state', 'integration requires a retained accepted candidate')
            root = self.workspace.prepare()
            self.workspace.checkpoint()
            conflicts = []
            import subprocess
            for parent in dict.fromkeys(parents):
                git(root, 'fetch', '--quiet', str(repository), parent)
                result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root),
                                         'merge-base', '--is-ancestor', parent, 'HEAD'], capture_output=True)
                if result.returncode == 0: continue
                try: git(root, 'merge', '--no-edit', parent)
                except subprocess.CalledProcessError:
                    conflicts = git(root, 'diff', '--name-only', '--diff-filter=U').splitlines()
                    if not conflicts: raise
                    break
            if conflicts:
                self.checkpoint(status='active', phase='implement', failures=[{
                    'unit': 'upstream-integration', 'reason': 'Resolve these merge conflicts while preserving both histories',
                    'paths': conflicts}], integration_parents=list(parents))
            else:
                self.checkpoint(status='active', phase='audit',
                                integration_parents=list(parents))
            self.store.event('integration_prepared', parents=list(parents), conflicts=conflicts)

    def recover_corrected_source(self, repository, commit):
        """Admit a new committed correction to acceptance, never refill agent retries.

        This is an explicit resume of an exhausted transaction. The correction
        retains both Git histories; source-identical commits cannot reopen it.
        Other blockers and the same invocation remain untouched.
        """
        from .workspace import git
        import subprocess
        with self.store.locked():
            self.state = self.store.load()
            if not self.state:
                return False
            exhausted = (self.state['status'] == 'blocked'
                         and self.state.get('blocker', {}).get('code') == 'no_progress')
            # Subscriber validation can revoke an externally corrected receipt
            # after acceptance. Admit a new committed correction on the next
            # invocation without first forcing a redundant no_progress failure.
            subscriber_failure = (self.state['status'] == 'active'
                and self.state.get('phase') == 'implement' and self.state.get('external_correction')
                and self.state.get('failures')
                and all(f.get('unit') == 'subscriber-boundary' for f in self.state['failures']))
            if (not (exhausted or subscriber_failure) or not self.resume_token
                    or self.resume_token == self.state.get('resume_token')):
                return False
            if self.state['request_digest'] != digest(self.request.to_dict()):
                raise RepairBlocked('request_changed', 'correction differs from the frozen repair contract')
            root = self.workspace.prepare()
            if git(root, 'diff', '--name-only', '--diff-filter=U'):
                return False  # Preserve unresolved external work for explicit resolution.
            self.workspace.checkpoint()
            pending = self.state.get('correction_pending')
            if pending:
                before, parent = pending['source_before'], pending['parent']
            else:
                before = source_identity(root)
                git(root, 'fetch', '--quiet', str(repository), commit)
                parent = git(root, 'rev-parse', 'FETCH_HEAD^{commit}')
                if subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', parent, 'HEAD'],
                                  capture_output=True).returncode == 0:
                    return False
                # Persist before merging so a crash after Git commits the merge
                # cannot strand the corrected bytes behind the old blocker.
                self.checkpoint(correction_pending={'source_before': before, 'parent': parent})
            try:
                git(root, 'merge', '--no-edit', parent)
            except subprocess.CalledProcessError:
                conflicts = git(root, 'diff', '--name-only', '--diff-filter=U').splitlines()
                if not conflicts: raise
                self.checkpoint(resume_token=self.resume_token, blocker={'code': 'no_progress',
                    'message': 'external correction has unresolved merge conflicts: ' + ', '.join(conflicts)})
                self.store.event('correction_conflicted', parent=parent, paths=conflicts)
                return False
            after = source_identity(root)
            if after == before:
                self.checkpoint(correction_pending=None)
                return False
            correction = {'parent': parent, 'source_before': before, 'source_after': after,
                          'resume_token': self.resume_token}
            self.checkpoint(status='active', phase='audit', blocker={}, resume_token=self.resume_token,
                            external_correction=correction, correction_pending=None)
            self.store.event('corrected_source_recovered', **correction)
            return True
