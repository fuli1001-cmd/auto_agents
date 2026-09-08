"""Trusted, declarative preparation of verification software prerequisites."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil

from .repair_control import atomic_json, digest
from .repair_environment_log import EnvironmentSetupLog
from .verification_dependencies import (
    MissingDependency, VerificationDependencyError, detect_verification_dependencies,
)


def missing_verification_dependency(output):
    """Compatibility accessor; the executor consumes the complete typed list."""
    dependencies = detect_verification_dependencies(output)
    return dependencies[0].name if dependencies else ""


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _stamp(path):
    info = Path(path).stat()
    return {"path": str(Path(path).absolute()), "mtime_ns": info.st_mtime_ns, "size": info.st_size}


def _valid_state(state, parent):
    try:
        root = Path(state["root"])
        if root.parent != parent or root.resolve().parent != parent.resolve():
            return False
        if _read(root / "ready.json") != state:
            return False
        if state.get("version") == 2:
            config_root = parent.parents[2]
            config = {"root": str(config_root), **_read(config_root / "operator.json", {})}
            kind, name = state["provides"][0].split(":", 1)
            recipe_name, recipe, source = _recipe(config, MissingDependency(kind, name))
            if state.get("specification") != _specification_fingerprint(recipe_name, recipe, _recipe_files(recipe, source)):
                return False
            return all(_stamp(item["path"]) == item for item in state["inputs"]) and all(
                (root / name).exists() for name in state["required_paths"])
        # Retain existing v1 installations inside the managed environment only.
        node = Path(state["node"]).stat()
        return (root / "bin" / state["dependency"]).is_file() and state.get("node_stat") == [node.st_mtime_ns, node.st_size]
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return False


def verification_dependency_state(python):
    parent = Path(python).absolute().parent.parent / "verification-tools"
    active = _read(parent / "active.json", {})
    if not isinstance(active, dict):
        return {}
    states = active.get("tools", {}) if active.get("version") == 2 else {"legacy": active}
    if not isinstance(states, dict):
        return {}
    tools = {key: state for key, state in states.items() if isinstance(state, dict) and _valid_state(state, parent)}
    if not tools:
        return {}
    result = {"version": 2, "tools": tools, "fingerprint": digest([(key, value["fingerprint"]) for key, value in sorted(tools.items())])}
    for key in ("read_roots", "path_entries", "python_paths", "node_paths", "library_paths"):
        result[key] = list(dict.fromkeys(value for state in tools.values() for value in state.get(key, [])))
    for state in tools.values():
        if state.get("version") != 2:
            result["read_roots"].append(state["root"])
            result["path_entries"].append(str(Path(state["root"]) / "bin"))
    return result


def _spec_path(root, name):
    path = (root / str(name)).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise RuntimeError("dependency specification must be a file inside its trusted specification directory")
    return path


def _recipe(config, dependency):
    bundled = Path(__file__).with_name("verification_tools")
    recipes = {key: (value, bundled) for key, value in _read(bundled / "catalog.json", {}).get("recipes", {}).items()}
    custom = config.get("verification_dependencies", {})
    if not isinstance(custom, dict):
        raise RuntimeError("verification_dependencies must contain named preparation recipes")
    root = Path(config["root"]) / "dependency-specs"
    recipes.update({key: (value, root) for key, value in custom.items()})
    matches = []
    keys = {dependency.key}
    if dependency.kind == "executable":
        keys.add("executable:" + Path(dependency.name.replace("\\", "/")).name)
    for name, (recipe, source) in recipes.items():
        if isinstance(recipe, dict) and keys.intersection(recipe.get("provides", [])):
            matches.append((name, recipe, source))
    if len(matches) != 1:
        reason = "no trusted preparation recipe" if not matches else "ambiguous preparation recipes"
        raise VerificationDependencyError(dependency, reason + "; provide the software or declare verification_dependencies in operator.json, then resume the saved repair")
    return matches[0]


def _recipe_files(recipe, source):
    if recipe.get("installer") == "npm":
        return {name: _spec_path(source, recipe.get(name, name)) for name in ("package.json", "package-lock.json")}
    if recipe.get("installer") == "pip":
        return {"requirements": _spec_path(source, recipe.get("requirements", "requirements.lock"))}
    return {}


def _specification_fingerprint(name, recipe, files):
    return digest([name, recipe, [(key, hashlib.sha256(path.read_bytes()).hexdigest()) for key, path in files.items()]])


def _executable(name, python):
    paths = verification_dependency_state(python).get("path_entries", [])
    return shutil.which(name, path=os.pathsep.join([*paths, os.environ.get("PATH", os.defpath)]))


def _recipe_inputs(recipe, source, python):
    installer = recipe.get("installer")
    files, inputs = _recipe_files(recipe, source), [Path(python).resolve()]
    if installer == "npm":
        node, npm = _executable("node", python), _executable("npm", python)
        if not node or not npm:
            missing = "node" if not node else "npm"
            raise VerificationDependencyError(MissingDependency("executable", missing), "required by the declared npm preparation recipe")
        inputs += [Path(node).resolve(), Path(npm).resolve()]
    elif installer == "pip":
        pass
    elif installer == "existing":
        path = Path(recipe.get("path", "")).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError("declared software path must be an existing absolute file")
        expected = recipe.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("declared software SHA-256 does not match")
        inputs.append(path.resolve())
    else:
        raise RuntimeError("unsupported preparation installer; expected npm, pip or existing")
    return files, inputs


def _install(recipe, files, inputs, root, config, python, log):
    installer = recipe["installer"]
    prerequisites = verification_dependency_state(python)
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join([*prerequisites.get("path_entries", []), environment.get("PATH", os.defpath)])
    if prerequisites.get("library_paths"):
        environment["LD_LIBRARY_PATH"] = os.pathsep.join([*prerequisites["library_paths"], environment.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    result = {"read_roots": [str(root)], "path_entries": [], "python_paths": [], "node_paths": [], "library_paths": [], "required_paths": []}
    if installer == "npm":
        for name, path in files.items():
            shutil.copyfile(path, root / name)
        node, npm = map(str, inputs[1:])
        cache = Path(config["root"]) / "package-cache/npm"
        cache.mkdir(parents=True, exist_ok=True)
        from .artifact_runtime import track
        track(cache, "cache", scope="repair:" + config["root"])
        log.run([node, npm, "ci", "--ignore-scripts", "--engine-strict", "--no-audit", "--no-fund", "--cache", str(cache)], cwd=root, timeout=300, env=environment)
        modules = root / "node_modules"
        result["node_paths"] = [str(modules)]
        result["required_paths"].append("node_modules")
        for name, relative in recipe.get("executables", {}).items():
            if not re.fullmatch(r"[\w.+-]+", name):
                raise RuntimeError("invalid declared executable name")
            entry = _spec_path(modules, relative)
            launcher = root / "bin" / name
            launcher.parent.mkdir(exist_ok=True)
            launcher.write_text("#!/bin/sh\nexec " + shlex.join([node, str(entry)]) + ' "$@"\n')
            launcher.chmod(0o755)
            result["required_paths"].append("bin/" + name)
            # Execute package code only in the original verification sandbox.
            # Installation readiness is not a passing behavior proof.
        result["path_entries"] = [str(root / "bin")]
    elif installer == "pip":
        # A hash-locked operator declaration supplies distributions and versions.
        # Missing import names never become pip package arguments.
        destination = root / "python"
        cache = Path(config["root"]) / "package-cache/pip"
        cache.mkdir(parents=True, exist_ok=True)
        log.run([python, "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes", "--only-binary=:all:",
                 "--no-compile", "--cache-dir", str(cache), "--target", str(destination),
                 "-r", str(files["requirements"])], cwd=root, timeout=300, env=environment)
        result["python_paths"] = [str(destination)]
        result["path_entries"] = [str(destination / "bin")]
        result["required_paths"] = ["python"]
    else:
        path = inputs[-1]
        frozen = root / "software" / path.name
        frozen.parent.mkdir(exist_ok=True)
        shutil.copy2(path, frozen)
        if hashlib.sha256(frozen.read_bytes()).hexdigest() != recipe["sha256"]:
            raise RuntimeError("declared software changed while preparing its snapshot")
        path = frozen
        result["required_paths"].append(str(path.relative_to(root)))
        result["receipt_inputs"] = [str(path)]
        for capability in recipe["provides"]:
            kind, name = capability.split(":", 1)
            if kind == "executable":
                if not re.fullmatch(r"[\w.+-]+", name) or not os.access(path, os.X_OK):
                    raise RuntimeError("declared executable is not executable or has an invalid name")
                launcher = root / "bin" / name
                launcher.parent.mkdir(exist_ok=True)
                launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(path)) + ' "$@"\n')
                launcher.chmod(0o755)
                result["path_entries"] = [str(root / "bin")]
                result["required_paths"].append("bin/" + name)
            elif kind == "shared_library":
                result["library_paths"] = [str(path.parent)]
            else:
                raise RuntimeError("existing software recipes provide executables or shared libraries")
    return result


def prepare_verification_dependency(config, python, dependency, *, _chain=()):
    if isinstance(dependency, str):
        dependency = MissingDependency("executable", dependency)
    environment = Path(python).absolute().parent.parent
    if environment.resolve().parent != Path(config["root"]).resolve() / "environments":
        raise RuntimeError("verification dependency preparation requires a supervisor-owned interpreter")
    if dependency.key in _chain:
        raise VerificationDependencyError(dependency, "cyclic verification prerequisite declaration")
    name, recipe, source = _recipe(config, dependency)
    for key in recipe.get("requires", []):
        kind, required = key.split(":", 1)
        available = _executable(required, python) if kind == "executable" else any(
            key in state.get("provides", []) for state in verification_dependency_state(python).get("tools", {}).values())
        if not available:
            prepare_verification_dependency(config, python, MissingDependency(kind, required), _chain=(*_chain, dependency.key))
    files, inputs = _recipe_inputs(recipe, source, python)
    specification = _specification_fingerprint(name, recipe, files)
    identity = digest([specification,
                       [(str(path), hashlib.sha256(path.read_bytes()).hexdigest()) for path in inputs]])
    parent = environment / "verification-tools"
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / "setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = verification_dependency_state(python)
        states = dict(active.get("tools", {}))
        if name in states and states[name]["fingerprint"] == identity:
            return states[name]
        root = parent / identity[:24]
        root.mkdir(exist_ok=True)
        # Verification workers share one attempt per recipe/repair generation.
        job = os.environ.get("AUTO_AGENTS_REPAIR_JOB", "")
        generation = ""
        if job:
            from .repair_control import Store
            generation = Store(config["root"]).job(job)["generation"]
        attempt = digest([identity, job, generation])
        attempts = _read(parent / "attempts.json", {})
        if job and attempt in attempts:
            raise VerificationDependencyError(dependency, "preparation already attempted in this repair generation; resume after correcting the environment or declaration")
        attempts[attempt] = {"recipe": name, "job": job, "generation": generation}
        atomic_json(parent / "attempts.json", attempts)
        from .artifact_runtime import track
        artifact = track(root, "incomplete", scope="repair:" + config["root"])
        log = EnvironmentSetupLog(config)
        prepared = _install(recipe, files, inputs, root, config, python, log)
        state = {"version": 2, "root": str(root), "fingerprint": identity, "recipe": name,
                 "specification": specification, "provides": recipe["provides"],
                 "inputs": [_stamp(path) for path in [inputs[0], *prepared.pop("receipt_inputs", inputs[1:])]], **prepared}
        atomic_json(root / "ready.json", state)
        states = {key: value for key, value in states.items() if not set(value.get("provides", [])).intersection(recipe["provides"])}
        states[name] = state
        atomic_json(parent / "active.json", {"version": 2, "tools": states})
        if artifact:
            from .artifact_store import ArtifactStore
            ArtifactStore().promote(artifact, "environment")
        return state
