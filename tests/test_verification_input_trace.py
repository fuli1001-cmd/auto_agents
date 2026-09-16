"""Observe real gate commands beneath the production metadata owner."""
import json
from pathlib import Path

import pytest

from test_verification_metadata import execute


def test_existing_gate_cache_regression_under_production_owner(tmp_path):
    test = Path(__file__).with_name('test_gate_execution.py')
    execute(tmp_path, f'''
import pytest
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider',
    {str(test) + '::test_auto_result_cache_reuses_when_only_unobserved_source_changes'!r}]))
''')


@pytest.mark.parametrize('scope', ['observed_inputs', 'auto'])
def test_nested_gate_tracing_keeps_private_and_shared_boundaries(tmp_path, scope):
    execute(tmp_path, f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_verification_input_trace import _nested_environment_probe
_nested_environment_probe({scope!r}, Path(SHARED))
''')


def _nested_environment_probe(scope, shared):
    import os
    import shlex
    import sys
    from pytest import MonkeyPatch
    from auto_agents.verification_input_trace import owner_identity
    from auto_agents.gate_execution import LocalGatePlanExecutor
    from auto_agents.gates import GateCommandMetadata
    from test_gate_execution import _project, _config, _git
    root = Path.cwd(); project = _project(root)
    (project / 'input.txt').write_text('one')
    (project / 'src').mkdir()
    (project / 'src/environment_choice.py').write_text("SOURCE = 'candidate'\n")
    ambient = root / 'ambient-source'; ambient.mkdir()
    (ambient / 'environment_choice.py').write_text("SOURCE = 'ambient'\n")
    _git(project, 'add', '-A'); _git(project, 'commit', '-m', 'input and selected module')
    before = shared.read_bytes(), shared.stat().st_mode
    for retain in (False, True):
        script = _environment_assertions(retain, ambient) + '''
import errno
assert Path('input.txt').read_text() == 'one'
Path('private').write_text('own'); Path('private').chmod(0o750)
fd=os.open('private',os.O_RDONLY); os.fchmod(fd,0o640); os.utime(fd,ns=(1,1)); os.close(fd)
assert Path('private').stat().st_mode & 0o777 == 0o640
assert Path('private').stat().st_mtime_ns == 1
'''
        script += 'shared = Path(' + repr(str(shared)) + ')\n' + '''
before = shared.read_bytes(), shared.stat().st_mode
fd = os.open(shared, os.O_RDONLY)
try:
    for action in (lambda: shared.write_bytes(b'bad'), lambda: shared.chmod(0o777),
                   lambda: os.fchmod(fd,0o777), lambda: os.chmod('/proc/self/fd/'+str(fd),0o777)):
        try: action()
        except OSError as error: assert error.errno in (1,13,30), error
        else: raise AssertionError('shared changed')
finally: os.close(fd)
assert (shared.read_bytes(), shared.stat().st_mode) == before
'''
        command = shlex.join([sys.executable, '-c', script])
        config = _config(root); config.verification_policy_version = 3
        with MonkeyPatch.context() as patch:
            patch.setenv('GATE_AMBIENT_SENTINEL', 'retained-value')
            patch.setenv('PYTHONPATH', str(ambient))
            with LocalGatePlanExecutor(project, config, {command: GateCommandMetadata(
                    cache_scope='source', result_cache_scope=scope)}) as executor:
                executor.sandbox_target = shared.parent
                executor.retain_execution_environment = retain
                result = executor.run(command, timeout_seconds=30, adaptive_timeout_enabled=False, idle_timeout_seconds=30)
            assert result.ok, result.stderr
            assert result.input_trace_complete, result
            assert owner_identity()['trace'] == 1
            _runtime_socket_probe(root, project, shared.parent, retain=retain, ambient=ambient)
        assert (shared.read_bytes(), shared.stat().st_mode) == before


def _environment_assertions(retain, ambient):
    # The final interpreter intentionally honors PYTHONPATH. -I belongs to the
    # trusted bootstrap, not this test of which source is actually executed.
    return '''import os
from pathlib import Path
import environment_choice
assert environment_choice.SOURCE == EXPECTED_SOURCE
assert os.environ.get('GATE_AMBIENT_SENTINEL') == EXPECTED_SENTINEL
assert Path(environment_choice.__file__).resolve() == EXPECTED_FILE
'''.replace('EXPECTED_SOURCE', repr('ambient' if retain else 'candidate')).replace(
        'EXPECTED_SENTINEL', repr('retained-value' if retain else None)).replace(
        'EXPECTED_FILE', ('Path(' + repr(str(ambient / 'environment_choice.py')) + ')' if retain else
         "Path(os.environ['AUTO_AGENTS_GATE_SANDBOX_ROOT']) / 'src/environment_choice.py'"))


def test_legacy_owner_executes_once_without_claiming_complete_trace(tmp_path):
    from auto_agents.verification_supervisor_checks import observation, LEGACY_SHA256
    result = observation('legacy_owner')
    assert result['returncode'] == 0, result
    assert result['count'] == 'executed\n'
    assert result['legacy_sha256'] == LEGACY_SHA256
    assert result['shared_unchanged'] is True
    record = result['trace']
    assert record['complete'] is False
    assert record['owner']['metadata'] == 1 and record['owner']['trace'] == 0


def test_incomplete_or_unresolved_stream_cannot_certify_inputs():
    from auto_agents.verification_input_trace import resolved_trace
    header = {'format': 'auto-agents-input-trace', 'version': 1, 'owner': 123}
    path = {'path': '/private/input', 'result': 1}
    for rows in ([header, path], [header, path, {'complete': False}],
                 [header, {'unknown': True}, {'complete': True}]):
        assert resolved_trace('\n'.join(map(json.dumps, rows))) is None


def test_candidate_imports_cannot_replace_the_live_owner(tmp_path):
    execute(tmp_path, '''
import json, subprocess, sys
from pathlib import Path
from auto_agents.verification_input_trace import owner_identity
import auto_agents.verification_input_trace as trace
before=owner_identity()
candidate=Path('candidate-runtime/auto_agents'); candidate.mkdir(parents=True)
(candidate/'__init__.py').write_text('')
(candidate/'verification_metadata.py').write_text('VERSION = 999\\n')
(candidate/'verification_input_trace.py').write_bytes(Path(trace.__file__).read_bytes())
code="""import sys
sys.path.insert(0,'candidate-runtime')
import auto_agents.verification_metadata as selected
from auto_agents.verification_input_trace import owner_identity
assert selected.VERSION == 999
import json
print(json.dumps(owner_identity()))
"""
observed=json.loads(subprocess.check_output([sys.executable,'-I','-c',code],text=True))
assert observed==before and observed['metadata']==1 and observed['trace']==1
# Both acceptance phases can negotiate with the owner that already exists;
# candidate code is never needed to start or replace that owner.
Path('input').write_text('retained')
for phase in ['quick','expanded']:
    result=subprocess.run([sys.executable,str(candidate/'verification_input_trace.py'),phase+'.json',
        'sh','-c','cat input'],capture_output=True,text=True)
    assert result.returncode==0 and result.stdout=='retained', result.stderr
    rows=[json.loads(line) for line in Path(phase+'.json').read_text().splitlines()]
    assert rows[0]['owner']==before['owner'] and rows[-1]['complete'] is True
''')


def test_tracing_captures_fork_cwd_fd_and_negative_inputs(tmp_path):
    execute(tmp_path, '''
import json, os, subprocess, sys
from pathlib import Path
import auto_agents.verification_input_trace as trace
Path('sub').mkdir(); Path('sub/input').write_text('retained')
program="""import os
pid=os.fork()
if pid==0:
    os.chdir('sub')
    directory=os.open('.',os.O_RDONLY)
    fd=os.open('input',os.O_RDONLY,dir_fd=directory)
    assert os.read(fd,99)==b'retained'
    try: os.stat('missing',dir_fd=directory)
    except FileNotFoundError: pass
    else: raise AssertionError('missing input exists')
    os._exit(0)
assert os.waitpid(pid,0)[1]==0
"""
result=subprocess.run([sys.executable,trace.__file__,'trace.json',sys.executable,'-c',program],capture_output=True,text=True)
assert result.returncode==0,result.stderr
rows=[json.loads(line) for line in Path('trace.json').read_text().splitlines()]
assert rows[-1]['complete'] is True,rows[-1]
assert any(row.get('path')==str(Path('sub/input').resolve()) for row in rows)
assert any(row.get('path')==str(Path('sub/missing').resolve()) and row['result']==-2 for row in rows)
''')


@pytest.mark.parametrize('scope', ['observed_inputs', 'auto'])
@pytest.mark.parametrize('switch', [False, True], ids=['retained', 'global_plan_switch'])
def test_public_resume_tracing_inside_production_owner(tmp_path, scope, switch):
    execute(tmp_path, f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_session_verification_ownership import project, run_session, git
from auto_agents.config import load_project_config, save_project_config, save_task_plan, load_session_state, save_session_state
from auto_agents.git_ops import head_ref
from auto_agents.models import VerificationStep
from auto_agents.gate_execution import LocalGatePlanExecutor
from pytest import MonkeyPatch
root, state=project(Path.cwd())
config=load_project_config(root)
step=config.gates.steps[0]; step.cache_scope='source'; step.result_cache_scope={scope!r}
save_project_config(root,config)
save_task_plan(root, {{'tasks':[{{'task_id':'task-owned','title':'Owned contract','requirement_ids':['REQ-owned'],
    'verification_refs':step.targets}}], 'verification_steps':[step.to_dict()], 'verification_policy_version':4}})
git(root,'add','-A'); git(root,'commit','-m','retained trace contract')
state.baseline_head_ref=state.baseline_git_ref=head_ref(root); save_session_state(root,state)
if {switch!r}:
    foreign=VerificationStep(runner='pytest',targets=['tests/test_future.py::test_future'],proof_id='foreign.future',levels=['affected','release'])
    config.gates.steps=[foreign]; save_project_config(root,config)
    save_task_plan(root,{{'tasks':[{{'task_id':'foreign','title':'Future','status':'pending'}}],
        'verification_steps':[foreign.to_dict()], 'verification_policy_version':4}})
before=(root/'.auto-agents/state/task_plan.json').read_bytes()
observed=[]; original=LocalGatePlanExecutor.run
def run(executor,command,**kwargs):
    result=original(executor,command,**kwargs)
    metadata=executor.metadata.get(command)
    observed.append((getattr(metadata,'cache_scope',''),getattr(metadata,'result_cache_scope',''),result.ok,result.stderr))
    return result
with MonkeyPatch.context() as patch:
    patch.setattr(LocalGatePlanExecutor,'run',run)
    result,calls,orch=run_session(root,patch)
assert result.status=='completed', result.to_dict()
assert calls and len(calls)==1, calls
assert any(row[:3]==('source',{scope!r},True) for row in observed),observed
assert (root/'.auto-agents/state/task_plan.json').read_bytes()==before
assert load_session_state(root,state.session_id).verification_binding['session_id']==state.session_id
''')


