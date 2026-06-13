from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator, TextIO


def build_run_logger(stream: TextIO) -> logging.Logger:
    logger = logging.getLogger(f"auto_agents.run.{id(stream)}")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


@contextmanager
def log_timing(logger: logging.Logger, label: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        logger.info("[timing] %s elapsed=%.3fs", label, elapsed)
