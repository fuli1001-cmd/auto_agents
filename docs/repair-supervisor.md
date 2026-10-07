# Optional external maintenance

`auto-agents` owns business execution. `auto-agents-watch` is an independently
installed package in `supervisor/`. It starts a business process and uses JSON
commands and observations; it never imports the business engine. Neither package
requires the other's database to start.

## Installation and invocation

```bash
python -m pip install .
python -m pip install ./supervisor

auto-agents collab --project /path/to/project --provider existing-alias --session SESSION
# Standalone execution, even when the watcher is installed:
auto-agents collab --project /path/to/project --session SESSION --no-supervisor
# Explicit supervision:
auto-agents-watch run --engine /path/to/auto_agents -- auto-agents collab --project /path/to/project --session SESSION
```

Ordinary business commands automatically start an installed watcher when
`execution.supervision.mode` is `auto`. If it is absent, they run independently.
Set the mode to `off` to disable automatic startup. Engine maintenance requires a
clean engine Git branch and Docker. Business execution has no Docker requirement.

```json
{
  "efforts": {"self_repair": "deep", "self_repair_review": "max"},
  "execution": {
    "supervision": {
      "mode": "auto",
      "no_progress_limit": 2,
      "loop_repeat_limit": 3,
      "heartbeat_timeout_seconds": 120,
      "max_model_calls": null,
      "max_duration_seconds": null,
      "publish": true
    }
  }
}
```

The repair writer and independent reviewer use the invocation's `--provider`, or
`active_provider`. They use distinct native CLI sessions and the existing effort
profile mappings. No repair-provider setting is added. The initial native CLI
adapters cover Codex, Claude Code and Copilot CLI. Other provider kinds stop with
an explicit unsupported-adapter message instead of substituting another account.

The tool image is built from the selected installed CLI and engine dependencies.
`execution.supervision.base_image` selects its Node/Debian base (default
`node:22-bookworm-slim`). `AUTO_AGENTS_WATCH_BASE_IMAGE` overrides that value.
A compatible locally pulled mirror can be used, for example:

```json
{"execution":{"supervision":{"base_image":"registry.cn-hangzhou.aliyuncs.com/fuli1001/node:lts-slim"}}}
```

The locally inspected image identity is part of the tool-image cache key;
updating a mutable base tag cannot reuse a tool image built from the old base.
Builds use `--pull=false`, and failures include the build-log path and final
diagnostic output.
An operator can supply a prepared image with `AUTO_AGENTS_WATCH_IMAGE`; it must
contain `python`, Git, pytest, engine dependencies and that selected CLI. Writer
and reviewer get private copies of the selected account configuration. Offline
verification has no account credentials or network access.

## One maintenance loop

The watcher has six states: RUNNING, REPAIRING, VERIFYING, RESTARTING, DONE and
STOPPED. A project file lock prevents two owners from executing the business
workflow simultaneously. Child processes inherit the lock; there is no descriptor
transfer service, controller daemon, registration protocol or recursive repair.

An engine exception produces a durable checkpoint. A repeated business step
without a new verified milestone can also stop the owned process and request a
checkpoint. Waiting for a user suspends cycle detection. Heartbeat loss and an
operation timeout preserve the scene and stop: they do not alone prove an engine
bug. There is no model continually interpreting workflow health.

Returning from a child workflow records a phase transition, without counting it
as repeated execution or awarding a milestone. Call and verification boundaries
still count toward cycle detection. Checkpoints retain the root session selected
or created during the invocation, so recovery resumes it explicitly even when
the original command omitted `--session`.

Interactive termination prints a short reason and the retained log location.
Full faults, tracebacks and evidence are saved beside the resume checkpoint;
`auto-agents-watch status --job JOB --json` returns the full maintenance record.
Watcher commands emit JSON only when `--json` is supplied.

If automatic repair stopped before making any model call because the engine
checkout was dirty, rerunning the business command checks the current engine
against the retained session once. The previous fault remains in history and
budgets are preserved; another fault still goes through normal repair admission.