def _custody_cache_probe(scope, attack=None, confined=True, recovery=False):
    import os
    import shlex
    import sys
    from pytest import MonkeyPatch
    from auto_agents.gate_execution import LocalGatePlanExecutor
    import auto_agents.gate_execution as gates
    import auto_agents.verification_input_trace as trace_module
    TraceCustody = getattr(trace_module, "TraceCustody", None)
    from auto_agents.gates import GateCommandMetadata
    from test_gate_execution import _project, _config, _git
    root = Path.cwd()
    project = _project(root)
    dependency = root / 'shared-dependency'; dependency.mkdir()
    shared_input = dependency / 'input.txt'; shared_input.write_text('shared')
    shared_input.chmod(0o640)
    dependency_links = {'.conda': dependency}
    (project / 'input.txt').write_text('one')
    (project / 'stat-only.txt').write_text('one')
    (project / 'pyproject.toml').write_text('[build-system]\nrequires=[]\n')
    (project / 'fault').write_text('signal' if recovery else 'healthy')
    (project / 'unrelated').write_text('first')
    _git(project, 'add', '-A'); _git(project, 'commit', '-m', 'trace custody inputs')
    program = '''import os, signal
from pathlib import Path
value = Path('input.txt').read_text()
shared = Path('.conda/input.txt')
assert Path('.conda').is_symlink()
assert shared.read_text() == 'shared', 'shared dependency changed'
lookup = Path(os.environ['PROBE_LOOKUP_ROOT'])
assert '..' in lookup.parts
assert (lookup / 'stat-only.txt').stat().st_size == 3, 'stat-only dependency changed'
try:
    (lookup / 'missing.txt').stat()
except FileNotFoundError:
    pass
else:
    raise AssertionError('negative dependency changed')
p = Path(os.environ['TMPDIR']) / 'private'; p.write_text('own'); p.chmod(0o750)
fd = os.open(p, os.O_RDONLY); os.fchmod(fd,0o640); os.utime(fd,ns=(1,1)); os.close(fd)
if Path('fault').read_text() == 'signal':
    pid = os.fork()
    if pid == 0: os.kill(os.getpid(), signal.SIGKILL)
    os.waitpid(pid, 0)
'''
    if attack:
        program += '''
import json
trace = Path(os.environ['PROBE_TRACE'])
fake = '\\n'.join(json.dumps(row) for row in [
    {'format':'auto-agents-input-trace','version':1,'owner':os.environ['PROBE_OWNER']},
    {'path':str(Path('pyproject.toml').resolve()),'result':0}, {'complete':True}])+'\\n'
def denied(action):
    try: action()
    except OSError as error: assert error.errno in (1,2,9,13,30), error
    else: raise AssertionError('writable trace evidence reached command')
'''
        if attack == 'replace':
            program += "denied(trace.unlink)\ndenied(lambda: trace.write_text(fake))\n"
        elif attack == 'overwrite':
            program += "denied(lambda: trace.open('w'))\ndenied(lambda: os.open(trace,os.O_WRONLY|os.O_TRUNC))\n"
            program += "denied(lambda: os.write(os.open(trace,os.O_WRONLY),fake.encode()))\n"
        else:
            program += '''
for number in list(Path('/proc/self/fd').iterdir()):
    try:
        if number.resolve() == trace:
            denied(lambda: os.write(int(number.name), fake.encode()))
            denied(lambda: number.open('w'))
    except FileNotFoundError: pass
# No inherited writable handle or newly opened proc alias can name the trace.
denied(lambda: Path('/proc/self/root'+str(trace)).open('w'))
for fd in os.environ.get('PROBE_HANDLES','').split(','):
    if fd:
        denied(lambda: Path('/proc/'+os.environ['PROBE_PARENT']+'/fd/'+fd).open('w'))
'''
        program += "Path(os.environ['AUTO_AGENTS_GATE_RUNTIME_ROOT'],'input-trace.log').write_text(fake)\n"
    program += "assert value == 'one', value\n"
    command = shlex.join([sys.executable, '-I', '-c', program])
    config = _config(root); config.verification_policy_version = 3
    metadata = {command: GateCommandMetadata(cache_scope='source', result_cache_scope=scope)}
    dispatches = []
    consumed = []
    original_dispatch = gates.run_supervised_shell_command
    if TraceCustody is not None:
        original_command, original_consume = TraceCustody.command, TraceCustody.consume
    def dispatch(*args, **kwargs):
        dispatches.append(args[0])
        if TraceCustody is None:
            # Keep the same attack executable on the base engine. Its writable
            # runtime trace is the original behavioral counterexample.
            env = dict(kwargs['env'])
            env.update(PROBE_TRACE=str(Path(env['AUTO_AGENTS_GATE_RUNTIME_ROOT']) / 'input-trace.log'),
                       PROBE_OWNER=trace_module.owner_identity()['owner'],
                       PROBE_LOOKUP_ROOT=str(Path(env['AUTO_AGENTS_GATE_SANDBOX_ROOT']) / '..' /
                                             Path(env['AUTO_AGENTS_GATE_SANDBOX_ROOT']).name))
            kwargs['env'] = env
        return original_dispatch(*args, **kwargs)
    def launch(self, argv, env):
        return original_command(self, argv, dict(env, PROBE_TRACE=self.payload['trace']['path'],
                                               PROBE_LOOKUP_ROOT=str(self.directory / '..' / os.path.relpath(
                                                   self.payload['roots'][0], self.directory.parent)),
                                               PROBE_OWNER=self.payload['owner']['owner'],
                                               PROBE_PARENT=str(os.getpid()),
                                               PROBE_HANDLES=','.join(map(str,self.handles.values()))))
    def consume(self, **kwargs):
        result = original_consume(self, **kwargs)
        consumed.append((self.payload.copy(), result))
        return result
    def run():
        before = shared_input.read_bytes(), shared_input.stat().st_mode, shared_input.stat().st_mtime_ns
        with LocalGatePlanExecutor(project, config, metadata, dependency_links=dependency_links) as executor:
            if confined:
                shared = root / 'shared-target'; shared.mkdir(exist_ok=True)
                executor.sandbox_target = shared
            result = executor.run(command, timeout_seconds=20, adaptive_timeout_enabled=False, idle_timeout_seconds=20)
        assert (shared_input.read_bytes(), shared_input.stat().st_mode, shared_input.stat().st_mtime_ns) == before
        return result
    with MonkeyPatch.context() as patch:
        patch.setattr(gates, 'run_supervised_shell_command', dispatch)
        if TraceCustody is not None:
            patch.setattr(TraceCustody, 'command', launch)
            patch.setattr(TraceCustody, 'consume', consume)
        first = run()
        assert first.ok, first.stderr
        assert len(dispatches) == 1
        if recovery:
            assert not first.input_trace_complete and 'signal' in first.input_trace_reason, first
            (project / 'fault').write_text('healthy')
            healthy = run()
            assert healthy.ok and not healthy.cached and healthy.input_trace_complete, healthy
            assert len(dispatches) == 2
        elif not attack:
            assert first.input_trace_complete, first
        if not attack:
            baseline = healthy if recovery else first
            assert baseline.observed_inputs.get('.conda') == 'link:' + str(dependency), baseline.observed_inputs
            assert '@' + str(shared_input) in baseline.observed_inputs, baseline.observed_inputs
            assert 'stat-only.txt' in baseline.observed_inputs, baseline.observed_inputs
            assert baseline.observed_inputs.get('!missing.txt') == 'missing', baseline.observed_inputs
            payload = consumed[-1][0]
            assert not any(key.lstrip('!@?') == payload['trace']['path']
                           or key.lstrip('!@?') == payload['receipt']['path']
                           for key in baseline.observed_inputs)
        count = len(dispatches)
        (project / 'unrelated').write_text('second')
        second = run()
        if not attack:
            from auto_agents.verification_manifest import manifest_matches
            mismatches = [key for key, value in first.observed_inputs.items() if not manifest_matches(project, {key: value})]
            assert second.ok and second.cached and second.backend == 'result-cache-observed-inputs', (second.cache_miss_reason, mismatches)
            assert len(dispatches) == count
            payload, (stream, reason) = consumed[-1]
            assert stream and not reason
            assert json.loads(stream.splitlines()[0])['owner'] == payload['owner']['owner']
            assert json.loads(stream.splitlines()[-1])['complete'] is True
        if not attack:
            shared_input.write_text('changed')
            count = len(dispatches)
            failed = run()
            assert not failed.ok and not failed.cached and 'shared dependency changed' in failed.stderr, failed
            assert len(dispatches) == count + 1
            shared_input.write_text('shared')
            assert run().ok
            # An unchanged source tree must not reuse a different registered binding.
            alternate = root / 'alternate-dependency'; alternate.mkdir()
            (alternate / 'input.txt').write_text('different')
            dependency_links['.conda'] = alternate
            count = len(dispatches)
            failed = run()
            assert not failed.ok and not failed.cached and 'shared dependency changed' in failed.stderr, failed
            assert len(dispatches) == count + 1
            dependency_links['.conda'] = dependency
            assert run().ok
            for path, data, message in [('stat-only.txt', 'longer', 'stat-only dependency changed'),
                                        ('missing.txt', 'present', 'negative dependency changed')]:
                # Independently restore the healthy baseline for each inverse.
                baseline = run()
                assert baseline.ok, baseline
                (project / path).write_text(data)
                count = len(dispatches)
                failed = run()
                assert not failed.ok and not failed.cached and message in failed.stderr, failed
                assert len(dispatches) == count + 1
                if path == 'stat-only.txt':
                    (project / path).write_text('one')
                else:
                    (project / path).unlink()
            assert run().ok
        (project / 'input.txt').write_text('two')
        count = len(dispatches)
        third = run()
        assert not third.ok and not third.cached, third
        assert len(dispatches) == count + 1
    if not attack:
        # The same inherited owner must also enforce shared mutation denial.
        # Denied writes are not stat-only inputs for a reusable read proof.
        denial = '''import os
from pathlib import Path
shared = Path('.conda/input.txt')
before = shared.read_bytes(), shared.stat().st_mode, shared.stat().st_mtime_ns
fd = os.open(shared, os.O_RDONLY)
try:
    for action in (lambda: shared.write_text('bad'), lambda: shared.chmod(0o777),
                   lambda: os.fchmod(fd,0o777), lambda: os.utime(fd,ns=(1,1))):
        try: action()
        except OSError as error: assert error.errno in (1,13,30), error
        else: raise AssertionError('shared dependency was writable')
finally: os.close(fd)
assert (shared.read_bytes(), shared.stat().st_mode, shared.stat().st_mtime_ns) == before
'''
        denial_command = shlex.join([sys.executable, '-I', '-c', denial])
        with LocalGatePlanExecutor(project, config, {}, dependency_links=dependency_links) as executor:
            executor.sandbox_target = root / 'shared-target'
            result = executor.run(denial_command, timeout_seconds=20,
                adaptive_timeout_enabled=False, idle_timeout_seconds=20)
        assert result.ok and not result.cached, result


