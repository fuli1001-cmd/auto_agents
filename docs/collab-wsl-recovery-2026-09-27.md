# Collab activation failure on WSL, 2026-09-27

Repair job `9d2924157fbd47b6a15334b0` accepted commit `49036a8` and passed
subscriber replay, then failed to activate session `091707202dc3` on the host.
The terminal `previous engine repair has not restored its bound child` error was
the last consequence of two earlier failures:

1. The actual child `891c808047b7` passed the historical-plan check but could not
   start verification: `bwrap: Can't mkdir /run/WSL: Read-only file system`.
   Earlier validation using an externally supplied private `/run` had hidden
   this normal-launch failure. The Docker replay also lacked the host's WSL
   runtime mount layout.
2. The returned engine handoff `hf-252ebf9332f4` retained the original product
   handoff `hf-d05add764339`. Parent diagnosis subsequently suggested another
   repair referring to the engine envelope, which has no product child of its
   own. On resume, the latest suggestion was parsed before the durable failed
   return, causing `engine return failed handoff has conflicting ownership`.
   Submitting another repair could not replace the missing actual-child ACK.

## Correction

The normal verification launcher mounts a private, empty `/run` inside its own
mount namespace before Codex/bwrap constructs the restricted view. `/run` stays
denied to the executed verification process; host mounts and files are unchanged.
The same launcher covers ordinary verification and retained-environment writers.

Explicit resume resolves a valid returned engine handoff before considering a
later parent route suggestion. The returned record must still match the parent,
workflow, blocked status and resolution. Its exact engine receipt and original
child binding must pass the existing checks. No diagnostic record is rewritten,
and no budget, ownership check, ACK requirement or publication guard is relaxed.

## Verification

Regression tests construct a private host-like `/run/WSL` submount, exercise the
normal launcher with both ordinary and retained environments, and verify denied
host access, permitted private writes and unchanged shared data/metadata. These
tests do not wrap the launcher in a pre-cleaned `/run` environment.

A retained-workflow regression reproduces the later parent suggestion masking
the failed engine return. Matching receipts re-enter the same child; mismatched
receipts remain blocked. The broader checks cover nested confinement, writer
boundaries, control-channel acknowledgements, child identity and budget retention.

The latest failed SDGP scene is copied and sealed before offline container replay.
The replay stops immediately before the implementation provider call, with
`preflight_rechecked: true`, the original child/handoff, preserved constraints,
unchanged parent counters and one reserved child attempt in the disposable copy.
No model call runs. Actual browser/video acceptance remains with the original
workflow after recovery; it is not claimed by this replay.

Validation completed with 99 distinct regression cases passing: 75 confinement,
writer and control-channel checks; 22 recovery/ownership cases; and two focused
historical-plan recovery cases. All ran on the normal WSL host launch path.
The sealed latest-scene replay passed its outer input/environment integrity
checks as well as actual child entry. The live parent, child and returned-handoff
records were unchanged. A compact observation is retained in
[`collab-wsl-recovery-validation.json`](collab-wsl-recovery-validation.json).
