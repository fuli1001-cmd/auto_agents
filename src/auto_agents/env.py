from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(paths: Iterable[Path], *, override: bool = False) -> None:
    """Load simple KEY=value entries from .env files into os.environ."""
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser()
        try:
            resolved = resolved.resolve()
        except OSError:
            pass
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for key, value in _parse_dotenv(resolved.read_text(encoding="utf-8")).items():
            if override or key not in os.environ:
                os.environ[key] = value


def _parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_PATTERN.match(key):
            continue
        values[key] = _normalize_value(raw_value.strip())
    return values


def _normalize_value(value: str) -> str:
    if value.startswith(("'", '"')):
        return _unquote_value(value)
    return _strip_value_comment(value)


def _unquote_value(value: str) -> str:
    if len(value) < 2:
        return value
    quote = value[0]
    if quote not in {"'", '"'} or value[-1] != quote:
        return value
    inner = value[1:-1]
    if quote == '"':
        return (
            inner.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    return inner


def _strip_value_comment(value: str) -> str:
    if not value or value[0] in {"'", '"'}:
        return value
    marker = value.find(" #")
    if marker == -1:
        return value
    return value[:marker].rstrip()
