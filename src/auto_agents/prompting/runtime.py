"""Read-only model resolution using the same arguments used for invocation.

No catalog/network/model calls are used to resolve aliases. Unknown configuration
layers fail to generic, rather than making the agent run under a guessed policy.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

try:
    import tomllib
except ImportError:  # Python 3.9 / 3.10
    import tomli as tomllib

from .core import ProviderRuntime, digest


def last_option(args: Sequence[str], *names: str) -> str:
    value = ""
    for index, item in enumerate(args):
        if item in names and index + 1 < len(args):
            value = args[index + 1]
        for name in names:
            if item.startswith(name + "="):
                value = item[len(name) + 1:]
    return value


@lru_cache(maxsize=32)
def _probe(path: str, mtime: int, size: int) -> tuple[str, tuple[str, ...]]:
    def call(flag):
        try:
            result = subprocess.run([path, flag], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=5, check=False)
            return result.stdout[:100000] if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return ""
    # Help is non-authenticated and only its flags (never arbitrary text) persist.
    help_text = call("--help")
    version = call("--version") if "--version" in help_text else ""
    match = re.search(r"\b\d+\.\d+(?:\.\d+)?(?:[-+][a-zA-Z0-9.-]+)?", version)
    flags = tuple(sorted(set(re.findall(r"(?<!\w)--[a-z][a-z-]+", help_text))))
    if "<name>.config.toml" in help_text:
        flags = (*flags, "profile-files")
    return match.group(0) if match else "", flags


def cli_capabilities(binary: str) -> tuple[str, tuple[str, ...]]:
    path = shutil.which(binary)
    if not path:
        return "", ()
    try:
        stat = Path(path).stat()
        return _probe(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return "", ()


def binary_identity(binary: str) -> str:
    executable = shutil.which(binary)
    if not executable:
        return ""
    try:
        path = Path(executable).resolve()
        stat = path.stat()
        return digest(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
    except OSError:
        return ""


def read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        value = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("configuration is not an object")
        return value
    except (OSError, UnicodeError, ValueError) as error:
        # Never include raw parsing diagnostics: they can contain credential text.
        raise ValueError("unreadable configuration") from None


def _toml_overrides(args: Sequence[str]) -> dict:
    result = {}
    values = []
    for i, arg in enumerate(args):
        if arg in {"-c", "--config"} and i + 1 < len(args):
            values.append(args[i + 1])
        elif arg.startswith("--config="):
            values.append(arg.split("=", 1)[1])
        elif arg.startswith("-c") and len(arg) > 2:
            values.append(arg[2:])
    for item in values:
        key, sep, value = item.partition("=")
        if sep:
            try:
                parsed = tomllib.loads("value = " + value)["value"]
            except ValueError:
                parsed = value.strip()
            result[key.strip()] = parsed
    return result


def _root_chain(cwd: Path) -> list[Path]:
    cwd = cwd.resolve()
    root = next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), cwd)
    chain = [cwd]
    while chain[-1] != root:
        chain.append(chain[-1].parent)
    return list(reversed(chain))


def _codex(config, request, env, capabilities):
    args = list(config.extra_args)
    overrides = _toml_overrides(args)
    explicit = last_option(args, "--model", "-m") or overrides.get("model", "")
    home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    system = read_config(Path("/etc/codex/config.toml"))
    user = read_config(home / "config.toml")
    effective = {**system, **user}
    source = "user-config" if user.get("model") else "system-config"
    profile = last_option(args, "--profile", "-p") or config.profile_map.get(request.effort, "")
    if profile:
        profile_path = home / (profile + ".config.toml")
        if "profile-files" in capabilities:
            if not profile_path.is_file():
                return "", "", "missing-profile"
            selected = read_config(profile_path)
        elif profile_path.exists():
            return "", "", "unknown-profile-format"
        else:
            profiles = user.get("profiles", {})
            if profile not in profiles and not explicit:
                return "", "", "missing-profile"
            selected = profiles.get(profile, {}) if isinstance(profiles, dict) else {}
        if not isinstance(selected, dict):
            return "", "", "invalid-profile"
        effective.update(selected)
        if selected.get("model"):
            source = "profile"
    chain = _root_chain(request.cwd)
    projects = user.get("projects", {})
    for directory in chain:
        path = directory / ".codex/config.toml"
        if not path.exists():
            continue
        trust = next((projects.get(str(p), {}).get("trust_level")
                      for p in reversed(chain) if isinstance(projects, dict)
                      and isinstance(projects.get(str(p)), dict)
                      and projects[str(p)].get("trust_level")), "")
        if trust not in {"trusted", "untrusted"}:
            return str(effective.get("model", "")), "", "unknown-project-trust"
        if trust == "trusted":
            layer = read_config(path)
            effective.update(layer)
            if layer.get("model"):
                source = "project-config"
    model = str(explicit or effective.get("model", ""))
    provider = str(overrides.get("model_provider", effective.get("model_provider", "openai")))
    if provider != "openai" or "--oss" in args:
        return model, "", "custom-model-provider"
    return model, model, "cli" if explicit else (source if model else "native-default")


def copilot_model(config_dir: Path) -> str:
    # Modern settings override the legacy config when both exist.
    merged = {**read_config(config_dir / "config.json"), **read_config(config_dir / "settings.json")}
    return str(merged.get("model", "")).strip()


def _claude(config, request, env):
    args = list(config.extra_args)
    explicit = last_option(args, "--model") or config.profile_map.get(request.effort, "")
    effective, settings_env = {}, {}
    home = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    sources = last_option(args, "--setting-sources")
    allowed = sources.split(",") if sources else ["user", "project", "local"]
    locations = [("user", home / "settings.json")]
    for root in _root_chain(request.cwd):
        locations.extend([("project", root / ".claude/settings.json"),
                          ("local", root / ".claude/settings.local.json")])
    for kind, path in locations:
        if kind in allowed:
            layer = read_config(path)
            effective.update(layer)
            if isinstance(layer.get("env"), dict):
                settings_env.update(layer["env"])
    additional = last_option(args, "--settings")
    if additional:
        layer = json.loads(additional) if additional.lstrip().startswith("{") else read_config(Path(additional))
        effective.update(layer)
        settings_env.update(layer.get("env", {}))
    managed = read_config(Path("/etc/claude-code/managed-settings.json"))
    effective.update(managed)
    settings_env.update(managed.get("env", {}))
    merged_env = {**env, **settings_env}
    model = str(explicit or merged_env.get("ANTHROPIC_MODEL") or effective.get("model", ""))
    resolved = model.removesuffix("[1m]")
    if resolved in {"sonnet", "opus", "haiku"}:
        resolved = str(merged_env.get("ANTHROPIC_DEFAULT_" + resolved.upper() + "_MODEL", "")).removesuffix("[1m]")
    if resolved in {"auto", "default", "best", "opusplan"}:
        resolved = ""
    # A gateway/deployment can map an otherwise recognizable name to another model.
    if any(merged_env.get(key) for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                                         "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        resolved = ""
    if last_option(args, "--fallback-model"):
        resolved = ""
    return model, resolved, "cli" if explicit else "native-settings"


def resolve_runtime(config, request, *, env: Mapping[str, str] | None = None,
                    probe: bool = True) -> ProviderRuntime:
    env = dict(os.environ if env is None else env)
    if config.kind not in {"codex", "claude-code", "copilot-cli", "antigravity"}:
        return ProviderRuntime(provider=config.kind, resolution_source="custom-provider")
    version, capabilities = cli_capabilities(config.binary) if probe else ("", ())
    provider = config.kind
    try:
        if provider == "codex":
            model, resolved, source = _codex(config, request, env, capabilities)
        elif provider == "claude-code":
            model, resolved, source = _claude(config, request, env)
        elif provider == "copilot-cli":
            model = last_option(config.extra_args, "--model", "-m")
            source = "cli"
            if not model:
                profile = config.profile_map.get(request.effort, "")
                home = Path(env.get("COPILOT_HOME") or Path.home() / ".copilot")
                config_dir = last_option(config.extra_args, "--config-dir")
                path = Path(config_dir) if config_dir else (Path(profile) if Path(profile).is_absolute() else home / "profiles" / profile)
                path = path.expanduser()
                if not path.is_absolute():
                    path = request.cwd / path
                model = (copilot_model(path) or str(env.get("COPILOT_MODEL", ""))) if profile or config_dir else str(env.get("COPILOT_MODEL") or copilot_model(home))
                source = "profile" if profile else "native-settings"
            resolved = "" if model in {"auto", "default"} else model
        elif provider == "antigravity":
            model = last_option(config.extra_args, "--model") or config.profile_map.get(request.effort, "")
            explicit = last_option(config.extra_args, "--model")
            source = "cli" if explicit else "profile"
            if not model:
                model = str(read_config(Path.home() / ".gemini/antigravity-cli/settings.json").get("model", ""))
                source = "native-settings"
            resolved = model.lower()
            resolved = re.sub(r"\s*\([^)]*\)\s*$", "", resolved).replace(" ", "-")
            if model and not explicit and config.profile_map.get(request.effort) and "--model" not in capabilities:
                resolved, source = "", "legacy-selection-unconfirmed"
        else:
            model, resolved, source = "", "", "custom-provider"
    except (ValueError, OSError, TypeError, AttributeError):
        model, resolved, source = "", "", "unreadable-or-unsupported-config"
    return ProviderRuntime(provider, version, model, resolved, source, capabilities,
                           binary_identity(config.binary) if probe else "")


def observed_model_metadata(request, stdout: str) -> dict:
    """Keep runtime observations separate from the policy chosen before the call."""
    metadata = dict(request.prompt_metadata)
    if not metadata:
        return metadata
    models = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(item, dict):
            continue
        model = None
        if item.get("type") == "system" and item.get("subtype") == "init":
            model = item.get("model")
        elif item.get("type") == "assistant" and isinstance(item.get("message"), dict):
            model = item["message"].get("model")
        elif item.get("type") in {"session.start", "session.model_change"} and isinstance(item.get("data"), dict):
            model = item["data"].get("selectedModel") or item["data"].get("model")
        if isinstance(model, str) and re.fullmatch(r"[a-zA-Z0-9._:/\[\]-]{1,200}", model) and model not in models:
            models.append(model)
    if models:
        metadata["observed_models"] = models
    return metadata
