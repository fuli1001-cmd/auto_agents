import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional, Tuple
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.config import archived_run_state_path, archived_task_plan_path, gate_baseline_cache_path, load_project_config, load_run_state, load_task_plan, provider_references_lock_path, requirements_trace_path, save_project_config, save_run_state, task_plan_path
from auto_agents.gates import FailureExtraction, GateCommandBaselineIdentityError, GateCommandExecutionError, GateCommandMetadata, build_failure_identity_diagnostic_command, command_from_verification_step
from auto_agents.git_ops import changed_files, changed_paths, commit_all, commit_changed_paths, hard_reset_clean, head_ref, worktree_fingerprint
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, CommandResult, GateIsolationConfig, GateParallelGroup, GateResult, PersistenceTargetConfig, RunState, TaskSpec, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.persistence import PersistenceContractError
from auto_agents.provider_contract import PROVIDER_REFERENCE_CONTRACT_VERSION, PROVIDER_REFERENCE_V2_HEADINGS
from auto_agents.requirements import requirement_contract_sha256
from auto_agents.validation import validate_task_plan_payload, validation_report

def _strict_requirement() -> dict:
    return {'id': 'REQ-001', 'text': 'Keep the public contract verified.', 'source': 'test scope', 'status': 'active', 'priority': 'mandatory', 'acceptance_oracles': ['The public contract passes.'], 'oracle_type': 'integration_test', 'oracle_strength': 'behavioral', 'evidence_boundary': 'system_boundary', 'forbidden_proxy_oracles': [], 'forbidden_patterns': [], 'external_docs_required': False, 'provider_reference': '', 'notes': ''}

def _strict_requirement_proof(requirement: dict, evidence_ref: str, *, status: str) -> dict:
    return {'requirement_id': str(requirement['id']), 'oracle_index': 1, 'acceptance_oracle': str(requirement['acceptance_oracles'][0]), 'requirement_contract_sha256': requirement_contract_sha256(requirement), 'proof_type': 'integration_test', 'oracle_strength': 'behavioral', 'evidence_boundary': 'system_boundary', 'evidence_refs': [evidence_ref], 'forbidden_proxy_oracles': [], 'proxy_oracles': [], 'status': status}

class RetryingPlanAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0

    def run(self, request):
        if request.stage == 'plan':
            self.plan_calls += 1
            if self.plan_calls == 1:
                write_json(task_plan_path(self.project_root), {'tasks': [{'task_id': 'bad id'}]})
                write_text(request.output_path, 'invalid plan\n')
            else:
                write_json(task_plan_path(self.project_root), {'test_strategy': 'python-pytest', 'verification_steps': [{'kind': 'test', 'runner': 'pytest', 'targets': ['tests']}], 'tasks': [{'task_id': 'task-001', 'title': 'Add CLI entrypoint', 'description': 'Add a runnable command line entrypoint.', 'acceptance': ['`python -m demo --help` exits successfully.'], 'status': 'pending', 'commit_message': ''}]})
                write_text(request.output_path, 'valid plan\n')
        else:
            write_text(request.output_path, f'{request.stage}\n')
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=request.output_path.read_text(encoding='utf-8').strip(), returncode=0)

class VerificationPlanAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == 'plan':
            write_json(task_plan_path(self.project_root), {'test_strategy': 'python-pytest', 'verification_steps': [{'kind': 'test', 'runner': 'pytest', 'targets': ['tests']}], 'tasks': [{'task_id': 'task-001', 'title': 'Add CLI entrypoint', 'description': 'Add a runnable command line entrypoint.', 'acceptance': ['`python -m demo --help` exits successfully.'], 'status': 'pending', 'commit_message': ''}]})
            write_text(request.output_path, 'valid verification plan\n')
        else:
            write_text(request.output_path, f'{request.stage}\n')
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=request.output_path.read_text(encoding='utf-8').strip(), returncode=0)

class OutOfScopePlanAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == 'plan':
            write_json(task_plan_path(self.project_root), {'test_strategy': 'python-pytest', 'verification_steps': [{'kind': 'test', 'runner': 'pytest', 'targets': ['tests']}], 'tasks': [{'task_id': 'task-001', 'title': 'Plan slice', 'description': 'A valid task plan entry.', 'acceptance': ['Plan remains valid.'], 'status': 'pending', 'commit_message': ''}]})
            leaked = self.project_root / 'tests' / 'test_stage_leak.py'
            leaked.parent.mkdir(parents=True, exist_ok=True)
            write_text(leaked, 'def test_stage_leak():\n    assert True\n')
            summary = 'plan with out-of-scope mutation\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class PlanWithDiagnosticLogAdapter:

    def __init__(self, project_root: Path, *, write_requirements_audit: bool=False) -> None:
        self.project_root = project_root
        self.write_requirements_audit = write_requirements_audit

    def run(self, request):
        if request.stage == 'plan':
            write_json(task_plan_path(self.project_root), {'test_strategy': 'python-pytest', 'verification_steps': [{'kind': 'test', 'runner': 'pytest', 'targets': ['tests']}], 'tasks': [{'task_id': 'task-001', 'title': 'Plan slice', 'description': 'A valid task plan entry.', 'acceptance': ['Plan remains valid.'], 'status': 'pending', 'commit_message': ''}]})
            diagnostic = self.project_root / '.auto-agents' / 'failed-verification-logs' / 'verify-stage-test.log'
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            write_text(diagnostic, 'FAILED tests/test_demo.py::test_contract\n')
            if self.write_requirements_audit:
                write_text(self.project_root / '.auto-agents' / 'docs' / 'requirements_audit.md', '# Requirements Audit\n\nResult: fail\n')
            summary = 'plan with diagnostic log\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class OutOfScopeProviderResearchAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == 'provider_research':
            reference_path = self.project_root / '.auto-agents' / 'docs' / 'provider_references' / 'provider.md'
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(reference_path, '# Provider reference\n')
            write_json(provider_references_lock_path(self.project_root), {'version': 1, 'references': {'provider': {'path': '.auto-agents/docs/provider_references/provider.md', 'status': 'verified', 'retrieved_at': '2026-04-11T00:00:00Z', 'source_urls': ['https://example.com/official'], 'notes': ''}}})
            leaked = self.project_root / 'tests' / 'test_provider_stage_leak.py'
            leaked.parent.mkdir(parents=True, exist_ok=True)
            write_text(leaked, 'def test_provider_stage_leak():\n    assert True\n')
            summary = 'provider research with out-of-scope mutation\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class OutOfScopeReviewAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == 'review':
            write_text(self.project_root / 'notes.txt', 'review should be read-only\n')
            summary = 'DECISION: pass\nLooks good.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class ReviewTsBuildInfoAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            write_text(self.project_root / 'artifact.txt', 'good\n')
            summary = 'implemented good\n'
        elif request.stage == 'review':
            self.review_calls += 1
            workbench = self.project_root / 'workbench'
            workbench.mkdir(parents=True, exist_ok=True)
            write_text(workbench / 'tsconfig.tsbuildinfo', '{"version":"incremental-2"}\n')
            summary = 'DECISION: pass\nreview passed despite tooling cache churn\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class ReviewBuildLibAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            write_text(self.project_root / 'artifact.txt', 'good\n')
            summary = 'implemented good\n'
        elif request.stage == 'review':
            self.review_calls += 1
            write_text(self.project_root / 'build' / 'lib' / 'app' / '__init__.py', '# generated build output\n')
            summary = 'DECISION: pass\nreview passed despite python build output churn\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class OutOfScopeImplementAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(task_plan_path(self.project_root), '{"tasks": []}\n')
            summary = 'implemented with forbidden auto-agents mutation\n'
        elif request.stage == 'review':
            summary = 'DECISION: pass\nLooks good.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RecoveringOutOfScopeImplementAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'good\n')
            if self.implement_calls == 1:
                write_text(task_plan_path(self.project_root), '{"tasks": []}\n')
                summary = 'implemented with first-attempt auto-agents mutation\n'
            else:
                summary = 'implemented clean retry\n'
        elif request.stage == 'review':
            summary = 'DECISION: pass\nLooks good.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RecoveringConfigMutationImplementAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'good\n')
            if self.implement_calls == 1:
                write_text(self.project_root / '.auto-agents' / 'config.json', '{"mutated": true}\n')
                summary = 'implemented with first-attempt config mutation\n'
            else:
                summary = 'implemented clean retry\n'
        elif request.stage == 'review':
            summary = 'DECISION: pass\nLooks good.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RecoveringProtectedInputMutationImplementAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_input_before_review = ''

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'good\n')
            if self.implement_calls == 1:
                write_text(self.project_root / '.auto-agents' / 'docs' / 'review.md', 'mutated review\n')
                write_text(self.project_root / 'specs' / '2026-05-07-iter-01.md', 'mutated spec\n')
                summary = 'implemented with first-attempt protected input mutation\n'
            else:
                summary = 'implemented clean retry\n'
        elif request.stage == 'review':
            self.review_input_before_review = (self.project_root / '.auto-agents' / 'docs' / 'review.md').read_text(encoding='utf-8')
            summary = 'DECISION: pass\nLooks good.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RecoveringStagedPublicSpecMutationImplementAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.second_attempt_spec_status = ''

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'good\n')
            if self.implement_calls == 1:
                write_text(self.project_root / 'spec.md', '# Unauthorized staged public spec change\n')
                subprocess.run(['git', 'add', '--', 'spec.md'], cwd=str(self.project_root), check=True, text=True, capture_output=True)
                summary = 'implemented with a staged protected spec mutation\n'
            else:
                self.second_attempt_spec_status = subprocess.run(['git', 'status', '--short', '--', 'spec.md'], cwd=str(self.project_root), check=True, text=True, encoding='utf-8', capture_output=True).stdout
                summary = 'implemented clean retry\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class PublicSpecImplementAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.implement_prompt = ''

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            self.implement_prompt = request.prompt
            write_text(self.project_root / 'artifact.txt', 'good\n')
            write_text(self.project_root / 'spec.md', '# Updated public product spec\n')
            summary = 'updated the declared public product spec\n'
        elif request.stage == 'review':
            summary = 'DECISION: pass\nLooks good.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RecoveringHistoryMutationImplementAdapter:

    def __init__(self, project_root: Path, archive_path: Path) -> None:
        self.project_root = project_root
        self.archive_path = archive_path
        self.implement_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'good\n')
            if self.implement_calls == 1:
                write_text(self.archive_path, '{"tasks": []}\n')
                summary = 'implemented with first-attempt history mutation\n'
            else:
                summary = 'implemented clean retry\n'
        elif request.stage == 'review':
            summary = 'DECISION: pass\nLooks good.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class ReadmeProposalMutationAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.calls = 0

    def run(self, request):
        if request.stage == 'readme':
            self.calls += 1
            write_text(self.project_root / 'README.md', '# premature write\n')
            summary = 'proposal mutated readme\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RetryingVerificationCommandAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0

    def run(self, request):
        if request.stage == 'plan':
            self.plan_calls += 1
            target = 'tests/test_missing.py' if self.plan_calls == 1 else 'tests/test_ok.py'
            write_json(task_plan_path(self.project_root), {'test_strategy': 'python-pytest', 'verification_commands': [f'conda run -p ./.conda python -m pytest -q {target}'], 'tasks': [{'task_id': 'task-001', 'title': 'Add CLI entrypoint', 'description': 'Add a runnable command line entrypoint.', 'acceptance': ['`python -m demo --help` exits successfully.'], 'status': 'pending', 'commit_message': ''}]})
            write_text(request.output_path, 'verification plan\n')
        else:
            write_text(request.output_path, f'{request.stage}\n')
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=request.output_path.read_text(encoding='utf-8').strip(), returncode=0)

class RetryingImplementAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            value = 'bad' if self.implement_calls == 1 else 'good'
            write_text(self.project_root / 'artifact.txt', value + '\n')
            write_text(request.output_path, f'implemented {value}\n')
            summary = f'implemented {value}'
        elif request.stage == 'review':
            current = (self.project_root / 'artifact.txt').read_text(encoding='utf-8').strip()
            decision = 'pass' if current == 'good' else 'fail'
            summary = f'DECISION: {decision}\nartifact is {current}\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class ResumeReviewAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            raise AssertionError('implement should not be called when resuming an interrupted task')
        if request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nresume review passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class BlockedRetryAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'fixed\n')
            summary = 'implemented fixed\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nblocked task recovered\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class ProviderReferenceRepairAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            reference_path = self.project_root / '.auto-agents' / 'docs' / 'provider_references' / 'apiyi_gpt_image_2.md'
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            write_text(reference_path, '# APIYI GPT-Image-2 Provider Reference\n\ngpt-image-2-vip uses POST /v1/images/generations and POST /v1/images/edits.\n')
            summary = 'updated provider reference\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nreview passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class TaskPlanRepairAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            payload = load_task_plan(self.project_root)
            for item in payload.get('tasks', []):
                if not isinstance(item, dict) or item.get('task_id') != 'task-001':
                    continue
                proofs = item.setdefault('requirement_proofs', [])
                if not proofs:
                    proofs.append({'requirement_id': 'REQ-001', 'oracle_index': 1, 'status': 'verified', 'evidence_refs': []})
                proofs[0]['evidence_refs'] = ['.auto-agents/docs/provider_references/apiyi_gpt_image_2.md']
            write_json(task_plan_path(self.project_root), payload)
            summary = 'updated task plan proof refs\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nreview passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class SequentialArtifactAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / f'artifact-{self.implement_calls}.txt', f'attempt-{self.implement_calls}\n')
            summary = f'implemented attempt {self.implement_calls}\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nsequential task passed review\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class VerifyBeforeReviewAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'bad\n')
            summary = 'implemented bad\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nreview should not run before verify passes\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class CachedReviewResumeAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            raise AssertionError('implement should not run when resuming cached review state')
        if request.stage == 'review':
            self.review_calls += 1
            raise AssertionError('review should be reused from cache when worktree is unchanged')
        summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class ReviewEffortAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_efforts = []

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            raise AssertionError('implement should not run when resuming for review effort checks')
        if request.stage == 'review':
            self.review_efforts.append(request.effort)
            summary = 'DECISION: pass\nreview passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RetryFeedbackAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_prompts = []
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_prompts.append(request.prompt)
            write_text(self.project_root / 'artifact.txt', 'bad\n')
            summary = 'implemented bad\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nreview passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class SplitPlanAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, request):
        if request.stage == 'plan':
            write_json(task_plan_path(self.project_root), {'test_strategy': 'python-pytest', 'verification_commands': ['true'], 'tasks': [{'task_id': 'task-child-a', 'title': 'First child', 'description': 'First split child.', 'acceptance': ['child a done'], 'status': 'pending', 'commit_message': '', 'parent_task_id': 'task-legacy', 'split_depth': 1}, {'task_id': 'task-child-b', 'title': 'Second child', 'description': 'Second split child.', 'acceptance': ['child b done'], 'status': 'pending', 'commit_message': '', 'parent_task_id': 'task-legacy', 'split_depth': 1}]})
            summary = 'plan split legacy task\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class StalePlanAuditRecoveryAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts = []
        self.review_prompts = []

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            if self.implement_calls == 2:
                write_text(self.project_root / 'tests' / 'test_plan_contract.py', "EXPECTED_TASK = 'task-child-a'\n")
            write_text(self.project_root / 'artifact.txt', f'attempt-{self.implement_calls}\n')
            summary = f'implemented attempt {self.implement_calls}\n'
        elif request.stage == 'review':
            self.review_calls += 1
            self.review_prompts.append(request.prompt)
            summary = 'DECISION: pass\nreview passed after stale test migration\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class StaleTaskStatusAuditRecoveryAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts = []
        self.review_prompts = []

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            if self.implement_calls == 2:
                write_text(self.project_root / 'tests' / 'test_status_contract.py', "EXPECTED = {\n    'task-080': {\n        'status': 'done',\n    },\n}\n")
            write_text(self.project_root / 'artifact.txt', f'attempt-{self.implement_calls}\n')
            summary = f'implemented attempt {self.implement_calls}\n'
        elif request.stage == 'review':
            self.review_calls += 1
            self.review_prompts.append(request.prompt)
            summary = 'DECISION: pass\nreview passed after stale task-status migration\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class SequencedVerifyFailureAdapter:

    def __init__(self, project_root: Path, values):
        self.project_root = project_root
        self.values = list(values)
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            index = min(self.implement_calls, len(self.values) - 1)
            value = self.values[index]
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', value + '\n')
            summary = f'implemented {value}\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nreview passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class MissingCondaFastFailAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'hello\n')
            summary = 'implemented hello\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nreview passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class MissingPytestTargetFastFailAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'hello\n')
            summary = 'implemented hello\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\nreview passed\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class PermanentReviewFailureAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            summary = 'implemented bad\n'
            write_text(self.project_root / 'artifact.txt', 'bad\n')
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: fail\nCore issue: health endpoint is not actually exercised.\n- Missing request test.\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class RepairReviewRecoveryAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.implement_prompts: list[str] = []

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            self.implement_prompts.append(request.prompt)
            write_text(self.project_root / 'artifact.txt', f'implementation round {self.implement_calls}\n')
            summary = f'implemented round {self.implement_calls}\n'
        elif request.stage == 'review':
            self.review_calls += 1
            if self.review_calls == 1:
                summary = 'DECISION: fail\nAcceptance proof is tautological; add two qualified candidates.\n'
            else:
                summary = 'DECISION: pass\nreview passed\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class AuditRecoveryAdapter:

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.plan_calls = 0
        self.implement_calls = 0
        self.provider_research_calls = 0
        self.review_calls = 0
        self.stage_calls: list[str] = []

    def run(self, request):
        self.stage_calls.append(request.stage)
        if request.stage == 'plan':
            self.plan_calls += 1
            write_json(task_plan_path(self.project_root), {'test_strategy': 'python-pytest', 'verification_commands': ['true'], 'tasks': [{'task_id': 'task-001', 'title': 'Existing done task', 'description': 'Already finished.', 'acceptance': ['done'], 'requirement_ids': [], 'status': 'done', 'commit_message': ''}, {'task_id': 'task-002', 'title': 'Cover requirement', 'description': 'Cover the missing mandatory requirement.', 'acceptance': ['coverage is explicit'], 'requirement_ids': ['REQ-001'], 'status': 'pending', 'commit_message': ''}]})
            summary = 'plan updated\n'
            write_text(request.output_path, summary)
        elif request.stage == 'provider_research':
            self.provider_research_calls += 1
            reference_path = self.project_root / '.auto-agents' / 'docs' / 'provider_references' / 'provider.md'
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            reference_lines = ['# Provider reference']
            for heading in PROVIDER_REFERENCE_V2_HEADINGS:
                reference_lines.extend(['', f'## {heading}', '', 'Not applicable: recovery fixture.'])
            write_text(reference_path, '\n'.join(reference_lines) + '\n')
            write_json(provider_references_lock_path(self.project_root), {'version': 1, 'references': {'provider': {'path': '.auto-agents/docs/provider_references/provider.md', 'status': 'verified', 'contract_version': PROVIDER_REFERENCE_CONTRACT_VERSION, 'retrieved_at': '2026-04-11T00:00:00Z', 'source_urls': ['https://example.com/official'], 'notes': ''}}})
            summary = 'provider research updated\n'
            write_text(request.output_path, summary)
        elif request.stage == 'implement':
            self.implement_calls += 1
            write_text(self.project_root / 'artifact.txt', 'modern_backend\n')
            service_path = self.project_root / 'app' / 'service.py'
            if service_path.exists():
                write_text(service_path, 'modern_backend = True\n')
            summary = 'implemented audit recovery\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: pass\naudit recovery review passed\n'
            write_text(request.output_path, summary)
        elif request.stage == 'readme':
            if 'Do NOT write the README yet. Only outline the planned sections.' in request.prompt:
                summary = '- Overview\n- Architecture\n- Usage\n'
            else:
                write_text(self.project_root / 'README.md', '# Demo\n## Overview\nRecovered project.\n## Architecture\nSimple test layout.\n## Usage\n```bash\npython -m demo\n```\n## Development\nRun tests.\n')
                summary = 'readme updated\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class IterationAdapter:
    """Adapter that tracks stage calls for iteration testing.

    On the plan stage it writes only the new active iteration tasks.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.stage_calls: list[str] = []

    def run(self, request):
        self.stage_calls.append(request.stage)
        if request.stage == 'clarify':
            write_text(request.output_path, 'Clarified iteration scope.\nREADY_TO_GENERATE\n')
        elif request.stage == 'plan':
            tp = task_plan_path(self.project_root)
            new_task = {'task_id': 'task-002', 'title': 'New iteration task', 'description': 'Task added in iteration.', 'acceptance': ['new feature works'], 'status': 'pending', 'commit_message': '', 'test_generated': True}
            write_json(tp, {'test_strategy': 'python-pytest', 'verification_steps': [{'kind': 'test', 'runner': 'pytest', 'targets': ['tests']}], 'tasks': [new_task]})
            write_text(request.output_path, 'iteration plan\n')
        elif request.stage == 'implement':
            write_text(self.project_root / 'iter_artifact.txt', 'done\n')
            write_text(request.output_path, 'implemented iteration task\n')
        elif request.stage == 'review':
            summary = 'DECISION: pass\niteration review passed\n'
            write_text(request.output_path, summary)
            return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)
        elif request.stage == 'readme':
            if 'Do NOT write the README yet. Only outline the planned sections.' in request.prompt:
                write_text(request.output_path, '- Overview\n- Architecture\n- Usage\n')
            else:
                readme_content = '# Demo\n## Overview\nA demo project.\n## Architecture\nSimple layout.\n## Usage\n```bash\npython main.py\n```\n## Development\nRun tests.\n'
                write_text(self.project_root / 'README.md', readme_content)
                write_text(request.output_path, 'readme updated\n')
        else:
            write_text(request.output_path, f'{request.stage}\n')
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=request.output_path.read_text(encoding='utf-8').strip(), returncode=0)

class RepeatReviewBlockerAdapter:
    """Implement touches code on every attempt; review always returns the same blockers.

    Used to trigger the scope-overflow fingerprint signal.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0

    def run(self, request):
        if request.stage == 'implement':
            self.implement_calls += 1
            (self.project_root / f'artifact-{self.implement_calls}.txt').write_text(f'attempt-{self.implement_calls}\n', encoding='utf-8')
            summary = f'implement attempt {self.implement_calls}\n'
            write_text(request.output_path, summary)
        elif request.stage == 'review':
            self.review_calls += 1
            summary = 'DECISION: fail\nCore issue: task bundles backend, API, and UI.\n- Split backend lifecycle from API surface.\n- Split workbench UI from server changes.\n'
            write_text(request.output_path, summary)
        else:
            summary = f'{request.stage}\n'
            write_text(request.output_path, summary)
        return AgentResult(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class VaryingReviewArbiterAdapter:
    """Implement touches code; review always fails with VARYING wording so the
    static fingerprint signal never matches; arbiter returns a configurable
    decision."""

    def __init__(self, project_root: Path, arbiter_decision: str='SPLIT', arbiter_text: Optional[str]=None) -> None:
        self.project_root = project_root
        self.implement_calls = 0
        self.review_calls = 0
        self.arbiter_calls = 0
        self.arbiter_decision = arbiter_decision
        self.arbiter_text = arbiter_text

    def run(self, request):
        from auto_agents.adapters.base import AgentResult as _AR
        if request.stage == 'implement':
            self.implement_calls += 1
            (self.project_root / f'artifact-{self.implement_calls}.txt').write_text(f'attempt-{self.implement_calls}\n', encoding='utf-8')
            summary = f'implement attempt {self.implement_calls}\n'
        elif request.stage == 'review':
            self.review_calls += 1
            summary = f'DECISION: fail\nThis is review #{self.review_calls} with unique wording {self.review_calls}.\nAcceptance criterion {self.review_calls} is not satisfied.\n'
        elif request.stage == 'arbiter':
            self.arbiter_calls += 1
            if self.arbiter_text is not None:
                summary = self.arbiter_text
            elif self.arbiter_decision == 'SPLIT':
                summary = 'DECISION: SPLIT\nRATIONALE: task spans backend and UI which keep alternating as blockers.\nSPLIT_AXIS:\n- backend: extract data layer change\n- UI: extract surface change\n'
            else:
                summary = 'DECISION: CONTINUE\nRATIONALE: implementer is close; one more sharp attempt should converge.\n'
        else:
            summary = f'{request.stage}\n'
        write_text(request.output_path, summary)
        return _AR(ok=True, command=['fake'], output_path=request.output_path, summary=summary.strip(), returncode=0)

class ScopeArbiterTests(unittest.TestCase):
    pass
if __name__ == '__main__':
    unittest.main()
