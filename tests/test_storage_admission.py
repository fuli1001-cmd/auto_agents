from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.storage_admission import CopyAdmission, DiskSpaceError, RESERVE_BYTES, require_space


def test_wsl_backing_drive_is_checked_even_when_linux_has_space(tmp_path):
    host = tmp_path / 'windows-drive'
    def usage(path):
        return SimpleNamespace(free=RESERVE_BYTES - 1 if Path(path) == host else 100 * 1024 ** 3)
    with patch('auto_agents.storage_admission.windows_backing_roots', return_value=(host,)), \
         patch('auto_agents.storage_admission.shutil.disk_usage', side_effect=usage):
        with pytest.raises(DiskSpaceError, match='windows-drive') as caught:
            require_space(tmp_path)
    assert caught.value.code == 'disk_space'


def test_copy_checks_again_after_admission_before_writing(tmp_path):
    source = tmp_path / 'source'; source.write_text('must remain intact')
    with patch('auto_agents.storage_admission.require_space') as check:
        copying = CopyAdmission(tmp_path / 'target')
        check.side_effect = DiskSpaceError('disk_space', 'space consumed by another process')
        with pytest.raises(DiskSpaceError): copying(source, tmp_path / 'target')
    assert not (tmp_path / 'target').exists() and source.read_text() == 'must remain intact'


@pytest.mark.parametrize('tracked', [False, True])
def test_diagnostic_snapshot_excludes_only_generated_next_builds(tmp_path, tracked):
    from auto_agents.root_cause import RootCauseCoordinator
    import subprocess
    def git(root, *args):
        return subprocess.check_output(['git', '-c', 'user.name=test', '-c', 'user.email=test@localhost',
                                        '-C', str(root), *args], text=True).strip()
    source = tmp_path / 'repo'; source.mkdir(); git(source, 'init', '-q')
    (source / '.gitignore').write_text('.next/\n')
    (source / 'source.py').write_text('source')
    cache = source / 'web/.next/cache'; cache.mkdir(parents=True)
    (cache / 'artifact.bundle').write_text('generated')
    git(source, 'add', '.')
    if tracked: git(source, 'add', '-f', 'web/.next/cache/artifact.bundle')
    git(source, 'commit', '-qm', 'baseline')
    if tracked: (cache / 'artifact.bundle').write_text('dirty tracked work')
    target = tmp_path / 'diagnosis'
    RootCauseCoordinator._copy_diagnostic_tree(source, target)
    assert (target / 'web/.next/cache/artifact.bundle').exists() == tracked
    if tracked:
        assert (target / 'web/.next/cache/artifact.bundle').read_text() == 'dirty tracked work'
