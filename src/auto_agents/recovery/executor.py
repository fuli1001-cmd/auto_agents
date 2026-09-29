"""Dispatch/reconciliation boundary. Unknown outcomes never cause redispatch."""
from uuid import uuid4
import json

from .model import Command, Event, Evidence, KernelError, Outcome, OutcomeKind, require


class Executor:
    def __init__(self, store, handlers, *, owner=None):
        self.store, self.handlers = store, dict(handlers)
        self.owner = owner or 'worker:' + uuid4().hex

    def _event(self, stream, kind, data, identity):
        snapshot = self.store.load(stream)
        return self.store.apply(stream, snapshot['revision'], Event(identity, kind, data))

    def execute(self, stream, command_id):
        state = self.store.load(stream)
        command = state['commands'][command_id]
        if command['status'] == 'finished': return Outcome.read(command['outcome'])
        require(command['status'] == 'reserved', 'outcome_unknown',
                'Previously dispatched operation must be reconciled, not repeated')
        handler = self.handlers.get(command['phase'])
        require(handler is not None, 'executor', 'No executor for stage', phase=command['phase'])
        epoch = command['epoch'] + 1
        dispatch = {'command_id': command_id, 'epoch': epoch, 'owner': self.owner}
        if command['phase'] == 'verify' and not command['model_call'] and self.owner.startswith('native:'):
            from ..artifact_store import process_identity
            dispatch['dispatch_identity'] = process_identity()
        self._event(stream, 'command_dispatched', dispatch,
                    command_id + ':dispatch')
        request = Command(**{key: command[key] for key in Command.__dataclass_fields__})
        try:
            result = handler.execute(request)
            require(isinstance(result, Outcome), 'protocol_invalid', 'Executor returned an untyped result')
        except BaseException as error:
            # The effect may have happened. Do not classify an arbitrary crash
            # as an engine defect and do not refund the reserved call.
            result = Outcome(OutcomeKind.OUTCOME_UNKNOWN, 'Executor exited before a durable outcome',
                             details={'error_type': type(error).__name__})
            self._finish(stream, command_id, epoch, result)
            raise
        self._finish(stream, command_id, epoch, result)
        return result

    def _finish(self, stream, command_id, epoch, result):
        reference = self.store.put(result.to_dict())
        if result.kind != OutcomeKind.OUTCOME_UNKNOWN:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT snapshot FROM kernel_streams WHERE id=?',(stream,)).fetchone()
                current = json.loads(row['snapshot'])['commands'][command_id] if row else {}
                require(current.get('status') in {'running','unknown'} and current.get('epoch') == epoch
                        and current.get('owner') == self.owner,'stale_owner','Expired worker cannot publish an authoritative receipt')
                existing = db.execute('SELECT receipt FROM kernel_results WHERE command_id=?', (command_id,)).fetchone()
                require(existing is None or existing['receipt'] == reference, 'receipt_conflict', 'Executor returned conflicting outcomes')
                db.execute('INSERT OR IGNORE INTO kernel_results VALUES(?,?)', (command_id, reference))
        self._event(stream, 'command_finished', {'command_id': command_id, 'epoch': epoch,
                    'owner': self.owner, 'outcome': result.to_dict(), 'receipt_ref': reference},
                    command_id + ':result:' + reference)

    def reconcile(self, stream, command_id):
        state = self.store.load(stream); command = state['commands'][command_id]
        if command['status'] == 'finished': return Outcome.read(command['outcome'])
        require(command['status'] in {'running', 'unknown'}, 'reconciliation', 'Operation was never dispatched')
        with self.store.connect() as db:
            saved = db.execute('SELECT receipt FROM kernel_results WHERE command_id=?', (command_id,)).fetchone()
        if saved:
            result = Outcome.read(self.store.read(saved['receipt']))
        else:
            handler = self.handlers.get(command['phase'])
            require(handler is not None and hasattr(handler, 'reconcile'), 'outcome_unknown', 'Executor has no receipt lookup')
            request = Command(**{key: command[key] for key in Command.__dataclass_fields__})
            result = handler.reconcile(request)
        if result is None or result.kind == OutcomeKind.OUTCOME_UNKNOWN:
            return Outcome(OutcomeKind.OUTCOME_UNKNOWN, 'Original operation outcome remains unconfirmed')
        require(isinstance(result, Outcome), 'protocol_invalid', 'Reconciliation returned an untyped result')
        reference = self.store.put(result.to_dict()); epoch = command['epoch'] + 1
        self._event(stream, 'command_reconciled', {'command_id': command_id, 'epoch': epoch,
                    'owner': self.owner, 'receipt_ref': reference}, command_id + ':reconcile:' + reference)
        self._finish(stream, command_id, epoch, result)
        return result


class FunctionExecutor:
    """Adapter for a bounded existing executor; reconciliation is explicit."""
    def __init__(self, execute, reconcile=None): self.function, self.lookup = execute, reconcile
    def execute(self, request): return self.function(request)
    def reconcile(self, request): return self.lookup(request) if self.lookup else None