@pytest.mark.parametrize('scope', ['observed_inputs', 'auto'])
@pytest.mark.parametrize('attack', ['replace', 'overwrite', 'fd_alias'])
@pytest.mark.parametrize('boundary', ['executor', 'confined'])
def test_gate_trace_tampering_cannot_authorize_reuse(tmp_path, scope, attack, boundary):
    execute(tmp_path, f'''
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_verification_input_trace import _custody_cache_probe
_custody_cache_probe({scope!r}, {attack!r}, {boundary == 'confined'!r})
''')


@pytest.mark.parametrize('scope', ['observed_inputs', 'auto'])
def test_gate_trace_custody_preserves_valid_reuse(tmp_path, scope):
    execute(tmp_path, f'''
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_verification_input_trace import _custody_cache_probe
_custody_cache_probe({scope!r})
''')


@pytest.mark.parametrize('scope', ['observed_inputs', 'auto'])
def test_gate_trace_custody_recovers_after_incomplete_run(tmp_path, scope):
    execute(tmp_path, f'''
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_verification_input_trace import _custody_cache_probe
_custody_cache_probe({scope!r}, recovery=True)
''')


def _public_custody_probe(scope, attack, switch):
    import sys
    from auto_agents.config import load_project_config, save_project_config, save_task_plan, save_session_state
    from auto_agents.git_ops import head_ref
    from auto_agents.models import AgentResult, VerificationStep
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.verification_input_trace import TraceCustody
    from auto_agents.gate_execution import LocalGatePlanExecutor
    from test_session_verification_ownership import project, git
    from pytest import MonkeyPatch
    root, state = project(Path.cwd())
    test = root / 'tests/test_owned.py'
    test.write_text('''import os
from pathlib import Path
def test_owned():
    value = Path('value.py').read_text()
    path = Path(os.environ['PROBE_TRACE'])
    try:
        OPERATION
    except OSError as error:
        assert error.errno in (1, 13, 30)
    else:
        raise AssertionError('trace evidence is writable')
    assert value == 'VALUE = 1\\n'
'''.replace('OPERATION', 'path.unlink()' if attack == 'replace' else "path.write_text('{\"complete\":true}')"))
    config = load_project_config(root)
    config.gates.steps[0].cache_scope = 'source'
    config.gates.steps[0].result_cache_scope = scope
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-owned', 'title': 'Owned contract',
        'requirement_ids': ['REQ-owned'], 'verification_refs': config.gates.steps[0].targets}],
        'verification_steps': [config.gates.steps[0].to_dict()], 'verification_policy_version': 4})
    git(root, 'add', '-A'); git(root, 'commit', '-m', 'retained tampering probe')
    state.baseline_head_ref = state.baseline_git_ref = head_ref(root)
    save_session_state(root, state)
    if switch:
        foreign = VerificationStep(runner='pytest', targets=['tests/test_future.py::test_future'],
                                   proof_id='foreign.future', levels=['affected', 'release'])
        config.gates.steps = [foreign]; save_project_config(root, config)
        save_task_plan(root, {'tasks': [{'task_id': 'foreign', 'title': 'Pending', 'status': 'pending'}],
                             'verification_steps': [foreign.to_dict()], 'verification_policy_version': 4})
    (root / 'foreign.py').write_text('VALUE = 8\n'); git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n'); (root / 'foreign.py').chmod(0o640)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00untracked')
    before = ((root / 'foreign.py').read_bytes(), (root / 'foreign.py').stat().st_mode,
              git(root, 'show', ':foreign.py'), (root / '.auto-agents/state/task_plan.json').read_bytes(),
              (root / 'foreign-note.txt').read_bytes())
    writes, verifications, results = [], [], []
    orch = Orchestrator(root, user_input_fn=lambda *a, **kw: 'y')
    def writer(request):
        writes.append(request.cwd)
        (request.cwd / 'value.py').write_text('VALUE = ' + str(len(writes)) + '\n')
        reply = 'Candidate\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    original_verify, original_run = Session._run_verify, LocalGatePlanExecutor.run
    original_command = TraceCustody.command
    def launch(self, command, environment):
        return original_command(self, command, dict(environment, PROBE_TRACE=self.payload['trace']['path']))
    def run(self, command, **kwargs):
        result = original_run(self, command, **kwargs)
        results.append(result)
        return result
    def verify(self, *args, **kwargs):
        outcome = original_verify(self, *args, **kwargs)
        verifications.append(outcome)
        if len(verifications) == 1:
            assert outcome['ok'], outcome
            # A separate downstream rejection requests a second real writer
            # candidate. Gate/cache evidence from the first remains authentic.
            return dict(outcome, ok=False, reason='downstream fixture requests next candidate')
        return dict(outcome, retry_fix=False)
    with MonkeyPatch.context() as patch:
        patch.setattr(orch, '_call_with_failover', writer)
        patch.setattr(Session, '_run_verify', verify)
        patch.setattr(LocalGatePlanExecutor, 'run', run)
        patch.setattr(TraceCustody, 'command', launch)
        result = Session(orch, mode='fix', auto_approve=True).resume(state.session_id)
    assert len(writes) == 2 and len(verifications) == 2, (writes, verifications, result.to_dict())
    assert verifications[0]['ok'] and not verifications[1]['ok']
    assert result.status != 'completed'
    assert any(not entry.ok and not entry.cached and 'VALUE = 2' in (entry.stdout + entry.stderr) for entry in results)
    assert result.verification_binding['session_id'] == state.session_id
    assert result.verification_binding['contract_fingerprint']
    after = ((root / 'foreign.py').read_bytes(), (root / 'foreign.py').stat().st_mode,
             git(root, 'show', ':foreign.py'), (root / '.auto-agents/state/task_plan.json').read_bytes(),
             (root / 'foreign-note.txt').read_bytes())
    assert after == before


