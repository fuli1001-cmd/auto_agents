"""Exercise pytest's bootstrap with the source selection used by repair proof."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('selected_revision', ['base', 'candidate', None])
def test_pytest_preserves_explicit_engine_source(tmp_path, selected_revision):
    checkout = tmp_path / 'candidate'
    checkout.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'conftest.py', checkout / 'conftest.py')
    for revision in ('base', 'candidate'):
        package = tmp_path / revision / 'src' / 'auto_agents'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text(f'REVISION = {revision!r}\n')
    (checkout / 'auto_agents.py').write_text('raise RuntimeError("launcher shadows package")\n')
    expected = selected_revision or 'candidate'
    (checkout / 'tests').mkdir()
    (checkout / 'tests/test_revision.py').write_text(
        'import auto_agents\n'
        'def test_revision():\n'
        f'    assert auto_agents.REVISION == {expected!r}\n'
        f'    assert auto_agents.__file__ == {str(tmp_path / expected / "src/auto_agents/__init__.py")!r}\n'
    )
    script = 'import sys; '
    if selected_revision:
        script += f'sys.path.insert(0, {str(tmp_path / selected_revision / "src")!r}); '
    script += 'import pytest; raise SystemExit(pytest.main(sys.argv[1:]))'
    environment = dict(os.environ)
    # This child models the verification driver's explicit sys.path, without
    # inheriting the outer test runner's import overrides or pytest options.
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTEST_ADDOPTS', None)
    completed = subprocess.run(
        [sys.executable, '-c', script, '-p', 'no:cacheprovider', '-q',
         '--confcutdir=' + str(checkout), 'tests/test_revision.py'],
        cwd=checkout, env=environment, text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '1 passed' in completed.stdout
