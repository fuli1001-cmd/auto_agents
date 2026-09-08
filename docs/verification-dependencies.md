# Verification software prerequisites

Self-repair distinguishes unavailable verification software from a failing
candidate. The classifier identifies software by error structure and trusted
pytest exception evidence, not a list of familiar tool names. It covers missing
or unlaunchable executables, missing Python imports, missing Node packages and
unavailable shared libraries. It also handles inaccessible PATH entries that
make a missing program appear as `PermissionError` in an isolated process.

Repository-owned modules/scripts, relative JavaScript imports, missing test data
and ordinary assertion failures retain their existing code/contract behavior.
Dependency evidence belongs to failed test reports; an expected, caught missing
program in a passing test is not a blocker.

Custom checks can report prerequisites explicitly with
`VerificationDependencyError(MissingDependency(kind, name), detail)` from
`auto_agents.verification_dependencies`; they do not need a new classifier
branch for each software product.

## Declaring preparation

The trusted executor ships `verification_tools/catalog.json`. Vitest is one
entry in that catalog, with its locked npm dependencies. Additional recipes live
in `verification_dependencies` in the controller's `operator.json`. Specification
paths are relative to `dependency-specs/` alongside that operator file. A model's
workspace, test output and project settings cannot supply these recipes.

For example, an operator can add these entries to the existing configuration:

```json
{
  "verification_dependencies": {
    "python-extra": {
      "installer": "pip",
      "provides": ["python:PIL"],
      "requirements": "python-extra/requirements.lock"
    },
    "node-extra": {
      "installer": "npm",
      "provides": ["node:typescript", "executable:tsc"],
      "requires": ["executable:node", "executable:npm"],
      "package.json": "node-extra/package.json",
      "package-lock.json": "node-extra/package-lock.json",
      "executables": {"tsc": "typescript/bin/tsc"}
    },
    "media-tool": {
      "installer": "existing",
      "provides": ["executable:ffmpeg"],
      "path": "/opt/verification-tools/ffmpeg",
      "sha256": "REPLACE_WITH_THE_FILE_SHA256"
    }
  }
}
```

`provides` uses `executable:NAME`, `python:IMPORT_NAME`, `node:PACKAGE_NAME` or
`shared_library:LIBRARY_NAME`. Import names are not assumed to equal distribution
names: the Python lockfile supplies the exact distributions, versions and hashes.
An ambiguous capability declaration blocks preparation rather than selecting an
arbitrary recipe. `requires` composes prerequisites; cycles are rejected.

- `pip` installs wheels into a private overlay with `--require-hashes` and
  `--only-binary=:all:`. Every transitive requirement must be pinned and hashed.
  The running engine's site-packages are not replaced. These constraints follow
  [pip's documented hash-checking mode](https://pip.pypa.io/en/stable/topics/secure-installs/).
- `npm` uses `npm ci`, the supplied lockfile, enforced engine requirements and
  disabled lifecycle scripts. Executable entry paths are relative to the
  installed `node_modules`; package code runs in the original proof sandbox,
  rather than as a host-side readiness probe.
- `existing` snapshots one self-contained executable or library after checking
  its SHA-256. Subsequent changes to the supplied file cannot change that
  snapshot. Applications requiring a directory of relative resources need their
  proper package/toolchain setup; copying one executable does not provide those
  additional resources.

Prepared tools are read-only verification inputs. Executables are exposed on
PATH, Python overlays on PYTHONPATH, CommonJS packages on NODE_PATH, and supplied
shared libraries on LD_LIBRARY_PATH. Managed npm entrypoints resolve their own
package dependencies. A test's ESM resolver or explicit absolute executable path
still needs an appropriate binding; an installation receipt alone never proves
that the requested check can execute.

## Bounds and recovery

The original check reruns after preparation, preserving the candidate and its
acceptance requirements. Tool and declaration fingerprints participate in cache
identity. A proof keeps the environment snapshot it started with; an older
parallel failure can retry a newly prepared environment without installing again.

There is one preparation attempt per recipe/specification and repair generation,
shared across verification workers. Failed setup, a missing declaration, a cycle,
or a dependency still unavailable after preparation returns
`verification_environment_blocked`, with the dependency kind/name and available
setup diagnostics. These outcomes never consume code-search patience or trigger
a redesign. Unknown software receives this explicit blocker too.

For provider-requested engine checks, the supervisor forwards a blocker only to
its current job generation, process identity and workspace inode. The owner stops
the active provider call and saves its unfinished patch; it does not try another
model/provider for the same missing software. Obsolete results cannot stop a new
generation or another workspace.

After supplying the software or correcting its operator declaration, use
`auto-agents repair resume --job JOB`. That explicit resume starts a new repair
generation and permits another preparation attempt. A stopped generation's
blocker remains evidence, not a signal to interrupt the new owner. Engine review,
full verification and original-workflow recovery checks are still required.
