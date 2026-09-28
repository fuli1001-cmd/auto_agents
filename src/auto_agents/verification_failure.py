"""Bounded, actionable test evidence for candidate correction and diagnosis."""
from .diagnostic_output import plain_text, redact
from .gates import extract_failure_info


def details(gate):
    failures = extract_failure_info(gate)
    commands = [result for result in gate.commands if not result.ok]
    # Conda commonly puts only its launcher wrapper on stderr. The assertion
    # and pytest node identity are on stdout and must survive that wrapper.
    excerpts = []
    for result in commands[:3]:
        stdout = plain_text(result.stdout).strip()
        stderr = plain_text(result.stderr).strip()
        lines = stdout.splitlines()
        important = [line for line in lines if line.startswith(('FAILED ', 'ERROR ', 'E ', '> '))]
        excerpts.append(redact('\n'.join(important[-20:]) or stdout[-3000:] or stderr[-3000:]))
    ids = [redact(value) for value in failures.failure_ids]
    reason = '; '.join(ids[:5])
    if excerpts:
        reason += ('\n' if reason else '') + '\n'.join(excerpts)
    return reason[:4000] or gate.summary or 'non-zero exit', {
        'failure_ids': ids, 'comparable': failures.comparable,
        'command_failures': [{'command': redact(result.command), 'returncode': result.returncode,
            'stdout_tail': redact(plain_text(result.stdout)[-6000:]),
            'stderr_tail': redact(plain_text(result.stderr)[-2000:]),
            'artifacts': result.artifacts} for result in commands[:3]],
    }
