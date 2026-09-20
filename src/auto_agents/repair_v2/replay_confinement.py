"""Trusted, model-free check of the confinement used by recovery discovery."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile


def probe():
    from auto_agents.process_supervision import run_supervised_shell_command
    from auto_agents.verification_sandbox import (
        ConfinementPreflightError, metadata_probe_command, verification_argv,
    )

    report = {'ok': False, 'phase': 'replay_confinement', 'provider_calls': 0,
              'python': sys.executable}
    try:
        with tempfile.TemporaryDirectory(prefix='replay-confinement-') as temporary:
            root = Path(temporary)
            candidate, protected = root / 'candidate', root / 'protected'
            candidate.mkdir()
            protected.mkdir()
            reference = protected / 'input'
            reference.write_bytes(b'retained')
            mode = reference.stat().st_mode
            command = metadata_probe_command(sys.executable, candidate, reference)
            # Exercise the real namespace launcher AND its metadata checks.
            # No product checkout, test selector or provider is involved.
            with verification_argv(command, candidate, protected,
                                   execution_environment=dict(os.environ)) as argv:
                result = run_supervised_shell_command(shlex.join(argv), cwd=candidate,
                    timeout_seconds=20, kind='replay_confinement')
            records = []
            for line in result.stdout.splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get('phase') == 'metadata_preflight':
                    records.append(value)
            if (result.returncode or result.cleanup_incomplete or not records
                    or records[-1].get('ok') is not True
                    or reference.read_bytes() != b'retained' or reference.stat().st_mode != mode):
                raise RuntimeError('recovery confinement probe failed: ' +
                                   (result.stdout + result.stderr)[-2000:])
            report.update(ok=True, metadata=records[-1])
    except ConfinementPreflightError as error:
        report.update(error=str(error), diagnostic=error.diagnostic)
    except (OSError, RuntimeError, ValueError) as error:
        report.update(error=str(error))
    return report


if __name__ == '__main__':
    # The controller supplies this source independently of /work. An isolated
    # interpreter prevents the candidate from replacing the probe's launcher.
    sys.path.insert(0, '/opt/repair/controller')
    observed = probe()
    print(json.dumps(observed, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if observed['ok'] else 1)
