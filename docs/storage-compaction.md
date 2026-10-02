# Storage compaction

The October 2 installation audit measured about 50 GiB in repair control,
including 28 GiB of legacy job copies, an 8.9 GB kernel database and 1.3 GiB of
kernel objects. `/tmp` used another 21 GiB. The legacy copies included 131
release databases (7.2 GB), copied baseline/audit caches and about 2.8 GB of
immutable checkpoint blobs. The D: drive had only 16 GiB free while Linux
reported 95 GiB free; the Ubuntu VHDX occupied about 207 GiB.

## Journal format 2

New installations retain one current JSON snapshot, losslessly compressed event
envelopes where beneficial, and a SHA-256 projection digest for each event.
Replay still checks every event identity, revision, chain checksum and expected
projection. A duplicate event returns the original revision, including after
an obsolete attachment expires; generation and draining fences still apply.

New verification observations explicitly use `compact_version=2`: retain every
check identity/status and all baseline/progress obligations, but bound reason
and per-check display text. Full details are separate referenced objects.
Version 1 observations retain their original replay behavior.

`recovery.journal_maintenance.compact_history()` converts legacy journals only
after a storage-capable runtime has been adopted and all effects/consumers have
drained. It stages compressed envelopes and hashes of the original full
projections, replays them, and records a bound `history_compacted` event if
display text can shrink. Budget, operation, continuation and goal identity
fields are checked before committing. Rows are replaced in a SQLite transaction
without replacing the database inode; `VACUUM` returns the unused pages. A
legacy runtime cannot read format 2 and cannot be used for rollback without a
compatible reader and a new replay proof.

## Other generated data

Release queues clear diagnostic payloads when candidates become superseded.
Current failure payloads round-trip through lossless compression, preserving
pending decisions, counts, IDs and proof references. Stopped, positively owned
diagnostic/subscriber copies receive the same database compaction; active jobs,
container mounts, pins and leases remain protected.

Hash-named checkpoint blobs are immutable and verified before sharing an inode.
New diagnostic copies link those blobs instead of copying their contents;
maintenance deduplicates existing stopped copies. Ordinary product files are
never hard-linked by this rule. Snapshot production omits rebuildable baseline
and audit caches and Git-confirmed generated temporary/build directories.

Kernel object collection marks current projections, command/contract proofs,
upgrade receipts, legacy control records and incomplete checkpoints before
sweeping. Recent unpublished objects are protected by a grace period. Expired
successful upgrade logs may be removed while the current upgrade logs remain.
Unknown or busy reference graphs defer collection. Workflow-owned evidence
uses its specific owner's completion state rather than unrelated open sessions.

Maintenance runs at most every ten minutes. Idle verifier images default to
one retained image and a 2 GiB idle budget; active consumers and explicit pins
override collection. Cache compaction uses `VACUUM` for large free lists,
including older databases without incremental auto-vacuum. Windows backing
drive pressure participates in cleanup decisions.
Each diagnostic stdout/stderr capture is bounded to an 8 MiB redacted tail with
an explicit truncation marker. This bounds the diagnostic copy independently
of the provider/runner's structured response.

## Windows physical space

Deleting Linux files and shrinking SQLite reclaim Linux blocks. A non-sparse
WSL VHDX can retain those blocks on Windows. Record Linux and Windows free space
separately. Reclaiming the physical VHDX requires supported disk operations;
offline compaction must run after stopping its WSL/Docker users, not against a
writable mounted virtual disk. This is separate from deleting application data.
