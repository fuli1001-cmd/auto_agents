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

    def test_partial_and_diagnostic_progress_keep_search_alive(self) -> None:
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
        self.assertEqual(progress, "diagnostic_progress")
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
        self.assertEqual(progress, "diagnostic_progress")
        self.assertEqual(experiment.best_search_candidate_id, "c2")
        self.assertEqual(
            experiment.findings["malformed-fail-open"].status,
            "resolved",
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