@pytest.mark.parametrize('scope', ['observed_inputs', 'auto'])
@pytest.mark.parametrize('attack', ['replace', 'overwrite'])
@pytest.mark.parametrize('state', ['retained', 'global_plan_switch'])
def test_public_resume_trace_tampering_keeps_owned_authority(tmp_path, scope, attack, state):
    execute(tmp_path, f'''
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_verification_input_trace import _public_custody_probe
_public_custody_probe({scope!r}, {attack!r}, {state == 'global_plan_switch'!r})
''')


@pytest.mark.parametrize('fault', ['denied_reservation', 'invalid_reservation', 'final_boundary'])
def test_gate_custody_refusal_preserves_siblings_and_recovers(tmp_path, fault):
    execute(tmp_path, f'''
import ctypes, json, os, shlex, sys
from pathlib import Path
from pytest import MonkeyPatch, raises
from auto_agents.gate_execution import LocalGatePlanExecutor
import auto_agents.gate_execution as gate_module
from auto_agents.verification_input_trace import TraceCustody
from auto_agents.verification_sandbox import RUNTIME_ROOT_ENV, RUNTIME_ID_ENV, ConfinementPreflightError
from auto_agents.gates import GateCommandMetadata
sys.path.insert(0,{str(Path(__file__).parent)!r})
from test_gate_execution import _project,_config
root=Path.cwd(); project=_project(root); config=_config(root); config.verification_policy_version=3
import secrets
pool=Path(os.environ[RUNTIME_ROOT_ENV]); sibling=pool/('sibling-'+secrets.token_hex(4)); sibling.mkdir()
(sibling/'retained').write_text('foreign'); before=(sibling/'retained').read_bytes()
command='printf actual-command-dispatched; test "$(cat tracked.txt)" = committed'
metadata={{command:GateCommandMetadata(cache_scope='source',result_cache_scope='observed_inputs')}}
original=TraceCustody.command
dispatched=[]
launch=gate_module.run_supervised_shell_command
def observe_dispatch(*args,**kwargs):
    dispatched.append(True)
    return launch(*args,**kwargs)
# Deny only the final Landlock restriction in a real child. No project body is
# allowed to execute if that kernel call fails; the inherited owner stays live.
def denied_boundary(self, argv, environment):
    original_argv=original(self,argv,environment)
    code="""import ctypes,os,sys
lib=ctypes.CDLL('libseccomp.so.2'); lib.seccomp_init.restype=ctypes.c_void_p
lib.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
lib.seccomp_load.argtypes=[ctypes.c_void_p]
context=lib.seccomp_init(0x7fff0000)
assert context and lib.seccomp_rule_add(context,0x50001,446,0)==0
assert lib.seccomp_load(context)==0
os.execv(sys.argv[1],sys.argv[1:])
"""
    return [sys.executable,'-I','-c',code,*original_argv]
def run():
    with LocalGatePlanExecutor(project,config,metadata) as executor:
        return executor.run(command,timeout_seconds=20,adaptive_timeout_enabled=False,idle_timeout_seconds=20)
with MonkeyPatch.context() as patch:
    patch.setattr(gate_module,'run_supervised_shell_command',observe_dispatch)
    if {fault!r}=='denied_reservation':
        patch.setenv(RUNTIME_ROOT_ENV,str(Path(SHARED).parent)); patch.delenv(RUNTIME_ID_ENV,raising=False)
    elif {fault!r}=='invalid_reservation':
        patch.setenv(RUNTIME_ID_ENV,'[0,0,0,0]')
    else: patch.setattr(TraceCustody,'command',denied_boundary)
    if {fault!r}!='final_boundary':
        with raises(ConfinementPreflightError) as stopped:
            run()
        diagnostic=stopped.value.diagnostic
        assert diagnostic['phase']=='runtime_allocation' and diagnostic['socket_byte_budget']==100
        assert diagnostic['command']==command
        assert not dispatched, 'failed allocation must stop before dispatch'
    else:
        refused=run()
        assert not refused.ok and 'actual-command-dispatched' not in refused.stdout, refused
        assert 'verification write boundary' in refused.stderr, refused
assert (sibling/'retained').read_bytes()==before
healthy=run(); assert healthy.ok and not healthy.cached and healthy.input_trace_complete, healthy
(project/'unrelated').write_text('new')
reused=run(); assert reused.ok and reused.cached, reused
assert (sibling/'retained').read_bytes()==before
''')