Maintenance takes a private project snapshot, including settled call receipts.
Registered native candidate repositories outside the project are copied too;
offline execution mounts these copies at their logical paths. Only the private
inode registration changes. Goals, source descriptors and call receipts remain
constraints. Unregistered sources and ownership conflicts stop as state errors,
without starting an engine repair. Offline checks do not send notifications.
Unconfirmed external requests require reconciliation before automatic recovery.
Routed fixes read their issue from the control repository and authenticate the
session, handoff and original command, including inside private source clones.
Classification replies remain evidence and cannot replace that authority or an
already bound verification command. Legacy pre-writer command overwrites may
be recovered only when the original handoff, retained issue and sealed binding
agree and no product changes, writer history or receipt exists. The overwritten
classification is retained in the execution log; conflicting sources remain
blocked.
When a parent still holds the old engine repair reply, a proven recoverable
child triggers a fresh parent diagnosis instead of replaying that reply as a
new engine failure. Eligibility alone neither resumes nor completes product
implementation. A business recheck that produces a different checkpoint starts
with new snapshot and candidate inputs; old inputs remain evidence and spent
model, attempt and no-progress budgets are preserved.
Resume handoffs reference the original handoff's source instead of registering
a new source. A legacy prepared wrapper can drop an incorrectly minted source
only when its controller registration exactly matches the retained parent,
original handoff and child, apart from the wrapper ID and resulting fingerprint.
The old descriptor remains in the execution log; other source conflicts stop.
The snapshot command returns a small receipt; full records and settled calls
remain in the copied database. Failed exports are discarded before retry, and
their exit code and structured error are reported even when stderr is empty.
Existing physical issue projections retain their original bytes during export,
so a legacy canonical overwrite cannot erase recovery evidence. The copied
database remains authoritative and all recovery authentication still applies.
Project verification infrastructure incidents are recorded as `verification`
faults, with command output in the checkpoint diagnostics. They do not authorize
engine editing. An ordinary supervisor resume retains such a stop; after fixing
the prerequisite, use `auto-agents-watch resume --job JOB --retry-business` to
recheck the original business task without replenishing maintenance budgets.
Legacy Chrome/CDP markers for an unmet expected DOM/text predicate remain
failed project tests when their failed test IDs are available. They can be
compared with the original baseline; they never count as passing browser proof.
Mixed or actual browser launch/protocol failures remain infrastructure stops.

Verification uses disposable HOME and private shared memory even when it retains
the admitted operator environment. The original read-only tool cache remains
available through `USER_CACHE_DIR`; a private HOME does not silently switch the
selected browser. Live project paths and the operator's actual HOME stay
read-only. Verification clears inherited DISPLAY, WAYLAND_DISPLAY and session
DBus addresses: their WSL desktop sockets are absent from the private runtime,
and retaining them can hang headless screenshots after successful navigation.
Each offline recovery check uses a temporary working copy and removes it on
success or failure. The original snapshot and diagnostic reports remain retained
for resume; correction commits do not accumulate additional project copies.
The original defect must reproduce before editing. The writer changes one engine
candidate. Verification first checks the original offline resume boundary, then
the fixed regression manifest from the admission commit. An independent reviewer
must accept the immutable revision before delivery. Existing tests cannot be
rewritten to approve a patch. The original goal, authorization and session limits
remain constraints during recovery.

A correction earns progress only when its verified passing checks strictly
extend the best previously passing set. Losing a passed check does not count as
improvement. Two consecutive corrections without improvement stop by default.
Unknown or unavailable verification prerequisites stop without inventing a pass.
Counters and candidate history survive watcher restarts and repeated invocations.
Optional total call/time limits are additional ceilings.

