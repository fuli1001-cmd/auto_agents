"""Owned process groups and fixed loop detection; no model observer."""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import ExitStack
import time


def process_identity(pid):
    try:
        return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]]
    except (OSError, IndexError): return None


def alive(record):
    return bool(record and process_identity(record['pid']) == record['identity'])


def cancel_owned(record):
    if alive(record):
        try:os.killpg(record['pid'],signal.SIGTERM)
        except ProcessLookupError:pass
    if record and record.get('container') and record.get('container_owner'):
        name=record['container']
        inspected=subprocess.run(['docker','inspect','--format',
            '{{index .Config.Labels "auto-agents-watch.owner"}}',name],capture_output=True,text=True,timeout=10)
        if inspected.returncode==0 and inspected.stdout.strip()==record['container_owner']:
            subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)


def cycle(observation, limit=3):
    if observation.get('waiting_for'):
        return False
    progress = observation.get('progress_seq', 0)
    counts = {}
    for row in observation.get('steps', []):
        if row['progress_seq'] != progress: continue
        counts[row['step_id']] = counts.get(row['step_id'], 0) + 1
    return any(count >= limit for count in counts.values())


def terminate(process, grace=10):
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: return
    try: process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=grace)
    # The group can outlive its leader. It belongs to this operation only.
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass


def run(argv, *, cwd, env, log, timeout=None, observation=None, repeat_limit=3, heartbeat_timeout=120,
        on_start=None, cancelled=None, stdin=None, pass_fds=(), stream=False):
    started = time.monotonic()
    record = {}
    position = 0
    def relay():
        nonlocal position
        if stream:
            with Path(log).open(errors='replace') as reader:
                reader.seek(position)
                content = reader.read()
                position = reader.tell()
            if content:
                sys.stderr.write(content); sys.stderr.flush()
    with ExitStack() as stack:
        output=stack.enter_context(Path(log).open('w'))
        input_file=None
        if stdin is not None:
            input_file=stack.enter_context(tempfile.TemporaryFile(mode='w+t',dir=Path(log).parent))
            input_file.write(stdin);input_file.seek(0)
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=input_file,
            stdout=output, stderr=subprocess.STDOUT, text=True, start_new_session=True, pass_fds=pass_fds)
        record = {'pid':child.pid,'identity':process_identity(child.pid),'log':str(log)}
        reason = ''
        try:
            if on_start: on_start(record)
            while child.poll() is None:
                relay()
                if cancelled and cancelled(): reason = 'cancelled'; break
                if timeout and time.monotonic() - started >= timeout: reason = 'operation_timeout'; break
                if observation and Path(observation).exists():
                    try: value = json.loads(Path(observation).read_text())
                    except (OSError, ValueError): value = {}
                    if cycle(value, repeat_limit): reason = 'control_cycle'; break
                    if value.get('heartbeat_at') and time.time() - value['heartbeat_at'] > heartbeat_timeout:
                        reason = 'heartbeat_lost'; break
                elif observation and time.monotonic()-started > heartbeat_timeout:
                    reason = 'heartbeat_lost'; break
                time.sleep(.1)
            if reason: terminate(child)
            relay()
            return {'ok': child.returncode == 0 and not reason, 'returncode':child.returncode,
                    'reason':reason,'process':record}
        finally:
            terminate(child)
