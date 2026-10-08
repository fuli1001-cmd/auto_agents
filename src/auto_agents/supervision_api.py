"""Thin public protocol facade. Only control.Store owns business progress."""

from contextvars import ContextVar
from functools import wraps
from .control.api import status, snapshot, resume_check
from .control.migration import migrate
from .control.observer import Observer
from .local_io import atomic_json as atomic

_active = ContextVar("retired_business_telemetry", default=None)


def command(argv):
    from .control.cli import main

    return main(argv)


def gate_boundary(function):
    # Verification is charged/recorded once by Engine.effect.
    return function


def observe_record(*args, **kwargs):
    # Legacy readers/migration can still use DTOs; no second state writer.
    return None


def operation_boundary(*args, **kwargs):
    return None


def milestone(*args, **kwargs):
    return None


def boundary_event(*args, **kwargs):
    return None
