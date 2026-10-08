from __future__ import annotations
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.adapters.base import AgentAdapter
from auto_agents.config import conversation_history_path, design_md_path, frontend_design_lock_path, frontend_prototype_dir, frontend_prototype_variants_registry_path, load_run_state, requirements_trace_path, save_run_state
from auto_agents.cli import build_parser, main
from auto_agents.frontend_design import CatalogEntry, CatalogSnapshot, AwesomeDesignCatalogClient, FrontendDesignUnavailable, discover_existing_frontend, frontend_design_artifact_hashes, frontend_scope_requested, load_frontend_design_lock, parse_catalog_entries, selected_surface_specs, validate_catalog_selection, validate_frontend_design_artifacts, validate_prototype_manifest
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentRequest, AgentResult, AgentTermination, TaskSpec
from auto_agents.prototype_variants import candidate_variants, ensure_registry, gallery_html, load_registry, registry_variants, variant_dir
HTML = "<!doctype html><html><head><meta name='viewport' content='width=device-width'></head><body>ok</body></html>"

class PrototypeAdapter(AgentAdapter):

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def run(self, request: AgentRequest) -> AgentResult:
        self.calls.append(request.attempt_id)
        if request.attempt_id.startswith('prototype-select'):
            match = re.search('Write JSON only to: (.+)', request.prompt)
            selection_path = Path(match.group(1).strip()) if match else self.project_root / '.auto-agents/docs/frontend_design/selection.json'
            write_json(selection_path, {'selected_slug': 'alpha', 'candidates': [{'slug': 'alpha', 'score': 95, 'rationale': 'best domain fit', 'risks': []}, {'slug': 'beta', 'score': 82, 'rationale': 'good fallback', 'risks': []}, {'slug': 'gamma', 'score': 70, 'rationale': 'usable but generic', 'risks': []}]})
        elif request.attempt_id.startswith('prototype-generate'):
            match = re.search('Write all output only inside: (.+)', request.prompt)
            root = Path(match.group(1).strip()) if match else frontend_prototype_dir(self.project_root)
            variant_marker = root.parent.name
            variant_html = HTML.replace('ok', variant_marker)
            write_text(root / 'index.html', variant_html)
            trace = json.loads(requirements_trace_path(self.project_root).read_text(encoding='utf-8'))
            surfaces = selected_surface_specs(trace, max_pages=3)
            pages = []
            for index, surface in enumerate(surfaces, start=1):
                filename = 'home.html' if index == 1 else f'surface-{index}.html'
                write_text(root / filename, variant_html)
                pages.append({'id': surface['id'], 'title': surface['name'], 'route': surface['route'], 'html_ref': (root / filename).relative_to(self.project_root).as_posix(), 'requirement_ids': surface['requirement_ids']})
            write_json(root / 'manifest.json', {'version': 1, 'index_ref': (root / 'index.html').relative_to(self.project_root).as_posix(), 'viewports': ['1440x900', '390x844'], 'pages': pages})
        write_text(request.output_path, 'ok\n')
        return AgentResult(ok=True, command=['prototype-test'], output_path=request.output_path, summary='ok', stdout='ok', returncode=0)

def write_frontend_trace(project_root: Path) -> None:
    write_json(requirements_trace_path(project_root), {'version': 1, 'frontend_scope': {'requested': True, 'surfaces': [{'id': 'surface-home', 'name': 'Home', 'route': '/', 'priority': 'core', 'purpose': 'Primary landing page', 'key_states': ['default'], 'requirement_ids': ['REQ-001']}]}, 'requirements': []})

def write_frontend_fidelity_trace(project_root: Path) -> None:
    write_json(requirements_trace_path(project_root), {'version': 1, 'frontend_scope': {'requested': True, 'surfaces': [{'id': 'surface-home', 'name': 'Home', 'route': '/', 'priority': 'core', 'purpose': 'Primary landing page', 'key_states': ['default'], 'requirement_ids': ['REQ-001']}]}, 'frontend_surfaces': [{'name': 'Home', 'route': '/', 'prototype_refs': ['specs/prototype/home.html', 'DESIGN.md'], 'viewports': ['1440x900'], 'requirement_ids': ['REQ-001']}], 'requirements': [{'id': 'REQ-001', 'status': 'active', 'priority': 'mandatory', 'text': 'The home page must match the supplied prototype.', 'acceptance_oracles': ['The rendered home page matches the prototype.'], 'oracle_type': 'mixed', 'oracle_strength': 'human', 'evidence_boundary': 'system_boundary', 'forbidden_proxy_oracles': []}]})

def frontend_task(*, status: str='in_progress') -> TaskSpec:
    return TaskSpec(task_id='task-frontend', title='Implement the home surface', description='Implement the approved home surface.', acceptance=['The rendered home page matches the prototype.'], requirement_ids=['REQ-001'], status=status, evidence_preflight={'decision': 'READY', 'reason': 'Browser evidence is feasible.', 'checklist': ['Capture the rendered page.'], 'fingerprint': 'cached-ready'})

