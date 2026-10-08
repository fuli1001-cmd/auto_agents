"""Provider and verification effects. Neither component updates business state."""

from pathlib import Path
import json
import os
import re
import shlex
import time
from dataclasses import asdict, replace

from .types import ControlError, VerificationSpec, canonical


def parse_reply(text):
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    markers = ("CONTROL_RESULT v2:", "FIX_DISPOSITION v1:", "ROUTE_WORKFLOW v1:")
    for marker in markers:
        lines = [
            line.split(marker, 1)[1].strip()
            for line in text.splitlines()
            if line.startswith(marker)
        ]
        if lines:
            if len(lines) != 1:
                raise ControlError(
                    "model_output",
                    "Exactly one structured result is required",
                    category="model",
                )
            text = lines[0]
            break

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ControlError(
                    "model_output", "Duplicate JSON field: " + key, category="model"
                )
            value[key] = item
        return value

    def invalid_constant(value):
        raise ControlError(
            "model_output", "Invalid JSON number: " + value, category="model"
        )

    try:
        value = json.loads(
            text, object_pairs_hook=unique, parse_constant=invalid_constant
        )
    except ValueError as error:
        # Some CLI transports join visible commentary and the final answer.
        # Accept exactly one complete terminal object; prose is never authority.
        decoder = json.JSONDecoder(
            object_pairs_hook=unique, parse_constant=invalid_constant
        )
        objects = []
        offset = 0
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith("{"):
                start = offset + len(line) - len(line.lstrip())
                try:
                    item, end = decoder.raw_decode(text[start:])
                except ValueError:
                    pass
                else:
                    if isinstance(item, dict):
                        objects.append((item, start + end))
            offset += len(line)
        if len(objects) != 1 or text[objects[0][1] :].strip():
            raise ControlError(
                "model_output", "Return one complete JSON object", category="model"
            ) from error
        value = objects[0][0]
    if not isinstance(value, dict):
        raise ControlError(
            "model_output", "Result must be a JSON object", category="model"
        )
    forbidden = {
        "status",
        "source",
        "contract",
        "authorization",
        "budget",
        "workflow_id",
        "work_id",
    } & set(value)
    if forbidden:
        raise ControlError(
            "model_authority",
            "Model result includes controller-owned fields",
            category="model",
            details={"fields": sorted(forbidden)},
        )
    return value


def compile_checks(values, project):
    from .proof import launches

    result = []
    for value in values:
        if isinstance(value, str):
            value = {"command": value}
        if not isinstance(value, dict):
            raise ControlError(
                "model_output", "Invalid verification declaration", category="model"
            )
        command = value.get("command", "")
        if not isinstance(command, str) or not command.strip():
            raise ControlError(
                "model_output", "Missing verification command", category="model"
            )
        target_values = value.get("targets", [])
        if not isinstance(target_values, (tuple, list)) or any(
            not isinstance(x, str) for x in target_values
        ):
            raise ControlError(
                "model_output",
                "Verification targets must be an array of paths",
                category="model",
            )
        targets = tuple(target_values)
        invocations = launches(command)
        if not targets:
            targets = tuple(
                ref for invocation in invocations for ref in invocation["targets"]
            )
        if value.get("purpose", "behavior") == "behavior" and (
            not invocations or any(x.get("known") is False for x in invocations)
        ):
            raise ControlError(
                "model_output",
                "Behavior checks need a supported test runner and exact targets",
                category="model",
            )
        for target in targets:
            path = target.split("::", 1)[0]
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ControlError(
                    "model_output",
                    "Verification target leaves the workspace",
                    category="model",
                )
        # Select a project interpreter before freezing an otherwise unbound command.
        words = shlex.split(command)
        if words and words[0] in {"python", "python3", "pytest"}:
            for prefix in (".conda", ".venv"):
                if (Path(project) / prefix / "bin/python").is_file():
                    if words[0] == "pytest":
                        command = "./" + prefix + "/bin/python -m " + command
                    else:
                        command = (
                            "./" + prefix + "/bin/python" + command[len(words[0]) :]
                        )
                    break
        purpose = value.get("purpose", "behavior")
        if purpose not in {"behavior", "environment", "artifact"}:
            raise ControlError(
                "model_output",
                "Check purpose must be exactly behavior, environment or artifact; put explanation in a separate field",
                category="model",
            )
        from .workspace import product_path

        outputs = value.get("outputs", [])
        if not isinstance(outputs, list) or any(
            not isinstance(x, str) or not product_path(x) for x in outputs
        ):
            raise ControlError(
                "model_output",
                "Declare exact workspace output artifact paths",
                category="model",
            )
        source_targets = {target.split("::", 1)[0] for target in targets}
        source_extensions = {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".sh",
            ".go",
            ".rs",
            ".c",
            ".cc",
            ".cpp",
            ".h",
            ".java",
            ".rb",
            ".cs",
        }
        if any(
            x in source_targets
            or Path(x).suffix.lower() in source_extensions
            or Path(x).name
            in {"pyproject.toml", "package.json", "package-lock.json", "tsconfig.json"}
            for x in outputs
        ):
            raise ControlError(
                "model_output",
                "Test sources cannot be mutable output artifacts",
                category="model",
            )
        result.append(
            VerificationSpec(
                command,
                targets,
                purpose,
                value.get("required", True),
                value.get("timeout", 600),
                tuple(outputs),
                value.get("cache_scope", "candidate"),
                value.get("result_cache_scope", "candidate"),
            )
        )
    return tuple(result)