def _runtime_socket_probe(root, project, shared, *, retain=False, ambient=None):
    import shlex
    import sys
    from pytest import MonkeyPatch
    import auto_agents.gate_execution as gates
    from auto_agents.gates import GateCommandMetadata
    from test_gate_execution import _config
    program = """import os,socket
from pathlib import Path
root=Path(os.environ['AUTO_AGENTS_GATE_RUNTIME_ROOT'])
assert Path(os.environ['TMPDIR'])==root/'t'
assert os.environ['TMP']==os.environ['TEMP']==os.environ['TMPDIR']
path=root/'t'/('s'*64)
assert len(os.fsencode(path))<=100
with socket.socket(socket.AF_UNIX) as sock:
    sock.bind(str(path))
path.unlink()
"""
    if ambient is not None:
        program = _environment_assertions(retain, ambient) + program
    command=shlex.join([sys.executable,'-c',program])
    allocated=[]
    original_allocate,original_env=gates.short_job_runtime_root,gates.gate_environment
    def allocate(job, **kwargs):
        path=original_allocate(job,**kwargs); allocated.append(path); return path
    def environment(*args, **kwargs):
        assert kwargs['runtime_root']==allocated[-1]
        result=original_env(*args, **kwargs)
        assert result['AUTO_AGENTS_GATE_RUNTIME_ROOT']==str(allocated[-1])
        return result
    with MonkeyPatch.context() as patch:
        patch.setattr(gates,'short_job_runtime_root',allocate)
        patch.setattr(gates,'gate_environment',environment)
        with gates.LocalGatePlanExecutor(project,_config(root),
            {command:GateCommandMetadata(result_cache_scope='off')}) as executor:
            executor.sandbox_target=shared
            executor.retain_execution_environment=retain
            result=executor.run(command,timeout_seconds=20,adaptive_timeout_enabled=False,idle_timeout_seconds=20)
        assert result.ok and len(allocated)==1, result
        assert not allocated[0].exists()


