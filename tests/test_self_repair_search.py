from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from auto_agents.config import bootstrap_project, load_run_state
from auto_agents.self_repair_search import (
    SelfRepairCandidateRecord,
    SelfRepairExperiment,
    SelfRepairExperimentStore,
    SelfRepairFinding,
)


class SelfRepairSearchTests(unittest.TestCase):
    def _experiment(self) -> SelfRepairExperiment:
        return SelfRepairExperiment.create(
            run_id="run-1",
            root_fingerprint="root-1",
            category="retained-worktree",
            base_commit="base",
            expected_postconditions=(
                "zero-overlap retired ownership does not arm the guard",
                "prepared blocker resumes through the live entrypoint",
            ),
            max_consecutive_non_improvements=3,
            max_frontier_candidates=8,
        )

    def test_root_and_validation_net_progress_keep_search_alive(self) -> None:
        experiment = self._experiment()
        root_obligations = sorted(
            key for key in experiment.obligations if key.startswith("root:")
        )
        c1 = SelfRepairCandidateRecord(
            candidate_id="c1",
            candidate_ref="refs/c1",
            candidate_commit="commit-c1",
            validation_rank=40,
            validation_stage="adversarial_review",
            passed_obligations=[root_obligations[0], "safety:scope_guard"],
            failed_obligations=[
                root_obligations[1],
                "safety:malformed_state_fail_closed",
            ],
            finding_ids=["malformed-fail-open"],
        )
        progress = experiment.register_candidate(
            c1,
            findings=(
                SelfRepairFinding(
                    finding_id="malformed-fail-open",
                    status="confirmed",
                    reason="malformed contexts disarm the guard",
                ),
            ),
        )
        self.assertEqual(progress, "net_progress")
        self.assertEqual(experiment.consecutive_non_improvements, 0)
        self.assertIn("c1", experiment.frontier)

        c2 = SelfRepairCandidateRecord(
            candidate_id="c2",
            parent_candidate_id="c1",
            candidate_ref="refs/c2",
            candidate_commit="commit-c2",
            validation_rank=40,
            validation_stage="adversarial_review",
            passed_obligations=[
                root_obligations[0],
                "safety:scope_guard",
                "safety:malformed_state_fail_closed",
            ],
            failed_obligations=[root_obligations[1]],
            resolved_finding_ids=["malformed-fail-open"],
            finding_ids=["prepared-resume"],
        )
        progress = experiment.register_candidate(
            c2,
            findings=(
                SelfRepairFinding(
                    finding_id="prepared-resume",
                    status="confirmed",
                    reason="prepared blocker merge restores stale proof",
                ),
            ),
        )
        self.assertEqual(progress, "net_progress")
        self.assertEqual(experiment.best_search_candidate_id, "c2")
        self.assertNotIn("malformed-fail-open", experiment.findings)

    def test_child_inherits_parent_proof_and_becomes_next_candidate_parent(self) -> None:
        experiment = self._experiment()
        root_obligations = sorted(
            key for key in experiment.obligations if key.startswith("root:")
        )
        first = SelfRepairCandidateRecord(
            candidate_id="c1",
            candidate_ref="refs/c1",
            candidate_commit="commit-c1",
            validation_rank=50,
            validation_stage="focused_verification",
            passed_obligations=[root_obligations[0], "validation:focused"],
        )
        experiment.register_candidate(first)

        second = SelfRepairCandidateRecord(
            candidate_id="c2",
            parent_candidate_id="c1",
            candidate_ref="refs/c2",
            candidate_commit="commit-c2",
            status="candidate_group_completed",
            validation_rank=60,
            validation_stage="finding_group",
            passed_obligations=[root_obligations[1], "validation:focused"],
        )
        experiment.register_candidate(second)

        self.assertTrue(
            {
                root_obligations[0],
                root_obligations[1],
                "validation:focused",
            }.issubset(set(second.passed_obligations))
        )
        self.assertEqual(experiment.best_search_candidate_id, "c2")
        self.assertEqual(experiment.best_search_ref, "refs/c2")

    def test_sticky_verification_commands_are_deduplicated_and_persisted(self) -> None:
        experiment = self._experiment()

        self.assertTrue(
            experiment.remember_sticky_verification_commands(
                [
                    "python -m pytest  -q tests/test_retry.py::test_regression",
                    "python -m pytest -q tests/test_retry.py::test_regression",
                    "python -m pytest -q tests/test_other.py",
                ]
            )
        )
        self.assertFalse(
            experiment.remember_sticky_verification_commands(
                ["python -m pytest -q tests/test_retry.py::test_regression"]
            )
        )

        restored = SelfRepairExperiment.from_dict(experiment.to_dict())
        self.assertEqual(
            restored.sticky_verification_commands,
            [
                "python -m pytest -q tests/test_retry.py::test_regression",
                "python -m pytest -q tests/test_other.py",
            ],
        )

    def test_patience_counts_only_consecutive_semantic_non_progress(self) -> None:
        experiment = self._experiment()
        for index in range(1, 4):
            progress = experiment.register_candidate(
                SelfRepairCandidateRecord(
                    candidate_id=f"duplicate-{index}",
                    status="candidate_duplicate",
                    patch_fingerprint="same",
                )
            )
            self.assertEqual(progress, "no_progress")
        self.assertTrue(experiment.patience_exhausted)

        infrastructure = SelfRepairCandidateRecord(
            candidate_id="infra",
            status="candidate_exception",
            infrastructure_failure=True,
        )
        experiment.register_candidate(infrastructure)
        self.assertEqual(experiment.consecutive_non_improvements, 3)

    def test_legacy_full_suite_finding_is_migrated_to_post_proof_review(self) -> None:
        finding = SelfRepairFinding.from_dict(
            {
                "finding_id": "candidate-regression-proof-inconclusive",
                "obligation_id": "conclusive_candidate_regression_validation",
                "reason": "The full-suite comparison has not run yet.",
                "required_test": "Complete equivalent base and candidate full suites.",
            }
        )

        self.assertEqual(finding.defer_until, "post_full_suite")

    def test_final_review_rank_counts_as_validation_progress(self) -> None:
        experiment = self._experiment()
        experiment.consecutive_non_improvements = 2
        experiment.candidates["full-suite"] = SelfRepairCandidateRecord(
            candidate_id="full-suite",
            candidate_ref="refs/full-suite",
            candidate_commit="commit-full-suite",
            status="candidate_full_suite_inconclusive",
            validation_stage="full_suite",
            validation_rank=90,
        )

        progress = experiment.register_candidate(
            SelfRepairCandidateRecord(
                candidate_id="final-review",
                parent_candidate_id="full-suite",
                candidate_ref="refs/final-review",
                candidate_commit="commit-final-review",
                status="candidate_final_review_rejected",
                validation_stage="final_review",
                validation_rank=95,
                passed_obligations=["validation:full_suite"],
                failed_obligations=["validation:final_review"],
            )
        )

        self.assertEqual(progress, "net_progress")
        self.assertEqual(experiment.consecutive_non_improvements, 0)

    def test_unrelated_review_observation_is_not_persisted_or_scheduled(self) -> None:
        experiment = self._experiment()
        record = SelfRepairCandidateRecord(
            candidate_id="candidate",
            parent_candidate_id="base",
            candidate_ref="refs/candidate",
            validation_rank=40,
        )

        experiment.register_candidate(
            record,
            findings=(
                SelfRepairFinding(
                    finding_id="generic-hardening",
                    status="confirmed",
                    disposition="unrelated_observation",
                    reason="a generic subsystem could be hardened",
                    counterexample="unrelated counterexample",
                    required_test="unrelated test",
                    evidence=["unrelated.py:1"],
                ),
            ),
        )

        self.assertNotIn("generic-hardening", experiment.findings)
        self.assertNotIn("finding:generic-hardening", experiment.obligations)

    def test_candidate_regression_is_local_and_counts_against_net_progress(self) -> None:
        experiment = self._experiment()
        record = SelfRepairCandidateRecord(
            candidate_id="candidate",
            parent_candidate_id="base",
            candidate_ref="refs/candidate",
            validation_rank=40,
        )

        progress = experiment.register_candidate(
            record,
            findings=(
                SelfRepairFinding(
                    finding_id="new-regression",
                    status="confirmed",
                    disposition="candidate_regression",
                    reason="the candidate breaks an existing boundary",
                ),
            ),
        )

        self.assertEqual(progress, "no_progress")
        self.assertLessEqual(record.net_progress, 0)
        self.assertNotIn("new-regression", experiment.findings)
        self.assertIn(
            "candidate_regression:new-regression",
            record.failed_obligations,
        )

    def test_contract_finding_must_map_to_frozen_obligation(self) -> None:
        experiment = self._experiment()
        obligation_id = next(
            item
            for item in experiment.contract_obligation_ids
            if item.startswith("root:")
        )
        record = SelfRepairCandidateRecord(
            candidate_id="candidate",
            parent_candidate_id="base",
            candidate_ref="refs/candidate",
            validation_rank=40,
        )

        experiment.register_candidate(
            record,
            findings=(
                SelfRepairFinding(
                    finding_id="causal-gap",
                    status="confirmed",
                    disposition="contract_violation",
                    causal_obligation_id=obligation_id,
                    reason="the design misses a root postcondition",
                    counterexample="the original failure still reproduces",
                    required_test="run the root differential",
                    evidence=["tests/test_root.py:1"],
                ),
            ),
        )

        self.assertIn("causal-gap", experiment.findings)
        self.assertIn(obligation_id, record.failed_obligations)
        self.assertNotIn("finding:causal-gap", experiment.obligations)

    def test_automatic_correction_blacklists_strategy_without_human_state(self) -> None:
        experiment = self._experiment()
        experiment.status = "needs_human"
        experiment.consecutive_non_improvements = 3
        experiment.repair_design = {"strategy_id": "bad"}

        experiment.apply_automatic_correction(
            reason="net progress stalled",
            candidate_id="candidate",
            strategy_fingerprint="bad-strategy",
        )

        self.assertEqual(experiment.status, "active")
        self.assertEqual(experiment.consecutive_non_improvements, 0)
        self.assertEqual(experiment.repair_design, {})
        self.assertIn("bad-strategy", experiment.strategy_blacklist)
        self.assertEqual(
            experiment.automatic_corrections[-1]["event"],
            "automatic_correction",
        )

    def test_finding_groups_follow_dependencies_and_preserve_completion(self) -> None:
        experiment = self._experiment()
        root_ids = sorted(
            item
            for item in experiment.contract_obligation_ids
            if item.startswith("root:")
        )
        experiment.finding_groups = [
            {
                "group_id": "second",
                "depends_on": ["first"],
                "contract_obligation_ids": [root_ids[1]],
                "finding_ids": [],
                "status": "pending",
            },
            {
                "group_id": "first",
                "depends_on": [],
                "contract_obligation_ids": [root_ids[0]],
                "finding_ids": [],
                "status": "pending",
            },
        ]

        first = experiment.next_finding_group()
        self.assertEqual(first["group_id"], "first")
        experiment.mark_finding_group_completed("first", candidate_id="c1")
        second = experiment.next_finding_group()

        self.assertEqual(second["group_id"], "second")
        self.assertIn(root_ids[0], experiment.completed_contract_obligation_ids)

    def test_prompt_context_keeps_only_compact_recent_history(self) -> None:
        experiment = self._experiment()
        for index in range(10):
            experiment.candidates[f"c{index}"] = SelfRepairCandidateRecord(
                candidate_id=f"c{index}",
                summary=("long candidate history " * 200) + str(index),
                verification="verification output " * 500,
            )

        context = experiment.prompt_context()

        self.assertEqual(len(context["recent_candidates"]), 3)
        self.assertEqual(
            [item["candidate_id"] for item in context["recent_candidates"]],
            ["c7", "c8", "c9"],
        )
        self.assertTrue(
            all("verification" not in item for item in context["recent_candidates"])
        )
        self.assertTrue(
            all(len(item["summary"]) <= 400 for item in context["recent_candidates"])
        )

    def test_store_restores_frontier_patience_and_health_oscillation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            store = SelfRepairExperimentStore(project, state.run_id, "root-1")
            experiment = self._experiment()
            experiment.run_id = state.run_id
            experiment.strategy_history = ["a", "b", "a", "b", "a", "b"]
            store.save(experiment)

            health = store.record_health(
                experiment,
                status="self_repairing",
            )
            restored = store.load()

            self.assertEqual(health["anomaly"], "strategy_oscillation")
            self.assertIsNotNone(restored)
            self.assertEqual(restored.strategy_history, experiment.strategy_history)
            self.assertEqual(
                restored.health_history[-1]["anomaly"],
                "strategy_oscillation",
            )

    def test_success_compacts_losing_code_but_preserves_candidate_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            store = SelfRepairExperimentStore(project, state.run_id, "root-1")
            experiment = self._experiment()
            experiment.run_id = state.run_id
            for candidate_id in ("c1", "c2"):
                experiment.candidates[candidate_id] = SelfRepairCandidateRecord(
                    candidate_id=candidate_id,
                    candidate_ref=f"refs/{candidate_id}",
                    status="candidate_review_rejected",
                )
                root = store.candidate_root(candidate_id)
                root.mkdir(parents=True)
                (root / "raw-output.txt").write_text("raw\n", encoding="utf-8")

            store.compact_success(experiment)

            self.assertTrue((store.root / "candidate-summaries.json").is_file())
            self.assertFalse(store.candidate_root("c1").exists())
            self.assertFalse(store.candidate_root("c2").exists())

    def test_malformed_persisted_experiment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            bootstrap_project(project, "demo")
            state = load_run_state(project)
            store = SelfRepairExperimentStore(project, state.run_id, "root-1")
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps(
                    {
                        "experiment_id": "experiment",
                        "candidates": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "experiment is malformed"):
                store.load()


if __name__ == "__main__":
    unittest.main()
