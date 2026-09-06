"""Explicit CLI evaluation, never invoked by normal unit tests.

Capture with this script and PYTHONPATH pointing at a baseline checkout's src;
run with the current package. Each variant gets an isolated, identical fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

PLACEHOLDER = "__EVALUATION_PROJECT_ROOT__"
CASES = (
    {"id": "small-change", "goal": "Return zero for an empty sum.",
     "source": "def total(values):\n    return sum(values) if values else None\n",
     "assertions": "assert total([]) == 0\nassert total([1, 2]) == 3\n"},
    {"id": "multiple-behaviors", "goal": "Sum numeric values; skip None, accept generators, and preserve negative values.",
     "source": "def total(values):\n    return 0\n",
     "assertions": "assert total([None, -2, 5]) == 3\nassert total(x for x in [1, 2]) == 3\nassert total([]) == 0\n"},
    {"id": "read-only-review", "purpose": "review", "decision": "fail",
     "goal": "The sum of an empty collection must be zero.",
     "source": "def total(values):\n    return None if not values else sum(values)\n",
     "assertions": "assert total([]) == 0\n"},
    {"id": "retry", "goal": "Sum numeric values while ignoring None.",
     "feedback": "The prior patch covered lists but crashed for generators. Preserve the None behavior while fixing generator input.",
     "source": "def total(values):\n    return sum(v for v in values if v is not None) if len(values) else 0\n",
     "assertions": "assert total(x for x in [None, 2, 3]) == 5\nassert total([]) == 0\n"},
    {"id": "frontend-evidence", "purpose": "review", "decision": "fail",
     "goal": "Release requires page-level browser screenshot evidence. The candidate has only an HTML file and no rendered screenshot evidence; static existence alone is insufficient.",
     "source": "def total(values):\n    return sum(values)\n",
     "files": {"index.html": "<main><h1>Example</h1></main>"},
     "assertions": "assert total([2]) == 2\n"},
    {"id": "persistence-authorization", "purpose": "review", "decision": "fail",
     "goal": "Startup must preserve existing database rows. This destructive startup implementation has no user approval and must fail review.",
     "source": "def startup(connection):\n    connection.execute('DELETE FROM records')\n",
     "assertions": "assert True\n"},
    {"id": "user-correction", "goal": "The previous idea was to round totals. User correction: preserve exact fractional totals and ignore None. Implement only the corrected behavior.",
     "source": "def total(values):\n    return round(sum(values))\n",
     "assertions": "assert total([0.25, None, 0.25]) == 0.5\n"},
    {"id": "retained-progress", "goal": "The helper already implements filtering; preserve it and complete total using it. Do not change completed.txt.",
     "source": "def numbers(values):\n    return (v for v in values if v is not None)\n\ndef total(values):\n    raise NotImplementedError\n",
     "files": {"completed.txt": "previous-stage-complete\n"},
     "assertions": "assert total([None, 2, 3]) == 5\nassert open('completed.txt').read() == 'previous-stage-complete\\n'\n"},
)


def seed(root: Path, case: dict):
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.models import TaskSpec
    Orchestrator.init_project(root, "prompt-evaluation", "mock")
    files = {"solution.py": case["source"], **case.get("files", {})}
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    task = TaskSpec(task_id="eval-001", title=case["id"], description=case["goal"],
                    acceptance=[case["goal"]], verification_refs=[])
    orch = Orchestrator(root)
    purpose = case.get("purpose", "implement")
    prompt = orch._build_task_prompt(task, purpose)
    if case.get("feedback"):
        # Older checkouts do not have the structured prompting package.
        if hasattr(prompt, "spec"):
            from auto_agents.prompting import append_context
            prompt = append_context(prompt, case["feedback"], "Previous attempt issues")
        else:
            prompt += "\n\nPrevious attempt issues:\n" + case["feedback"]
    return prompt


def fixture_fingerprint(root: Path) -> dict:
    result = {}
    excluded = {".git", ".auto-agents", ".conda", ".venv", ".pytest_cache", "__pycache__", "node_modules", ".tmp", ".tmp-tests", ".data"}
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name not in excluded]
        for name in files:
            path = Path(directory) / name
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def check_result(case: dict, root: Path, summary: str, before: dict) -> dict:
    if case.get("purpose") == "review":
        first = summary.strip().splitlines()[0] if summary.strip() else ""
        return {"protocol_ok": first in {"DECISION: pass", "DECISION: fail"},
                "accepted": first == "DECISION: " + case["decision"],
                "scope_ok": fixture_fingerprint(root) == before}
    verification = subprocess.run([sys.executable, "-c", "from solution import *\n" + case["assertions"]],
                                  cwd=root, capture_output=True, text=True, timeout=30, check=False)
    after = fixture_fingerprint(root)
    allowed = lambda name: name == "solution.py" or name.startswith("tests/")
    scope_ok = all(after.get(name) == value for name, value in before.items() if not allowed(name))
    scope_ok = scope_ok and all(name in before or allowed(name) for name in after)
    return {"protocol_ok": bool(summary.strip()), "accepted": verification.returncode == 0,
            "scope_ok": scope_ok,
            "verification_output": (verification.stdout + verification.stderr)[-4000:]}


def capture(output: Path):
    cases = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="auto-agents-eval-capture-") as directory:
            root = Path(directory) / "project"
            prompt = seed(root, case)
            # Include native instruction inputs so baseline and new each use their own system.
            native = {str(p.relative_to(root)): p.read_text(encoding="utf-8").replace(str(root), PLACEHOLDER)
                      for p in root.rglob("*.md") if ".git" not in p.relative_to(root).parts
                      and (p.name in {"AGENTS.md", "CLAUDE.md"} or ".github" in p.parts or ".agents" in p.parts
                           or p.name == "project-rules.agent.md")}
            cases.append({**case, "baseline_prompt": str(prompt).replace(str(root), PLACEHOLDER), "baseline_native": native})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"version": 1, "cases": cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args):
    from auto_agents.config import load_project_config
    from auto_agents.models import AgentRequest
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.prompting import prepare_request
    config = load_project_config(args.project)
    payload = json.loads(args.baseline.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not payload.get("cases"):
        raise ValueError("expected a captured version 1 baseline corpus")
    for case in payload["cases"]:
        for name in (*case.get("files", {}), *case.get("baseline_native", {})):
            if Path(name).is_absolute() or ".." in Path(name).parts or ".git" in Path(name).parts:
                raise ValueError("evaluation fixture paths must stay inside their project")
    providers = args.providers or [config.active_provider]
    if any(name not in config.providers for name in providers):
        raise ValueError("every evaluation provider must be configured in the source project")
    # A new report directory prevents silently overwriting prior evaluation evidence.
    args.output.mkdir(parents=True, exist_ok=False)
    report = args.output / "results.jsonl"
    for provider in providers:
        for case in payload["cases"]:
            for repetition in range(args.repetitions):
                for variant in ("baseline", "new"):
                    label = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{provider}-{case['id']}-{repetition + 1}-{variant}")
                    with tempfile.TemporaryDirectory(prefix="auto-agents-eval-") as directory:
                        root = Path(directory) / "project"
                        fresh = seed(root, case)
                        if variant == "baseline":
                            # Only generated instruction files in this isolated fixture are replaced.
                            for p in root.rglob("*.md"):
                                if ".git" not in p.parts and (p.name in {"AGENTS.md", "CLAUDE.md"}
                                    or ".github" in p.parts or ".agents" in p.parts or p.name == "project-rules.agent.md"):
                                    p.unlink()
                            for name, text in case["baseline_native"].items():
                                path = root / name
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.write_text(text.replace(PLACEHOLDER, str(root)), encoding="utf-8")
                        # Reuse the configured adapter without changing source-project configuration.
                        orch = Orchestrator(root)
                        orch.config.providers = config.providers
                        orch.config.active_provider = provider
                        adapter = orch._build_adapter_for_provider(provider)
                        if not adapter.available():
                            raise ValueError(f"provider unavailable: {provider}")
                        req = AgentRequest(case.get("purpose", "implement"), args.effort,
                                           case["baseline_prompt"].replace(PLACEHOLDER, str(root)) if variant == "baseline" else fresh,
                                           root, root / ".auto-agents/eval-answer.md",
                                           sandbox_mode="read-only" if case.get("purpose") == "review" else "workspace-write",
                                           timeout_seconds=args.timeout, model_adaptation=config.prompting.model_adaptation)
                        runtime = adapter.describe_runtime(req)
                        if variant == "new":
                            req = prepare_request(req, runtime)
                        before = fixture_fingerprint(root)
                        start = time.monotonic()
                        result = adapter.run(req)
                        elapsed = time.monotonic() - start
                        checks = check_result(case, root, result.summary, before)
                        row = {"case": case["id"], "provider": provider, "variant": variant,
                               "repetition": repetition + 1, "effort": args.effort, "runtime": asdict(runtime),
                               "ok": result.ok, **checks, "elapsed_seconds": elapsed,
                               "prompt_bytes": len(req.prompt.encode("utf-8")), "prompt_metadata": result.prompt_metadata,
                               "usage": asdict(result.usage) if result.usage else None}
                        with report.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                        (args.output / (label + ".prompt.txt")).write_text(req.prompt, encoding="utf-8")
                        (args.output / (label + ".answer.txt")).write_text(result.summary, encoding="utf-8")
                        print(f"{label}: accepted={checks['accepted']} protocol={checks['protocol_ok']} scope={checks['scope_ok']}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="auto-agents prompt-eval", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("capture", help="capture baseline prompts without calling any provider")
    command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("run", help="explicitly call real configured providers in isolated fixtures")
    command.add_argument("--project", type=Path, required=True)
    command.add_argument("--baseline", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--providers", nargs="+")
    command.add_argument("--effort", choices=("balanced", "deep", "max"), default="deep")
    command.add_argument("--repetitions", type=int, choices=range(1, 11), default=3)
    command.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    capture(args.output) if args.command == "capture" else run(args)


if __name__ == "__main__":
    main()
