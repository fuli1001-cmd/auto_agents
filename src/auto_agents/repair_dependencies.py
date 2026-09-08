"""Prepare allowlisted verifier tools using the trusted executor's lockfile.

Candidate output can identify a missing capability; it cannot select packages,
versions, setup commands or installation roots. Preparation runs outside the
credential-free proof sandbox, and the resulting tools are read-only inputs.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil

from .repair_control import atomic_json, digest
from .repair_environment_log import EnvironmentSetupLog


class VerificationDependencyError(RuntimeError):
    """An unsatisfied proof prerequisite, never evidence against a candidate."""

    def __init__(self, dependency, detail, cause=None):
        super().__init__(f"verification environment prerequisite {dependency}: {detail}")
        self.dependency = dependency
        for name in ("environment_diagnostics", "environment_diagnostics_error"):
            if cause is not None and hasattr(cause, name):
                setattr(self, name, getattr(cause, name))


def missing_verification_dependency(output):
    text = str(output)
    # Match executor errors, not just occurrences in test names/source snippets.
    if re.search(r"(?:^|\n)(?:E\s+)?(?:RuntimeError: )?Vitest integration dependency is missing:", text):
        return "vitest"
    if re.search(r"npm (?:ERR!|error) code ENOTCACHED", text, re.I) and re.search(
            r"request to https?://[^\s]+/vitest\b[^\n]*only-if-cached", text, re.I):
        return "vitest"
    if re.search(r"(?:Error: |Error \[ERR_MODULE_NOT_FOUND\]: )Cannot find (?:package|module) ['\"]vitest(?:/[^'\"]*)?['\"]", text):
        return "vitest"
    if re.search(r"(?:^|\n)(?:[^\n]*sh: (?:\d+: )?)?vitest: (?:command )?not found\b", text):
        return "vitest"
    return ""


def verification_dependency_state(python):
    """Read only completed tool installations bound to this interpreter's venv."""
    root = Path(python).absolute().parent.parent / "verification-tools"
    try:
        state = json.loads((root / "active.json").read_text())
        tool = Path(state["root"])
        if tool.parent != root or tool.resolve().parent != root.resolve():
            return {}
        ready = json.loads((tool / "ready.json").read_text())
        node = Path(state["node"]).stat()
        if (ready != state or not (tool / "bin/vitest").is_file()
                or state.get("node_stat") != [node.st_mtime_ns, node.st_size]):
            return {}
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def prepare_verification_dependency(config, python, dependency):
    if dependency != "vitest":
        raise RuntimeError("unsupported engine verification dependency: " + dependency)
    environment = Path(python).absolute().parent.parent
    managed = Path(config["root"]).resolve() / "environments"
    if environment.resolve().parent != managed:
        raise RuntimeError("verification dependency preparation requires a supervisor-owned interpreter")
    source = Path(__file__).with_name("verification_tools")
    # Never read setup metadata from the writable candidate or target project.
    manifests = {name: (source / name).read_bytes() for name in ("package.json", "package-lock.json")}
    log = EnvironmentSetupLog(config)
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("Vitest verification requires Node.js and npm on the supervisor PATH")
    node = str(Path(node).resolve())
    version = log.run([node, "--version"], text=True, timeout=15).stdout.strip()
    node_hash = hashlib.sha256(Path(node).read_bytes()).hexdigest()
    node_stat = Path(node).stat()
    identity = digest([[(name, hashlib.sha256(data).hexdigest()) for name, data in manifests.items()],
                       node, node_hash, version])
    parent = environment / "verification-tools"
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / "setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        root = parent / identity[:24]
        ready = root / "ready.json"
        launcher = root / "bin/vitest"
        state = {"dependency": dependency, "root": str(root), "fingerprint": identity,
                 "node": node, "node_version": version,
                 "node_stat": [node_stat.st_mtime_ns, node_stat.st_size]}
        root.mkdir(exist_ok=True)
        from .artifact_runtime import track
        artifact = track(root, "environment" if ready.exists() else "incomplete", scope="repair:" + config["root"])
        if not ready.exists() or not launcher.is_file():
            for name, data in manifests.items():
                (root / name).write_bytes(data)
            cache = Path(config["root"]) / "package-cache/npm"
            cache.mkdir(parents=True, exist_ok=True)
            track(cache, "cache", scope="repair:" + config["root"])
            # Lockfile integrity, optional native binaries and engine requirements
            # are enforced. Package lifecycle scripts are never executed.
            log.run([npm, "ci", "--ignore-scripts", "--engine-strict", "--no-audit", "--no-fund",
                     "--cache", str(cache)], cwd=root, timeout=300)
            launcher.parent.mkdir(exist_ok=True)
            launcher.write_text("#!/bin/sh\nexec " + shlex.join([node, str(root / "node_modules/vitest/vitest.mjs")])
                                + ' "$@"\n')
            launcher.chmod(0o755)
        try:
            log.run([str(launcher), "--version"], cwd=root, text=True, timeout=30)
        except BaseException:
            ready.unlink(missing_ok=True)
            (parent / "active.json").unlink(missing_ok=True)
            raise
        atomic_json(ready, state)
        atomic_json(parent / "active.json", state)
        if artifact:
            from .artifact_store import ArtifactStore
            ArtifactStore().promote(artifact, "environment")
        return state