class Provider:
    def __init__(self, config, *, alias=None):
        self.config = config
        self.alias = alias or config.active_provider

    def adapter(self, alias):
        from ..adapters import (
            CodexAdapter,
            ClaudeCodeAdapter,
            CopilotCliAdapter,
            AntigravityAdapter,
            ShellAdapter,
            MockAdapter,
        )

        if alias not in self.config.providers:
            raise ControlError(
                "provider", "Unknown provider " + alias, category="environment"
            )
        settings = self.config.providers[alias]
        adapters = {
            "codex": CodexAdapter,
            "claude-code": ClaudeCodeAdapter,
            "copilot-cli": CopilotCliAdapter,
            "antigravity": AntigravityAdapter,
            "antigravity-claude": AntigravityAdapter,
            "antigravity-gemini": AntigravityAdapter,
            "mock": MockAdapter,
        }
        return (
            MockAdapter()
            if settings.kind == "mock"
            else adapters.get(settings.kind, ShellAdapter)(
                settings, self.config.execution.smart_timeout
            )
        )

    def run(self, context, phase, prompt, output, *, readonly=False, attachments=()):
        from ..models import AgentRequest

        alias = context.provider
        settings = self.config.providers[alias]
        adapter = self.adapter(alias)
        from .confinement import Boundary

        boundary = Boundary(
            context.workspace_root, Path(output).parent, readonly=readonly
        )
        if settings.kind != "mock":
            boundary.check()
        if not adapter.available():
            raise ControlError(
                "provider_unavailable",
                "Provider binary is unavailable: " + alias,
                category="environment",
            )
        effort_phase = {
            "classify": "fix_converse",
            "diagnose": "collab",
            "acceptance_review": "review",
            "research": "provider_research",
        }.get(phase, phase)
        effort = self.config.efforts.get(
            effort_phase, self.config.efforts.get("implement", "deep")
        )
        request = AgentRequest(
            stage=phase,
            effort=effort,
            prompt=prompt,
            cwd=context.workspace_root,
            output_path=Path(output),
            purpose=(
                "fix_converse"
                if phase == "classify"
                else "collab" if phase == "diagnose" else phase
            ),
            sandbox_mode="read-only" if readonly else "workspace-write",
            record_execution_incidents=False,
            stream_transport=True,
            attempt_id=Path(output).parent.name,
            attachments=list(attachments),
        )
        request.execution_environment = dict(context.environment)
        if settings.kind != "mock":
            request.writer_boundary = boundary
        from .budget import admit

        admit(alias, phase)
        result = adapter.run(request)
        text = (result.summary or result.stdout).strip()
        if (
            result.cleanup_incomplete
            or result.termination
            and result.termination.reason not in {"", "completed"}
        ):
            raise ControlError(
                "outcome_unknown",
                "Provider process ended without a confirmed outcome",
                category="reconciliation",
                details={"provider": alias},
            )
        if not result.ok:
            detail = (result.stderr or text)[-2000:]
            quota = bool(
                re.search(r"quota|usage limit|rate.limit|credits|额度", detail, re.I)
            )
            raise ControlError(
                "provider_quota" if quota else "provider_failed",
                detail or "Provider failed",
                category="provider",
            )
        return {
            "text": text,
            "provider": alias,
            "session": result.provider_session_id,
            "model": result.model,
            "usage": asdict(result.usage) if result.usage else {},
        }


