"""Pre-compaction cleanup must preserve live or unowned temporary inputs."""
import importlib.util
import os
from pathlib import Path
import pytest

script = Path(__file__).resolve().parents[1]/'scripts/reclaim_wsl_space.py'
spec = importlib.util.spec_from_file_location('reclaim_wsl_space', script)
reclaim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reclaim)


def process(root, pid, argv, cwd=None):
    folder=root/str(pid);folder.mkdir();(folder/'fd').mkdir()
    (folder/'cmdline').write_bytes(b'\0'.join(v.encode() for v in argv)+b'\0')
    if cwd:(folder/'cwd').symlink_to(cwd, target_is_directory=True)
    return folder


def test_running_agent_is_detected_without_exposing_arguments(tmp_path):
    process(tmp_path,987654,['/opt/codex','--secret','sensitive-value'])
    assert reclaim.busy_agents(tmp_path)==[{'pid':987654,'executable':'codex'}]
    process(tmp_path,987655,['python','helper.py','codex'])
    assert len(reclaim.busy_agents(tmp_path))==1


def test_all_open_files_and_cwd_protect_fixtures(tmp_path):
    pool=tmp_path/'pool';pool.mkdir()
    retained=pool/'pytest-10';retained.mkdir()
    proc=tmp_path/'proc';proc.mkdir()
    p=process(proc,987654,['python'],retained)
    f=retained/'input';f.write_text('keep');(p/'fd/3').symlink_to(f)
    refs=reclaim.open_paths(proc)
    assert retained in refs and f in refs
    assert reclaim.clean_test_temporaries(pool,older_than=0,referenced=refs)==[]
    assert f.read_text()=='keep'


def test_only_old_owned_numbered_pytest_dirs_are_removed(tmp_path):
    pool=tmp_path/'pool';pool.mkdir()
    old=pool/'pytest-1';old.mkdir();(old/'output').write_text('disposable')
    os.utime(old,(10,10))
    fresh=pool/'pytest-2';fresh.mkdir();os.utime(fresh,(95,95))
    unknown=pool/'user-data';unknown.mkdir();os.utime(unknown,(10,10))
    (pool/'pytest-3').symlink_to(unknown,target_is_directory=True)
    (pool/'pytest-current').symlink_to(old,target_is_directory=True)
    assert reclaim.clean_test_temporaries(pool,older_than=20,referenced=set(),now=100)==[str(old)]
    assert fresh.exists() and unknown.exists() and (pool/'pytest-3').is_symlink()
    assert not (pool/'pytest-current').is_symlink()


def test_symbolic_pool_is_never_adopted(tmp_path):
    directory=tmp_path/'user';directory.mkdir();(directory/'pytest-1').mkdir()
    link=tmp_path/'pool';link.symlink_to(directory,target_is_directory=True)
    assert reclaim.clean_test_temporaries(link,older_than=0,referenced=set())==[]
    assert (directory/'pytest-1').is_dir()


def test_busy_preflight_never_imports_or_runs_cleanup(tmp_path,monkeypatch):
    monkeypatch.setattr(reclaim,'busy_agents',lambda:[{'pid':99,'executable':'codex'}])
    with pytest.raises(RuntimeError,match='Close running agents'):
        reclaim.reclaim([],engine=tmp_path,preflight=True)
