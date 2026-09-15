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