@pytest.mark.parametrize('symlink', [False, True])
def test_runtime_allocation_rejects_foreign_job_leaves(tmp_path, symlink):
    execute(tmp_path, f'''
import os
from pathlib import Path
from pytest import MonkeyPatch, raises
import auto_agents.gate_execution as gates
from auto_agents.gates import GateCommandMetadata
import sys
sys.path.insert(0,{str(Path(__file__).parent)!r})
from test_gate_execution import _project,_config
root=Path.cwd(); project=_project(root)
foreign=root/'foreign-runtime'; foreign.mkdir(); (foreign/'retained').write_bytes(b'foreign')
original=gates.short_job_runtime_root
leaves=[]
def conflict(job, **kwargs):
    leaf=original(job,create=False); leaves.append(leaf)
    if {symlink!r}: leaf.symlink_to(foreign,target_is_directory=True)
    else:
        leaf.mkdir(mode=0o700); (leaf/'retained').write_bytes(b'foreign')
    return original(job,**kwargs)
marker=root/'must-not-execute'
command='printf forbidden > '+str(marker)
with MonkeyPatch.context() as patch:
    patch.setattr(gates,'short_job_runtime_root',conflict)
    with gates.LocalGatePlanExecutor(project,_config(root),{{command:GateCommandMetadata(result_cache_scope='auto')}}) as executor:
        with raises(gates.ConfinementPreflightError) as stopped:
            executor.run(command,timeout_seconds=20,adaptive_timeout_enabled=False,idle_timeout_seconds=20)
diagnostic=stopped.value.diagnostic
assert diagnostic['phase']=='runtime_allocation' and diagnostic['socket_byte_budget']==100
assert diagnostic['command']==command and diagnostic['attempted_location']==str(leaves[0])
assert not marker.exists(), 'failed admission must not dispatch the command'
assert (foreign/'retained').read_bytes()==b'foreign'
assert (leaves[0]/'retained').read_bytes()==b'foreign'
assert leaves[0].is_symlink()=={symlink!r}
''')


