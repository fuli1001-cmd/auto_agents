"""Small engine test command binding; no self-repair execution dependency."""
from pathlib import Path
from typing import Optional
import re
import shlex
import sys


def self_repair_verification_command(command, repo_root, *, repository_aliases=None, python_executable=None):
    normalized = str(command).strip()
    aliases = {repo_root.name, *(repository_aliases or ())}
    leading = re.fullmatch(r'cd\s+((?:\'[^\']*\'|"[^"]*"|[^\s;&|]+))\s*&&\s*(.+)', normalized, flags=re.DOTALL)
    if leading:
        parts = shlex.split(leading.group(1))
        if len(parts)==1:
            path=Path(parts[0]); alias=path.as_posix().removeprefix('./').rstrip('/')
            if not path.is_absolute() and '/' not in alias and alias in aliases and not (repo_root/path).is_dir():
                normalized=leading.group(2).strip()
    try: parts=shlex.split(normalized)
    except ValueError: return normalized
    if len(parts)>=3 and parts[0] in {'python','python3'} and parts[1:3]==['-m','pytest']: arguments=parts[3:]
    elif parts and parts[0]=='pytest': arguments=parts[1:]
    else: return normalized
    code='import sys; sys.path.insert(0, '+repr(str((repo_root/'src').resolve()))+'); import pytest; raise SystemExit(pytest.main(sys.argv[1:]))'
    return shlex.join([python_executable or sys.executable,'-c',code,*arguments])


def _is_pytest_verification_command(command):
    try: parts=shlex.split(str(command))
    except ValueError: return False
    return any(Path(part).name=='pytest' or part=='-m' and index+1<len(parts) and parts[index+1]=='pytest'
               for index,part in enumerate(parts))


def self_repair_verify_commands(env=None):
    return [shlex.join([sys.executable,'-m','pytest','-q','tests'])]


def _supplemental_verification_skip_reason(*args, **kwargs):
    return ''
