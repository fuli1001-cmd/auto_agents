# auto-agents

Auto Agents runs `run`, `fix`, `collab`, and `provider-resolve` through one business
controller. Version 0.2 replaces the former Orchestrator, Session, and Workflow
execution loops. The optional `auto-agents-watch` package supervises the process
through public interfaces and repairs only evidenced engine defects.

See [the control and migration guide](docs/control-v2.md),
[supervisor operations](docs/repair-supervisor.md), and
[bounded native acceptance](docs/control-v2-acceptance.md).

## Install

Install both packages in the Python environment used for the CLI. Linux provider
confinement requires Landlock ABI 3. Docker is needed for supervisor repair
acceptance; ordinary business execution does not depend on Docker.

```bash
cd /path/to/auto_agents
python -m pip install -e . -e ./supervisor

auto-agents capabilities --json
auto-agents-watch --help
```

The watcher is independently installed. `execution.supervision.mode` defaults to
`auto`: when installed, it starts with a business command. `--no-supervisor` or
`mode: off` runs the business CLI directly.

## Run and resume

Configure named providers in the project's `.auto-agents/config.json`. The
selected `--provider`, or `active_provider`, serves both normal work and repair;
`efforts.self_repair` and `self_repair_review` set maintenance effort. There is
no separate `repair_provider` setting.

```bash
auto-agents init --project /path/to/project
auto-agents run --project /path/to/project --provider configured-name --auto-approve
auto-agents fix --project /path/to/project --goal 'Specific observed defect' --auto-approve
auto-agents collab --project /path/to/project --goal 'Original user outcome' --auto-approve

auto-agents collab --project /path/to/project --session retained-id --auto-approve
auto-agents business-status --project /path/to/project --json
```

`run` writes requirements, a frontend prototype when needed, architecture, a
plan, provider evidence, independently verified task changes, and README.
`fix` classifies one bounded issue, implements it, verifies real behavior, and
requires independent review. `collab` delegates owned work and returns to the
original user acceptance. Provider resolution changes only reference artifacts.

For goals explicitly limited to acceptance of existing behavior, `collab` uses
the current product directly and does not create a development `run` or regenerate
requirements/prototypes. Acceptance may use untracked files under the owned
workspace's `.data/`, `.tmp/`, and `.tmp-tests/`; source, credentials, operator
configuration, and original project data remain protected. Local temp aliases,
Chromium lock metadata, and ignored, untracked Next.js generated declarations
are recognized as runtime output. Its checks observe
that same private runtime under filesystem confinement. Resuming an old mistaken
development route before implementation returns to acceptance while retaining
the child workspace, operation receipts, original contract, and call accounting.
Repeating a native command for an existing session rechecks stopped business
faults against current business state, preserving the supervisor's fault history
and spent maintenance budget.
Failed acceptance retains its reason, evidence hashes and external project/request
identities. A retry diagnoses that blocker before continuing the same test project.

Frontend prototype approval always requires a human decision. Unknown external
results require reconciliation before any resend, including a provider change.
Resume retains the original work, goal, source, verification contract, and call
limits. A newer global plan does not replace a retained task's checks.

```bash
auto-agents approve --project /path/to/project --session retained-id --gate prototype
auto-agents answer --project /path/to/project --session retained-id --from-env ANSWER --no-resume
auto-agents verify --project /path/to/project --level release --fresh
auto-agents cancel --project /path/to/project --session retained-id
```

Answers resume by default; `--no-resume` saves the decision only. Secret values
stay in operator storage, with references in business state. Independent review,
real executed tests, source identity, and artifact integrity are required before
local Git delivery. Unrelated staged user files are preserved. Conflicts stop
with the candidate retained; the business controller does not push or resolve
product conflicts automatically.

## Migrate each computer

Business state is local SQLite state, not a database to share across computers.
After upgrading the installation, migrate each computer's own project state:

```bash
auto-agents migrate-state --project /path/to/project --check --json
auto-agents migrate-state --project /path/to/project --json
```

`already_current` means schema 2 is installed. `migrated_with_blocks` means the
import succeeded and preserved historical evidence gaps as blocked work. A
missing original test, conflicting handoff, or unknown call never becomes a
successful migration proof. The migration keeps an archive under
`.auto-agents/state/legacy-archive`; retain it until the imported state is checked.
The legacy executors are not used after migration.

## Resource lifecycle

Every work item owns its temporary files, workspaces, and operation receipts.
Settled operation scratch is collected promptly; terminal workspaces and unused
private Git references are released. Large duplicate terminal operation data and
old event tails are compacted. Call accounting, contracts, final proofs, and
unknown operations remain. Verification logs belong to their operation rather
than accumulating in a global temporary directory.

The watcher collects its own settled sandboxes and old tool images using
ownership labels and live leases. It preserves base images and unmanaged
resources. Neither package performs global Docker pruning or WSL disk compaction.

```bash
auto-agents storage status --project /path/to/project
auto-agents storage maintain --project /path/to/project
```

## Development checks

The suite uses private fixture projects and fake workers to test accounting,
crash recovery, approvals, Git delivery, parallel integration, and actual local
test execution. Linux isolation tests exercise the real production boundaries.
Existing low-level verification, storage, provider, and domain tests remain.
[The test migration audit](docs/control-v2-test-migration.md) explains retired
private driver tests and their behavior-level replacements.

```bash
python -m pytest -q tests
# Optional native integration, using an already available local tool image:
AUTO_AGENTS_TEST_DOCKER_IMAGE=your-local-tool-image python -m pytest -q tests/test_control_docker_recovery.py
```

Browser tests need the configured local verification tools. Native acceptance
used 30 framework provider invocations in total and did not generate paid SDGP
media. See the acceptance record for the exact scope and limitations.

The convergence release gate runs `tests/test_control_runtime_chain.py` with
`AUTO_AGENTS_RUNTIME_ACCEPTANCE=1`, `AUTO_AGENTS_TEST_NEXT_MODULES` pointing to
Next.js 15.2.4 and `AUTO_AGENTS_TEST_CHROME` pointing to a local Chromium executable.
It exercises the actual CLI, watcher, Next.js, browser and SQLite against a local
deterministic external interface. Missing tools fail this enabled gate; an
ordinary skip is not release evidence. One project is created, blocked, repaired
and resumed without another creation or duplicate external submission.

Public behavior coverage is tracked in `schemas/behavior_contracts.json` and
checked by `tests/test_behavior_contracts.py`. Core and watcher negotiate control
features before dispatch. Schema-2 upgrades add policy/outcome metadata under the
project lock with a backup; frozen contracts and call accounting are unchanged.
Confirmed native results survive rejected validation and can be revalidated
locally. Unknown results and legacy failures lacking their native response stay
blocked for reconciliation.
