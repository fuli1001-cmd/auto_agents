# Self-repair system audit — 2026-09-14

This audit covers repair planning and scope recovery, component completion,
verification scheduling and aggregation, process cancellation and cleanup,
runtime/source selection, and the recovery control plane. The SDGP project and
its saved repair/session state were inspected but not modified or resumed.

Final production validation passed **all 3,185 tests in 163 fresh shards**, in
**1,362.21 seconds (22m42s)**. Every changed file matched the frozen validation
copy before adding this result to the report. No shard reused a cached success.

## Findings and changes

| Defect | Consequence | Correction |
| --- | --- | --- |
| Supervisor observations compared selected-runtime hashes with candidate imports | Refreshing the same launcher could never satisfy cross-version acceptance | Resolve the producing source through the live tracer; retain source/owner checks and explicit provenance |
| Capability identities included disposable PIDs | Equivalent probes invalidated approvals and could start another planning episode after restart | Bind stable capability semantics while preserving PID relationships, trace-owner source identities, source hashes, protocols and failures; retain raw audit observations |
| Command deduplication crossed preparation boundaries | A test required both before and after preparation could run only once | Deduplicate only within contiguous supported pytest sequences; preserve preparation and execution occurrence mappings in quick and expanded schedules |
| Component completion compared acceptance command sets | Dropped repeated commands could still match a completion inventory | Check multiplicity, and preserve ordering when preparation commands occur |
| A failed process with exit code zero could produce successful verification | Infrastructure errors, cancellations or an explicit failed result could be lost | Normalize non-success process states before recording evidence and stop subsequent dispatch |
| Integration/full-suite aggregation dropped payloads | Failure evidence, executed tests, proof references, timings and incomplete cleanup disappeared at stage boundaries | Carry execution evidence and fatal process state through aggregation; require the full distinct shard inventory |
| Full-suite checkpoint reuse ignored `fresh` and accepted malformed/incomplete records | Requested fresh checks could be skipped or cached data could crash/incorrectly satisfy verification | Bypass both checkpoint and proof reuse for fresh runs; admit only clean success and treat malformed records as misses |
| Full-suite executor threads lost context variables | Cancellation did not reach actual child execution or stop queued work | Copy execution context into threads, stop dispatch on interruption, and drain running work |
| Shard directories were deleted after incomplete child cleanup | Still-running children could lose their source/evidence; later work could proceed with incomplete custody | Retain the checkout and propagate fatal cleanup to the candidate owner, including reviewer exceptions |
| Dependencies could change midway through a full suite | Results from different environments could be combined or stored under the wrong environment | Suppress mixed-environment checkpoint/proof writes and retry the same source once; repeated changes remain an explicit revalidation blocker |
| A malformed memory record could raise an unexpected exception | Damaged optional evidence crashed planning instead of requiring revalidation | Reject non-object records as invalid receipts |
| Lightweight verification callers unconditionally recorded durable experiment history | Scheduling-only calls without an experiment store attempted filesystem/Git access | Keep in-memory timing hints and require a store before writing durable records |
| Historical contract lookup raced with temporary/job directory cleanup | A vanished sibling directory produced `FileNotFoundError`, falsely reported as runtime incompatibility | Read sibling receipts defensively without following directory links; give native runtime probes a private job-cache directory |
| The metadata supervisor rejected private filesystem sockets and FIFOs | A nested repair controller failed to chmod its private socket and never became ready | Permit pinned private IPC inodes under every ancestor policy; retain shared-path, symlink-escape, hardlink and device restrictions |
| Nested gates allocated short runtime paths directly under `/tmp` | Inherited Landlock restrictions rejected trace/cache allocation before tests could run | Reserve a short private runtime parent before confinement, propagate it across nested boundaries, and reuse the already allocated per-job directory |
| Verification worker handoffs dropped boundary bookkeeping | A worker lost the inherited owner's diagnostics or writable temporary location | Forward private TMPDIR, runtime reservation and source-bound supervisor diagnostics through trusted process handoffs |
| Clean nested verification dropped the selected engine's import path | Deep child processes loaded an older installed package and could not import current supervisor helpers | Keep candidate and explicit Python paths first, with the selected runtime's package root as a fallback; retained real-project environments stay unchanged |
| Engine verification left `/dev/shm` read-only | Native multiprocessing semaphores failed before worker capacity checks ran | Mount private shared memory in the outer engine verification namespace and propagate that private boundary to nested children |
| Python filesystem wrappers lost `os.supports_*` membership | `shutil.copy2(..., follow_symlinks=False)` treated supported operations as unavailable and failed while copying an unborn repository | Preserve and restore the native capability sets alongside the observer wrappers |
| Source-conflict test selected the adjacent JSON receipt with a directory glob | The test ran Git against a file, masking the actual retained-merge behavior | Select the retained directory explicitly; keep the real conflict and resume assertions |
| A restart test mocked the shared `time.sleep` function | Subprocess wait polling under load appeared as extra restart-delay calls | Scope the clock mock to the restart module and keep the exact quiescence-delay assertion |

These changes preserve independent review, exact acceptance obligations, sandbox
boundaries and final integration. Stable diagnostic identities do not turn
selected-runtime observations into proof of changes to candidate supervision.

## Efficiency measurements

The manifest reader is a fixed trusted program which rechecks files inside the
verification namespace. It no longer starts the four fresh-owner lifecycle
diagnostics on every input-manifest lookup. Actual acceptance launches retain
those diagnostics and all filesystem/metadata restrictions.

Five local runs per variant, using the real production wrapper and the same
manifest, produced these timings:

| Manifest validation | Median | Samples, seconds |
| --- | ---: | --- |
| With lifecycle diagnostics | 0.841 s | 0.862, 0.863, 0.834, 0.837, 0.841 |
| Fixed-reader path | 0.370 s | 0.373, 0.370, 0.366, 0.363, 0.377 |

The median reduction was **56.1% for this operation**. Both variants rejected a
changed input. This is not an end-to-end repair speedup estimate.

A separate regression reloads an approved plan with fresh PID observations
and verifies that it makes no additional planner, reviewer or diagnostic-probe
calls. Two real native launcher reports also produce the same semantic binding
despite different process identities. Capability changes and broken identity
relationships still invalidate that binding.

Repeated native runtime admission reproduced the directory-discovery race once
in twelve runs. The failure named a disappearing `aav-*` directory during
`acceptance_planning`; it was not a missing runtime capability. Regression tests
now simulate concurrent directory removal and reject symlinked history. All 95
runtime, contract, source-sync and explicit-engine-request checks passed after
the correction.

## Validation

`tests/test_repair_system_audit.py` exercises failures across stage boundaries,
including real preparation/test execution, retained approvals, malformed evidence,
fresh checkpoints, cancellation context, incomplete cleanup and changing
dependency environments. Existing planning, component completion, restart,
control-plane and verification suites supply the surrounding integration checks.
The same 20 regression cases failed against the audit's initial code snapshot
and pass with the corrections; six additional cases check that semantic
capability bindings still reject changed behavior and broken owner relationships.

Validation uses disposable source copies and private verification/worker/storage
state. No model calls, remote publication or business-session recovery are part
of these offline checks. The original stopped job remains available for an
operator-requested continuation after the corrected engine version is selected.

The final run uses the original repair job's validated, already prepared
verification Python and pinned Vitest toolchain. It covers private IPC, nested
gates, worker handoff, symlink copying, retained-session recovery and real Vitest
execution. The shared-memory regression checks host isolation and semaphore
operation across a nested metadata boundary. The operator's Conda environment
is not changed.
