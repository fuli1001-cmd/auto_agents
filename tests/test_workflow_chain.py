from __future__ import annotations
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from auto_agents.cli import build_parser, main
from auto_agents.config import archived_run_state_path, create_session, list_sessions, load_run_state, load_session_state, save_run_state, save_session_state
from auto_agents.git_ops import commit_only_paths
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, SessionState
from auto_agents.session import Session
from auto_agents.workflow_chain import IterationSpecBuilder, WorkflowRef, WorkflowStore
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_session import _configure_git_identity, _confirm_collab_state, _make_project

def _commit_baseline(root: Path) -> None:
    _configure_git_identity(root)
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-m', 'chore: baseline'], cwd=root, check=True, text=True, capture_output=True)

class WorkflowStoreTests(unittest.TestCase):

    def test_atomic_text_write_preserves_existing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'document.md'
            path.write_text('before\n', encoding='utf-8')
            path.chmod(416)
            write_text(path, 'after\n')
            self.assertEqual(path.stat().st_mode & 511, 416)
            self.assertEqual(path.read_text(encoding='utf-8'), 'after\n')

    def test_event_sequence_reloads_durable_head_for_stale_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            store = WorkflowStore(root)
            created = store.create_root(WorkflowRef('collab', 'session-a'))
            first = store.load(created.workflow_id)
            stale = store.load(created.workflow_id)
            store.append_event(first, 'first')
            store.append_event(stale, 'second')
            events = sorted((store.workflow_root(created.workflow_id) / 'events').glob('*.json'))
            sequences = [json.loads(path.read_text())['sequence'] for path in events]
            self.assertEqual(sequences, [1, 2, 3])
            self.assertEqual(store.load(created.workflow_id).event_sequence, 3)

    def test_stale_parent_lifecycle_updates_preserve_the_child_journal_head(self) -> None:
        for action in ('interrupt', 'resume', 'complete'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                store = WorkflowStore(Path(tmp))
                original = store.create_root(WorkflowRef('run', 'run-a'))
                stale = store.load(original.workflow_id)
                child = store.load(original.workflow_id)
                child.active_frame = WorkflowRef('provider_resolve', 'child-a')
                store.save(child)
                event = store.append_event(child, 'provider_resolve_child_started', details={'session_id': 'child-a'})
                if action == 'interrupt':
                    store.mark_recovery_required(stale, reason='run interrupted by SIGINT')
                elif action == 'resume':
                    store.begin_resume(stale)
                else:
                    store.complete(stale)
                events = store.events(original.workflow_id)
                self.assertEqual([e['sequence'] for e in events], [1, 2, 3])
                self.assertEqual(events[-1]['previous_event_sha256'], event['event_sha256'])
                self.assertEqual(store.load(original.workflow_id).active_frame, child.active_frame)
                store.snapshot_path(original.workflow_id).unlink()
                rebuilt = store.load(original.workflow_id)
                self.assertEqual(rebuilt.event_sequence, 3)
                self.assertEqual(rebuilt.active_frame, child.active_frame)

    def test_display_journal_loss_does_not_hide_authoritative_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            store = WorkflowStore(root)
            snapshot = store.create_root(WorkflowRef('collab', 'session-a'))
            store.append_event(snapshot, 'diagnostic_progress', details={'step': 1})
            for path in (store.workflow_root(snapshot.workflow_id) / 'events').glob('*.json'):
                path.unlink()
            self.assertEqual([item['sequence'] for item in store.events(snapshot.workflow_id)], [1, 2])

    def test_authoritative_journal_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            store = WorkflowStore(root)
            snapshot = store.create_root(WorkflowRef('collab', 'session-a'))
            from auto_agents.business_state import BusinessStore
            with BusinessStore(root).connect() as db:
                db.execute("UPDATE records SET payload='{}' WHERE path LIKE ?", ('workflows/' + snapshot.workflow_id + '/events/%',))
            with self.assertRaisesRegex(RuntimeError, 'hash chain is invalid'):
                store.events(snapshot.workflow_id)

    def test_corrupt_snapshot_is_rebuilt_from_hash_chained_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            store = WorkflowStore(root)
            snapshot = store.create_root(WorkflowRef('collab', 'session-a'))
            store.append_event(snapshot, 'diagnostic_progress', details={'step': 1})
            store.snapshot_path(snapshot.workflow_id).write_text('{broken', encoding='utf-8')
            rebuilt = store.load(snapshot.workflow_id)
            self.assertEqual(rebuilt.root, WorkflowRef('collab', 'session-a'))
            self.assertEqual(rebuilt.event_sequence, 2)
            self.assertEqual(len(store.events(snapshot.workflow_id)), 2)

class RoutedWorkflowTests(unittest.TestCase):
    pass
if __name__ == '__main__':
    unittest.main()
