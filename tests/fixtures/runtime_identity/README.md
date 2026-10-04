This fixture is the unmodified `src/auto_agents/session_verification.py` from
commit `c4906fee7becfbda6c06fb96be6df6843d36754a` in this repository.

It retains the historical catalog counterexample for both function-only and
whole-module runtime identity checks. Standalone runtime snapshots do not carry
the developer repository's historical Git objects. The test checks the fixture's
SHA-256 before executing it under the candidate module filename; no candidate
source file is overwritten.
