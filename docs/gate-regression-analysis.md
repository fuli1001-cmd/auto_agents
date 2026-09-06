# Gate input tracing and 200ms timeout regression analysis

## Observed-input cache

The original cache-reuse test failed consistently, including on the pre-repair
baseline. The real trace included `openat(AT_FDCWD, "input.txt", O_RDONLY)` but
also ordinary `newfstatat(fd, "", ..., AT_EMPTY_PATH)` calls from the loader,
shell, and `cat`. The parser rejected every non-`AT_FDCWD` descriptor operation,
discarding the entire manifest. This was not a missing `strace` installation or
a sandbox-only failure.

The executor now requests descriptor targets with `strace -y`. Empty-path stat
operations can use those targets as dependencies, even for inherited fds with
no preceding open in the trace. Unresolved descriptors, relative dirfd access,
cwd changes, deleted targets, and source symlinks still fail closed. Cache
certificate version 5 prevents older fallback certificates from masking the
new tracing behavior.

Regression coverage checks real cross-source reuse after an unrelated edit,
invalidation after the observed file changes, decoded descriptor-stat inputs,
and rejection of unsupported cases.

## 200ms timeout test

`run_supervised_shell_command` starts its absolute budget before diagnostics
setup and `Popen`, not after the interpreter reaches the command body. The old
test required both a 200ms hard deadline and Python printing `started` before
that deadline. The latter is not guaranteed by the former.

Evidence collected during diagnosis:

- Forty repetitions of the original command passed; inspecting the temporary
  output file immediately before termination found no emitted output lost.
- A real, unmocked 200ms run with a one-second Python startup hook reproduced
  empty stdout. Stderr contained `startup-hook-entered` and the timeout message;
  elapsed time including cleanup was approximately 251ms, and cleanup completed.
  The interpreter was terminated before reaching the command body's print.
- Synchronized regression cases separately establish startup-before-print and
  emitted-stdout/stderr states, then advance only the supervisor's clock to its
  200ms deadline. Emitted bytes survive termination in both streams.

The integration test still enforces the real 200ms timeout and bounded cleanup.
Output preservation is tested independently without assuming interpreter startup
speed. No production timeout increase or output-collection change was needed.

The earlier full-suite failure did not retain startup timing evidence, so its
specific scheduling or interpreter-startup delay cannot be reconstructed. The
invalid timing assumption and an exact empty-stdout failure mechanism are
reproduced; an output-loss defect was not observed.
