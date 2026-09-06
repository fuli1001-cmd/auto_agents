from __future__ import annotations

import logging
import time
import re
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO
from .diagnostic_output import plain_text, redact


_PREFIX = re.compile(r"^\[aa-log \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00 [A-Z]+\] ", re.MULTILINE)


class RunLogFormatter(logging.Formatter):
    """Each physical line has a removable, version-specific time prefix."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
        message = plain_text(redact(super().format(record)))
        return "\n".join(f"[aa-log {timestamp} {record.levelname}] {line}" for line in message.split("\n"))


def read_diagnostic_log(path: Path) -> str:
    """Normalize presentation prefixes before applying legacy evidence budgets."""
    try:
        return _PREFIX.sub("", path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""


class _ReporterHandler(logging.Handler):
    def __init__(self, reporter) -> None:
        super().__init__()
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.reporter.diagnostic(record)
        except Exception as error:
            self.reporter.capture_failed(error)


class _ParentFileHandler(logging.Handler):
    """Reuse the owning run's file handler and write lock for worker lanes."""
    def __init__(self, parent) -> None:
        super().__init__(logging.INFO)
        self.parent_reporter = parent
        self.target = next((
            handler for logger in parent._loggers[:1] for handler in logger.handlers
            if getattr(handler, "_auto_agents_file_path", None)
        ), None)

    def emit(self, record: logging.LogRecord) -> None:
        if self.target is not None and record.levelno >= self.target.level:
            self.target.acquire()
            try:
                if not self._closed and not self.parent_reporter._closed:
                    self.target.handle(record)
            finally:
                self.target.release()

    def close(self) -> None:
        live_handlers = [handler for logger in self.parent_reporter._loggers[:1] for handler in logger.handlers]
        if self.target is not None and self.target not in live_handlers:
            self.target.close()
        super().close()


def build_run_logger(stream: TextIO, reporter=None) -> logging.Logger:
    # A stream is not a run identity: nested workflows and parallel lanes share it.
    logger = logging.Logger(f"auto_agents.run.{id(reporter) if reporter is not None else id(stream)}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    if reporter is not None:
        logger.addHandler(_ReporterHandler(reporter))
        if reporter.parent is not None:
            logger.addHandler(_ParentFileHandler(reporter.parent))
        reporter._loggers.append(logger)
        return logger
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RunLogFormatter("%(message)s"))
    handler._auto_agents_stream_id = id(stream)  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


def attach_run_file_logger(logger: logging.Logger, path: Path) -> Path:
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        if getattr(handler, "_auto_agents_file_path", None) == str(resolved):
            return resolved
        if getattr(handler, "_auto_agents_file_path", None):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.FileHandler(resolved, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(RunLogFormatter("%(message)s"))
    handler._auto_agents_file_path = str(resolved)  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return resolved


@contextmanager
def log_timing(logger: logging.Logger, label: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        logger.info("[timing] %s elapsed=%.3fs", label, elapsed)
