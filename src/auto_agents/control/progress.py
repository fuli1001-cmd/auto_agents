"""Stop on repeated outcomes, while a hard call cap remains unchanged."""

import re
from .types import digest
from .proof import executed_count


def observe(previous, error, *, limit=2):
    checks = error.details.get("checks", [])
    passed = sum(
        executed_count(
            str(check.get("stdout", "")) + "\n" + str(check.get("stderr", ""))
        )
        for check in checks
    )
    failed = sum(
        int(n)
        for check in checks
        for n in re.findall(
            r"(?<![=\w])\b(\d+) failed\b",
            str(check.get("stdout", "")) + "\n" + str(check.get("stderr", "")),
        )
    )
    score = (passed, -failed)
    best = tuple(previous.get("best", (-1, -1000000)))
    improved = bool(checks) and score > best
    stable = {
        "code": error.code,
        "checks": [
            (check.get("command"), check.get("ok"), check.get("returncode"))
            for check in checks
        ],
    }
    if not checks:
        stable["reason"] = re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", str(error))
    fingerprint = digest(stable)
    repeats = (
        previous.get("repeats", 0) + 1
        if fingerprint == previous.get("fingerprint") and not improved
        else 0
    )
    stagnant = 0 if improved else previous.get("stagnant", 0) + 1
    state = {
        "best": list(score if improved else best),
        "fingerprint": fingerprint,
        "repeats": repeats,
        "stagnant": stagnant,
    }
    return state, stagnant < limit and repeats < limit
