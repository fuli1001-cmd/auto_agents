# Control v2 test migration audit

The old driver implementations were removed; their private-method tests cannot
remain an executable second implementation. This audit maps the retained public
constraints to behavior tests. It does not claim a one-to-one equivalence of
old and new test cases. The retirement inventory contains 1280 private-method
test functions across 73 modules, including tests of deleted recovery branches.
Original test source remains in Git history. No rejected safety test was removed
as a workaround to approval review.

| Retained constraint | Current evidence |
| --- | --- |
| One controller for run/fix/collab | test_control_engine, test_control_cli, test_control_behaviors |
| Exact original goal, checks, source, scope and budgets on resume | test_control_store, test_control_faults, test_control_boundaries, test_session_recovery |
| Receipt consumption / contract binding is atomic; UNKNOWN is not resent | test_control_store, test_control_api, test_control_behaviors |
| Actual executed behavior; no empty/skipped/masked proof | test_control_boundaries, test_control_behaviors, existing gate and pytest execution tests |
| Production input-trace owner, evidence tampering and foreign global plan | original named public resume tests in test_verification_input_trace, rewritten in place |
| Kernel metadata denial, private writes, owner death and short runtime allocation | test_verification_metadata, test_verification_input_trace, test_verification_supervisor_checks |
| Original shared files/index and exact clean delivery | test_control_boundaries, test_control_behaviors, original test_release_jobs attestation test |
| Crash while integrating completed child work | test_control_parallel (after apply and after commit) |
| Mandatory whole-package prototype approval and preserved decisions | test_control_cli, test_control_domains, existing frontend design validators |
| Secret references and operator answer continuation | test_control_behaviors, test_control_operator, existing operator-input tests |
| Persistence configuration, explicit changes, immutable migrations, no user DB reset | test_control_operator, existing persistence domain tests |
| Required provider reference contracts cannot be skipped | test_control_reference_contracts, existing provider contract and freshness tests |
| Public fault summary and complete saved diagnostics | original test_terminal_failure_output, rewritten in place |
| One-way migration and missing historical proof refusal | test_control_migration and the SDGP rehearsal |
| Settled cleanup, symlink/inode checks, live cache and unknown lease retention | test_control_cleanup, test_control_boundaries, existing storage/artifact tests |
| Independent optional watcher and credential-free offline recovery | supervisor/tests, test_control_docker_recovery (explicit local image) |

Pure legacy DTO, Git, receipt, archive and migration validators remain when needed
for read-only import. Default CLI execution cannot call the retired
Orchestrator/Session/Workflow executors. Tests of former implicit rerouting,
private handoff normalization and old parallel fallback branches were retired
with those branches. Compatibility tests exercise the public controller instead
of recreating private methods to keep old mocks alive.
