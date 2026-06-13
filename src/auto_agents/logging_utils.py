from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


def build_run_logger(stream: TextIO) -> logging.Logger:
    logger = logging.getLogger(f"auto_agents.run.{id(stream)}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    for handler in logger.handlers:
        if getattr(handler, "_auto_agents_stream_id", None) == id(stream):
            return logger
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._auto_agents_stream_id = id(stream)  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


def attach_run_file_logger(logger: logging.Logger, path: Path) -> Path:
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, "_auto_agents_file_path", None) == str(resolved):
            return resolved
    handler = logging.FileHandler(resolved, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
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
