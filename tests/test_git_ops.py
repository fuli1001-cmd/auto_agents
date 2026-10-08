import hashlib
import os
import stat
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.gate_execution import repository_exclusion_paths
from auto_agents.git_ops import add_worktree, apply_checkpoint_application, begin_checkpoint_application, cherry_pick_no_commit, changed_entries, changed_paths, checkpoint_application_state, commit_all, commit_all_except, commit_changed_paths, commit_only_paths, delete_ref, detach_checkpoint_application, head_ref, list_worktrees, prove_legacy_applied_checkpoint, ref_exists, reconcile_managed_worktree, remove_worktree, update_ref, worktree_fingerprint
from auto_agents.io_utils import write_text
from auto_agents.models import RunState, TaskSpec

class GitOpsWorktreeTests(unittest.TestCase):

    @staticmethod
    def _configure_git_identity(project_root: Path) -> None:
        subprocess.run(['git', 'config', 'user.name', 'test'], cwd=str(project_root), check=True, text=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(project_root), check=True, text=True, capture_output=True)

    def test_changed_paths_preserve_quoted_and_special_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            names = ['数据.txt', 'literal -> name.txt', ' leading.txt', 'quote"file.txt', 'line\nbreak.txt']
            for name in names:
                (root / name).write_text('one\n', encoding='utf-8')
            self.assertEqual(sorted(changed_paths(root)), sorted(names))
            before = worktree_fingerprint(root)
            (root / '数据.txt').write_text('two\n', encoding='utf-8')
            self.assertNotEqual(worktree_fingerprint(root), before)

    def test_changed_entries_read_rename_destination_and_following_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            self._configure_git_identity(root)
            (root / 'old.txt').write_text('original\n', encoding='utf-8')
            commit_all(root, 'initial')
            destination = 'renamed -> 数据.txt'
            subprocess.run(['git', 'mv', 'old.txt', destination], cwd=root, check=True)
            (root / 'untracked.txt').write_text('new\n', encoding='utf-8')
            self.assertEqual(changed_entries(root), [('R ', destination), ('??', 'untracked.txt')])

    def test_worktree_fingerprint_distinguishes_file_modes_and_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            source = root / 'script.sh'
            source.write_text('exit 0\n', encoding='utf-8')
            source.chmod(420)
            before = worktree_fingerprint(root)
            source.chmod(493)
            self.assertNotEqual(worktree_fingerprint(root), before)
            for name in ('one', 'two'):
                (root / name).write_text('same content\n', encoding='utf-8')
            link = root / 'active'
            link.symlink_to('one')
            before = worktree_fingerprint(root)
            link.unlink()
            link.symlink_to('two')
            self.assertNotEqual(worktree_fingerprint(root), before)

