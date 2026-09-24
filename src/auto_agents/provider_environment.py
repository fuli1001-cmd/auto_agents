"""Resolve the environment for one configured provider instance."""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Mapping, Optional


def validate_environment(values: object) -> dict[str, Optional[str]]:
    if not isinstance(values, dict):
        raise ValueError("provider environment must be an object")
    for name, value in values.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or name.startswith("AUTO_AGENTS_")):
            raise ValueError("provider environment contains an invalid or reserved variable name")
        if value is not None and not isinstance(value, str):
            raise ValueError("provider environment values must be strings or null")
    return dict(values)


def effective_environment(config, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    for name, value in validate_environment(getattr(config, "environment", {})).items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


def environment_binding(config) -> str:
    """Bind continuations to an instance without recording credential values."""
    values = {name: None if value is None else hashlib.sha256(value.encode()).hexdigest()
              for name, value in sorted(getattr(config, "environment", {}).items())}
    return hashlib.sha256(json.dumps({"provider": getattr(config, "provider_name", ""),
                                      "environment": values}, sort_keys=True).encode()).hexdigest()
