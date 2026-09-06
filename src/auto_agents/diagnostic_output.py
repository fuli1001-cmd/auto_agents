"""Durable output capture; these files are evidence, never execution inputs."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional
from uuid import uuid4


_SECRET_KEY = re.compile(r"(?i)(?:api.?key|authorization|password|secret|(?:^|[_-])(?:access.?|refresh.?)?token(?:$|[_-]))")
_SECRET = re.compile(
    r"""(?ix)(\b[\w-]*(?:api[_-]?key|password|secret|access[_-]?token|refresh[_-]?token|token)\b
    ["']?\s*(?:[:=]|\s)\s*["']?)([^\s"',;}]+)"""
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s\"',;]+")
_TERMINAL_CONTROL = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])|[\x00-\x08\x0b-\x1f\x7f]")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return _BEARER.sub(r"\1<redacted>", _SECRET.sub(r"\1<redacted>", text))


def plain_text(value: object) -> str:
    return _TERMINAL_CONTROL.sub("", str(value or ""))


def clean_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): ("<redacted>" if _SECRET_KEY.search(str(key)) else clean_payload(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean_payload(item) for item in value]
    return redact(value) if isinstance(value, str) else value


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class OutputCapture:
    """A callable output observer independent of the visible stream callback.

    Lines are assembled before redaction so secrets split between reads cannot
    leak. Captures own no renderer/health locks and never raise into a provider.
    """

    def __init__(
        self, root: Path, metadata: Mapping[str, object], *,
        register: Callable[[Path, Mapping[str, object]], None],
        failed: Callable[[Exception], None],
        enabled: bool = True,
    ) -> None:
        self.root = root
        self.metadata = {**dict(metadata), "started_at": now(), "status": "running" if enabled else "closed"}
        self._register = register
        self._failed = failed
        self._pending = {"stdout": "", "stderr": ""}
        self._secrets: tuple[str, ...] = ()
        self._lock = threading.RLock()
        self._finished = not enabled
        self._disabled = not enabled
        self._write_metadata()

    def start(self, command: object, env: Mapping[str, str], **metadata: object) -> None:
        with self._lock:
            if self._finished:
                return
            self._secrets = tuple(sorted(
                {*self._secrets, *(str(value) for key, value in env.items()
                 if _SECRET_KEY.search(str(key)) and len(str(value)) >= 4)},
                key=len, reverse=True,
            ))
            self.metadata.update(metadata)
            if isinstance(command, (tuple, list)):
                masked = []
                mask_next = False
                for item in command:
                    value = str(item)
                    masked.append("<redacted>" if mask_next else redact(value, self._secrets))
                    mask_next = bool(value.startswith("-") and "=" not in value
                                     and (_SECRET_KEY.search(value.lstrip("-")) or value == "--token"))
                self.metadata["command"] = masked
            else:
                self.metadata["command"] = redact(command, self._secrets)
            self._write_metadata()

    def protect(self, values: tuple[str, ...]) -> None:
        self._secrets = tuple(sorted({*self._secrets, *(str(value) for value in values if value)},
                                    key=len, reverse=True))

    def _write_metadata(self) -> None:
        if self._disabled:
            return
        try:
            atomic_json(self.root / "attempt.json", clean_payload(self.metadata))
            self._register(self.root / "attempt.json", self.metadata)
        except Exception as error:
            self._disabled = True
            self._failed(error)

    def _write(self, stream: str, text: str) -> None:
        if not text or self._disabled:
            return
        try:
            path = self.root / f"{stream}.txt"
            with path.open("a", encoding="utf-8") as output:
                output.write(redact(text, self._secrets))
            self._register(path, self.metadata)
        except Exception as error:
            self._disabled = True
            self._failed(error)

    def __call__(self, stream: str, chunk: str) -> None:
        if stream not in self._pending or self._finished:
            return
        with self._lock:
            if self._finished:
                return
            combined = self._pending[stream] + str(chunk)
            boundary = combined.rfind("\n") + 1
            self._write(stream, combined[:boundary])
            self._pending[stream] = combined[boundary:]

    def file(self, stream: str, source: object) -> None:
        """Archive before the caller bounds the returned output or closes it."""
        if self._finished:
            return
        try:
            position = source.tell()
            source.seek(0)
            try:
                for line in source:
                    self(stream, line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line)
            finally:
                source.seek(position)
        except Exception as error:
            self._failed(error)

    def finish(self, **metadata: object) -> None:
        with self._lock:
            if self._finished:
                return
            for stream, pending in self._pending.items():
                self._write(stream, pending)
            self._pending = {"stdout": "", "stderr": ""}
            self._finished = True
            self.metadata.update({"status": "finished", "finished_at": now(), **metadata})
            self._write_metadata()


def content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def diagnostic_attachments(project_root: Path, run_id: str = "") -> list[dict]:
    """Select recorded attempts, following only project-local diagnostic indexes."""
    base = (Path(project_root) / ".auto-agents").resolve()
    indexes = []
    if run_id:
        indexes.append(base / "runs" / run_id / "diagnostics.json")
    from .reporting import find_reporter
    reporter = find_reporter(project_root)
    if reporter is not None and reporter.root is not None:
        indexes.append(reporter.root / "diagnostics.json")
    visited: set[str] = set()
    artifacts: dict[str, dict] = {}
    while indexes:
        index = indexes.pop()
        try:
            resolved = index.resolve()
            resolved.relative_to(base)
            if str(resolved) in visited:
                continue
            visited.add(str(resolved))
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            for entry in payload.get("artifacts", {}).values():
                if not isinstance(entry, dict):
                    continue
                path = Path(str(entry.get("path", ""))).resolve()
                relative = path.relative_to(base)
                if path.name == "diagnostics.json":
                    indexes.append(path)
                elif "diagnostic-output" in relative.parts and path.name in {"attempt.json", "stdout.txt", "stderr.txt"}:
                    if str(entry.get("stage", "")).startswith(("self_repair", "root-cause")):
                        continue
                    # Attempt metadata includes observation timestamps. Keep them
                    # available locally but do not make them certificate inputs.
                    if path.name != "attempt.json" and path.is_file():
                        artifacts[str(path)] = {
                            "path": str(path), "kind": path.stem,
                            **{key: entry[key] for key in ("task_id", "stage", "attempt_id") if key in entry},
                        }
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    # Only hash/copy relevant attempts; a long run may contain gigabytes of
    # unrelated successful output. Full originals remain in their owning index.
    try:
        state = json.loads((base / "state/run_state.json").read_text(encoding="utf-8"))
        blocker = state.get("active_blocker", {})
        task_id = str(blocker.get("task_id", "")) if isinstance(blocker, dict) else ""
    except (OSError, ValueError, AttributeError):
        task_id = ""
    selected = list(artifacts.values())
    owned = [item for item in selected if task_id and item.get("task_id") == task_id]
    if owned:
        selected = owned
    groups = {}
    for item in selected:
        path = Path(item["path"])
        try:
            groups[path.parent] = max(groups.get(path.parent, 0), path.stat().st_mtime_ns)
        except OSError:
            continue
    recent = set(sorted(groups, key=groups.get)[-24:])
    result = []
    for item in selected:
        path = Path(item["path"])
        if path.parent not in recent:
            continue
        try:
            result.append({**item, "sha256": content_hash(path)})
        except OSError:
            continue
    return sorted(result, key=lambda item: (str(item.get("attempt_id", "")), item["kind"], item["sha256"]))


def copy_diagnostic_attachments(attachments: list[dict], destination: Path) -> list[dict]:
    """Make evidence independently readable after the original run is gone."""
    copied = []
    root = destination / ".auto-agents" / "diagnostic-evidence"
    for item in attachments:
        try:
            path = Path(item["path"])
            digest = str(item["sha256"])
            if not re.fullmatch(r"[a-f0-9]{64}", digest) or content_hash(path) != digest:
                continue
            target = root / (digest + ".txt")
            root.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(path, target)
            copied.append({**item, "path": str(target)})
        except (OSError, KeyError, ValueError):
            continue
    if copied:
        atomic_json(root / "index.json", {"schema_version": 1, "artifacts": copied})
    return copied
