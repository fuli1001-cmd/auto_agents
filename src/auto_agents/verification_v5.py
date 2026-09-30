"""Conservative, contract-bound optimization of structured verification."""
from __future__ import annotations

import ast
import copy
import fnmatch
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path


def _functions(tree):
    result = {}

    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result[prefix + node.name] = node
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
    visit(tree.body)
    return result


def changed_symbols(root: Path, path: str, baseline: str = "HEAD"):
    """Return body-only changes; namespace/signature/init changes stay broad.

    None means unknown, including new/deleted functions and files. Nested
    functions belong to their enclosing function's body, not a separate proof.
    """
    if not path.endswith(".py"):
        return None
    before = subprocess.run(["git", "show", f"{baseline}:{path}"], cwd=root,
                            capture_output=True, text=True)
    if before.returncode:
        return None
    try:
        old = ast.parse(before.stdout)
        new = ast.parse((root / path).read_text())
    except (OSError, SyntaxError, UnicodeError):
        return None
    old_functions, new_functions = _functions(old), _functions(new)
    if old_functions.keys() != new_functions.keys():
        return None
    changed = {name for name in old_functions
               if ast.dump(old_functions[name]) != ast.dump(new_functions[name])}
    skeletons = [copy.deepcopy(tree) for tree in (old, new)]
    for tree in skeletons:
        for node in _functions(tree).values():
            node.body = [ast.Pass()]
    if ast.dump(skeletons[0]) != ast.dump(skeletons[1]):
        return None
    return changed


def symbol_impact(step, path, symbols):
    """None preserves ordinary file impact; False is an explicit declaration.

    <module> declares initialization-only dependence. Wildcards describe a
    reviewed class/module component. These declarations are frozen with the
    proof inventory; a candidate cannot invent them to weaken its own checks.
    """
    patterns = [entry.split("::", 1)[1] for entry in step.impact_symbols
                if "::" in entry and entry.split("::", 1)[0] == path]
    if symbols is None or not patterns:
        return None
    return any(fnmatch.fnmatchcase(symbol, pattern)
               for symbol in symbols for pattern in patterns)


def coalesce_steps(steps, *, estimates=None, target_seconds=300):
    """Keep every logical proof, merging only explicitly compatible checks."""
    estimates = estimates or {}
    groups = {}
    output = []
    for step in steps:
        if (not step.proof_id or not step.coalesce_safe or step.runner != "pytest" or step.command
                or step.artifact_globs or step.dynamic_ports or step.exclusive_resources
                or len({target.split("::", 1)[0] for target in step.targets}) != 1):
            output.append(step)
            continue
        value = asdict(step)
        for key in ("proof_id", "purpose", "targets", "impact_paths", "impact_symbols"):
            value.pop(key, None)
        key = (step.targets[0].split("::", 1)[0], json.dumps(value, sort_keys=True))
        buckets = groups.setdefault(key, [])
        weight = max(0, estimates.get(step.proof_id, 0))
        if (not buckets or sum(estimates.get(item.proof_id, 0) for item in buckets[-1])
                + weight > target_seconds):
            buckets.append([])
        buckets[-1].append(step)
    for buckets in groups.values():
        for bucket in buckets:
            targets = list(dict.fromkeys(target for step in bucket for target in step.targets))
            # The existing command resolver unions proof IDs for equal commands.
            output.extend(replace(step, targets=targets) for step in bucket)
    order = {step.proof_id: index for index, step in enumerate(steps)}
    return sorted(output, key=lambda step: order.get(step.proof_id, len(order)))
