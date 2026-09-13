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
import json, os, subprocess, sys
from pathlib import Path
from auto_agents.verification_input_trace import owner_identity
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.gates import GateCommandMetadata
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_gate_execution import _project, _config, _git
root=Path.cwd(); project=_project(root)
(project/'input.txt').write_text('one')
_git(project,'add','-A'); _git(project,'commit','-m','input')
script="""import os, errno
from pathlib import Path
assert Path('input.txt').read_text() == 'one'
Path('private').write_text('own'); Path('private').chmod(0o750)
fd=os.open('private',os.O_RDONLY); os.fchmod(fd,0o640); os.utime(fd,ns=(1,1)); os.close(fd)
try: os.chmod({{SHARED!r}},0o777)
except OSError as e: assert e.errno in (1,13,30)
else: raise AssertionError('shared changed')
""".replace('{{SHARED!r}}',repr(SHARED))
import shlex
command=shlex.join([sys.executable,'-c',script])
config=_config(root); config.verification_policy_version=3
with LocalGatePlanExecutor(project,config,{{command:GateCommandMetadata(cache_scope='source',result_cache_scope={scope!r})}}) as executor:
    executor.sandbox_target=Path(SHARED).parent
    result=executor.run(command,timeout_seconds=30,adaptive_timeout_enabled=False,idle_timeout_seconds=30)
assert result.ok, result.stderr
assert result.input_trace_complete, result
assert owner_identity()['trace'] == 1
''')


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
