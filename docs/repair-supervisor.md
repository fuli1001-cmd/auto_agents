# Optional external supervision

`auto-agents-watch` is an independently installed package in `supervisor/`.
Business execution uses the schema-2 controller described in
[control-v2.md](control-v2.md). The watcher communicates through CLI JSON and
process observations; it does not import the business controller or interpret
its private handoffs.

## Installation and startup

```bash
python -m pip install -e . -e ./supervisor

auto-agents collab --project /path/to/project --provider existing-alias --session SESSION
# Standalone business execution:
auto-agents collab --project /path/to/project --session SESSION --no-supervisor
# Explicit supervision:
auto-agents-watch run --engine /path/to/auto_agents -- auto-agents collab --project /path/to/project --session SESSION
```

`execution.supervision.mode` defaults to `auto`. An installed watcher starts
with business commands; an absent watcher does not prevent execution. `off`
disables automatic startup. Engine repair needs a clean engine Git checkout and
Docker. Ordinary business execution does not need Docker.

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
      "publish": true,
      "base_image": "registry.cn-hangzhou.aliyuncs.com/fuli1001/node:lts-slim"
    }
  }
}
```

The repair writer and independent reviewer use the invocation's `--provider`,
or `active_provider`, and the corresponding effort mappings. They use separate
native CLI sessions. There is no `repair_provider` configuration. The native
maintenance adapters support Codex, Claude Code and Copilot CLI; an unsupported
kind stops explicitly rather than substituting an account.

The default tool-image base is `node:22-bookworm-slim`.
`execution.supervision.base_image` or `AUTO_AGENTS_WATCH_BASE_IMAGE` can select a
compatible local mirror. The local base identity participates in the cache key;
builds use `--pull=false`. `AUTO_AGENTS_WATCH_IMAGE` selects a prepared tool image
with Python, Git, pytest, engine dependencies and the selected CLI. Build failures
include the build log and tail diagnostics.

## One maintenance loop

The watcher has six states: RUNNING, REPAIRING, VERIFYING, RESTARTING, DONE and
STOPPED. An inherited project file lock prevents concurrent business owners.
It has no controller daemon, descriptor-transfer service or recursive repair.

The business process emits a heartbeat and actual step transitions. Completed
work, approvals and verified improvement count as milestones. Repeated steps
without a milestone can request a checkpoint. Waiting for a user suspends cycle
detection. Heartbeat loss and timeouts stop with evidence; neither alone proves
an engine bug. No model continually interprets workflow health.

Only an evidenced engine fault enters repair. Business failure, missing test
infrastructure, operator decisions, migration conflicts and unknown request
outcomes retain their own category and stop without editing the engine.

Repair uses this sequence:

1. Export a private project snapshot and exact checkpoint through public APIs.
2. Reproduce the original exception in the isolated offline copy.
3. Have the configured provider edit an isolated engine candidate.
4. Run the fixed verification contract and independent review.
5. Confirm that offline resume visits the original blocked step and crosses it.
6. Commit the verified engine delta and restart the same business root.

Offline acceptance has no account credentials, network or model calls. Reaching
another model-call boundary in the **same** phase is insufficient. Frozen goals,
source, scope, checks, authorization and call limits must remain intact. An
unreproduced exception or changed checkpoint retains the candidate and evidence.
Repair continuation uses verified improvement and stagnation, with configured
hard maintenance limits. Retrying does not refund model calls or attempts.

## Stops and reconciliation

```bash
auto-agents-watch status --root /path/to/watch-state --job JOB --json
auto-agents-watch resume --root /path/to/watch-state --job JOB
# Recheck a business/environment stop after its prerequisite is resolved:
auto-agents-watch resume --root /path/to/watch-state --job JOB --retry-business
```

Interactive output shows a short reason and log location; full fault evidence and
tracebacks live beside the business checkpoint. `--retry-business` resumes the
same retained work and accounting. It does not turn a business problem into an
engine defect or erase missing historical checks.

If an old stopped job's maintenance snapshots were explicitly retired, repeating
the business command creates a linked successor and runs the current business
before considering repair. `resume --retry-business` has the same behavior and
subsequent resumes of the old job follow its successor. Old faults and checkpoints
remain in the predecessor for diagnosis; model-call counts, attempts, stagnation
and the maintenance deadline are carried forward. Unknown model calls still need
reconciliation and cancellation still requires an explicit resume. Retired inputs
are never passed to Docker as if they were available.

An unconfirmed business call requires a receipt or an explicit confirmation that
the original request was cancelled:

```bash
auto-agents reconcile-call --project PROJECT --call ID --result receipt.json
auto-agents reconcile-call --project PROJECT --call ID --confirm-cancelled
```

A receipt has `{"operation_id":"ID","result":{...}}`. Confirming cancellation
never refunds the original call. An unconfirmed maintenance call uses
`auto-agents-watch reconcile --job JOB --result FILE`; its receipt identifies the
job and original call and supplies a confirmed result. Neither reconciliation
path blindly resends a possibly completed request.

## Git publication across computers

Verified fixes create local commits. Publication is governed by
`execution.supervision.publish`. With publication enabled and a configured
remote, the watcher can fetch, validate an integration against its fixed checks,
and publish. Conflicting merges stop with evidence; there is no force push or
model inventing a resolution. A pending publication retains the original inputs.

```bash
auto-agents-watch retry-publish --job JOB
```

Installing a verified repair and restarting use the selected runtime directly;
they do not require a business `upgrade` step. Business `upgrade` concerns project
configuration and state compatibility.

## State and cleanup

Each computer migrates its own business state once with `migrate-state`. The new
controller uses `.auto-agents/state/business.sqlite3` as its sole authority.
Original records are archived for checking; missing or conflicting historical
proof remains blocked. Old internal repair routes, semantic sidecars, session
normalization and workflow recovery loops are not execution paths in version 0.2.

Settled maintenance jobs release their private project copies and CLI homes.
Candidate commits are retained as verified Git bundles; pending publication,
unknown calls and live processes retain their inputs. Owned container labels and
live image leases govern collection of obsolete tool images. Base images and
unmanaged Docker resources are preserved. Business scratch and duplicate terminal
SQLite data are collected by the business controller. No global Docker prune or
WSL disk compaction is performed.
