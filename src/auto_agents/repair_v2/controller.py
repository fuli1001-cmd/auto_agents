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


class Cancellation:
    def __init__(self, parent, local): self.parent, self.local = parent, local
    def is_set(self): return self.parent.is_set() or self.local.is_set()


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
        if not reply.ok: raise RepairBlocked('provider_failed', reply.error or 'provider did not complete the turn')
        return reply

    def context(self):
        return json.dumps({'goal': self.request.goal,
            'requirements': [asdict(item) for item in self.request.acceptance],
            'evidence': self.request.evidence,
            'test_preservation_findings': self.state.get('test_preservation_findings', [])}, ensure_ascii=False)

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
        self.workspace.checkpoint()
        # A completed implementation is durable even if its local audit fails.
        # Restarting at this boundary must not repeat the model turn.
        self.checkpoint(attempts=self.state['attempts'] + 1, phase='audit')

    def test_findings(self, root):
        from .audit import test_protection_findings
        findings = test_protection_findings(self.workspace.source, self.request.engine_base, root)
        for parent in self.state.get('integration_parents', []):
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
        units = self.units(snapshot)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tests = pool.submit(self.verifier.validate, identity, snapshot, units, Cancellation(self.cancel, tests_cancel))
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
        self.record_failures(failures, identity)
        return False

    def record_failures(self, failures, identity):
        """All concrete candidate failures share the same persistent budget."""
        def keys(rows):
            # Traceback paths, elapsed times and wording do not measure progress.
            values = set()
            for row in rows:
                if row.get('failed'):
                    values.update(('test', n) for n in row['failed'])
                elif row.get('missing'):
                    values.update(('missing', n) for n in row['missing'])
                elif row.get('requirement'):
                    values.add(('review', row['requirement'], row.get('check', '')))
                else: values.add(('unit', row.get('unit', row.get('command', row.get('reason', 'unknown')))))
            return values
        current = keys(failures)
        best = {tuple(item) for item in self.state.get('best_failure_keys', [])}
        progressed = bool(best and current < best)
        if not best or progressed: best = current
        self.checkpoint(failures=failures, best_failure_keys=sorted(best),
                        stagnant=0 if progressed else self.state['stagnant'] + 1, phase='implement')
        self.store.event('acceptance_failed', snapshot=identity, failures=failures, progressed=progressed)

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
            if self.state['status'] == 'blocked':
                # Earlier V2 controllers made a recoverable test audit terminal,
                # before counting/checkpointing the completed implementation.
                # One explicit resume re-audits its exact retained bytes under
                # the upgraded controller, preserving plan, session and budget.
                old_audit = (self.state.get('blocker', {}).get('code') in {'tests_weakened', 'tests_invalid'}
                             and not self.state.get('audit_recovery_version') and self.state.get('plan'))
                retryable = self.state.get('blocker', {}).get('code') in {
                    'provider_failed', 'provider_configuration', 'docker_unavailable', 'disk_space',
                    'verification_infrastructure', 'upstream_unavailable', 'disk_observation', 'execution_failed'}
                if not ((retryable or old_audit) and self.resume_token and self.resume_token != self.state.get('resume_token')):
                    return self.state
                if old_audit:
                    self.checkpoint(phase='audit', attempts=self.state['attempts'] + 1, audit_recovery_version=1)
                    self.store.event('test_audit_recovered', previous=self.state['blocker'])
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
                    if self.state['phase'] not in ('audit', 'validate', 'boundary', 'regression'):
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
                self.checkpoint(status='blocked', blocker={'code': error.code, 'message': str(error)})
                self.store.event('blocked', **self.state['blocker'])
                return self.state
            except Exception as error:
                from ..repair_environment_log import sanitize
                self.checkpoint(status='blocked', blocker={'code': 'execution_failed',
                    'message': sanitize(f'{type(error).__name__}: {error}')[:2000]})
                self.store.event('blocked', **self.state['blocker'])
                return self.state

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
