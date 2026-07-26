# Managed runtime layout repair

Gate jobs on POSIX workers use a short, private per-job runtime under `/tmp`.
This prevents tools such as Chrome from exceeding the Unix-domain socket path
limit when they append their own runtime directory and socket names.

Workers advertise `managed_runtime_layout_repair_v1` while retaining worker
protocol version 4. Controllers only send the additive
`runtime_profile: short_socket_path_v1` job field to workers that advertise the
feature. Updated workers also use the short profile by default for older
controllers.

The runtime directory:

- is unique per job and owned by the worker user;
- has mode `0700`;
- supplies `TMPDIR`, `TMP`, `TEMP`, and `XDG_RUNTIME_DIR`;
- is removed when the job reaches a terminal state;
- is opportunistically garbage-collected after 24 hours if a worker process
  terminates before cleanup.

Reported `Socket path too long`, `SingletonSocket`, and `ENAMETOOLONG` failures
are classified as the confirmed cause `unix_socket_path_too_long`. The
distributed gate retries the unchanged command with the short runtime profile
across feature-compatible workers before attempting browser artifact
replacement. A worker that does not advertise the feature is recorded as
unsupported rather than silently downgraded.

Configuration defaults:

```json
{
  "execution": {
    "recovery": {
      "managed_runtime_layout_repairs_enabled": true,
      "max_managed_repair_attempts_per_incident": 6
    }
  }
}
```
