# Introduce TDD Paradigm to auto_agents Orchestrator

This plan details the implementation of a strict Test-Driven Development (TDD) pipeline inside `auto_agents`. By breaking the "self-testing" loop, we force a new `Test-Writer` phase to generate black-box tests as immutable contracts *before* the `Implement` phase begins.

## User Review Required
> [!IMPORTANT]
> - Adding a new intermediate git commit for the TDD tests means for each task you will see two commits logically: one `test: Add TDD acceptance contract...` and one `feat({task_id}): ...`.
> - The prompt for `test_writer` currently assumes the project is Python/pytest compatible as defined in existing project defaults, but is made generic enough.
> Please review the Git diff behavior and test generation logic.

## Proposed Changes

### 1. Update Models (src/auto_agents/models.py)
We need to track whether a task has generated its tests and which files are protected.
- **[MODIFY] src/auto_agents/models.py**: Add `test_generated: bool = False` and `contract_files: List[str] = field(default_factory=list)` to `TaskSpec`. Update `from_dict` and `to_dict` serialization logic to persist these new fields.

### 2. Orchestrator TDD Pipeline (src/auto_agents/orchestrator.py)

#### JIT Test Generation Stage
- **[MODIFY] src/auto_agents/orchestrator.py**: In `_run_implementation_loop`, right before calling `_execute_task_with_retries(state, task)`, add logic:
  ```python
  if not task.test_generated:
      self._run_task_test_writer(state, task)
  ```
- Add the `_run_task_test_writer` method. It will build a specific prompt (stage=`"test_writer"`), run the agent, check `git diff --name-only HEAD` to capture the modified files, commit them with a `test:` prefix, and save the list to `task.contract_files`.

#### Tool-Level / Git Diff Audit 
- **[MODIFY] src/auto_agents/orchestrator.py**: Inside the loop of `_execute_task_with_retries` for the `implement` stage, right after the Implement Agent finishes (`result.ok` is True), check the modified files via `git diff HEAD --name-only`.
- If any modified file intersects with `task.contract_files`, the framework will **reject** the iteration:
  - Run `git restore --staged <files>` and `git restore <files>` to surgically revert the unauthorized tampering of contract files.
  - Set the `feedback` to a strict Permission Denied error and `continue` to the next Implement loop attempt.

#### Prompt Injection
- **[MODIFY] src/auto_agents/orchestrator.py**: In `_build_task_prompt`, handle `stage == "test_writer"`:
  Provide strict instructions to generate *only* test cases for the acceptance criteria and *no* business logic.

## Open Questions
- If the `Test-Writer` agent fails to generate tests or generates syntax-error tests, should it retry or just fallback to regular implementation? *Decision: We will allow the `Test-Writer` to retry using the standard `config.retries` mechanics. If it totally exhausts retries, it throws an error like other stages.*
