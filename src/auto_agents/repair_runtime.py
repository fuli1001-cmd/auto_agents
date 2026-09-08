"""Controller-owned admission policy for replaceable repair runtimes."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess


# Versions are minimum behavioral capabilities, not engine commit identities.
# Read this declaration without importing the selected runtime.
RUNTIME_CAPABILITIES = {
    "control_protocol": 1,
    "progress_supervision": 1,
    "acceptance_planning": 2,
    "terminal_repair_status": 1,
}

INCOMPATIBLE_RUNTIME = "repair runtime is incompatible; synchronize the engine versions before retrying"


class RuntimeCompatibilityError(RuntimeError):
    def __init__(self, runtime, reason, *, phase, available=None, checks=None):
        super().__init__(INCOMPATIBLE_RUNTIME)
        self.details = {
            "runtime": str(Path(runtime).resolve()), "phase": phase,
            "reason": reason, "required_capabilities": dict(RUNTIME_CAPABILITIES),
            "available_capabilities": available or {}, "checks": checks or {},
        }

    def to_result(self):
        return {"ok": False, "category": "runtime_incompatible", "error": str(self),
                "runtime_compatibility": self.details}


def require_runtime(runtime, *, phase="worker"):
    runtime = Path(runtime).resolve()
    manifest = runtime / "src/auto_agents/repair_runtime.py"
    try:
        tree = ast.parse(manifest.read_text())
        declarations = [node.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "RUNTIME_CAPABILITIES"
                                for target in node.targets)]
        available = ast.literal_eval(declarations[0]) if len(declarations) == 1 else {}
        if (not isinstance(available, dict)
                or any(not isinstance(name, str) or type(version) is not int or version < 0
                       for name, version in available.items())):
            raise ValueError("capability versions must be nonnegative integers")
    except (OSError, SyntaxError, ValueError, TypeError) as error:
        raise RuntimeCompatibilityError(runtime, "missing or invalid capability declaration",
                                        phase=phase) from error
    missing = [name for name, version in RUNTIME_CAPABILITIES.items()
               if type(available.get(name)) is not int or available[name] < version]
    if missing:
        raise RuntimeCompatibilityError(runtime, "missing capabilities: " + ", ".join(missing),
                                        phase=phase, available=available)
    return available


def verify_runtime(runtime, python, *, phase="worker"):
    """Use the calling controller's probes, never tests supplied by a candidate.

    No receipt skips this check: cached candidates and successful full-suite
    receipts still have to meet the currently installed controller's policy.
    """
    runtime = Path(runtime).resolve()
    available = require_runtime(runtime, phase=phase)
    probe = Path(__file__).with_name("repair_runtime_probe.py")
    environment = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if name in os.environ}
    try:
        completed = subprocess.run(
            [str(python), "-I", str(probe), str(runtime)],
            capture_output=True, text=True, timeout=30, env=environment,
        )
        report = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise RuntimeCompatibilityError(runtime, "controller compatibility probe did not complete",
                                        phase=phase, available=available) from error
    checks = report.get("checks", {}) if isinstance(report, dict) else {}
    if (completed.returncode or not isinstance(checks, dict)
            or any(checks.get(name) is not True for name in RUNTIME_CAPABILITIES)):
        raise RuntimeCompatibilityError(runtime, "controller compatibility checks failed",
                                        phase=phase, available=available, checks=checks)
    return {"runtime": str(runtime), "capabilities": available, "checks": checks,
            "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest()}
