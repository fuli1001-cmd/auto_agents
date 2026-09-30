"""Reusable baseline comparisons, never candidate acceptance certificates."""
from dataclasses import asdict
import hashlib
import json
import re
import shlex
import sqlite3
import time

from .gate_baseline_cache import GateBaselineCache, MAX_AGE_SECONDS
from .gate_result_cache import execution_policy_fingerprint
from .gates import extract_failure_info
from .models import CommandResult, GateResult


def failure_signatures(result):
    signatures = {}
    for node, data in result.test_results.items():
        phases = data.get('phases', {})
        failed = sorted(phase for phase, outcome in phases.items() if outcome == 'failed')
        if not failed or not data.get('detail'):
            continue
        detail = str(data['detail'])
        error_lines = re.findall(r'^E\s+.*$', detail, re.MULTILINE)
        if error_lines:
            detail = '\n'.join(error_lines)
        detail = re.sub(r'/tmp/[^\s:\"\']+', '<temporary>', detail)
        detail = re.sub(r'0x[0-9a-fA-F]+', '<address>', detail)
        signatures[node] = hashlib.sha256(json.dumps([failed, detail], sort_keys=True).encode()).hexdigest()
    return signatures


def node_replay_command(command, result, safe=False):
    if not safe or not result.test_results:
        return command
    nodes = [node for node, data in result.test_results.items()
             if data.get('phases', {}).get('call') == 'failed'
             and data.get('phases', {}).get('setup') == 'passed'
             and data.get('phases', {}).get('teardown') == 'passed']
    other_failures = [node for node, data in result.test_results.items()
                      if 'failed' in data.get('phases', {}).values() and node not in nodes]
    if not nodes or other_failures:
        return command
    args = shlex.split(command)
    if any(token in {';', '&&', '||', '|', '>', '<'} for token in args):
        return command
    if len(args) < 3 or args[1:3] != ['-m', 'pytest']:
        return command
    # Preserve every option (including -m/-k values); replace only explicit
    # repository node/file arguments, never a shell launcher or discovery dir.
    from .execution_binding import test_invocations
    invocations = test_invocations(command)
    if len(invocations) != 1 or not invocations[0].targets:
        return command
    targets = set(invocations[0].targets)
    if any(not target.split('::')[0].endswith('.py') for target in targets):
        return command
    return shlex.join([arg for arg in args if arg not in targets] + sorted(nodes))


class BaselineCertificates(GateBaselineCache):
    def __init__(self, executor):
        cache = executor.result_cache
        super().__init__(executor.project_root, cache.cache_path, environment_fingerprint=cache.environment_fingerprint)
        self.executor = executor
        self.source = executor.snapshot.tree_sha
        self.context = cache.context_fingerprint

    def key(self, command, metadata):
        value = asdict(metadata) if hasattr(metadata, '__dataclass_fields__') else metadata or {}
        return hashlib.sha256(json.dumps(['baseline-certificate-v1', self.source,
            self.environment_fingerprint, self.context, execution_policy_fingerprint(), command, value],
            sort_keys=True).encode()).hexdigest()

    def get_result(self, command, metadata):
        if self.environment_fingerprint.startswith('unavailable:'):
            return None
        try:
            with self._connect() as db:
                self._table(db)
                row = db.execute('SELECT payload FROM baseline_certificates WHERE identity=? AND updated_at>=?',
                    (self.key(command, metadata), int(time.time()) - MAX_AGE_SECONDS)).fetchone()
            if not row:
                return None
            value = json.loads(row[0])
            if not self.executor.result_cache._manifest_matches(value['observed_inputs']):
                return None
            result = CommandResult(**value)
            result.cached, result.backend, result.duration_seconds = True, 'baseline-certificate', 0
            return result
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return None

    def put_result(self, command, metadata, result):
        extraction = extract_failure_info(GateResult(result.ok, [result]))
        if (self.environment_fingerprint.startswith('unavailable:') or not extraction.comparable
                or result.termination_reason or result.cleanup_incomplete or result.infrastructure_error
                or result.mutation_paths or result.network_observed or not result.input_trace_complete
                or not result.observed_inputs):
            return
        value = asdict(result)
        value.update(command=command, artifacts={}, cached=False)
        payload = json.dumps(value, ensure_ascii=False)
        if len(payload.encode()) > 2 * 1024 * 1024:
            return
        try:
            with self._connect() as db:
                self._table(db)
                db.execute('INSERT OR REPLACE INTO baseline_certificates VALUES (?,?,?)',
                           (self.key(command, metadata), payload, int(time.time())))
                db.execute('DELETE FROM baseline_certificates WHERE updated_at<?',
                           (int(time.time()) - MAX_AGE_SECONDS,))
        except (OSError, sqlite3.Error):
            return

    @staticmethod
    def _table(db):
        db.execute('CREATE TABLE IF NOT EXISTS baseline_certificates '
                   '(identity TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at INTEGER NOT NULL)')


def baseline_plan(executor, candidate_results, metadata):
    """Return misses and reusable comparison results for the original commands."""
    certificates = BaselineCertificates(executor)
    pending, reused, originals = [], [], {}
    for result in candidate_results:
        if result.ok:
            continue
        item = metadata.get(result.command, {})
        cached = certificates.get_result(result.command, item)
        if cached is not None:
            reused.append(cached)
            continue
        replay = node_replay_command(result.command, result, getattr(item, 'node_replay_safe', False))
        pending.append(replay)
        originals[replay] = result.command
    return certificates, list(dict.fromkeys(pending)), reused, originals
