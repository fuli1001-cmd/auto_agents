"""One in-memory environment for a bound verification operation."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .execution_binding import test_invocations


@dataclass(frozen=True)
class VerificationExecutionContext:
    environment: Mapping[str, str]
    operator_environment: Mapping[str, str]

    @classmethod
    def capture(cls, session, state):
        from .operator_inputs import OperatorInputStore
        from .session_verification import session_gates, _owned_tasks, _task_refs, _ref_covered

        bindings = []
        if state.verification_binding.get('gates'):
            gates = session_gates(session, state)
            for step in gates.steps:
                bindings.extend(step.operator_input_bindings)
            owned = {task.get('task_id') for task in _owned_tasks(state)}
            for task in state.verification_binding.get('tasks', []):
                if task.get('task_id') in owned or any(
                        _ref_covered(ref, step) for ref in _task_refs(task) for step in gates.steps):
                    bindings.extend(task.get('operator_input_bindings', []))
        control = getattr(session, '_custody_control_root', session.project_root)
        overrides = OperatorInputStore(Path(control)).environment(bindings)[0] if bindings else {}
        return cls(MappingProxyType({**os.environ, **overrides}), MappingProxyType(overrides))

    def invocations(self, command):
        return test_invocations(command, environment=self.environment)


def current_context(session, state):
    active = getattr(session, '_proof_execution_context', None)
    return active if active is not None else VerificationExecutionContext.capture(session, state)


@contextmanager
def execution_context(session, state):
    active = getattr(session, '_proof_execution_context', None)
    if active is not None:
        yield active
        return
    context = VerificationExecutionContext.capture(session, state)
    session._proof_execution_context = context
    try:
        yield context
    finally:
        del session._proof_execution_context


def proof_context_descriptor(session, state, gates=None):
    """Seal effective inputs, not the full (potentially secret) environment."""
    from .gates import command_from_verification_step
    from .session_verification import fingerprint, session_gates, _legacy_commands

    gates = gates if gates is not None else session_gates(session, state)
    commands = [*(step.command or command_from_verification_step(step, session.project_root)
                  for step in gates.steps), *_legacy_commands(gates), state.fix_verify_command]
    context = current_context(session, state)
    entries = []
    for command in dict.fromkeys(filter(None, commands)):
        try:
            entries.append([command, [[item.runner, item.cwd, item.shell_cwd,
                                      item.arguments, item.targets] for item in context.invocations(command)]])
        except ValueError as error:
            from .session_verification import ownership_error, diagnostic_owners
            raise ownership_error(state, 'verification environment cannot be parsed', command=command,
                                  failure_kind='unsupported_invocation', owners=diagnostic_owners(state, command)) from error
    binding = state.verification_binding
    configurations = {path: binding.get('proof_sources', {}).get(path)
                      for path in binding.get('proof_config_paths', [])}
    return {'schema_version': 1, 'fingerprint': fingerprint([
        binding.get('contract_revision'), entries, configurations])}
