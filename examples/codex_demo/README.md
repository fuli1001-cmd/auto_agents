# Codex Demo

This example is the smallest real-provider demonstration for `auto-agents`.

It is intentionally narrow:

- one-file Python CLI target
- one unit test
- one verified feature slice

Run it with:

```bash
./examples/run_codex_demo.sh
```

The script bootstraps a temporary project, enables the standard unittest gate, runs one task with the
real `codex` provider, and prints the resulting status and git log.