class FrontendDesignTests(unittest.TestCase):

    def test_changed_spec_does_not_reuse_interrupted_prototype(self):
        from auto_agents.prototype_recovery import PrototypeGenerationCheckpoint
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = PrototypeGenerationCheckpoint(root, 'run', {'spec_sha256': 'first'})
            draft = variant_dir(root, 'proto-old')
            write_text(draft / 'DESIGN.md', 'design')
            from auto_agents.frontend_design import sha256_file
            first.save(variant_id='proto-old', status='interrupted', phase='generation', design_sha256=sha256_file(draft / 'DESIGN.md'))
            self.assertEqual(first.resumable_variant_id(), 'proto-old')
            changed = PrototypeGenerationCheckpoint(root, 'run', {'spec_sha256': 'second'})
            self.assertEqual(changed.resumable_variant_id(), '')
            write_text(draft / 'DESIGN.md', 'changed design')
            self.assertEqual(first.resumable_variant_id(), '')
            self.assertTrue(draft.is_dir())

    def test_catalog_network_failure_uses_complete_cache_or_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = AwesomeDesignCatalogClient(root, repository='VoltAgent/awesome-design-md', requested_ref='main', timeout_seconds=1)
            with patch.object(client, '_resolve_ref', side_effect=urllib.error.URLError('offline')):
                with self.assertRaises(FrontendDesignUnavailable):
                    client.load()
            sha = 'c' * 40
            cache = root / '.auto-agents/cache/awesome-design-md' / sha
            write_text(cache / '.complete', 'done\n')
            write_text(cache / 'LICENSE', 'MIT\n')
            write_text(cache / 'design-md/alpha/DESIGN.md', '# Alpha\n')
            write_text(cache / 'README.md', '### SaaS\n- [**Alpha**](https://getdesign.md/alpha/design-md) - Cached design\n')
            with patch.object(client, '_resolve_ref', side_effect=urllib.error.URLError('offline')):
                snapshot = client.load()
            self.assertTrue(snapshot.from_cache)
            self.assertEqual(snapshot.commit_sha, sha)
            self.assertEqual(snapshot.entries[0].slug, 'alpha')

    def test_frontend_discovery_ignores_docs_and_detects_real_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_text(root / 'docs/example.html', HTML)
            write_text(root / 'tests/fixture.tsx', 'export const Fixture = () => null')
            write_text(root / '.tmp/conda-pkgs/python/idlelib/help.html', HTML)
            write_text(root / '.tmp-tests/browser/report.html', HTML)
            self.assertFalse(discover_existing_frontend(root).existing_frontend)
            write_text(root / 'src/pages/Home.tsx', 'export const Home = () => <main />')
            result = discover_existing_frontend(root)
            self.assertTrue(result.existing_frontend)
            self.assertIn('src/pages/Home.tsx', result.evidence)

    def test_approved_manifest_can_grow_beyond_generation_batch_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prototype = frontend_prototype_dir(root)
            write_text(prototype / 'index.html', HTML)
            pages = []
            for index in range(1, 5):
                page = prototype / f'surface-{index}.html'
                write_text(page, HTML)
                pages.append({'id': f'SURF-{index:03d}', 'title': f'Surface {index}', 'route': f'/surface-{index}', 'html_ref': page.relative_to(root).as_posix(), 'requirement_ids': [f'REQ-{index:03d}']})
            errors = validate_prototype_manifest(root, {'version': 1, 'index_ref': (prototype / 'index.html').relative_to(root).as_posix(), 'viewports': ['1440x900', '390x844'], 'pages': pages}, max_pages=3)
            self.assertEqual(errors, [])
            selected = selected_surface_specs({'frontend_scope': {'requested': True, 'surfaces': [{'id': page['id'], 'name': page['title'], 'route': page['route'], 'priority': 'core', 'requirement_ids': page['requirement_ids']} for page in pages]}}, max_pages=3)
            self.assertEqual(len(selected), 4)

    def test_catalog_parser_and_selection_require_unique_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for slug in ('alpha', 'beta', 'gamma'):
                write_text(root / f'design-md/{slug}/DESIGN.md', f'# {slug}\n')
            readme = '\n'.join(['### SaaS', '- [**Alpha**](https://getdesign.md/alpha/design-md) - Dense dashboard', '- [**Beta**](https://getdesign.md/beta/design-md) - Friendly workspace', '- [**Gamma**](https://getdesign.md/gamma/design-md) - Minimal product'])
            entries = parse_catalog_entries(readme, root)
            snapshot = CatalogSnapshot('VoltAgent/awesome-design-md', 'main', 'a' * 40, root, tuple(entries), False)
            selected, candidates = validate_catalog_selection({'selected_slug': 'alpha', 'candidates': [{'slug': 'alpha', 'score': 90, 'rationale': 'best', 'risks': []}, {'slug': 'beta', 'score': 80, 'rationale': 'second', 'risks': []}, {'slug': 'gamma', 'score': 70, 'rationale': 'third', 'risks': []}]}, snapshot)
            self.assertEqual(selected.slug, 'alpha')
            self.assertEqual(len(candidates), 3)
            with self.assertRaisesRegex(ValueError, 'unique highest-scoring'):
                validate_catalog_selection({'selected_slug': 'alpha', 'candidates': [{'slug': 'alpha', 'score': 90, 'rationale': 'best', 'risks': []}, {'slug': 'beta', 'score': 90, 'rationale': 'tie', 'risks': []}, {'slug': 'gamma', 'score': 70, 'rationale': 'third', 'risks': []}]}, snapshot)

    def test_manifest_rejects_remote_assets_in_page_and_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prototype = frontend_prototype_dir(root)
            write_text(prototype / 'index.html', HTML.replace('</body>', "<script src='https://cdn.test/x.js'></script></body>"))
            write_text(prototype / 'home.html', HTML.replace('</body>', "<img src='https://cdn.test/x.png'></body>"))
            payload = {'version': 1, 'index_ref': '.auto-agents/docs/frontend_prototype/index.html', 'viewports': ['1440x900'], 'pages': [{'id': 'home', 'title': 'Home', 'route': '/', 'html_ref': '.auto-agents/docs/frontend_prototype/home.html', 'requirement_ids': ['REQ-001']}]}
            errors = validate_prototype_manifest(root, payload, max_pages=3)
            self.assertTrue(any(('remote or file URL' in error for error in errors)))
            self.assertTrue(any(('script src' in error for error in errors)))
if __name__ == '__main__':
    unittest.main()
