"""Persist only sanitized output from repair-environment setup commands."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote, quote_plus, unquote, urlsplit, urlunsplit
from uuid import uuid4

from .diagnostic_output import plain_text, redact
from .repair_control import atomic_json, private_directory

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_SECRET_ENV = re.compile(r"(?i)api.?key|token|password|passwd|secret|credential|authorization")
_URL = re.compile(r"\b(?:https?|ftp|socks5h?)://[^\s<>\"']+", re.I)
_AUTH = re.compile(r"(?im)(\b(?:proxy-)?authorization[\"']?\s*[:=]\s*)[^\r\n]+")
_ASSIGN = re.compile(
    r'''(?ix)(\b[\w-]*(?:api[_-]?key|password|passwd|secret|token|credential)[\w-]*["']?\s*[:=]\s*)
        (?:"[^"]*"|'[^']*'|[^\s,;}]+)'''
)


def sanitize(value, environment=None):
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    text = plain_text(text)
    secrets = set()
    for key, raw in (os.environ if environment is None else environment).items():
        if raw and _SECRET_ENV.search(key):
            secrets.add(raw)
            secrets.add(plain_text(raw))
        for match in _URL.finditer(raw):
            try:
                url = urlsplit(match.group())
                if url.username is not None and url.password is not None:
                    credentials = unquote(url.username) + ":" + unquote(url.password)
                    secrets.add(base64.b64encode(credentials.encode()).decode())
                for credential in (url.username, url.password):
                    if credential:
                        secrets.add(unquote(credential))
            except ValueError:
                pass

    def url_without_credentials(match):
        try:
            url = urlsplit(match.group())
            host = url.netloc.rsplit("@", 1)[-1]
            if "@" in url.netloc:
                host = "<redacted>@" + host
            return urlunsplit((url.scheme, host, url.path,
                               "<redacted>" if url.query else "", "<redacted>" if url.fragment else ""))
        except ValueError:
            return "<redacted-url>"

    text = _URL.sub(url_without_credentials, text)
    text = _ASSIGN.sub(lambda match: match.group(1) + "<redacted>", text)
    variants = {variant for secret in secrets if secret for variant in (secret, quote(secret, safe=""), quote_plus(secret, safe=""))}
    text = redact(text, tuple(sorted(variants, key=len, reverse=True)))
    return _AUTH.sub(lambda match: match.group(1) + "<redacted>", text)


def _bounded(text):
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text, False
    marker = "\n[output truncated after redaction]\n"
    half = max(1, (MAX_OUTPUT_BYTES - len(marker.encode())) // 2)
    return (encoded[:half].decode("utf-8", errors="ignore") + marker
            + encoded[-half:].decode("utf-8", errors="ignore")), True


class EnvironmentSetupLog:
    def __init__(self, config):
        root = Path(config["root"])
        self.controller_root = root
        job = os.environ.get("AUTO_AGENTS_REPAIR_JOB", "")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job):
            root = root / "jobs" / job
        self.root = root / "environment-setup" / uuid4().hex
        self.sequence = 0

    def run(self, command, **options):
        supplied_environment = options.get("env")
        environment = dict(os.environ if supplied_environment is None else supplied_environment)
        options["env"] = environment
        started = time.monotonic()
        result, error = None, None
        try:
            result = subprocess.run(command, check=True, capture_output=True, **options)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as caught:
            error = caught
            error.environment_error = sanitize(f"{type(error).__name__}: {error}", environment)[:2000]
        self.sequence += 1
        directory = self.root / f"{self.sequence:02d}"
        try:
            private_directory(directory)
            record = {"schema_version": 1, "command": [sanitize(arg, environment) for arg in command],
                      "status": "timeout" if isinstance(error, subprocess.TimeoutExpired) else "failed" if error else "passed",
                      "returncode": getattr(error or result, "returncode", None),
                      "timeout_seconds": options.get("timeout"), "duration_seconds": time.monotonic() - started}
            if error is not None:
                record["error"] = error.environment_error
            for stream in ("stdout", "stderr"):
                output = getattr(error or result, stream, None)
                text, truncated = _bounded(sanitize(output, environment))
                path = directory / (stream + ".txt")
                # Raw output is never written, even to a temporary file.
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                record[stream] = str(path)
                record[stream + "_truncated"] = truncated
            atomic_json(directory / "command.json", record)
            from .artifact_runtime import track
            track(directory, "evidence", scope="repair:" + str(self.controller_root))
            if error is not None:
                error.environment_diagnostics = {"directory": str(directory), "metadata": str(directory / "command.json"),
                                                 "stdout": record["stdout"], "stderr": record["stderr"]}
        except OSError as logging_error:
            if error is not None:
                error.environment_diagnostics_error = sanitize(logging_error, environment)
            else:
                # Logging must not change the outcome of a successful install.
                import sys
                print("Environment diagnostics could not be saved: " + sanitize(logging_error, environment), file=sys.stderr)
        if error is not None:
            raise error
        return result


def failure_result(error):
    result = {"ok": False, "error": getattr(error, "environment_error", None) or sanitize(f"{type(error).__name__}: {error}")[:2000]}
    diagnostics = getattr(error, "environment_diagnostics", None)
    if diagnostics:
        result["environment_diagnostics"] = diagnostics
        result["error"] += "; diagnostics: " + diagnostics["metadata"]
    if getattr(error, "environment_diagnostics_error", None):
        result["environment_diagnostics_error"] = error.environment_diagnostics_error
    return result
