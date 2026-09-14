"""Fresh-owner diagnostics must survive both production acceptance boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

from auto_agents.verification_sandbox import verification_argv
from auto_agents.verification_supervisor_checks import REPORT_ENV


@pytest.mark.parametrize('different_candidate', [False, True])
def test_all_fresh_owner_checks_through_two_production_boundaries(tmp_path, monkeypatch, different_candidate):
    from auto_agents.verification_input_trace import owner_identity
    from auto_agents.verification_supervisor_checks import observation, source_identity
    expected_sources = (observation('owner_death')['sources'] if owner_identity()['metadata']
                        else source_identity())
    root, shared = tmp_path / 'candidate', tmp_path / 'shared'
    root.mkdir(); shared.mkdir()
    (shared / 'input').write_text('retained')
    source = Path(__file__).resolve().parents[1]
    if different_candidate:
        shutil.copytree(source / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('verification_sandbox.py', 'verification_input_trace.py'):
            with (root / 'src' / 'auto_agents' / name).open('a') as stream:
                stream.write('\n# Candidate differs from the selected launcher.\n')
    else:
        (root / 'src').symlink_to(source / 'src', target_is_directory=True)
    tests = [str(source / 'tests' / name) + '::' + node for name, node in [
        ('test_verification_input_trace.py', 'test_legacy_owner_executes_once_without_claiming_complete_trace'),
        ('test_verification_metadata.py', 'test_unsupported_supervision_never_executes_the_command'),
        ('test_verification_metadata.py', 'test_supervisor_death_kills_its_tracees'),
        ('test_verification_supervisor_checks.py', 'test_death_check_detects_missing_exitkill_and_cleans_up')]]
    # Match the worker's existing sandbox context, without requesting another
    # namespace. The real launcher still establishes and narrows its owner.
    monkeypatch.setenv('AUTO_AGENTS_VERIFICATION_SANDBOX', '1')
    program = f'''
import os,subprocess,sys
from pathlib import Path
from auto_agents.verification_input_trace import owner_identity
from auto_agents.verification_sandbox import verification_argv
from auto_agents.verification_supervisor_checks import observation, source_identity, REPORT_ENV
import json
assert owner_identity()['trace']==1
observed=observation('owner_death')
assert observed['sources']=={expected_sources!r}
if {different_candidate!r}:
    assert observed['sources']!=source_identity()
    retained=os.environ[REPORT_ENV]
    for field,value,reason in [('sources',source_identity(),'source mismatch'),
                               ('launcher_pid',-1,'owner mismatch')]:
        report=json.loads(retained); report[field]=value
        os.environ[REPORT_ENV]=json.dumps(report)
        try: observation('owner_death')
        except RuntimeError as error: assert reason in str(error),str(error)
        else: raise AssertionError('accepted a mismatched report')
    os.environ[REPORT_ENV]=retained
for phase in ['quick','expanded']:
    private=Path(phase); private.mkdir()
    with verification_argv([sys.executable,'-m','pytest','-q','-p','no:cacheprovider',
            '--basetemp='+str(private.resolve()/'pytest'),*{tests!r}], private, Path({str(shared)!r}),
            python_paths=[{str(root / 'src')!r}], supervisor_checks=True) as command:
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert '4 passed' in result.stdout,result.stdout
'''
    with verification_argv([sys.executable, '-c', program], root, shared,
                           supervisor_checks=True) as command:
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (shared / 'input').read_text() == 'retained'


@pytest.mark.parametrize('report', [None, {'version': 1, 'sources': {}, 'checks': {}}, 'foreign_owner'])
def test_inherited_owner_cannot_invent_or_reuse_foreign_diagnostics(tmp_path, report):
    source = Path(__file__).resolve().parents[1] / 'src'
    program = '''
from auto_agents.verification_supervisor_checks import observation
try: observation('owner_death')
except RuntimeError as error:
    assert 'selected engine verification launcher' in str(error)
else: raise AssertionError('accepted absent or foreign fresh-owner evidence')
'''
    bootstrap = f'''
import os,sys
sys.path.insert(0,{str(source)!r})
from auto_agents.verification_metadata import metadata_exec
raise SystemExit(metadata_exec([sys.executable,'-c',{'import sys; sys.path.insert(0,' + repr(str(source)) + ');' + program!r}], [{str(tmp_path)!r}]))
'''
    env = dict(os.environ)
    env.pop(REPORT_ENV, None)
    if report == 'foreign_owner':
        from auto_agents.verification_supervisor_checks import source_identity
        report = {'version': 1, 'sources': source_identity(), 'launcher_pid': -1, 'checks': {}}
    if report is not None:
        env[REPORT_ENV] = json.dumps(report)
    result = subprocess.run([sys.executable, '-I', '-c', bootstrap], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_death_check_detects_missing_exitkill_and_cleans_up():
    from auto_agents.verification_supervisor_checks import observation
    result = observation('missing_exitkill')
    assert result['tracer_pid'] == result['supervisor_pid']
    assert result['pid'] != result['supervisor_pid']
    assert result['returncode'] == -9
    assert result['tracee_pipes_closed'] is False
