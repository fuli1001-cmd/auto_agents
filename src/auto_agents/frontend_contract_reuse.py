"""Bind implementation requirements to existing, immutable visual approvals."""
import copy
import re
from pathlib import Path

from .config import frontend_design_lock_path, requirements_trace_path, save_run_state
from .git_ops import _git_bytes
from .io_utils import read_json, read_text, write_text


class BoundDesignLock(dict):
    """Derived bindings are not serialized into the approved lock."""
    def __init__(self, payload, bindings):
        super().__init__(payload)
        self.implementation_bindings = bindings


def bind(project_root, payload, trace=None):
    from .frontend_design import (frontend_design_contract_sha256,
        selected_surface_specs, validate_frontend_design_artifacts)
    from .frontend_fidelity import frontend_requirement_ids_are_preservation_only
    if not isinstance(payload, dict):
        return payload
    if (payload.get('status') != 'approved'
            or payload.get('contract_sha256') != frontend_design_contract_sha256(payload)
            or validate_frontend_design_artifacts(project_root, payload, require_approved=True)):
        return payload
    trace = trace if trace is not None else read_json(requirements_trace_path(project_root), default={})
    if not isinstance(trace, dict):
        return payload
    scope = trace.get('frontend_scope', {})
    if isinstance(scope, dict) and scope.get('design_action') not in {None, '', 'reuse', 'reuse_approved'}:
        return payload
    refs = trace.get('frontend_surfaces', [])
    if not isinstance(refs, list):
        return payload
    prototype = payload.get('prototype', {})
    raw_pages = prototype.get('pages') if isinstance(prototype, dict) else None
    rows = trace.get('requirements')
    if not isinstance(raw_pages, list) or not isinstance(rows, list):
        return payload
    pages = {page['id']: page for page in raw_pages
             if isinstance(page, dict) and isinstance(page.get('id'), str)
             and isinstance(page.get('html_ref'), str)}
    locked_viewports = prototype.get('viewports', [])
    if (not isinstance(locked_viewports, list)
            or any(not isinstance(v, str) for v in locked_viewports)
            or any(not isinstance(page.get('requirement_ids'), list)
                   or any(not isinstance(rid, str) for rid in page['requirement_ids'])
                   for page in pages.values())):
        return payload
    active = {row.get('id') for row in rows
              if isinstance(row, dict) and isinstance(row.get('id'), str) and row.get('status') == 'active'}
    covered = {rid for page in pages.values() for rid in page.get('requirement_ids', [])}
    selected = selected_surface_specs(trace, max_pages=3)
    missing = [rid for surface in selected for rid in surface['requirement_ids'] if rid not in covered]
    for row in rows:
        if not isinstance(row, dict) or row.get('id') not in missing:
            continue
        text = ' '.join([str(row.get('text', '')), *map(str, row.get('acceptance_oracles', []))]).lower()
        text = re.sub(r'no redesign|do not redesign|不(?:得)?(?:重新)?设计|不(?:得)?新增交互|无新增交互|'
                      r'(?:no|without|do not add|must not add) (?:a )?new (?:prototype|interaction|ui|visual|button)', '', text)
        if re.search(r'\bnew(?:ly approved)? (?:prototype|interaction|ui|visual|button)\b|'
                     r'\bredesign\b|新增交互|重新设计|重做原型|重新审批原型', text):
            return payload
    # Preserve the existing handling of scope that requests only preservation,
    # rather than implementing behavior against an existing visual contract.
    if missing and frontend_requirement_ids_are_preservation_only(trace, missing):
        return payload
    bindings = {}
    for surface in selected:
        page = pages.get(surface['id'])
        if not page or not page.get('route') or surface['route'] != page['route']:
            continue
        allowed = {page['html_ref'], 'DESIGN.md', prototype.get('manifest_ref'),
                   prototype.get('index_ref'), '.auto-agents/state/frontend_design.lock.json'}
        if surface.get('html_ref') and surface['html_ref'] != page['html_ref']:
            continue
        matches = [ref for ref in refs if isinstance(ref, dict)
                   and ref.get('route') == page['route']
                   and (ref.get('id') == page['id'] if ref.get('id') else ref.get('name') == surface['name'])]
        if len(matches) != 1:
            continue
        ref = matches[0]
        paths = ref.get('prototype_refs', [])
        viewports = ref.get('viewports', [])
        if (not isinstance(paths, list) or any(not isinstance(p, str) for p in paths)
                or page['html_ref'] not in paths or not set(paths) <= allowed
                or not isinstance(viewports, list) or any(not isinstance(v, str) for v in viewports)
                or not set(viewports) <= set(locked_viewports)):
            continue
        ids = surface['requirement_ids']
        if ids and set(ids) <= active:
            bindings[page['id']] = list(ids)
    return BoundDesignLock(payload, bindings) if bindings else payload


def recover(project_root, state):
    """Undo only the old controller's exact spurious reapproval mutation."""
    import json
    from .frontend_design import missing_frontend_design_contract_requirement_ids, selected_surface_specs
    from .workflow_chain import WorkflowRef, WorkflowStore, sha256_text
    context = state.resume_context
    if (state.current_stage != 'prototype' or state.tasks or state.rejected_stage
            or not context.get('frontend_design_contract_recovery')
            or not context.get('parent_handoff_id') or not context.get('iteration_spec_commit')):
        return False
    path = frontend_design_lock_path(project_root)
    current = read_json(path, default={})
    if current.get('status') != 'pending_approval' or not current.get('redesign_requested_at'):
        return False
    store = WorkflowStore(project_root)
    original = store.resolve_handoff_chain(context['parent_handoff_id'], workflow_id=context['workflow_id'])[-1]
    if original.target != 'run' or original.child != WorkflowRef('run', state.run_id):
        return False
    spec = Path(context.get('spec_file', ''))
    if (not spec.is_file() or spec.is_symlink()
            or not spec.resolve().is_relative_to(Path(project_root).resolve())
            or sha256_text(read_text(spec)) != context.get('iteration_spec_sha256')):
        return False
    revision = str(context['iteration_spec_commit'])
    if len(revision) != 40 or any(ch not in '0123456789abcdef' for ch in revision):
        return False
    saved = _git_bytes(Path(project_root), 'show', revision + ':.auto-agents/state/frontend_design.lock.json')
    if saved.returncode:
        return False
    try:
        approved = json.loads(saved.stdout)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(approved, dict) or approved.get('status') != 'approved':
        return False
    trace = read_json(requirements_trace_path(project_root), default={})
    required = [rid for surface in selected_surface_specs(trace, max_pages=3) for rid in surface['requirement_ids']]
    expected = copy.deepcopy(approved)
    expected.update(status='pending_approval', redesign_requested_at=current['redesign_requested_at'],
                    redesign_requirement_ids=missing_frontend_design_contract_requirement_ids(approved, required))
    for key in ('approved_at', 'approval', 'contract_sha256'):
        expected.pop(key, None)
    bound = bind(project_root, approved, trace)
    if (current != expected or not isinstance(bound, BoundDesignLock)
            or missing_frontend_design_contract_requirement_ids(bound, required)):
        return False
    receipt = {'revision': revision, 'lock_sha256': sha256_text(saved.stdout.decode()),
               'requirement_ids': current['redesign_requirement_ids']}
    # Restore the actual historical approval, never synthesize a new one.
    write_text(path, saved.stdout.decode())
    context.pop('frontend_design_contract_recovery', None)
    context['frontend_contract_reuse_recovery'] = receipt
    save_run_state(project_root, state)
    return True
