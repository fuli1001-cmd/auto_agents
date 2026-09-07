# Managed verification acceleration

Verification now shares a private, per-repository certificate store under
`$XDG_STATE_HOME/auto-agents/verification` (default `~/.local/state/...`).
`AUTO_AGENTS_VERIFICATION_ROOT` selects another operator-owned directory.
Model output is never imported as a certificate. Only the trusted executor
records successful, input-bound executions after cleanup. Identical in-flight
requests share execution; failures, timeouts and incomplete cleanup are not
durable success hits. Artifacts must be present with their recorded digest or
restorable from the private object store; a conflicting user edit is not
overwritten by a cache hit.

## Commands and rollout

Existing `verify --level affected|release` and `--fresh` remain available:

```sh
auto-agents verify --project PROJECT --level focused --test tests/test_example.py
auto-agents verify --project ENGINE --engine --level focused --test tests/test_example.py::test_behavior
auto-agents verify --project ENGINE --engine --level release --fresh
auto-agents verify --project PROJECT --level affected --changed-from BASE --explain
auto-agents performance --project PROJECT --session SESSION
```

Repeat `--test` for multiple repository-local pytest/vitest targets. A test
target is not a shell command. Engine release verification cannot be narrowed
with `--test`. `--explain` does not create diagnostics, initialize a project,
install dependencies or execute tests. Focused verification never publishes a
release attestation, even when a selected test belongs to a release proof.

`execution.acceleration.mode` retains its `off/observe/on` behavior. The new
`execution.acceleration.verification_input_mode` defaults to `observe`: exact
snapshot certificates can be reused, while a proposed cross-snapshot input hit
is checked by actual execution. Set it to `on` after validating the workload;
`off` disables input-based cross-snapshot reuse. Existing audit sampling remains
available. Disagreement revokes the namespace's certificates and narrows reuse.
Changed executor policy, tests, dirty/untracked inputs, dependency environment
or required context invalidate the relevant certificate.

The supervisor provides a separate verification lane so a repair provider can
wait for tests without blocking their execution. Workflow/repair owners mint
scoped contexts; models may request only focused targets in that workspace.
Contexts are fenced by owner identity, workspace inode and repair generation.
Cancellation terminates tracked verification process groups; obsolete owners
cannot approve recovery. Executor code is loaded from a committed, pinned
runtime, not the model's writable candidate.

Old supervisors use the previous diagnostic-only self-test path until they can
upgrade safely. New clients replace an idle old supervisor while holding a
database write transaction that fences new submissions. Live workflows,
repairs, publications and leases prevent that replacement. A new generation
loads the currently committed implementation; dirty developer files are not
promoted as supervisor code.

## Candidate lifecycle

1. Check the trusted upstream, scoped behavior and original recovery boundary.
   Reusing an upstream repair also requires a full engine proof before switching.
2. During editing, use managed focused tests. Raw shell tests remain diagnostic
   and cannot certify acceptance. Different execution environments do not share
   certificates merely because their command text matches.
3. Run deterministic guards and focused proofs first. Invalid or failed required
   boundary/differential proof stops the candidate before expensive review.
4. Freeze the final candidate commit before authoritative verification. Review,
   fixed engine regressions, contract checks and the full plan consume matching
   certificates. Non-final components still defer root integration proofs.
5. A successful candidate full suite needs no old baseline full suite. Failures
   first compare the affected baseline batches; uncertain attribution expands to
   the conservative full baseline comparison. Existing unrelated baseline-failure
   policy is retained, not extended to collection or infrastructure failures.
6. Only passing completed shards resume from checkpoints. Each subscribing
   project still needs its own current recovery replay. Valid engine full-proof
   receipts are shared within the approved job; legacy receipts need revalidation.
7. Integration after remote advancement uses the same full-shard executor.
   A network-only publication retry does not regenerate code or rerun proofs.

## Input completeness, resources and performance

Python observation tracks actual source/file reads rather than treating every
imported `monkeypatch` or file-I/O keyword as a whole-tree dependency. Time,
randomness, process context, unknown native calls, unresolved descriptors and
other unproved inputs decline cross-snapshot reuse. The gate strace adapter can
resolve process cwd, inherited fork context and decoded directory descriptors;
unresolved/shared-cwd namespace changes fail closed. Denied reads never become
privileged host-side content hashes. The original full-tree/context fallback
remains for opaque checks; this is not a promise that every test is portable.

All local verification uses worker CPU/memory leases. Named-resource waits are
admitted before CPU slots; waiting does not consume a test's execution timeout.
Priority aging and per-project rotation prevent small foreground requests or
large/exclusive requests from starving indefinitely. Per-batch writable isolation
and process state are retained. Large cold suites can split at 80 collected
nodes; smaller whole-file shards match the normal focused command and can reuse
its valid certificate. Timings use the shared SQLite timing store.

Local gate startup probes only requested runtime capabilities, rather than
launching Chrome, Docker or FFmpeg checks for unrelated Python tests. LAN
discovery is advisory-cached for 30 seconds in auto mode; authentication and
capability validation still occur before remote work. Required discovery and
explicit diagnostic calls remain fresh. No remote worker is deployed or enabled
by this optimization.

The performance report includes shared request/execution counts, hit/miss
reasons, queue/execution time and slow tests. Trusted pytest receipts include
collection, setup, call and teardown timings. Parent phase spans and parallel
command durations should not be added as if they were sequential wall time.

Development measurements on the pre-change committed suite: 2024 tests and 142
subtests passed in 779.30 seconds in the isolation sandbox. A separately profiled
slow retry-flow case spent about 10 of 13 seconds entering gate executors,
including four LAN discoveries. After capability scoping and test cluster-state
isolation, that case took 2.78 seconds; a routing-only session case took 0.79
seconds. These individual measurements are not a full-iteration speedup claim.

Tests now isolate cluster, worker and verification state, so unrelated workflow
tests cannot inherit a real user's paired workers. Real worker discovery,
authentication, process/timeout, lock, rollback and sandbox tests are retained.
This work does not modify SDGP test code or start its saved business session.

`scripts/benchmark_verification.py --project ENGINE --test TEST` runs one fresh
and two warm checks against a fixed isolated copy, with private benchmark state.
For `tests/test_verification_ledger.py`, the measured sequence was 3.93 seconds,
0.031 seconds and 0.034 seconds; both warm calls reused the same proof and did
not start the tests again. Cross-environment checks still execute when their
identities differ; in particular, credential-free model project tests are not
silently substituted for native-environment gates.
