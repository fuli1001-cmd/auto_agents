from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_result_cache import GateResultCache
from auto_agents.models import CommandResult


def _cache(tmp_path: Path) -> GateResultCache:
    return GateResultCache(
        tmp_path,
        cache_path=tmp_path / "cache.sqlite3",
        environment_fingerprint="env-1",
        context_fingerprint="context-1",
    )


def test_candidate_cache_reuses_stable_failure_only_for_exact_candidate(tmp_path: Path) -> None:
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
    failed_hit = cache.lookup(
        "check",
        source_fingerprint="source-1",
        cache_scope="run_context",
        result_cache_scope="candidate",
        metadata_signature="metadata-1",
    )
    assert failed_hit is not None
    assert not failed_hit.ok and failed_hit.cached
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
