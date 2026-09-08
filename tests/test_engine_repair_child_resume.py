"""Stable repair-contract selectors backed by the public recovery regressions."""

from test_engine_child_recovery import (
    test_verified_engine_repair_resumes_bound_existing_child_once as
    test_verified_engine_repair_resumes_existing_child_with_retained_constraints,
    test_resumed_child_delivers_snapshot_matching_retained_provider_semantics as
    test_resumed_child_delivers_semantically_valid_provider_snapshots,
    test_snapshot_repair_rejects_stale_or_forged_provenance as
    test_snapshot_delivery_rejects_invalid_source_or_unsupported_capability,
)