class GitOpsTests(unittest.TestCase):

    @staticmethod
    def _git(project_root: Path, *args: str, text: bool=False) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env['GIT_OPTIONAL_LOCKS'] = '0'
        return subprocess.run(['git', *args], cwd=str(project_root), check=True, capture_output=True, text=text, env=env)

    @staticmethod
    def _configure_git_identity(project_root: Path) -> None:
        subprocess.run(['git', 'config', 'user.name', 'test'], cwd=str(project_root), check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(project_root), check=True, capture_output=True)

    def _legacy_applied_fixture(self, tmp: str) -> tuple[Path, dict[str, object], str, list[str], str, str]:
        project_root = Path(tmp) / 'legacy-proof'
        project_root.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=str(project_root), check=True, capture_output=True)
        self._configure_git_identity(project_root)
        (project_root / 'plain.bin').write_bytes(b'baseline\x00bytes\n')
        (project_root / 'script.sh').write_bytes(b'#!/bin/sh\nexit 1\n')
        (project_root / 'script.sh').chmod(420)
        (project_root / 'removed.txt').write_text('remove me\n', encoding='utf-8')
        baseline_sha = commit_all(project_root, 'test: legacy proof baseline')
        candidate_worktree = Path(tmp) / 'legacy-candidate'
        add_worktree(project_root, candidate_worktree, ref=baseline_sha)
        try:
            (candidate_worktree / 'plain.bin').write_bytes(b'retained\x00candidate\n')
            (candidate_worktree / 'script.sh').write_bytes(b'#!/bin/sh\nexit 0\n')
            (candidate_worktree / 'script.sh').chmod(493)
            os.symlink('plain.bin', candidate_worktree / 'plain-link')
            (candidate_worktree / 'removed.txt').unlink()
            candidate_sha = commit_all(candidate_worktree, 'test: retained legacy candidate')
        finally:
            remove_worktree(project_root, candidate_worktree)
        retained_ref = 'refs/auto-agents/tests/legacy-applied-proof'
        update_ref(project_root, retained_ref, candidate_sha)
        cherry_pick_no_commit(project_root, candidate_sha)
        paths = commit_changed_paths(project_root, candidate_sha)
        owner = 'legacy-owner'
        checkpoint: dict[str, object] = {'schema_version': 1, 'status': 'applied', 'task_id': owner, 'ref': retained_ref, 'commit_sha': candidate_sha, 'changed_paths': list(paths)}
        return (project_root, checkpoint, owner, paths, baseline_sha, retained_ref)

    def _repository_snapshot(self, project_root: Path, paths: list[str]) -> dict[str, object]:
        head = self._git(project_root, 'rev-parse', 'HEAD').stdout
        status = self._git(project_root, 'status', '--porcelain=v1', '-z', '-uall').stdout
        raw_index_path = self._git(project_root, 'rev-parse', '--git-path', 'index', text=True).stdout.strip()
        index_path = Path(raw_index_path)
        if not index_path.is_absolute():
            index_path = project_root / index_path
        owned: dict[str, object] = {}
        for path in sorted(set(paths)):
            candidate = project_root / path
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                owned[path] = {'kind': 'missing'}
                continue
            if stat.S_ISREG(details.st_mode):
                owned[path] = {'kind': 'file', 'mode': stat.S_IMODE(details.st_mode), 'content': candidate.read_bytes()}
            elif stat.S_ISLNK(details.st_mode):
                owned[path] = {'kind': 'symlink', 'mode': stat.S_IMODE(details.st_mode), 'content': os.fsencode(os.readlink(candidate))}
            else:
                owned[path] = {'kind': 'other', 'mode': stat.S_IMODE(details.st_mode)}
        return {'head': head, 'index': index_path.read_bytes(), 'status': status, 'owned': owned}

    @staticmethod
    def _prove(project_root: Path, checkpoint: dict[str, object], owner: str, *, state_owner: str='') -> dict[str, object]:
        with patch('auto_agents.git_ops._git', side_effect=AssertionError('legacy proof must use optional-lock-free Git reads')):
            return prove_legacy_applied_checkpoint(project_root, checkpoint, state_map_owner_task_id=state_owner or owner, intended_task_id=owner)

    def test_legacy_applied_proof_accepts_exact_ref_identity_bytes_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, checkpoint, owner, paths, _, _ = self._legacy_applied_fixture(tmp)
            before = self._repository_snapshot(project_root, paths)
            proof = self._prove(project_root, checkpoint, owner)
            self.assertTrue(proof['ok'], proof)
            self.assertEqual(proof['proof'], 'legacy_applied_checkpoint_exact_owner')
            self.assertEqual(proof['mismatch_codes'], [])
            self.assertTrue(proof['classification']['matches'])
            self.assertTrue(proof['owner']['matches'])
            self.assertEqual(proof['retained_identity']['ref_state'], 'matched')
            self.assertTrue(proof['retained_identity']['matches'])
            self.assertTrue(proof['changed_paths']['matches'])
            self.assertEqual(set(proof['expected_entries']), set(paths))
            self.assertEqual(proof['expected_entries']['script.sh']['mode'], '100755')
            self.assertEqual(proof['expected_entries']['plain-link']['kind'], 'symlink')
            self.assertEqual(proof['expected_entries']['removed.txt']['kind'], 'missing')
            self.assertEqual(self._repository_snapshot(project_root, paths), before)

    def test_legacy_applied_proof_accepts_missing_ref_only_from_canonical_retained_commit_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, checkpoint, owner, paths, _, retained_ref = self._legacy_applied_fixture(tmp)
            delete_ref(project_root, retained_ref)
            checkpoint['application_transaction'] = None
            before = self._repository_snapshot(project_root, paths)
            proof = self._prove(project_root, checkpoint, owner)
            self.assertTrue(proof['ok'], proof)
            self.assertEqual(proof['classification']['application_transaction'], 'null')
            self.assertEqual(proof['retained_identity']['ref_state'], 'missing')
            self.assertTrue(proof['retained_identity']['missing_ref_fallback'])
            self.assertEqual(proof['retained_identity']['recorded_commit'], proof['retained_identity']['resolved_commit'])
            self.assertEqual(self._repository_snapshot(project_root, paths), before)

    def test_legacy_applied_proof_rejects_ref_object_owner_or_path_mismatch_without_mutation(self) -> None:
        cases = (('owner', 'owner_identity_mismatch'), ('ref', 'retained_ref_commit_mismatch'), ('object', 'retained_commit_unresolvable'), ('abbreviated-object', 'retained_commit_not_canonical'), ('recorded-path', 'recorded_changed_paths_mismatch'), ('duplicate-path', 'recorded_changed_paths_invalid'), ('current-path', 'current_changed_paths_mismatch'), ('transaction-present', 'legacy_classification_mismatch'))
        for case_name, expected_code in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                project_root, checkpoint, owner, paths, baseline_sha, retained_ref = self._legacy_applied_fixture(tmp)
                state_owner = owner
                snapshot_paths = list(paths)
                if case_name == 'owner':
                    state_owner = 'different-owner'
                elif case_name == 'ref':
                    update_ref(project_root, retained_ref, baseline_sha)
                elif case_name == 'object':
                    checkpoint['commit_sha'] = 'f' * 40
                elif case_name == 'abbreviated-object':
                    checkpoint['commit_sha'] = str(checkpoint['commit_sha'])[:12]
                elif case_name == 'recorded-path':
                    checkpoint['changed_paths'] = list(paths[:-1])
                elif case_name == 'duplicate-path':
                    checkpoint['changed_paths'] = [*paths, paths[0]]
                elif case_name == 'current-path':
                    (project_root / 'unrelated.txt').write_text('unrelated state\n', encoding='utf-8')
                    snapshot_paths.append('unrelated.txt')
                elif case_name == 'transaction-present':
                    checkpoint['application_transaction'] = {}
                before = self._repository_snapshot(project_root, snapshot_paths)
                proof = self._prove(project_root, checkpoint, owner, state_owner=state_owner)
                self.assertFalse(proof['ok'], proof)
                self.assertIn(expected_code, proof['mismatch_codes'])
                self.assertEqual(self._repository_snapshot(project_root, snapshot_paths), before)

    def test_legacy_applied_proof_rejects_owned_byte_mode_or_index_mismatch_without_mutation(self) -> None:
        cases = (('bytes', 'owned_worktree_entry_mismatch'), ('mode', 'owned_worktree_entry_mismatch'), ('symlink', 'owned_worktree_entry_mismatch'), ('index', 'owned_index_entry_mismatch'))
        for case_name, expected_code in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                project_root, checkpoint, owner, paths, _, _ = self._legacy_applied_fixture(tmp)
                if case_name == 'bytes':
                    (project_root / 'plain.bin').write_bytes(b'tampered\n')
                elif case_name == 'mode':
                    (project_root / 'script.sh').chmod(420)
                elif case_name == 'symlink':
                    (project_root / 'plain-link').unlink()
                    os.symlink('wrong-target', project_root / 'plain-link')
                elif case_name == 'index':
                    subprocess.run(['git', 'update-index', '--chmod=-x', '--', 'script.sh'], cwd=str(project_root), check=True, capture_output=True)
                before = self._repository_snapshot(project_root, paths)
                proof = self._prove(project_root, checkpoint, owner)
                self.assertFalse(proof['ok'], proof)
                self.assertIn(expected_code, proof['mismatch_codes'])
                self.assertTrue(proof['expected_entries'])
                self.assertTrue(proof['observed_entries'])
                self.assertTrue(proof['entry_mismatches'])
                self.assertEqual(self._repository_snapshot(project_root, paths), before)

    def test_legacy_applied_proof_rejects_replace_object_substitution_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, checkpoint, owner, paths, baseline_sha, _ = self._legacy_applied_fixture(tmp)
            candidate_sha = str(checkpoint['commit_sha'])
            candidate_bytes = (project_root / 'plain.bin').read_bytes()
            replacement_bytes = b'replacement candidate bytes\n'
            replacement_worktree = Path(tmp) / 'legacy-replacement'
            add_worktree(project_root, replacement_worktree, ref=candidate_sha)
            try:
                (replacement_worktree / 'plain.bin').write_bytes(replacement_bytes)
                replacement_tip = commit_all(replacement_worktree, 'test: replacement candidate tree')
                replacement_tree = self._git(replacement_worktree, 'rev-parse', f'{replacement_tip}^{{tree}}', text=True).stdout.strip()
                replacement_sha = subprocess.run(['git', 'commit-tree', replacement_tree, '-p', baseline_sha], cwd=str(project_root), check=True, capture_output=True, text=True, input='test: same-path replacement candidate\n').stdout.strip()
            finally:
                remove_worktree(project_root, replacement_worktree)
            self._git(project_root, 'replace', candidate_sha, replacement_sha)
            (project_root / 'plain.bin').write_bytes(replacement_bytes)
            self._git(project_root, 'add', '--', 'plain.bin')
            self.assertEqual(self._git(project_root, 'show', f'{candidate_sha}:plain.bin').stdout, replacement_bytes)
            self.assertEqual(self._git(project_root, '--no-replace-objects', 'show', f'{candidate_sha}:plain.bin').stdout, candidate_bytes)
            before = self._repository_snapshot(project_root, paths)
            proof = self._prove(project_root, checkpoint, owner)
            self.assertFalse(proof['ok'], proof)
            self.assertIn('owned_index_entry_mismatch', proof['mismatch_codes'])
            self.assertIn('owned_worktree_entry_mismatch', proof['mismatch_codes'])
            self.assertEqual(proof['expected_entries']['plain.bin']['content_sha256'], hashlib.sha256(candidate_bytes).hexdigest())
            self.assertEqual(proof['observed_entries']['plain.bin']['worktree']['content_sha256'], hashlib.sha256(replacement_bytes).hexdigest())
            self.assertIn({'path': 'plain.bin', 'index_matches': False, 'worktree_matches': False}, proof['entry_mismatches'])
            self.assertEqual(self._repository_snapshot(project_root, paths), before)

    def test_legacy_applied_proof_accepts_root_commit_gitlink_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'root-gitlink-proof'
            project_root.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=str(project_root), check=True, capture_output=True)
            self._configure_git_identity(project_root)
            subprocess.run(['git', 'commit', '--allow-empty', '-m', 'test: empty head'], cwd=str(project_root), check=True, capture_output=True)
            linked = project_root / 'linked'
            linked.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=str(linked), check=True, capture_output=True)
            self._configure_git_identity(linked)
            (linked / 'payload.txt').write_text('linked candidate\n', encoding='utf-8')
            linked_sha = commit_all(linked, 'test: linked candidate')
            subprocess.run(['git', 'update-index', '--add', '--cacheinfo', f'160000,{linked_sha},linked'], cwd=str(project_root), check=True, capture_output=True)
            tree_sha = self._git(project_root, 'write-tree', text=True).stdout.strip()
            root_candidate = subprocess.run(['git', 'commit-tree', tree_sha], cwd=str(project_root), check=True, capture_output=True, text=True, input='test: root gitlink candidate\n').stdout.strip()
            retained_ref = 'refs/auto-agents/tests/root-gitlink-proof'
            update_ref(project_root, retained_ref, root_candidate)
            owner = 'root-gitlink-owner'
            checkpoint: dict[str, object] = {'schema_version': 1, 'status': 'applied', 'task_id': owner, 'ref': retained_ref, 'commit_sha': root_candidate, 'changed_paths': ['linked']}
            before = self._repository_snapshot(project_root, ['linked'])
            linked_head_before = self._git(linked, 'rev-parse', 'HEAD').stdout
            proof = self._prove(project_root, checkpoint, owner)
            self.assertTrue(proof['ok'], proof)
            self.assertEqual(proof['retained_identity']['commit_parent'], '')
            self.assertEqual(proof['expected_entries']['linked']['mode'], '160000')
            self.assertEqual(proof['observed_entries']['linked']['index'], [{'mode': '160000', 'object_id': linked_sha, 'stage': '0'}])
            self.assertEqual(proof['observed_entries']['linked']['worktree'], {'kind': 'gitlink', 'mode': '160000', 'object_id': linked_sha})
            self.assertEqual(self._repository_snapshot(project_root, ['linked']), before)
            self.assertEqual(self._git(linked, 'rev-parse', 'HEAD').stdout, linked_head_before)

    def test_legacy_applied_proof_rejects_merge_commit_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root, checkpoint, owner, paths, baseline_sha, retained_ref = self._legacy_applied_fixture(tmp)
            candidate_sha = str(checkpoint['commit_sha'])
            candidate_tree = self._git(project_root, 'rev-parse', f'{candidate_sha}^{{tree}}', text=True).stdout.strip()
            merge_sha = subprocess.run(['git', 'commit-tree', candidate_tree, '-p', baseline_sha, '-p', candidate_sha], cwd=str(project_root), check=True, capture_output=True, text=True, input='test: ambiguous retained merge\n').stdout.strip()
            update_ref(project_root, retained_ref, merge_sha)
            checkpoint['commit_sha'] = merge_sha
            before = self._repository_snapshot(project_root, paths)
            proof = self._prove(project_root, checkpoint, owner)
            self.assertFalse(proof['ok'], proof)
            self.assertIn('retained_commit_parent_ambiguous', proof['mismatch_codes'])
            self.assertEqual(self._repository_snapshot(project_root, paths), before)

    def test_legacy_applied_proof_rejects_shallow_boundary_merge_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'shallow-merge-proof'
            project_root.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=str(project_root), check=True, capture_output=True)
            self._configure_git_identity(project_root)
            subprocess.run(['git', 'commit', '--allow-empty', '-m', 'test: first parent'], cwd=str(project_root), check=True, capture_output=True)
            first_parent = self._git(project_root, 'rev-parse', 'HEAD', text=True).stdout.strip()
            empty_tree = self._git(project_root, 'write-tree', text=True).stdout.strip()
            second_parent = subprocess.run(['git', 'commit-tree', empty_tree], cwd=str(project_root), check=True, capture_output=True, text=True, input='test: second parent\n').stdout.strip()
            owned_path = 'owned.txt'
            (project_root / owned_path).write_bytes(b'retained candidate\n')
            (project_root / owned_path).chmod(493)
            self._git(project_root, 'add', '--', owned_path)
            candidate_tree = self._git(project_root, 'write-tree', text=True).stdout.strip()
            merge_sha = subprocess.run(['git', 'commit-tree', candidate_tree, '-p', first_parent, '-p', second_parent], cwd=str(project_root), check=True, capture_output=True, text=True, input='test: shallow retained merge\n').stdout.strip()
            retained_ref = 'refs/auto-agents/tests/shallow-merge-proof'
            update_ref(project_root, retained_ref, merge_sha)
            checkpoint: dict[str, object] = {'schema_version': 1, 'status': 'applied', 'task_id': 'shallow-merge-owner', 'ref': retained_ref, 'commit_sha': merge_sha, 'changed_paths': [owned_path]}
            shallow_name = self._git(project_root, 'rev-parse', '--git-path', 'shallow', text=True).stdout.strip()
            shallow_path = Path(shallow_name)
            if not shallow_path.is_absolute():
                shallow_path = project_root / shallow_path
            shallow_path.write_text(f'{merge_sha}\n', encoding='ascii')
            for parent_sha in (first_parent, second_parent):
                self._git(project_root, '--no-replace-objects', 'cat-file', '-e', f'{parent_sha}^{{commit}}')
            hidden_parents = self._git(project_root, '--no-replace-objects', 'rev-list', '--parents', '-n', '1', merge_sha, text=True).stdout.strip().split()
            self.assertEqual(hidden_parents, [merge_sha])
            before = self._repository_snapshot(project_root, [owned_path])
            proof = self._prove(project_root, checkpoint, 'shallow-merge-owner')
            self.assertFalse(proof['ok'], proof)
            self.assertIn('retained_commit_parent_ambiguous', proof['mismatch_codes'])
            self.assertEqual(proof['retained_identity']['commit_parents'], [first_parent, second_parent])
            self.assertEqual(proof['retained_identity']['commit_parent_count'], 2)
            self.assertEqual(self._repository_snapshot(project_root, [owned_path]), before)
if __name__ == '__main__':
    unittest.main()
