"""Console observations for foreground repair; never recovery authority."""
import threading
import time

from .model import digest


class RepairProgress:
    def __init__(self, reporter, payload):
        self.reporter = reporter
        self.job = {'id': 'kernel:' + digest([payload.get('project'), payload.get('invocation')]),
                    'generation': 1, 'state': 'repairing', 'payload': payload, 'display': {}}
        self.subscriber = {'state': 'waiting'}
        self.attempt = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self.phase('environment_preparation')
        if self.reporter is not None:
            self._thread = threading.Thread(target=self._refresh_loop,
                                            name='auto-agents-repair-display', daemon=True)
            self._thread.start()
        return self

    def _refresh_loop(self):
        while not self._stop.wait(1):
            self.refresh()

    def refresh(self):
        with self._lock:
            if self.reporter is not None and not self._stop.is_set():
                self.reporter.repair_update(self.job, self.subscriber)

    def phase(self, phase, **details):
        with self._lock:
            # repair_display expects the number of prior attempts for implement.
            attempt = max(0, self.attempt - 1) if phase == 'implement' else self.attempt
            self.job['display'] = {'phase': phase, 'attempt': attempt,
                'sequence': self.job['display'].get('sequence', 0) + 1, **details}
            self.refresh()

    def command(self, command, task):
        with self._lock:
            self.attempt = task['attempts']
            phase = {'verify': 'audit', 'review': 'review_candidate'}.get(command['phase'], command['phase'])
            if command.get('status') in {'running', 'unknown'}:
                phase = 'boundary_preflight'
            self.phase(phase, correcting=bool(task.get('failure')))

    def output(self):
        with self._lock:
            if self._stop.is_set(): return
            # Multiple verification workers can report output together.
            now = time.time()
            if now - self.job['display'].get('last_output_at', 0) < 1: return
            self.job['display']['last_output_at'] = now
            self.refresh()

    def agent(self, event):
        kind = event.get('event', '').lower()
        if kind.endswith('delta') or kind in {'item/completed', 'item.completed', 'content_block_stop'}:
            self.output()

    def check(self, kind, details):
        with self._lock:
            if self._stop.is_set(): return
            if kind in {'checks_started', 'check_finished', 'checks_finished'}:
                cancelled = details.get('cancelled_count', 0)
                self.job['display']['checks'] = {
                    'completed': max(0, details.get('completed', 0) - cancelled),
                    'total': details.get('total', 0), 'failed': details.get('failed_count', 0),
                    'cancelled': cancelled}
                self.job['display']['checks_finished'] = kind == 'checks_finished'
            if kind in {'check_output', 'check_finished'}: self.output()
            self.refresh()

    def rejected(self, failures):
        self.phase('acceptance_failed', failures=failures)

    def blocked(self, failure, incident=''):
        with self._lock:
            if self._stop.is_set(): return
            if self.reporter is not None:
                self.reporter.event('repair.kernel_result', {'ok': False, 'incident': incident, 'failure': failure})
            self.job.update(state='blocked', result={'error': failure.get('reason', '')})
            self.subscriber = {'state': 'blocked'}
            self.refresh()

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def handoff(self):
        with self._lock:
            self.job['state'] = 'completed'
            self.subscriber = {'state': 'resuming'}
            self.refresh()
        self.close()
        if self.reporter is not None:
            self.reporter.handoff()

    def __exit__(self, kind, error, traceback):
        from ..process_supervision import RunInterruptedError
        if error is not None and not self._stop.is_set():
            if isinstance(error, (KeyboardInterrupt, InterruptedError, RunInterruptedError)):
                with self._lock:
                    self.job['state'] = 'cancelled'
                    self.subscriber = {'state': 'cancelled'}
                    self.refresh()
            else:
                self.blocked({'kind': 'exception', 'reason': str(error)})
        self.close()
