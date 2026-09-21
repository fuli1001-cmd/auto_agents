from types import SimpleNamespace

from auto_agents import session_verification as verification
from auto_agents.verification_selection import StaticDependencyIndex
from test_session_verification_ownership import git


def test_explicit_impact_does_not_parse_unneeded_dependencies(tmp_path, monkeypatch):
    def unexpected(*args):
        raise AssertionError('An explicit match must not build the import graph')
    monkeypatch.setattr('auto_agents.verification_selection.StaticDependencyIndex', unexpected)
    session = SimpleNamespace(project_root=tmp_path)
    state = SimpleNamespace(candidate_paths={'value.py': 'changed'}, lineage_changed_paths=[], verification_binding={})
    assert verification._step_affected(session, state, {'targets': ['tests/test_value.py'], 'impact_paths': ['*.py']})


def test_one_selection_shares_parsing_but_next_selection_observes_edits(tmp_path, monkeypatch):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_value.py').write_text('import value\n')
    (tmp_path / 'value.py').write_text('VALUE = 1\n')
    (tmp_path / 'unrelated.py').write_text('VALUE = 2\n')
    git(tmp_path, 'add', '.')
    indexes, parsed = [], []
    class ObservedIndex(StaticDependencyIndex):
        def __init__(self, *args):
            indexes.append(1)
            super().__init__(*args)
        def _python_dependencies(self, path, text):
            parsed.append(path)
            return super()._python_dependencies(path, text)
    monkeypatch.setattr('auto_agents.verification_selection.StaticDependencyIndex', ObservedIndex)
    session = SimpleNamespace(project_root=tmp_path)
    state = SimpleNamespace(candidate_paths={'value.py': 'changed'}, lineage_changed_paths=[])
    def selection(session, state):
        return [verification._step_affected(session, state, {'targets': ['test_value.py']}) for _ in range(30)]
    monkeypatch.setattr(verification, '_session_gates', selection)
    assert all(verification.session_gates(session, state))
    assert len(indexes) == 1 and parsed.count('test_value.py') == 1
    assert not hasattr(session, '_proof_selection_pass')
    (tmp_path / 'test_value.py').write_text('import unrelated\n')
    assert not any(verification.session_gates(session, state))
    assert len(indexes) == 2 and parsed.count('test_value.py') == 2


def test_historical_selector_parsing_is_shared_only_for_the_bound_revision(tmp_path, monkeypatch):
    git(tmp_path, 'init', '-q')
    path = tmp_path / 'test_value.py'
    path.write_text('def test_old():\n    assert True\n')
    git(tmp_path, 'add', '.'); git(tmp_path, 'commit', '-qm', 'Original proof')
    session = SimpleNamespace(project_root=tmp_path)
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[],
                            verification_binding={'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip()})
    target = 'test_value.py::test_future'
    parsed, parse = [], verification.ast.parse
    def observe(*args, **kwargs):
        parsed.append(1)
        return parse(*args, **kwargs)
    monkeypatch.setattr(verification.ast, 'parse', observe)
    def selection(session, state):
        return [verification._future_foreign_step(session, state, {'targets': [target]}, {target}) for _ in range(30)]
    monkeypatch.setattr(verification, '_session_gates', selection)
    assert all(verification.session_gates(session, state))
    assert len(parsed) == 1
    path.write_text('def test_future():\n    assert True\n')
    git(tmp_path, 'commit', '-qam', 'Proof now exists')
    state.verification_binding['contract_revision'] = git(tmp_path, 'rev-parse', 'HEAD').strip()
    assert not any(verification.session_gates(session, state))
    assert len(parsed) == 2