def test_custody_explicit_empty_environment_does_not_adopt_ambient(tmp_path):
    execute(tmp_path, '''
import json, os, subprocess, sys
from pathlib import Path
from pytest import MonkeyPatch
from auto_agents.verification_input_trace import TraceCustody, RETAINED_ENV
from auto_agents.verification_sandbox import verification_argv
root=Path.cwd(); private=root/'empty-environment'; private.mkdir()
program="""import os
assert 'GATE_AMBIENT_SENTINEL' not in os.environ
assert 'PYTHONPATH' not in os.environ
assert 'HOME' not in os.environ
assert os.environ['AUTO_AGENTS_VERIFICATION_SANDBOX']=='1'
"""
with MonkeyPatch.context() as patch:
    patch.setenv('GATE_AMBIENT_SENTINEL','must-not-inherit')
    patch.setenv('PYTHONPATH',str(root/'ambient-source'))
    custody=TraceCustody(Path(os.environ['TMPDIR']),'empty-environment',[private])
    try:
        with verification_argv([sys.executable,'-c',program],private,Path(SHARED).parent,
                               execution_environment={},trace_custody=custody) as argv:
            # Transport is supplied to the bootstrap, never env -i or argv.
            assert custody.environment not in ' '.join(argv)
            process=subprocess.run(argv,cwd=private,env={RETAINED_ENV:custody.environment},
                                   capture_output=True,text=True,timeout=20)
            assert process.returncode==0,process.stdout+process.stderr
    finally: custody.close()
''')
