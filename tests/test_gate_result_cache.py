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