When the original business task is DONE and publication is complete or disabled,
the watcher bundles candidate Git history, then removes project snapshots,
candidate and publication workspaces, and private agent homes. Checkpoints,
verification reports and call receipts remain. Dirty candidates, unconfirmed
calls, active containers and pending/conflicting publication retain their inputs.
Completed publication retries are idempotent; after inputs are released, a new
publication destination uses normal Git rather than replaying the finished job.
The watcher also requests bounded project storage maintenance after releasing
the business lock. Cleanup errors are recorded without undoing task completion.

The watcher commits accepted candidate changes locally and only fast-forwards
an unchanged, clean engine branch. It installs an immutable revision in a private
venv, records successful installation, updates the normal installation pointer,
and starts a fresh business process with the original arguments. No manual
`upgrade` step is necessary. The pointer belongs to ordinary engine installation;
standalone execution can use it without a running watcher.

## Git publication across machines

Publishing uses the branch's configured Git upstream and an ordinary push.
Divergent remote history is merged in a separate copy, then verified and reviewed
again. Content conflicts retain that copy for manual resolution. Failed merged
validation or repeated remote advancement retains a pending publication. There
is one additional refresh retry, no force push and no model resolving conflicts.
Publication failure preserves accepted local code and does not block local
business recovery.

```bash
auto-agents-watch status --json
auto-agents-watch status --job JOB --json
auto-agents-watch resume --job JOB
auto-agents-watch cancel --job JOB
auto-agents-watch retry-publish --job JOB
```

The watcher store defaults to `$XDG_STATE_HOME/auto-agents-watch` (otherwise
`~/.local/state/auto-agents-watch`). `AUTO_AGENTS_WATCH_ROOT` overrides it. An
unconfirmed model dispatch is retained and never automatically repeated on
resume. Cancellation remains durable and prevents automatic restart.

## Business state and retirement

Business records and call receipts live in `.auto-agents/state/business.sqlite3`.
JSON files remain display projections; stale writes and corrupt records fail
closed. Public commands are `business-status`, `snapshot`, `resume-check`,
`checkpoint` and `migrate-state`. Offline resume checks are sandbox-only and stop
before a new provider request or delivery.

```bash
auto-agents migrate-state --project /path/to/project --check --json
auto-agents migrate-state --project /path/to/project --json
```

Migration reads the old controller database and content-addressed objects,
hydrates original business records, preserves settled/cancelled calls and unknown
outcomes, and archives the original database, marker and referenced objects.
It acquires both old and current project locks before switching authority. Old
controller data is retained; stop any old maintenance process before migration.

The former repair command family, semantic health sidecar, internal engine-repair
handoffs, multi-model diagnosis controller and runtime adoption protocol are
retired. Historical incident documents describe the former implementation and
are not operational instructions for this design.

For an unresolved **business** model call, inspect the native output first. A
confirmed result file has `{"operation_id":"ID","result":{...AgentResult...}}`.
Use `auto-agents reconcile-call --project PROJECT --call ID --result FILE`.
`--confirm-cancelled` is an explicit operator confirmation that the original
request was cancelled; it does not authorize repeating that request.

Provider exhaustion is a confirmed failure, including when no provider binary
is available. Its receipt preserves the original exception and any terminal
provider result; replay never dispatches that same request again. A genuinely
unconfirmed dispatch blocks the session immediately with
`external_call_reconciliation_required`, preserves its resume phase, and reports
the pending call ID and reconciliation command instead of retrying clarification.

For an unresolved **maintenance** CLI call, `auto-agents-watch reconcile --job JOB
--result FILE` accepts an operator-confirmed receipt with `job_id`, `call` (the
retained call number), and `result` containing `confirmed: true` and a boolean
`ok`. It preserves counters and requires an explicit `resume`. Writer changes
are verified before another implementation call; a receipt grants no code
approval. Cancellation stops only positively identified processes/containers.

Automatic process replacement preserves native session attempt budgets. A later
fault can start another isolated candidate only after new verified business
progress. A repeated fault, or a changed symptom without such progress, stops
with the new evidence retained. Previous candidates and publication conflicts
remain separate. `quiesce` is a lock-owned public engine command for stopping
registered business children; it does not terminate the watcher owner.
