from auto_agents.repair_v2.image import conda_runtime_markers


def test_selected_conda_prefix_remains_discoverable_without_history_contents(tmp_path):
    base = tmp_path / 'conda/envs/selected'
    (base / 'conda-meta').mkdir(parents=True)
    history = base / 'conda-meta/history'
    history.write_text('private package URL and historical command arguments\n')
    venv = tmp_path / 'private-venv'; venv.mkdir()
    markers = conda_runtime_markers({'base': str(base), 'prefix': str(venv)})
    assert list(markers) == [str(history).lstrip('/')]
    assert 'private package' not in next(iter(markers.values()))
    assert history.read_text() == 'private package URL and historical command arguments\n'
    assert conda_runtime_markers({'base': str(venv), 'prefix': str(venv)}) == {}
