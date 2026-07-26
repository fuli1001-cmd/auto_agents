from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
from typing import Dict, List, Sequence
from urllib.request import urlopen

from .execution_recovery import ExecutionIncident
from .workers import enrich_worker_probe, worker_probe


@dataclass
class InfrastructureRepairResult:
    repaired: bool
    capability: str
    action: str
    reason: str
    environment: Dict[str, str] = field(default_factory=dict)
    manifest_path: str = ""
    artifact_fingerprint: str = ""
    probe: Dict[str, object] = field(default_factory=dict)
    candidate_attempts: List[Dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _incident_capability(incident: ExecutionIncident) -> str:
    text = " ".join(
        [
            incident.command,
            incident.stdout_tail,
            incident.stderr_tail,
            str(incident.process_snapshot.get("infrastructure_failure_id", "")),
        ]
    ).lower()
    if any(token in text for token in ("browser", "chrome", "chromium", "devtools")):
        return "chrome"
    return ""


def _managed_root() -> Path:
    configured = os.environ.get("AUTO_AGENTS_WORKER_MANAGED_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".local" / "share" / "auto-agents-worker"


_CHROME_LINUX_AMD64 = {
    "version": "150.0.7871.186-1",
    "url": (
        "https://dl.google.com/linux/chrome/deb/pool/main/g/"
        "google-chrome-stable/google-chrome-stable_150.0.7871.186-1_amd64.deb"
    ),
    "size": 133561688,
    "sha256": "4193e00b6d5d5969ee63f7a69596868f546aa0e8cb077b3e0bf9cc1e2c719d00",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chrome_cdp_probe(binary: Path, timeout_seconds: float = 20.0) -> Dict[str, object]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="auto-agents-chrome-cdp-") as root:
        data_dir = Path(root)
        process = subprocess.Popen(
            [
                str(binary),
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--remote-debugging-port=0",
                f"--user-data-dir={data_dir}",
                "about:blank",
            ],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            active_port = data_dir / "DevToolsActivePort"
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=1)
                    return {
                        "state": "unhealthy",
                        "probe_kind": "chrome_cdp_v1",
                        "returncode": process.returncode,
                        "stderr": (stderr or stdout)[-2000:],
                        "elapsed_seconds": time.monotonic() - started,
                    }
                if active_port.exists():
                    lines = active_port.read_text(encoding="utf-8").splitlines()
                    if lines and lines[0].isdigit():
                        with urlopen(
                            f"http://127.0.0.1:{lines[0]}/json/version",
                            timeout=3,
                        ) as response:
                            payload = json.loads(response.read().decode("utf-8"))
                        return {
                            "state": "healthy",
                            "probe_kind": "chrome_cdp_v1",
                            "browser": str(payload.get("Browser", "")),
                            "protocol_version": str(
                                payload.get("Protocol-Version", "")
                            ),
                            "elapsed_seconds": time.monotonic() - started,
                        }
                time.sleep(0.1)
            return {
                "state": "unhealthy",
                "probe_kind": "chrome_cdp_v1",
                "error": "DevTools endpoint did not become ready before the probe deadline",
                "elapsed_seconds": time.monotonic() - started,
            }
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {
                "state": "unhealthy",
                "probe_kind": "chrome_cdp_v1",
                "error": str(error),
                "elapsed_seconds": time.monotonic() - started,
            }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def _cached_chrome_candidates(root: Path) -> List[Path]:
    candidates: List[Path] = []
    runtime_root = root / "runtimes" / "chrome"
    if not runtime_root.exists():
        return candidates
    for manifest in sorted(runtime_root.glob("*/manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            binary = Path(str(payload.get("path", "")))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if binary.is_file():
            candidates.append(binary)
    return candidates


def _system_chrome_candidates() -> List[Path]:
    paths: List[Path] = []
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        resolved = shutil.which(name)
        candidate = Path(resolved).resolve() if resolved else None
        if candidate is not None and candidate not in paths:
            paths.append(candidate)
    return paths


def _download_managed_chrome(root: Path) -> Path:
    catalog = _CHROME_LINUX_AMD64
    fingerprint = str(catalog["sha256"])
    runtime_root = root / "runtimes" / "chrome" / fingerprint
    binary = runtime_root / "opt" / "google" / "chrome" / "google-chrome"
    package_cache = runtime_root / "package.deb"
    if (
        binary.is_file()
        and package_cache.is_file()
        and _sha256(package_cache) == fingerprint
    ):
        return binary
    if platform.system().lower() != "linux" or platform.machine() != "x86_64":
        raise RuntimeError("managed Chrome download is supported only on Linux x86_64")
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="auto-agents-chrome-install-",
        dir=runtime_root.parent,
    ) as temporary:
        temporary_root = Path(temporary)
        package = temporary_root / "package.deb"
        expected_size = int(catalog["size"])
        with urlopen(str(catalog["url"]), timeout=60) as response, package.open(
            "wb"
        ) as output:
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    raise RuntimeError("managed Chrome artifact exceeded its pinned size")
                output.write(chunk)
        if received != expected_size or _sha256(package) != fingerprint:
            raise RuntimeError("managed Chrome artifact failed pinned size/SHA-256 validation")
        extracted = temporary_root / "runtime"
        extracted.mkdir()
        unpack = subprocess.run(
            ["dpkg-deb", "--extract", str(package), str(extracted)],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=120,
            check=False,
        )
        if unpack.returncode != 0:
            raise RuntimeError(
                f"managed Chrome extraction failed: {(unpack.stderr or unpack.stdout)[-1000:]}"
            )
        extracted_binary = extracted / "opt" / "google" / "chrome" / "google-chrome"
        if not extracted_binary.is_file():
            raise RuntimeError("managed Chrome artifact omitted its browser executable")
        bin_root = extracted / "bin"
        bin_root.mkdir()
        relative_binary = Path("..") / "opt" / "google" / "chrome" / "google-chrome"
        (bin_root / "google-chrome").symlink_to(relative_binary)
        (bin_root / "google-chrome-stable").symlink_to(relative_binary)
        shutil.copy2(package, extracted / "package.deb")
        (extracted / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "capability": "chrome",
                    "version": catalog["version"],
                    "path": str(binary),
                    "artifact_sha256": fingerprint,
                    "source": catalog["url"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if runtime_root.exists():
            shutil.rmtree(runtime_root)
        extracted.replace(runtime_root)
    return binary


def repair_execution_infrastructure(
    incident: ExecutionIncident,
    *,
    failed_artifacts: Sequence[str] = (),
    allow_downloads: bool = True,
    max_candidates: int = 3,
) -> InfrastructureRepairResult:
    """Perform only allowlisted, user-owned runtime recovery operations."""
    capability = _incident_capability(incident)
    if not capability:
        return InfrastructureRepairResult(
            repaired=False,
            capability="",
            action="no_managed_driver",
            reason="no managed capability repair driver matches this incident",
        )
    if capability != "chrome":
        return InfrastructureRepairResult(
            repaired=False,
            capability=capability,
            action="unsupported_capability",
            reason=f"managed repair is not implemented for {capability}",
        )

    root = _managed_root()
    crash_root = root / "crashes" / incident.run_id / incident.incident_id
    report_root = root / "infrastructure-reports" / incident.run_id
    crash_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)
    excluded = {str(item) for item in failed_artifacts if str(item)}
    candidates = _cached_chrome_candidates(root) + _system_chrome_candidates()
    attempts: List[Dict[str, object]] = []
    installed = False
    considered: set[str] = set()
    while len(attempts) < max(1, int(max_candidates)):
        if candidates:
            candidate = candidates.pop(0)
        elif allow_downloads and not installed:
            installed = True
            try:
                candidate = _download_managed_chrome(root)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                attempts.append(
                    {"source": "managed_download", "state": "failed", "error": str(error)}
                )
                break
        else:
            break
        try:
            fingerprint = _sha256(candidate)
        except OSError as error:
            attempts.append({"path": str(candidate), "state": "failed", "error": str(error)})
            continue
        if fingerprint in excluded or fingerprint in considered:
            continue
        considered.add(fingerprint)
        deep_probe = _chrome_cdp_probe(candidate)
        attempts.append(
            {
                "path": str(candidate),
                "artifact_sha256": fingerprint,
                "probe": deep_probe,
            }
        )
        if deep_probe.get("state") != "healthy":
            continue
        version = str(deep_probe.get("browser", ""))
        runtime_root = candidate.parent
        managed_parent = root / "runtimes" / "chrome"
        for parent in candidate.parents:
            if parent.parent == managed_parent:
                runtime_root = parent
                break
        manifest_path = runtime_root / "manifest.json"
        environment = {
            "AUTO_AGENTS_CAPABILITY_CHROME_PATH": str(candidate),
            "AUTO_AGENTS_CAPABILITY_CHROME_VERSION": version,
            "AUTO_AGENTS_CAPABILITY_CHROME_SHA256": fingerprint,
            "AUTO_AGENTS_PATH_PREPEND": str(runtime_root / "bin"),
            "AUTO_AGENTS_CRASH_DIR": str(crash_root),
            "AUTO_AGENTS_INFRA_REPORT_PATH": str(
                report_root / f"{incident.incident_id}.json"
            ),
        }
        return InfrastructureRepairResult(
            repaired=True,
            capability="chrome",
            action="selected_cdp_healthy_runtime",
            reason="Chrome passed the managed CDP launch probe",
            environment=environment,
            manifest_path=str(manifest_path) if manifest_path.exists() else "",
            artifact_fingerprint=fingerprint,
            probe=deep_probe,
            candidate_attempts=attempts,
        )
    return InfrastructureRepairResult(
        repaired=False,
        capability="chrome",
        action="managed_candidates_exhausted",
        reason="no non-quarantined Chrome candidate passed the managed CDP probe",
        candidate_attempts=attempts,
    )


def managed_diagnostic_refs(incident: ExecutionIncident) -> List[Dict[str, object]]:
    root = _managed_root()
    candidates = [
        root / "crashes" / incident.run_id / incident.incident_id,
        root / "infrastructure-reports" / incident.run_id,
    ]
    refs: List[Dict[str, object]] = []
    total = 0
    for directory in candidates:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > 16 * 1024 * 1024 or total + size > 32 * 1024 * 1024:
                continue
            refs.append(
                {
                    "path": str(path),
                    "size": size,
                    "kind": (
                        "minidump"
                        if path.suffix == ".dmp"
                        else "infrastructure_report"
                        if path.suffix == ".json"
                        else "crash_log"
                    ),
                }
            )
            total += size
    return refs


def scoped_verification_repair_instructions() -> str:
    return (
        "Repair only verification launchers, runtime selection/locks, or diagnostic "
        "collection. Consume AUTO_AGENTS_CAPABILITY_<NAME>_PATH when compatible. "
        "Do not skip, delete, focus, weaken, or remove tests/assertions; do not lower "
        "runtime minimums, remove required capabilities, suppress infrastructure "
        "markers, or fabricate evidence. The exact original verification command must "
        "pass before integration."
    )


_FORBIDDEN_REPAIR_PATTERNS = (
    re.compile(r"^\+.*\.(?:skip|todo|only)\s*\(", re.MULTILINE),
    re.compile(r"^\+.*(?:pytest\.mark\.skip|pytest\.skip)\b", re.MULTILINE),
    re.compile(r"^\+.*AUTO_AGENTS_INFRA_FAILURE.*(?:disable|false)", re.MULTILINE),
    re.compile(r"^\-.*(?:expect|assert|should)\b", re.MULTILINE),
)


def verification_repair_guard(diff_text: str) -> List[str]:
    """Reject obvious attempts to weaken verification during scoped repair."""
    findings: List[str] = []
    labels = (
        "new skipped or focused test",
        "new pytest skip",
        "infrastructure reporting disabled",
        "assertion removed",
    )
    for label, pattern in zip(labels, _FORBIDDEN_REPAIR_PATTERNS):
        if pattern.search(diff_text):
            findings.append(label)
    return findings
