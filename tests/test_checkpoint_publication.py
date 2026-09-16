"""Checkpoint refresh must not overwrite shared content-addressed evidence."""
import errno
import hashlib
from types import SimpleNamespace

import pytest

from auto_agents.session import Session
import auto_agents.session as session_module


@pytest.mark.parametrize('cross_device', [False, True])
def test_refresh_is_idempotent_and_preserves_other_checkpoint_blobs(tmp_path, monkeypatch, cross_device):
    if cross_device:
        def no_link(*args): raise OSError(errno.EXDEV, 'cross-device link')
        monkeypatch.setattr(session_module.os, 'link', no_link)
    owner = SimpleNamespace(project_root=tmp_path)
    source = tmp_path / 'source'; source.write_bytes(b'original')
    first, second = tmp_path / 'first', tmp_path / 'second'
    Session._copy_checkpoint_file(owner, source, first)
    Session._copy_checkpoint_file(owner, source, second)
    Session._copy_checkpoint_file(owner, source, first)
    source.write_bytes(b'new')
    Session._copy_checkpoint_file(owner, source, first)
    assert first.read_bytes() == b'new' and second.read_bytes() == b'original'
    digest = hashlib.sha256(b'original').hexdigest()
    assert (tmp_path / '.auto-agents/state/checkpoint_blobs' / digest[:2] / digest).read_bytes() == b'original'


def test_checkpoint_replacement_never_follows_existing_target_symlink(tmp_path):
    owner = SimpleNamespace(project_root=tmp_path)
    foreign = tmp_path / 'foreign'; foreign.write_bytes(b'foreign')
    target = tmp_path / 'target'; target.symlink_to(foreign)
    source = tmp_path / 'source'; source.write_bytes(b'new')
    Session._copy_checkpoint_file(owner, source, target)
    assert not target.is_symlink() and target.read_bytes() == b'new'
    assert foreign.read_bytes() == b'foreign'
    source.unlink(); source.symlink_to(foreign)
    Session._copy_checkpoint_file(owner, source, target)
    Session._copy_checkpoint_file(owner, source, target)
    assert target.is_symlink() and target.readlink() == foreign
    assert foreign.read_bytes() == b'foreign'


@pytest.mark.parametrize('existing_blob', [False, True])
def test_checkpoint_copy_failure_preserves_published_target(tmp_path, monkeypatch, existing_blob):
    owner = SimpleNamespace(project_root=tmp_path)
    source = tmp_path / 'source'; source.write_bytes(b'new')
    if existing_blob: Session._copy_checkpoint_file(owner, source, tmp_path / 'prime')
    target = tmp_path / 'target'; target.write_bytes(b'previous')
    def no_link(*args): raise OSError(errno.EXDEV, 'cross-device link')
    def no_copy(src, dst, **kwargs):
        dst.write_bytes(b'partial')
        raise OSError(errno.ENOSPC, 'disk full')
    monkeypatch.setattr(session_module.os, 'link', no_link)
    monkeypatch.setattr(session_module.shutil, 'copy2', no_copy)
    with pytest.raises(OSError, match='disk full'):
        Session._copy_checkpoint_file(owner, source, target)
    assert target.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.target.*.tmp'))
    assert not list((tmp_path / '.auto-agents/state/checkpoint_blobs').glob('*/*.tmp'))
