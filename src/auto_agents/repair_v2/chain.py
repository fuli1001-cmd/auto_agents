"""Controller-owned limits shared by every repair of one business workflow."""
from pathlib import Path
from contextlib import contextmanager
import fcntl
import json

from .store import Store, digest
from .transaction import transaction_root
from .types import RepairBlocked


DEFAULT_LIMITS = {'transactions': None, 'implementations': None, 'model_calls': None}


def workflow_key(payload):
    invocation = payload.get('invocation', {})
    # Session/run IDs are durable before workflow_id is populated. Neither a
    # rewritten issue, a new job UUID nor a changed provider grants new budget.
    session = str(invocation.get('session_id') or '')
    run = str(invocation.get('run_id') or '')
    return {'project': str(Path(payload['project']).resolve()),
            'subject': 'session:' + session if session else 'run:' + run if run else 'unscoped'}


class RepairChain:
    def __init__(self, config, payload, transaction):
        from ..repair_control import operator_policy
        self.config, self.identity = config, workflow_key(payload)
        self.policy = operator_policy(config)
        self.transaction = Path(transaction)
        self.store = Store(Path(config['root']) / 'repair-chains' / digest(self.identity))
        self.limits = {**DEFAULT_LIMITS, **self.policy.get('repair_chain_limits', {})}
        if (set(self.limits) != set(DEFAULT_LIMITS)
                or any(v is not None and (type(v) is not int or v < 1) for v in self.limits.values())):
            raise RepairBlocked('repair_chain_policy', 'repair_chain_limits must contain positive integer limits')

    @contextmanager
    def locked(self):
        with (self.store.root / 'chain.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def totals(state):
        entries = state['transactions'].values()
        return {'transactions': len({entry['canonical'] for entry in entries}),
                'implementations': sum(e['implementations'] for e in entries),
                'model_calls': sum(e['model_calls'] for e in entries)}

    def _load(self):
        state = self.store.load() or {'version': 1, 'identity': self.identity, 'transactions': {}}
        if state['identity'] != self.identity:
            raise RepairBlocked('repair_chain_identity', 'repair chain belongs to another workflow')
        return state

    def _history(self, state):
        for file in (Path(self.config['root']) / 'v2-transactions').glob('*/original-payload.json'):
            payload = json.loads(file.read_text())
            if workflow_key(payload) != self.identity:
                continue
            root = file.parent
            entry = state['transactions'].setdefault(root.name,
                {'canonical': transaction_root(self.config, payload).name, 'model_calls': 0, 'implementations': 0})
            saved = Store(root).load() or {}
            implementations = 0
            events = root / 'events.jsonl'
            if events.exists():
                for line in events.read_text().splitlines():
                    try: event = json.loads(line)
                    except ValueError: continue  # A crash may leave a trailing incomplete event.
                    if event.get('kind') == 'agent_started' and event.get('role') == 'implement':
                        implementations += 1
            entry['model_calls'] = max(entry['model_calls'], int(saved.get('calls', 0)))
            entry['implementations'] = max(entry['implementations'], implementations, int(saved.get('attempts', 0)))

    def admit(self):
        with self.locked():
            state = self._load()
            self._history(state)
            # Old ledgers saved implicit defaults without provenance. They are
            # accounting history, not an operator's spending authorization.
            state['limits'] = (self.limits if 'repair_chain_limits' in self.policy
                               else state.get('limits', self.limits) if state.get('explicit_limits')
                               else dict(DEFAULT_LIMITS))
            state['explicit_limits'] = bool(self.policy.get('repair_chain_limits') or
                                            state.get('explicit_limits'))
            known = {entry['canonical'] for entry in state['transactions'].values()}
            limit = state['limits']['transactions']
            if self.transaction.name not in known and limit is not None and len(known) >= limit:
                self.store.save(state)
                raise RepairBlocked('repair_chain_exhausted',
                    f"workflow repair chain already has {len(known)} transactions; "
                    'a new issue description cannot renew its budget; retain the original blocker and evidence')
            state['transactions'].setdefault(self.transaction.name,
                {'canonical': self.transaction.name, 'model_calls': 0, 'implementations': 0})
            self.store.save(state)
            return self.totals(state)

    def reserve(self, role):
        """Charge before dispatch; a crash or cancellation cannot refund a call."""
        with self.locked():
            state = self._load()
            used = self.totals(state)
            fields = ['model_calls', *(['implementations'] if role == 'implement' else [])]
            for field in fields:
                if state['limits'][field] is not None and used[field] >= state['limits'][field]:
                    raise RepairBlocked('repair_chain_exhausted',
                        f"workflow repair chain exhausted {field}: {used[field]}/{state['limits'][field]}; "
                        'preserve the candidate and diagnose the remaining blocker before granting more work')
            entry = state['transactions'][self.transaction.name]
            for field in fields: entry[field] += 1
            self.store.save(state)
            self.store.event('call_reserved', transaction=self.transaction.name, role=role, totals=self.totals(state))

    def context(self):
        state = self.store.load()
        return {'identity': self.identity, 'limits': state['limits'], 'used': self.totals(state)}
