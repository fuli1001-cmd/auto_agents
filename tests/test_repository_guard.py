import subprocess

from auto_agents.repository_guard import capture_repository_guard, changed_guard_paths


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_exact_guard_catches_change_hidden_by_long_diff(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "a.py").write_text("original\n")
    (tmp_path / "z.py").write_text("original\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "initial")
    (tmp_path / "z.py").write_text("dirty\n" * 20000)
    before = capture_repository_guard(tmp_path)
    (tmp_path / "a.py").write_text("changed\n")
    assert changed_guard_paths(before, capture_repository_guard(tmp_path)) == ["a.py"]


def test_guard_protects_untracked_contents_modes_and_config_but_ignores_run_output(tmp_path):
    _git(tmp_path, "init", "-q")
    file = tmp_path / "new.py"
    file.write_text("first\n")
    config = tmp_path / ".auto-agents/config.json"
    config.parent.mkdir()
    config.write_text("{}")
    before = capture_repository_guard(tmp_path, ignore_run_artifacts=True)
    output = tmp_path / ".auto-agents/runs/run-1/log.txt"
    output.parent.mkdir(parents=True)
    output.write_text("progress")
    assert not changed_guard_paths(before, capture_repository_guard(tmp_path, ignore_run_artifacts=True))
    file.write_text("second\n")
    config.write_text('{"approval":false}')
    changes = changed_guard_paths(before, capture_repository_guard(tmp_path, ignore_run_artifacts=True))
    assert changes == [".auto-agents/config.json", "new.py"]
    before = capture_repository_guard(tmp_path)
    file.chmod(0o755)
    assert changed_guard_paths(before, capture_repository_guard(tmp_path)) == ["new.py"]


def test_diagnostic_copy_keeps_private_checkpoint_refs_and_staged_state(tmp_path):
    from auto_agents.root_cause import RootCauseCoordinator
    source = tmp_path / "source"
    source.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "test@example.com"], ["config", "user.name", "Test"]):
        _git(source, *args)
    file = source / "code.py"
    file.write_text("before\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "initial")
    _git(source, "update-ref", "refs/auto-agents/gate-snapshots/owner", "HEAD")
    file.write_text("staged\n")
    file.chmod(0o755)
    _git(source, "add", "code.py")
    file.write_text("unstaged\n")
    before = capture_repository_guard(source)
    target = tmp_path / "copy"
    RootCauseCoordinator._copy_diagnostic_tree(source, target)
    assert not changed_guard_paths(before, capture_repository_guard(source))
    assert capture_repository_guard(target) == before
    _git(target, "rev-parse", "--verify", "refs/auto-agents/gate-snapshots/owner")


def test_repeated_diagnostic_copy_preserves_deleted_tracked_inputs(tmp_path):
    from auto_agents.root_cause import RootCauseCoordinator
    source = tmp_path / 'frozen'; source.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "test@example.com"], ["config", "user.name", "Test"]):
        _git(source, *args)
    marker = source / '.auto-agents/state/recovery-kernel.json'
    marker.parent.mkdir(parents=True)
    marker.write_text('{"control_root":"/host-only/control"}')
    deleted = source / 'removed.py'; deleted.write_text('OLD = 1\n')
    kept = source / 'kept.py'; kept.write_text('KEEP = 1\n')
    _git(source, 'add', '-A'); _git(source, 'commit', '-qm', 'Historical bindings')
    marker.unlink(); deleted.unlink()
    before = capture_repository_guard(source)
    for name in ('copy', 'repeated-copy'):
        target = tmp_path / name
        RootCauseCoordinator._copy_diagnostic_tree(source, target)
        assert not (target / '.auto-agents/state/recovery-kernel.json').exists()
        assert not (target / 'removed.py').exists()
        assert (target / 'kept.py').read_text() == 'KEEP = 1\n'
        assert capture_repository_guard(target) == before
        source = target
