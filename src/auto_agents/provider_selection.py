"""Persist controller provider preferences after stage ownership checks finish."""
from contextvars import ContextVar
from functools import wraps


_PENDING = ContextVar('auto_agents_pending_provider_selection', default=None)


def retain_selection(orchestrator, record):
    pending = _PENDING.get()
    if pending is None or pending[0] is not orchestrator:
        return False
    pending[1].clear()
    pending[1].update(record)
    return True


def defer_selection(operation):
    @wraps(operation)
    def observed(orchestrator, *args, **kwargs):
        parent = _PENDING.get()
        record = {}
        token = _PENDING.set((orchestrator, record))
        try:
            return operation(orchestrator, *args, **kwargs)
        finally:
            _PENDING.reset(token)
            if record:
                if parent is not None and parent[0] is orchestrator:
                    parent[1].clear()
                    parent[1].update(record)
                else:
                    orchestrator._persist_provider_selection(record)
    return observed
