from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from .config import (
    ensure_auto_gitignore,
    operator_dir,
    operator_inputs_path,
    operator_secrets_path,
)
from .io_utils import read_json


INPUT_KINDS = {
    "boolean",
    "choice",
    "text",
    "url",
    "path",
    "secret",
    "attestation",
    "install_approval",
}
PERSISTENCE_SCOPES = {"one_time", "run", "project"}
SENSITIVITY_LEVELS = {"public", "private", "secret"}
ECHO_MODES = {"auto", "visible", "hidden"}
PROJECTIONS = {"value", "artifact_path", "runtime_path", "version", "sha256"}
_INPUT_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRET_LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UserInputRequest:
    request_id: str
    key: str
    kind: str
    question: str
    purpose: str
    why_required: str
    how_to_obtain: List[str] = field(default_factory=list)
    recommended_answer: str = ""
    default: object = ""
    persistence: str = "project"
    sensitivity: str = "private"
    validation: Dict[str, object] = field(default_factory=dict)
    bindings: List[Dict[str, object]] = field(default_factory=list)
    task_id: str = ""
    stage: str = "implement"
    requirement_ids: List[str] = field(default_factory=list)
    subject_fingerprint: str = ""
    question_version: int = 1
    status: str = "pending"
    created_at: str = ""
    answered_at: str = ""
    answer_ref: str = ""
    validation_error: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "UserInputRequest":
        key = str(payload.get("key", "")).strip()
        request_id = str(payload.get("request_id", "")).strip()
        if not request_id and key:
            request_id = hashlib.sha256(
                json.dumps(
                    {
                        "key": key,
                        "subject": str(payload.get("subject_fingerprint", "")),
                        "version": int(payload.get("question_version", 1) or 1),
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:16]
        request = cls(
            request_id=request_id,
            key=key,
            kind=str(payload.get("kind", "text")).strip().lower(),
            question=str(payload.get("question", "")).strip(),
            purpose=str(payload.get("purpose", "")).strip(),
            why_required=str(payload.get("why_required", "")).strip(),
            how_to_obtain=[
                str(item).strip()
                for item in (payload.get("how_to_obtain", []) or [])
                if str(item).strip()
            ],
            recommended_answer=str(
                payload.get("recommended_answer", payload.get("recommendation", ""))
            ).strip(),
            default=payload.get("default", ""),
            persistence=str(payload.get("persistence", "project")).strip(),
            sensitivity=str(payload.get("sensitivity", "private")).strip(),
            validation=(
                dict(payload.get("validation", {}))
                if isinstance(payload.get("validation", {}), Mapping)
                else {}
            ),
            bindings=[
                dict(item)
                for item in (payload.get("bindings", []) or [])
                if isinstance(item, Mapping)
            ],
            task_id=str(payload.get("task_id", "")).strip(),
            stage=str(payload.get("stage", "implement")).strip() or "implement",
            requirement_ids=[
                str(item).strip()
                for item in (payload.get("requirement_ids", []) or [])
                if str(item).strip()
            ],
            subject_fingerprint=str(payload.get("subject_fingerprint", "")).strip(),
            question_version=max(1, int(payload.get("question_version", 1) or 1)),
            status=str(payload.get("status", "pending")).strip() or "pending",
            created_at=str(payload.get("created_at", "")).strip() or utc_now_iso(),
            answered_at=str(payload.get("answered_at", "")).strip(),
            answer_ref=str(payload.get("answer_ref", "")).strip(),
            validation_error=str(payload.get("validation_error", "")).strip(),
        )
        errors = request.errors()
        if errors:
            raise ValueError("invalid user input request: " + "; ".join(errors))
        return request

    def errors(self) -> List[str]:
        errors: List[str] = []
        if not _INPUT_KEY.fullmatch(self.key):
            errors.append("key must be a stable lowercase dotted identifier")
        if not self.request_id:
            errors.append("request_id is required")
        if self.kind not in INPUT_KINDS:
            errors.append(f"unsupported kind: {self.kind}")
        if not self.question:
            errors.append("question is required")
        if not self.purpose:
            errors.append("purpose is required")
        if not self.why_required:
            errors.append("why_required is required")
        if self.persistence not in PERSISTENCE_SCOPES:
            errors.append(f"unsupported persistence: {self.persistence}")
        if self.sensitivity not in SENSITIVITY_LEVELS:
            errors.append(f"unsupported sensitivity: {self.sensitivity}")
        if self.kind in {"attestation", "install_approval"} and self.default not in {
            "",
            False,
            "false",
            "n",
            "no",
        }:
            errors.append("attestations and install approvals must default to no")
        for binding in self.bindings:
            input_key = str(binding.get("input_key", self.key)).strip()
            env = str(binding.get("env", "")).strip()
            projection = str(binding.get("projection", "value")).strip()
            if not _INPUT_KEY.fullmatch(input_key):
                errors.append(f"invalid binding input key: {input_key}")
            if not _ENV_NAME.fullmatch(env):
                errors.append(f"invalid binding environment name: {env}")
            if projection not in PROJECTIONS:
                errors.append(f"invalid binding projection: {projection}")
        return errors

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    def render(self) -> str:
        lines = [
            f"问题：{self.question}",
            f"作用：{self.purpose}",
        ]
        if self.how_to_obtain:
            lines.append("如何获得或回答：")
            lines.extend(f"- {item}" for item in self.how_to_obtain)
        if self.recommended_answer:
            lines.append(f"建议：{self.recommended_answer}")
        return "\n".join(lines)


class OperatorInputStore:
    """Project-local, Git-ignored operator input storage.

    Ordinary/private values live in inputs.json. Secrets live in secrets.env and
    records contain only the environment key and a monotonically changing
    revision. Neither file is copied into gate worktrees.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.root = operator_dir(self.project_root)
        self.inputs_path = operator_inputs_path(self.project_root)
        self.secrets_path = operator_secrets_path(self.project_root)

    def ensure(self) -> None:
        ensure_auto_gitignore(self.project_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.root.chmod(0o700)

    def _read_payload(self) -> Dict[str, object]:
        payload = read_json(self.inputs_path, default=None)
        if not isinstance(payload, dict):
            return {"version": 1, "records": {}}
        records = payload.get("records", {})
        if not isinstance(records, dict):
            records = {}
        return {"version": 1, "records": records}

    def records(self) -> Dict[str, Dict[str, object]]:
        payload = self._read_payload()
        return {
            str(key): dict(value)
            for key, value in dict(payload.get("records", {})).items()
            if isinstance(value, dict)
        }

    def get(self, key: str) -> Optional[Dict[str, object]]:
        record = self.records().get(key)
        return dict(record) if record is not None else None

    def _atomic_text(self, path: Path, text: str) -> None:
        self.ensure()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name == "posix":
                path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _write_records(self, records: Mapping[str, object]) -> None:
        self._atomic_text(
            self.inputs_path,
            json.dumps(
                {"version": 1, "records": dict(records)},
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )

    def _read_secrets(self) -> Dict[str, str]:
        if not self.secrets_path.is_file():
            return {}
        secrets: Dict[str, str] = {}
        for line in self.secrets_path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#"):
                continue
            match = _SECRET_LINE.fullmatch(line)
            if match is None:
                raise ValueError("operator secrets.env contains an invalid line")
            key, encoded = match.groups()
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid encoded secret for {key}") from error
            if not isinstance(value, str):
                raise ValueError(f"secret {key} must decode to a string")
            secrets[key] = value
        return secrets

    def _write_secrets(self, secrets: Mapping[str, str]) -> None:
        text = "".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n"
            for key, value in sorted(secrets.items())
        )
        self._atomic_text(self.secrets_path, text)

    @staticmethod
    def _secret_env_key(input_key: str) -> str:
        return "AUTO_AGENTS_INPUT_" + re.sub(r"[^A-Za-z0-9]", "_", input_key).upper()

    def save_answer(
        self,
        request: UserInputRequest,
        value: object,
        *,
        source: str = "interactive",
    ) -> Dict[str, object]:
        normalized, error = validate_answer(request, value)
        if error:
            raise ValueError(error)
        records = self.records()
        previous = records.get(request.key, {})
        revision = int(previous.get("revision", 0) or 0) + 1
        record: Dict[str, object] = {
            "key": request.key,
            "kind": request.kind,
            "persistence": request.persistence,
            "sensitivity": request.sensitivity,
            "subject_fingerprint": request.subject_fingerprint,
            "question_version": request.question_version,
            "answered_at": utc_now_iso(),
            "source": source,
            "revision": revision,
        }
        if request.sensitivity == "secret" or request.kind == "secret":
            if not isinstance(normalized, str):
                raise ValueError("secret answer must be text")
            env_key = self._secret_env_key(request.key)
            secrets = self._read_secrets()
            secrets[env_key] = normalized
            self._write_secrets(secrets)
            record.update({"secret_env": env_key, "present": True})
        else:
            record["value"] = normalized
        if request.kind == "attestation" and normalized is True:
            artifact = self._write_attestation(request)
            record["artifact_path"] = str(artifact)
            record["rights_basis"] = "ownership_or_explicit_permission_attested"
        records[request.key] = record
        self._write_records(records)
        return dict(record)

    def _write_attestation(self, request: UserInputRequest) -> Path:
        artifact_dir = self.root / "attestations"
        artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = artifact_dir / f"{request.request_id}.json"
        claims = request.validation.get("claims", [])
        raw_subject = request.validation.get("subject", {})
        subject = dict(raw_subject) if isinstance(raw_subject, Mapping) else {}
        resolved_subject: Dict[str, object] = {}
        for name, value in subject.items():
            if str(name).endswith("_input_key"):
                referenced = self.value_for(str(value))
                if referenced is not None:
                    resolved_subject[str(name)[: -len("_input_key")]] = referenced
            else:
                resolved_subject[str(name)] = value
        payload = {
            "version": 1,
            "request_id": request.request_id,
            "input_key": request.key,
            "question": request.question,
            "question_version": request.question_version,
            "authorization_attested": True,
            "rights_basis": "ownership_or_explicit_permission_attested",
            "authorized_uses": list(claims) if isinstance(claims, list) else [],
            "stable_test_use": bool(
                request.validation.get("stable_test_use", True)
            ),
            "subject": resolved_subject,
            "declared_at": utc_now_iso(),
        }
        for compatibility_key in ("source_url", "video_id"):
            if compatibility_key in resolved_subject:
                payload[compatibility_key] = resolved_subject[compatibility_key]
        self._atomic_text(
            path,
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
        return path

    def remove(self, key: str) -> bool:
        records = self.records()
        record = records.pop(key, None)
        if record is None:
            return False
        secret_env = str(record.get("secret_env", "")).strip()
        if secret_env:
            secrets = self._read_secrets()
            secrets.pop(secret_env, None)
            self._write_secrets(secrets)
        artifact = str(record.get("artifact_path", "")).strip()
        if artifact:
            path = Path(artifact)
            try:
                path.relative_to(self.root)
            except ValueError:
                pass
            else:
                if path.is_file():
                    path.unlink()
        self._write_records(records)
        return True

    def save_runtime_records(
        self,
        runtime_records: Mapping[str, Mapping[str, object]],
        *,
        manifest_fingerprint: str,
    ) -> Dict[str, Dict[str, object]]:
        records = self.records()
        saved: Dict[str, Dict[str, object]] = {}
        for tool_id, runtime in runtime_records.items():
            key = f"runtime.{tool_id}"
            previous = records.get(key, {})
            record = {
                "key": key,
                "kind": "runtime",
                "persistence": "project",
                "sensitivity": "private",
                "subject_fingerprint": manifest_fingerprint,
                "question_version": 1,
                "answered_at": utc_now_iso(),
                "source": "project-runtime-manager",
                "revision": int(previous.get("revision", 0) or 0) + 1,
                "runtime_path": str(runtime.get("runtime_path", "")),
                "version": str(runtime.get("version", "")),
                "sha256": str(runtime.get("sha256", "")),
                "source_sha256": str(runtime.get("source_sha256", "")),
                "trust": str(runtime.get("trust", "")),
            }
            records[key] = record
            saved[key] = dict(record)
        self._write_records(records)
        return saved

    def is_valid(self, request: UserInputRequest) -> Tuple[bool, str]:
        record = self.get(request.key)
        if record is None:
            return False, "answer is missing"
        if int(record.get("question_version", 0) or 0) != request.question_version:
            return False, "the question contract changed"
        if str(record.get("subject_fingerprint", "")) != request.subject_fingerprint:
            return False, "the bound subject changed"
        if request.sensitivity == "secret" or request.kind == "secret":
            env_key = str(record.get("secret_env", ""))
            if not env_key or not self._read_secrets().get(env_key):
                return False, "the stored secret is unavailable"
            return True, ""
        value = record.get("value")
        if request.kind == "attestation" and value is not True:
            return False, "authorization was not attested"
        _normalized, error = validate_answer(request, value)
        if error:
            return False, error
        artifact = str(record.get("artifact_path", "")).strip()
        if artifact and not Path(artifact).is_file():
            return False, "the generated input artifact is missing"
        if request.kind == "path" and bool(request.validation.get("must_exist", False)):
            if not Path(str(value)).expanduser().exists():
                return False, "the configured path no longer exists"
        return True, ""

    def value_for(self, key: str, projection: str = "value") -> Optional[str]:
        record = self.get(key)
        if record is None:
            return None
        if projection == "artifact_path":
            return str(record.get("artifact_path", "")) or None
        if projection in {"runtime_path", "version", "sha256"}:
            value = record.get(projection)
            if projection == "runtime_path" and value and not Path(str(value)).is_file():
                return None
            return str(value) if value not in (None, "") else None
        secret_env = str(record.get("secret_env", "")).strip()
        if secret_env:
            return self._read_secrets().get(secret_env)
        value = record.get("value")
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value) if value not in (None, "") else None

    def environment(
        self, bindings: Iterable[Mapping[str, object]]
    ) -> Tuple[Dict[str, str], List[str]]:
        environment: Dict[str, str] = {}
        missing: List[str] = []
        for binding in bindings:
            key = str(binding.get("input_key", "")).strip()
            env = str(binding.get("env", "")).strip()
            projection = str(binding.get("projection", "value")).strip()
            required = bool(binding.get("required", True))
            if not _INPUT_KEY.fullmatch(key) or not _ENV_NAME.fullmatch(env):
                raise ValueError("invalid operator input binding")
            if projection not in PROJECTIONS:
                raise ValueError(f"invalid operator input projection: {projection}")
            value = self.value_for(key, projection)
            if value is None:
                if required:
                    missing.append(key)
                continue
            environment[env] = value
        return environment, list(dict.fromkeys(missing))

    def fingerprint(self, keys: Iterable[str] = ()) -> str:
        selected = set(str(item) for item in keys if str(item))
        payload = []
        for key, record in sorted(self.records().items()):
            if selected and key not in selected:
                continue
            payload.append(
                {
                    "key": key,
                    "revision": int(record.get("revision", 0) or 0),
                    "question_version": int(record.get("question_version", 0) or 0),
                    "subject_fingerprint": str(record.get("subject_fingerprint", "")),
                    "present": bool(
                        record.get("present", record.get("value") is not None)
                    ),
                }
            )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()


def parse_boolean(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"y", "yes", "true", "1"}:
        return True
    if normalized in {"n", "no", "false", "0", ""}:
        return False
    return None


def validate_answer(
    request: UserInputRequest, value: object
) -> Tuple[object, str]:
    if request.kind in {"boolean", "attestation", "install_approval"}:
        parsed = parse_boolean(value)
        if parsed is None:
            return value, "please answer y or n"
        return parsed, ""
    text = str(value).strip()
    if not text:
        return text, "an answer is required"
    if "\x00" in text:
        return text, "answers must not contain NUL bytes"
    if request.kind == "choice":
        choices = [str(item) for item in request.validation.get("choices", [])]
        if choices and text not in choices:
            return text, "choose one of: " + ", ".join(choices)
    if request.kind == "url":
        parsed = urlparse(text)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return text, "provide a valid HTTP(S) URL"
        if bool(request.validation.get("https_only", False)) and parsed.scheme != "https":
            return text, "the URL must use HTTPS"
        hosts = [str(item).lower() for item in request.validation.get("hosts", [])]
        if hosts and parsed.hostname.lower() not in hosts:
            return text, "the URL host is not in the approved list"
    if request.kind == "path":
        path = Path(text).expanduser()
        if bool(request.validation.get("must_exist", False)) and not path.exists():
            return text, "the path does not exist"
        text = str(path.resolve())
    return text, ""


def prompt_for_request(
    request: UserInputRequest,
    *,
    echo_mode: str = "auto",
    input_fn=input,
    secret_input_fn=getpass.getpass,
) -> object:
    if echo_mode not in ECHO_MODES:
        raise ValueError(f"unsupported echo mode: {echo_mode}")
    hidden = echo_mode == "hidden" or (
        echo_mode == "auto"
        and (request.kind == "secret" or request.sensitivity == "secret")
    )
    suffix = " [y/N]: " if request.kind in {
        "boolean",
        "attestation",
        "install_approval",
    } else ": "
    prompt = request.render() + "\n回答" + suffix
    return secret_input_fn(prompt) if hidden else input_fn(prompt)
