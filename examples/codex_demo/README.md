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

The script bootstraps a temporary project, validates the initial scaffold, runs one task with the
real `codex` provider, and prints the resulting status and git log.

If you want a slightly richer test input than the default greeter demo, try
`examples/codex_demo/idea_tasklog.md` as the source idea. It is still small, but it exercises
subcommands, local file persistence, and a few tests instead of a single print-only script.
