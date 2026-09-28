"""Add installed lifecycle expectations to the separately pinned release oracle."""
import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET


def run(store, manager_root, candidate, image):
    from auto_agents.repair_v2.docker import run as execute, REPLAY_ISOLATION
    root = Path(manager_root)
    with tempfile.TemporaryDirectory(prefix='runtime-probe-', dir=store.root) as temporary:
        result = Path(temporary)
        command = ['docker', 'run', '--rm', '--init', '--network', 'none', '--read-only',
                   '--user', f'{os.getuid()}:{os.getgid()}', *REPLAY_ISOLATION['session'],
                   '--memory', '2g', '--pids-limit', '512', '--tmpfs', '/tmp:rw,nosuid,exec,mode=1777,size=4g',
                   '--workdir', '/work', '-e', 'HOME=/tmp/home', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                   '-e', 'PYTHONPATH=/work/src', '-e', 'AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1',
                   '--mount', f'type=bind,src={candidate["path"]},dst=/work,readonly',
                   '--mount', f'type=bind,src={root / "tests"},dst=/work/tests,readonly',
                   '--mount', f'type=bind,src={root / "conftest.py"},dst=/work/conftest.py,readonly',
                   '--mount', f'type=bind,src={result},dst=/result', image, 'python', '-c',
                   "import sys; sys.path.insert(0,'/work/src'); from pathlib import Path; "
                   "Path('/tmp/home').mkdir(); import pytest; "
                   "raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','--confcutdir=/work',"
                   "'--junitxml=/result/proof.xml','tests/test_runtime_adoption.py']))"]
        code, _ = execute(command, timeout=1800, output=result / 'output.log')
        failed, passed, skipped = [], [], []
        if (result / 'proof.xml').is_file():
            for case in ET.parse(result / 'proof.xml').getroot().iter('testcase'):
                name = case.get('classname', '') + ':' + case.get('name', '')
                if case.find('failure') is not None or case.find('error') is not None: failed.append(name)
                elif case.find('skipped') is not None: skipped.append(name)
                else: passed.append(name)
        return {'ok': code == 0 and bool(passed) and not (failed or skipped),
                'report': {'exitstatus': code, 'passed': passed, 'failed': failed, 'skipped': skipped},
                'log': store.put_file(result / 'output.log')}
