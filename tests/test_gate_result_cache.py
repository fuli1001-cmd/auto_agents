from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_result_cache import GateResultCache
from auto_agents.gate_execution import _observed_input_manifest
from auto_agents.models import CommandResult


def _cache(tmp_path: Path) -> GateResultCache:
    return GateResultCache(
        tmp_path,
        cache_path=tmp_path / "cache.sqlite3",
        environment_fingerprint="env-1",
        context_fingerprint="context-1",
    )


@pytest.mark.parametrize('helper', ['verification_metadata.py', 'gate_verification.py', 'verification_input_trace.py'])
def test_supervision_policy_changes_invalidate_existing_proofs(tmp_path, monkeypatch, helper):
    from auto_agents import gate_result_cache as module
    policy = tmp_path / 'policy'; policy.mkdir()
    (policy / 'gate_result_cache.py').write_text('cache implementation')
    source = policy / helper; source.write_text('old supervision')
    monkeypatch.setattr(module, '__file__', str(policy / 'gate_result_cache.py'))
    monkeypatch.setattr(module, '_POLICY_CACHE', (None, ''))
    cache = _cache(tmp_path)
    identity = dict(source_fingerprint='source', cache_scope='source', result_cache_scope='candidate',
                    metadata_signature='owned')
    cache.record('check', CommandResult('check', True, 0), **identity)
    assert cache.lookup('check', **identity) is not None
    source.write_text('changed supervision behavior')
    assert cache.lookup('check', **identity) is None


def test_live_owner_change_invalidates_proof_with_unchanged_candidate_source(tmp_path, monkeypatch):
    from auto_agents import verification_input_trace
    owner = {'metadata': 1, 'trace': 1, 'owner': 'first-outer-runtime'}
    monkeypatch.setattr(verification_input_trace, 'owner_identity', lambda: dict(owner))
    cache = _cache(tmp_path)
    identity = dict(source_fingerprint='unchanged', cache_scope='source', result_cache_scope='candidate',
                    metadata_signature='owned')
    cache.record('check', CommandResult('check', True, 0), **identity)
    assert cache.lookup('check', **identity) is not None
    owner['owner'] = 'second-outer-runtime'
    assert cache.lookup('check', **identity) is None


@pytest.mark.parametrize('network', [False, True])
def test_cached_success_preserves_network_dependency_metadata(tmp_path, network):
    cache = _cache(tmp_path)
    metadata = dict(source_fingerprint='source', cache_scope='run_context', result_cache_scope='candidate', metadata_signature='policy')
    cache.record('check', CommandResult('check', True, 0, input_trace_complete=True, network_observed=network), **metadata)
    result = cache.lookup('check', **metadata)
    assert result is not None and result.network_observed is network


