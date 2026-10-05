"""Local filesystem/Git primitives shared by business components only."""
from pathlib import Path
import json
import os
import subprocess
import uuid

from .engine_fault import digest


def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex)
    with temporary.open('w') as stream:
        json.dump(value,stream,sort_keys=True,ensure_ascii=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary,path)


def private_directory(path):
    path=Path(path)
    if path.is_symlink(): raise ValueError('Private state cannot be a symbolic link')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.chmod(0o700)
    return path


def git(root,*args,check=True):
    result=subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(root),*args],capture_output=True,text=True)
    if check and result.returncode: raise RuntimeError(result.stderr.strip())
    return result.stdout.strip() if check else result


def process_alive(pid, ticks=None):
    from .process_supervision import process_start_ticks
    try: current=process_start_ticks(int(pid))
    except (OSError,ValueError,TypeError): return False
    return bool(current and (ticks is None or str(current)==str(ticks)))
