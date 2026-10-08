"""Review effort follows actual Git changes, not the size of edited files."""
import subprocess
import pytest
from auto_agents.git_ops import changed_line_count
from auto_agents.models import ProjectConfig, TaskSpec
from auto_agents.orchestrator import Orchestrator

def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout

def lines(count):
    return ''.join((f'value_{i} = {i}\n' for i in range(count)))

@pytest.fixture
def review(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Review Test')
    git(tmp_path, 'config', 'user.email', 'review@example.com')
    (tmp_path / 'app.py').write_text(lines(1000))
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_app.py').write_text('assert True\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'baseline')
    (tmp_path / 'tests/test_app.py').write_text('assert 1 == 1\n')
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.project_root = tmp_path
    orchestrator.config = ProjectConfig(project_name='review-test')
    orchestrator.config.execution.evidence_preflight.mode = 'off'
    return (orchestrator, TaskSpec('task-001', 'Update app', 'Adjust behavior', []))

@pytest.mark.parametrize('staged', [False, True])
def test_unborn_repository_counts_additions(tmp_path, staged):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'new.py').write_text('first\nlast')
    if staged:
        git(tmp_path, 'add', 'new.py')
    assert changed_line_count(tmp_path, ['new.py']) == 2