def test_candidate_cache_never_reuses_failed_proof(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    failed = CommandResult(command="check", ok=False, returncode=1)
    cache.record(
        "check",
        failed,
        source_fingerprint="source-1",
        cache_scope="run_context",
        result_cache_scope="candidate",
        metadata_signature="metadata-1",
    )
    assert cache.lookup(
        "check",
        source_fingerprint="source-1",
        cache_scope="run_context",
        result_cache_scope="candidate",
        metadata_signature="metadata-1",
    ) is None

    assert cache.lookup(
        "check",
        source_fingerprint="source-2",
        cache_scope="run_context",
        result_cache_scope="candidate",
        metadata_signature="metadata-1",
    ) is None

    cache.record(
        "check",
        CommandResult(command="check", ok=True, returncode=0),
        source_fingerprint="source-1",
        cache_scope="run_context",
        result_cache_scope="candidate",
        metadata_signature="metadata-1",
    )
    hit = cache.lookup(
        "check",
        source_fingerprint="source-1",
        cache_scope="run_context",
        result_cache_scope="candidate",
        metadata_signature="metadata-1",
    )
    assert hit is not None
    assert hit.ok and hit.cached
    assert hit.backend == "proof-certificate-candidate"
    assert (
        cache.lookup(
            "check",
            source_fingerprint="source-2",
            cache_scope="run_context",
            result_cache_scope="candidate",
            metadata_signature="metadata-1",
        )
        is None
    )


def test_observed_input_cache_invalidates_when_an_input_changes(tmp_path: Path) -> None:
    source = tmp_path / "src.txt"
    source.write_text("one\n", encoding="utf-8")
    cache = _cache(tmp_path)

    import hashlib

    digest = "file:" + hashlib.sha256(source.read_bytes()).hexdigest()
    cache.record(
        "check",
        CommandResult(
            command="check",
            ok=True,
            returncode=0,
            observed_inputs={"src.txt": digest},
            input_trace_complete=True,
        ),
        source_fingerprint="source-1",
        cache_scope="source",
        result_cache_scope="observed_inputs",
        metadata_signature="metadata-1",
    )
    hit = cache.lookup(
        "check",
        source_fingerprint="source-2",
        cache_scope="source",
        result_cache_scope="observed_inputs",
        metadata_signature="metadata-1",
    )
    assert hit is not None
    assert hit.backend == "result-cache-observed-inputs"

    source.write_text("two\n", encoding="utf-8")
    assert (
        cache.lookup(
            "check",
            source_fingerprint="source-3",
            cache_scope="source",
            result_cache_scope="observed_inputs",
            metadata_signature="metadata-1",
        )
        is None
    )


def test_auto_cache_reuses_complete_inputs_and_tracks_negative_lookups(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src.txt"
    source.write_text("one\n", encoding="utf-8")
    cache = _cache(tmp_path)

    import hashlib

    digest = "file:" + hashlib.sha256(source.read_bytes()).hexdigest()
    cache.record(
        "check",
        CommandResult(
            command="check",
            ok=True,
            returncode=0,
            observed_inputs={"src.txt": digest, "!optional.txt": "missing"},
            input_trace_complete=True,
        ),
        source_fingerprint="source-1",
        cache_scope="source",
        result_cache_scope="auto",
        metadata_signature="metadata-1",
    )

    hit = cache.lookup(
        "check",
        source_fingerprint="source-2",
        cache_scope="source",
        result_cache_scope="auto",
        metadata_signature="metadata-1",
    )
    assert hit is not None
    assert hit.backend == "result-cache-observed-inputs"

    (tmp_path / "optional.txt").write_text("now present\n", encoding="utf-8")
    assert cache.lookup(
        "check",
        source_fingerprint="source-3",
        cache_scope="source",
        result_cache_scope="auto",
        metadata_signature="metadata-1",
    ) is None


def test_observed_input_cache_invalidates_when_a_symlink_is_retargeted(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two\n", encoding="utf-8")
    source = tmp_path / "active.txt"
    source.symlink_to("one.txt")
    trace = tmp_path / "trace.log"
    trace.write_text('openat(AT_FDCWD, "active.txt", O_RDONLY) = 3\n', encoding="utf-8")
    manifest, network = _observed_input_manifest(trace, tmp_path, {})
    cache = _cache(tmp_path)
    metadata = dict(cache_scope="source", result_cache_scope="auto", metadata_signature="metadata-1")
    cache.record(
        "check", CommandResult(
            command="check", ok=True, returncode=0, observed_inputs=manifest,
            input_trace_complete=True, network_observed=network,
        ), source_fingerprint="source-1", **metadata,
    )
    assert cache.lookup("check", source_fingerprint="source-1", **metadata) is not None

    source.unlink()
    source.symlink_to("two.txt")

    assert cache.lookup("check", source_fingerprint="source-3", **metadata) is None


@pytest.mark.parametrize("operation", [
    'chdir("subdirectory") = 0',
    'fchdir(3) = 0',
    'openat(3, "input.txt", O_RDONLY) = 4',
    'openat(3</tmp>, "input.txt", O_RDONLY) = 4',
    'newfstatat(3, "", {st_mode=S_IFREG}, AT_EMPTY_PATH) = 0',
    'newfstatat(3</tmp>, "input.txt", {st_mode=S_IFREG}, 0) = 0',
    'newfstatat(3</tmp/input.txt (deleted)>, "", {st_mode=S_IFREG}, AT_EMPTY_PATH) = 0',
    'newfstatat(3<socket:[123]>, "", {st_mode=S_IFSOCK}, AT_EMPTY_PATH) = 0',
])
def test_unresolved_working_directory_or_dirfd_cannot_certify_inputs(tmp_path, operation):
    (tmp_path / "input.txt").write_text("root input\n", encoding="utf-8")
    trace = tmp_path / "trace.log"
    trace.write_text(
        operation + '\nopenat(AT_FDCWD, "input.txt", O_RDONLY) = 5\n',
        encoding="utf-8",
    )

    manifest, network = _observed_input_manifest(trace, tmp_path, {})

    assert manifest == {}
    assert not network


@pytest.mark.parametrize("syscall,arguments", [
    ("newfstatat", "{st_mode=S_IFREG}, AT_EMPTY_PATH"),
    ("fstatat64", "{st_mode=S_IFREG}, AT_EMPTY_PATH"),
    ("statx", "AT_EMPTY_PATH, STATX_BASIC_STATS, {stx_mode=S_IFREG}"),
])
def test_descriptor_stat_records_decoded_target_without_prior_open(tmp_path, syscall, arguments):
    source = tmp_path / "input.txt"
    source.write_text("one\n", encoding="utf-8")
    trace = tmp_path / "trace.log"
    # An inherited fd has no open in this trace: its decoded target is still
    # required in the certificate. Pipe metadata is not a source dependency.
    trace.write_text(
        f'42 {syscall}(3<{source}>, "", {arguments}) = 0\n'
        '42 newfstatat(1<pipe:[123]>, "", {st_mode=S_IFIFO}, AT_EMPTY_PATH) = 0\n',
        encoding="utf-8",
    )
    manifest, network = _observed_input_manifest(trace, tmp_path, {})
    assert set(manifest) == {"input.txt"}
    assert manifest["input.txt"].startswith("file:")
    assert not network


def test_descriptor_stat_does_not_certify_symlink_dependencies(tmp_path):
    (tmp_path / "input.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to("input.txt")
    trace = tmp_path / "trace.log"
    trace.write_text(
        'openat(AT_FDCWD, "link.txt", O_RDONLY) = 3\n'
        f'newfstatat(3<{tmp_path / "input.txt"}>, "", {{st_mode=S_IFREG}}, AT_EMPTY_PATH) = 0\n',
        encoding="utf-8",
    )
    assert _observed_input_manifest(trace, tmp_path, {}) == ({}, False)


@pytest.mark.parametrize('scope', ['observed_inputs', 'auto'])
@pytest.mark.parametrize('fault', ['replacement', 'overwrite', 'missing_footer', 'owner_mismatch', 'replayed_job'])
def test_gate_trace_evidence_requires_custody(tmp_path, scope, fault):
    from auto_agents.verification_input_trace import TraceCustody
    from test_verification_metadata import execute
    execute(tmp_path, f'''
import json, os, subprocess, sys
from pathlib import Path
from auto_agents.verification_input_trace import TraceCustody
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.gates import GateCommandMetadata
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_gate_execution import _project, _config
from pytest import MonkeyPatch
root=Path.cwd(); project=_project(root)
config=_config(root); config.verification_policy_version=3
command='cat tracked.txt'
original=TraceCustody.consume
fault={fault!r}
def consume(self, **kwargs):
    path=Path(self.payload['trace']['path'])
    if fault=='replacement':
        data=path.read_bytes(); path.unlink(); path.write_bytes(data)
    elif fault=='overwrite':
        path.write_text('{{"complete":true}}\\n')
    elif fault=='missing_footer':
        path.write_text('\\n'.join(path.read_text().splitlines()[:-1])+'\\n')
    elif fault=='owner_mismatch':
        receipt=Path(self.payload['receipt']['path'])
        data=json.loads(receipt.read_text()); data['owner']['owner']='foreign'
        receipt.write_text(json.dumps(data))
    else:
        receipt=Path(self.payload['receipt']['path'])
        data=json.loads(receipt.read_text()); data['job']='previous-job'
        receipt.write_text(json.dumps(data))
    result=original(self, **kwargs)
    assert result[0] is None and result[1], result
    assert original(self, **kwargs)[0] is None
    return result
with MonkeyPatch.context() as patch:
    patch.setattr(TraceCustody,'consume',consume)
    with LocalGatePlanExecutor(project,config,{{command:GateCommandMetadata(cache_scope='source',result_cache_scope={scope!r})}}) as executor:
        result=executor.run(command,timeout_seconds=20,adaptive_timeout_enabled=False,idle_timeout_seconds=20)
assert result.ok and not result.input_trace_complete and not result.observed_inputs, result
assert result.input_trace_reason
''')
