"""Shared resource admission for foreground, repair and release verification."""
from contextlib import contextmanager
import time

from .workers import WorkerSlotLease, load_local_worker_config
from .gate_execution import exclusive_resource_lease


@contextmanager
def verification_resources(project, *, resources=(), priority=0, timeout=900, cancel_event=None):
    config = load_local_worker_config()
    started = time.monotonic()
    required = config.max_slots if "global:exclusive" in resources else 1
    # Consistent acquisition order. Named resource waiters do not occupy CPU
    # slots; unrelated ready commands can use the worker in the meantime.
    names = ["host:verification:" + name for name in resources if name != "global:exclusive"]
    with exclusive_resource_lease(names, worker_id=config.worker_id,
                                  **({'cancel_event': cancel_event} if cancel_event is not None else {})):
        with WorkerSlotLease(config.managed_root, config.worker_id, config.max_slots, required,
            timeout_seconds=timeout,
            cancel_event=cancel_event,
            owner_metadata={"project_root": str(project), "priority": priority, "backend": "verification"}):
            yield time.monotonic() - started