class Verifier:
    def __init__(self, config=None, *, fresh=False):
        self.config = config
        self.fresh = fresh

    def run(self, context, specs):
        from ..gates import run_commands, run_gate_plan
        from .proof import launches, executed_count

        reports = []
        if not specs:
            raise ControlError(
                "verification_missing",
                "No required verification contract",
                category="verification",
            )
        from .quality import source_seal, artifact_hashes

        outputs = tuple({name for spec in specs for name in spec.outputs})
        sealed = source_seal(context.workspace_root, outputs)
        envkeys = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")
        # The gate sandbox already isolates processes, dependencies and transient HOME.
        from contextlib import nullcontext
        from .verification import executor

        with (
            executor(context, self.config, specs, fresh=self.fresh)
            if self.config
            else nullcontext(None)
        ) as selected:
            return self._run(context, specs, selected, sealed, outputs)

    def _run(self, context, specs, selected, sealed, outputs):
        from ..gates import run_commands, run_gate_plan
        from .proof import launches, executed_count
        from .quality import source_seal, artifact_hashes

        reports = []
        for spec in specs:
            if selected:
                gate = run_gate_plan(
                    [spec.command],
                    [],
                    context.workspace_root,
                    collect_all=False,
                    gate_executor=selected,
                    command_timeout_seconds=spec.timeout,
                )
            else:
                import tempfile
                import sys
                from .types import canonical
                from .quality import workspace_guard
                from contextlib import nullcontext

                before = workspace_guard(context.workspace_root)
                if context.operation_id:
                    directory = (
                        context.control_root
                        / ".auto-agents/state/owned/operations"
                        / context.operation_id
                    )
                    directory.mkdir(parents=True, exist_ok=True)
                    from .store import Store
                    from .cleanup import register

                    register(
                        Store(context.control_root),
                        directory,
                        "scratch",
                        context.work_id,
                        references=[context.work_id],
                    )
                    private = nullcontext(directory)
                else:
                    private = tempfile.TemporaryDirectory(prefix="check-")
                with private as directory:
                    scratch = Path(directory)
                    payload = scratch / "request.json"
                    payload.write_text(
                        canonical(
                            {
                                "workspace": str(context.workspace_root),
                                "scratch": str(scratch),
                                "command": spec.command,
                                "environment": dict(context.environment),
                            }
                        )
                    )
                    wrapper = shlex.join(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("verification_exec.py")),
                            str(payload),
                        ]
                    )
                    gate = run_commands(
                        [wrapper],
                        context.workspace_root,
                        command_timeout_seconds=spec.timeout,
                    )
                    gate.commands[0].command = spec.command
                    if gate.commands[0].cleanup_incomplete:
                        raise ControlError(
                            "outcome_unknown",
                            "Verification children did not quiesce",
                            category="reconciliation",
                            details={"scratch": str(scratch)},
                        )
                after = workspace_guard(context.workspace_root)
                if before["head"] != after["head"] or before["index"] != after["index"]:
                    raise ControlError(
                        "proof_source_changed",
                        "Verification changed Git execution inputs",
                        category="verification",
                    )
            if any(command.cleanup_incomplete for command in gate.commands):
                raise ControlError(
                    "outcome_unknown",
                    "Verification children did not quiesce",
                    category="reconciliation",
                    details={"command": spec.command},
                )
            if source_seal(context.workspace_root, outputs) != sealed:
                raise ControlError(
                    "proof_source_changed",
                    "Verification changed undeclared inputs; declare only actual runtime output artifacts before execution",
                    category="verification",
                )
            command = gate.commands[0]
            output = command.stdout + "\n" + command.stderr
            summary_output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
            if command.infrastructure_error:
                raise ControlError(
                    "verification_infrastructure",
                    gate.summary,
                    category="environment",
                    details={
                        "command": spec.command,
                        "failure_id": command.infrastructure_failure_id,
                    },
                )
            if spec.purpose == "behavior":
                if not launches(spec.command):
                    raise ControlError(
                        "proof", "Unsupported behavior runner", category="verification"
                    )
                if gate.ok and executed_count(output) <= 0:
                    raise ControlError(
                        "proof",
                        "Verification executed no confirmed behavioral proof",
                        category="verification",
                    )
                if gate.ok and (
                    re.search(r"(?<![=\w])\b[1-9]\d* (?:failed|errors?)\b", summary_output)
                    or re.search(r"^FAILED \(", summary_output, re.M)
                ):
                    raise ControlError(
                        "proof",
                        "Test failures were hidden by the command exit status",
                        category="verification",
                        details={
                            "command": spec.command,
                            "stdout": command.stdout[-10000:],
                            "stderr": command.stderr[-10000:],
                        },
                    )
            reports.append(
                {
                    "command": spec.command,
                    "ok": gate.ok,
                    "returncode": command.returncode,
                    "stdout": command.stdout[-10000:],
                    "stderr": command.stderr[-10000:],
                    "executed": (
                        executed_count(output) if spec.purpose == "behavior" else 0
                    ),
                }
            )
            if not gate.ok:
                raise ControlError(
                    "tests_failed",
                    gate.summary,
                    category="verification",
                    details={"checks": reports},
                )
        return {
            "ok": True,
            "checks": reports,
            "behavior_checks": sum(s.purpose == "behavior" for s in specs),
            "source_hash": sealed,
            "outputs": artifact_hashes(context.workspace_root, outputs),
            "environment_hash": getattr(
                getattr(selected, "result_cache", None), "environment_fingerprint", ""
            ),
        }
