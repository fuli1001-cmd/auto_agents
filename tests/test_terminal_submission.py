"""Real terminal submissions exercise EOF, repeated reads and interruption."""

import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time
import pytest
from auto_agents.control import Store
from auto_agents.control.interaction import read_multiline
from auto_agents.control.workspace import git
from test_watch_invocation import native_fixture, ENGINE


def read_until(stream, marker, timeout=15):
    result=bytearray()
    deadline=time.monotonic()+timeout
    while marker not in result:
        remaining=deadline-time.monotonic()
        assert remaining>0,f'Terminal output did not contain {marker!r}: {bytes(result)!r}'
        ready,_,_=select.select([stream],[],[],remaining)
        assert ready,f'Terminal output did not contain {marker!r}: {bytes(result)!r}'
        chunk=os.read(stream.fileno(),4096)
        assert chunk,f'Terminal closed before {marker!r}: {bytes(result)!r}'
        result.extend(chunk)
    return bytes(result)


def launch_terminal(tmp_path, *, supervised=True, flags=()):
    root=native_fixture(tmp_path)
    (root/'spec.md').write_text('Reference specification must not become the collab goal')
    git(root,'add','spec.md')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','reference fixture')
    binary=tmp_path/'bin';binary.mkdir()
    watcher=binary/'auto-agents-watch'
    watcher.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0,{str(ENGINE/"supervisor/src")!r})\n'
        'from auto_agents_watch.cli import main\nraise SystemExit(main())\n')
    watcher.chmod(0o700)
    environment={**os.environ,'PYTHONPATH':str(ENGINE/'src'),
        'PATH':str(binary)+os.pathsep+os.environ['PATH'],'AUTO_AGENTS_WATCH_ROOT':str(tmp_path/'watch')}
    environment.pop('AUTO_AGENTS_NO_SUPERVISOR',None)
    environment.pop('AUTO_AGENTS_ACCEPTANCE_BUDGET',None)
    master,slave=pty.openpty()
    argv=[sys.executable,'-m','auto_agents','collab','--project',str(root),'--provider','fixture','--auto-approve',*flags]
    if not supervised:argv+=['--no-supervisor']
    child=subprocess.Popen(argv,cwd=ENGINE,env=environment,stdin=slave,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    os.close(slave)
    return root,master,child


def finish(child, master):
    if child.poll() is None:
        try:os.killpg(child.pid,signal.SIGTERM)
        except ProcessLookupError:child.terminate()
        try:child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:child.kill()
            child.wait(timeout=5)
    os.close(master)


@pytest.mark.parametrize('text',['第一行\n\n第二段\n  保留代码缩进\n','Single-line goal\n'])
@pytest.mark.parametrize('supervised',[False,True])
def test_enter_keeps_reading_and_ctrl_d_submits_exact_goal(tmp_path,text,supervised):
    root,master,child=launch_terminal(tmp_path,supervised=supervised)
    try:
        read_until(child.stderr,b'Ctrl+D')
        os.write(master,text.encode())
        assert child.poll() is None and Store(root,readonly=True).works()==[]
        os.write(master,b'\x04')
        output,error=child.communicate(timeout=25)
        assert child.returncode==0,(output+error).decode()
        work=Store(root).works()[0]
        assert Store(root).contract(work['contract']).goal==text.strip()
        assert work['calls']==1 and work['status']=='WAITING'
    finally:finish(child,master)


def test_ctrl_d_does_not_poison_a_second_read_of_same_terminal(tmp_path):
    script='''import sys,json
from auto_agents.control.interaction import read_multiline
for i in range(2):
 value=read_multiline('Goal '+str(i))
 print('RESULT '+str(i)+' '+json.dumps(value,ensure_ascii=False),flush=True)
'''
    master,slave=pty.openpty()
    child=subprocess.Popen([sys.executable,'-c',script],cwd=tmp_path,
        env={**os.environ,'PYTHONPATH':str(ENGINE/'src')},stdin=slave,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    os.close(slave)
    try:
        read_until(child.stderr,b'Ctrl+D')
        os.write(master,'第一个目标\n\x04'.encode())
        first=read_until(child.stdout,b'RESULT 0 ')
        assert '第一个目标'.encode() in first or '第一个目标'.encode() in os.read(child.stdout.fileno(),4096)
        read_until(child.stderr,b'Ctrl+D')
        os.write(master,'第二个目标\n\x04'.encode())
        output,error=child.communicate(timeout=10)
        assert child.returncode==0 and '第二个目标'.encode() in output,output+error
    finally:finish(child,master)


@pytest.mark.parametrize('supervised',[False,True])
def test_empty_ctrl_d_does_not_create_work_or_dispatch(tmp_path,supervised):
    root,master,child=launch_terminal(tmp_path,supervised=supervised)
    try:
        read_until(child.stderr,b'Ctrl+D')
        os.write(master,b'\x04')
        output,error=child.communicate(timeout=15)
        assert child.returncode!=0 and b'Traceback' not in output+error
        assert Store(root).works()==[] and Store(root).operations()==[]
    finally:finish(child,master)


@pytest.mark.parametrize('supervised',[False,True])
def test_ctrl_c_cancels_before_goal_without_traceback_or_dispatch(tmp_path,supervised):
    root,master,child=launch_terminal(tmp_path,supervised=supervised)
    try:
        read_until(child.stderr,b'Ctrl+D')
        os.killpg(child.pid,signal.SIGINT)
        output,error=child.communicate(timeout=15)
        assert child.returncode==130,(output+error).decode()
        assert b'Traceback' not in output+error and b'KeyboardInterrupt' not in output+error
        assert Store(root).works()==[] and Store(root).operations()==[]
        if supervised:
            from auto_agents_watch.store import Store as WatchStore
            jobs=WatchStore(tmp_path/'watch').list()
            assert len(jobs)==1 and jobs[0]['state']=='STOPPED' and not jobs[0].get('process')
    finally:finish(child,master)


def test_multiline_keeps_paragraphs_and_partial_final_line():
    output=io.StringIO()
    assert read_multiline('Goal',stream=io.StringIO('first\n\n  indented\nlast'),output=output)=='first\n\n  indented\nlast'
    assert 'Ctrl+D' in output.getvalue()


def test_partial_terminal_line_is_not_lost_at_eof(tmp_path):
    script='''import sys,os,io
from auto_agents.control.interaction import read_multiline
class TracedRaw(io.RawIOBase):
 def readable(self):return True
 def readinto(self,buffer):
  value=os.read(sys.stdin.fileno(),len(buffer))
  buffer[:len(value)]=value
  print('CHUNK '+str(len(value)),flush=True)
  return len(value)
stream=io.TextIOWrapper(io.BufferedReader(TracedRaw()),encoding='utf-8')
print(repr(read_multiline('Goal',stream=stream)),flush=True)
'''
    master,slave=pty.openpty()
    child=subprocess.Popen([sys.executable,'-c',script],cwd=tmp_path,
        env={**os.environ,'PYTHONPATH':str(ENGINE/'src')},stdin=slave,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    os.close(slave)
    try:
        read_until(child.stderr,b'Ctrl+D')
        os.write(master,b'Goal without trailing newline\x04')
        # The first EOF flushes the unfinished line. Send the final empty
        # EOF after that read, as a person pressing Ctrl+D again would.
        read_until(child.stdout,b'CHUNK 29')
        os.write(master,b'\x04')
        output,error=child.communicate(timeout=10)
        assert child.returncode==0 and b'Goal without trailing newline' in output,output+error
    finally:finish(child,master)


def test_json_terminal_prompt_stays_out_of_machine_stdout(tmp_path):
    root,master,child=launch_terminal(tmp_path,supervised=False,flags=['--json'])
    try:
        prompt=read_until(child.stderr,b'Ctrl+D')
        assert '请输入目标或问题'.encode() in prompt
        os.write(master,b'Exact JSON-mode goal\n\x04')
        output,error=child.communicate(timeout=20)
        value=json.loads(output)
        assert child.returncode==0 and value['work']['status']=='WAITING',output+error
        assert Store(root).contract(value['work']['contract']).goal=='Exact JSON-mode goal'
    finally:finish(child,master)


def test_interrupt_preserves_unconfirmed_maintenance_call_and_budget(tmp_path,monkeypatch):
    from auto_agents_watch.runner import Runner
    from auto_agents_watch.store import Store as WatchStore
    from types import SimpleNamespace

    store=WatchStore(tmp_path/'watch')
    job=store.create(['business'],tmp_path/'project',ENGINE)
    store.save(job,'REPAIRING',needs_maintenance=True)
    runner=Runner(store)
    def interrupted(job):
        store.reserve(job,'implement',max_calls=3)
        raise KeyboardInterrupt
    monkeypatch.setattr(runner,'maintain',interrupted)
    monkeypatch.setattr(runner,'quiesce',lambda *a:SimpleNamespace(returncode=0))
    result=runner.resume(job['id'])
    assert result['state']=='STOPPED' and result['interrupted'] and result['cancel_requested']
    assert result['model_calls']==1 and result['active_call']
    with pytest.raises(RuntimeError,match='Unconfirmed model operation'):
        runner.resume(job['id'],explicit=True)


def test_interrupt_quiesces_registered_child_and_releases_project_lock(tmp_path):
    from auto_agents_watch.store import Store as WatchStore
    from auto_agents.process_supervision import process_start_ticks
    from auto_agents.run_lock import ProjectRunLock
    from auto_agents_watch.runner import Runner
    import threading

    store=WatchStore(tmp_path/'watch')
    job=store.create(['business'],tmp_path/'project',ENGINE)
    owned=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    reaper=threading.Thread(target=owned.wait,daemon=True);reaper.start()
    class Interrupted(Runner):
        def business(self,job):
            control=Path(job['project'])/'.auto-agents/state/run.processes.json'
            control.write_text(json.dumps({'project':job['project'],'run_token':self.token,'processes':[
                {'pid':owned.pid,'pgid':owned.pid,'start_ticks':process_start_ticks(owned.pid)}]}))
            raise KeyboardInterrupt
    try:
        result=Interrupted(store).resume(job['id'])
        assert result['state']=='STOPPED' and result['quiesced'] and result['interrupted']
        reaper.join(timeout=5)
        assert owned.poll() is not None
        with ProjectRunLock(Path(job['project']),environ={}):pass
    finally:
        if owned.poll() is None:owned.terminate();owned.wait(timeout=5)
