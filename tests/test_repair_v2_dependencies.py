from auto_agents.repair_v2.dependencies import cache_key, witness
from auto_agents.repair_v2.types import ValidationUnit
from auto_agents.repair_v2.store import digest


def test_closed_unit_reuses_only_when_all_inputs_match(tmp_path):
    (tmp_path / 'tests').mkdir(); (tmp_path / 'src').mkdir()
    (tmp_path / 'src/pure.py').write_text('def add(a, b): return a + b\n')
    (tmp_path / 'tests/test_pure.py').write_text('from pure import add\ndef test_add(): assert add(1, 2) == 3\n')
    unit = ValidationUnit('pure', 'python -m pytest -q tests/test_pure.py')
    first = witness(tmp_path, unit, 'runtime')
    assert first['complete']
    assert cache_key('one', unit, first) == cache_key('two', unit, first)
    (tmp_path / 'src/pure.py').write_text('def add(a, b): return a - b\n')
    assert cache_key('two', unit, witness(tmp_path, unit, 'runtime')) != cache_key('one', unit, first)


def test_fixture_file_reads_are_opaque_even_without_path_import(tmp_path):
    (tmp_path / 'test_data.py').write_text('def test_data(tmp_path): assert tmp_path.read_text() == "data"\n')
    unit = ValidationUnit('data', 'python -m pytest -q test_data.py')
    inputs = witness(tmp_path, unit, 'runtime')
    assert not inputs['complete']
    assert cache_key('one', unit, inputs) != cache_key('two', unit, inputs)


def test_description_and_group_labels_are_not_verification_inputs(tmp_path):
    (tmp_path / 'test_pure.py').write_text('def test_value(): assert 1 == 1\n')
    unit = ValidationUnit('descriptive label', 'python -m pytest -q test_pure.py')
    renamed = ValidationUnit('a new description', unit.command)
    assert witness(tmp_path, unit, 'runtime') == witness(tmp_path, renamed, 'runtime')


def test_package_initializers_and_from_imported_submodules_are_bound(tmp_path):
    package = tmp_path / 'src/package'; package.mkdir(parents=True)
    (package / '__init__.py').write_text('value = 1\n')
    (package / 'child.py').write_text('value = 2\n')
    (tmp_path / 'test_package.py').write_text('from package import child\ndef test_value(): assert child.value == 2\n')
    unit = ValidationUnit('package', 'python -m pytest -q test_package.py')
    first = witness(tmp_path, unit, 'runtime')
    assert 'src/package/__init__.py' in first['files'] and 'src/package/child.py' in first['files']
    (package / 'child.py').write_text('value = 3\n')
    assert cache_key('same', unit, witness(tmp_path, unit, 'runtime')) != cache_key('same', unit, first)


def test_import_shadowing_and_startup_customization_invalidate_cache(tmp_path):
    (tmp_path / 'test_math.py').write_text('from math import pi\ndef test_pi(): assert pi > 3\n')
    unit = ValidationUnit('math', 'python -m pytest -q test_math.py')
    first = witness(tmp_path, unit, 'runtime')
    (tmp_path / 'math.py').write_text('pi = 0\n')
    assert cache_key('one', unit, first) != cache_key('two', unit, witness(tmp_path, unit, 'runtime'))
    (tmp_path / 'sitecustomize.py').write_text('value = 1\n')
    current = witness(tmp_path, unit, 'runtime')
    (tmp_path / 'sitecustomize.py').write_text('value = 2\n')
    assert witness(tmp_path, unit, 'runtime') != current


def test_shell_operators_are_not_reinterpreted_as_pytest_arguments():
    from auto_agents.repair_v2.dependencies import pytest_parts
    assert pytest_parts('python -m pytest test_x.py;echo success') is None
    assert pytest_parts('/some/other/python -m pytest test_x.py') is None


def test_literal_shell_characters_in_generated_node_ids_remain_pytest_arguments():
    import shlex
    from auto_agents.repair_v2.dependencies import pytest_parts
    node = 'tests/test_shell.py::test_escape[$(touch bad);*]'
    command = shlex.join(['python', '-m', 'pytest', '-q', node])
    assert pytest_parts(command) == ['-q', node]
    assert pytest_parts('python -m pytest "$TEST_PATH"') is None
