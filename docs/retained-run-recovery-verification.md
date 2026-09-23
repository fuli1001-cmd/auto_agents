# Retained run recovery verification

An engine repair that clears a blocker has not necessarily restored the task.
For metadata/checkpoint recovery, acceptance now observes the original saved
workflow through implementation entry, fresh managed verification, and review
entry. The offline probe stops before the review provider call. Neither a
`pending` state nor synthetic tests can replace this observation.

The controller supplies the recovery driver independently of the candidate.
Each job generation pins its controller source; a committed operator upgrade
can provide a new driver on the next invocation. Candidate edits do not replace
the verifier that approves them. The retained request, candidate, histories and
consumed repair budgets remain in the same transaction.

The metadata/checkpoint repair:

- Preserves meaningful leading dots in paths, so quoted SQL in `.auto-agents`
  evidence is excluded from application schema detection.
- Shares ignore-aware Git exclusions with gate snapshots, retaining ordinary
  root staging so directory/file/symlink replacements and deletions work.
- Revalidates ownership of the implementation-ready candidate before retiring
  an older unavailable checkpoint. It retains the old checkpoint as evidence.
- Executes the original task's verification selectors without result-cache
  reuse and records command identities, executed test nodes and hashed reports.
- Requires successful execution of the declared selectors and subsequent review
  entry. A no-new-failures gate result does not substitute for passing selectors.

The isolated run replay discovers the selected task's environment from its
frozen run, workflow and verification declarations. It supports direct
`.conda/bin/python` commands as well as `conda run -p`; unrelated stopped sessions
and pending tasks cannot add environment inputs. Conda snapshots exclude
activation credentials and remain bound to their actual contents.

Managed verification uses private container scratch and the existing nested
verification isolation profile, with network disabled and capabilities dropped.
The local tool image includes installed system fonts and media executables;
missing container resources must not induce changes to application behavior.

Failed checks and preflight failures are preserved before any implementation
retry. Incomplete recovery evidence is a controller blocker, not permission to
keep modifying candidate code. Full diagnostics retain the detailed cause,
while the terminal distinguishes missing recovery evidence from exhausted
verified progress.

Validation includes real Git regressions, real managed subprocess receipts,
rejection of stale/cached/cross-workflow evidence, retained-controller upgrades,
and isolated replay of the retained project. The live project is not resumed
by these checks and its state and budget files remain unchanged.
