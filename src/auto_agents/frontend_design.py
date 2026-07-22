from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Mapping, Optional, Tuple

from .config import (
    auto_gitignore_path,
    design_md_path,
    frontend_design_cache_dir,
    frontend_design_lock_path,
    frontend_prototype_dir,
)
from .io_utils import read_json, read_text, write_json, write_text
from .frontend_fidelity import frontend_fidelity_requirement_ids


FRONTEND_SCOPE_FIELD = "frontend_scope"
FRONTEND_DESIGN_LOCK_VERSION = 1
CATALOG_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
CATALOG_MAX_FILE_BYTES = 512 * 1024
CATALOG_MAX_EXTRACTED_BYTES = 12 * 1024 * 1024
CATALOG_ALLOWED_FILES = {"README.md", "LICENSE"}

_IGNORED_SCAN_DIRS = {
    ".auto-agents",
    ".git",
    ".github",
    ".next",
    ".nuxt",
    ".output",
    ".svelte-kit",
    ".venv",
    ".conda",
    "build",
    "coverage",
    "dist",
    "docs",
    "node_modules",
    "spec",
    "specs",
    "test",
    "tests",
    "vendor",
}
_DIRECT_SURFACE_SUFFIXES = {
    ".astro",
    ".ejs",
    ".hbs",
    ".htm",
    ".html",
    ".jinja",
    ".jinja2",
    ".svelte",
    ".vue",
}
_UI_SOURCE_SUFFIXES = {".jsx", ".tsx"}
_UI_PATH_PARTS = {"app", "pages", "routes", "screens", "templates", "views"}
_UI_ENTRY_STEMS = {"app", "index", "layout", "main", "page", "root"}
_REMOTE_ASSET_PATTERN = re.compile(
    r"(?:src|href)\s*=\s*['\"]\s*(?:https?:|//|file:)|url\(\s*['\"]?\s*(?:https?:|//|file:)",
    re.IGNORECASE,
)
_SCRIPT_SRC_PATTERN = re.compile(r"<script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_CATALOG_ITEM_PATTERN = re.compile(
    r"^- \[\*\*(?P<name>.+?)\*\*\]\(https://getdesign\.md/(?P<slug>[^/]+)/design-md\)\s*-\s*(?P<description>.+)$"
)
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class FrontendDesignUnavailable(RuntimeError):
    """The design catalog is temporarily unavailable and no verified cache exists."""


@dataclass(frozen=True)
class FrontendDiscovery:
    existing_frontend: bool
    evidence: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "existing_frontend": self.existing_frontend,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    slug: str
    category: str
    description: str
    design_path: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "slug": self.slug,
            "category": self.category,
            "description": self.description,
            "design_path": self.design_path,
        }


@dataclass(frozen=True)
class CatalogSnapshot:
    repository: str
    requested_ref: str
    commit_sha: str
    root: Path
    entries: Tuple[CatalogEntry, ...]
    from_cache: bool


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def discover_existing_frontend(project_root: Path) -> FrontendDiscovery:
    root = project_root.resolve()
    evidence: List[str] = []
    if not root.exists():
        return FrontendDiscovery(False, ())

    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        dirnames[:] = [name for name in dirnames if name not in _IGNORED_SCAN_DIRS]
        try:
            relative_dir = current.relative_to(root)
        except ValueError:
            continue
        parts = {part.lower() for part in relative_dir.parts}
        for filename in filenames:
            path = current / filename
            suffix = path.suffix.lower()
            stem = path.stem.lower()
            is_surface = suffix in _DIRECT_SURFACE_SUFFIXES
            if suffix in _UI_SOURCE_SUFFIXES:
                is_surface = bool(parts & _UI_PATH_PARTS) or stem in _UI_ENTRY_STEMS
            if not is_surface:
                continue
            evidence.append(path.relative_to(root).as_posix())
            if len(evidence) >= 20:
                return FrontendDiscovery(True, tuple(evidence))
    return FrontendDiscovery(bool(evidence), tuple(evidence))


def trace_frontend_scope(trace_payload: object) -> Mapping[str, object]:
    if not isinstance(trace_payload, Mapping):
        return {}
    value = trace_payload.get(FRONTEND_SCOPE_FIELD)
    return value if isinstance(value, Mapping) else {}


def frontend_scope_requested(trace_payload: object) -> bool:
    scope = trace_frontend_scope(trace_payload)
    if scope.get("requested") is True:
        return True
    if isinstance(trace_payload, Mapping):
        surfaces = trace_payload.get("frontend_surfaces")
        return isinstance(surfaces, list) and bool(surfaces)
    return False


def frontend_scope_surfaces(trace_payload: object) -> List[Mapping[str, object]]:
    raw = trace_frontend_scope(trace_payload).get("surfaces")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def validate_frontend_scope(trace_payload: object) -> List[str]:
    if not isinstance(trace_payload, Mapping):
        return []
    raw = trace_payload.get(FRONTEND_SCOPE_FIELD)
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return [f"{FRONTEND_SCOPE_FIELD} must be an object when present"]
    errors: List[str] = []
    requirements = trace_payload.get("requirements")
    requirements_by_id = {
        str(item.get("id", "")).strip(): item
        for item in requirements
        if isinstance(item, Mapping) and str(item.get("id", "")).strip()
    } if isinstance(requirements, list) else {}
    if not isinstance(raw.get("requested"), bool):
        errors.append(f"{FRONTEND_SCOPE_FIELD}.requested must be a boolean")
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, list):
        errors.append(f"{FRONTEND_SCOPE_FIELD}.surfaces must be an array")
        return errors
    if raw.get("requested") is True and not surfaces:
        errors.append(f"{FRONTEND_SCOPE_FIELD}.surfaces must not be empty when requested=true")
    seen = set()
    for index, item in enumerate(surfaces, start=1):
        prefix = f"{FRONTEND_SCOPE_FIELD}.surfaces[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        surface_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not surface_id or surface_id in seen:
            errors.append(f"{prefix}.id must be non-empty and unique")
        seen.add(surface_id)
        if not name:
            errors.append(f"{prefix}.name must be non-empty")
        priority = str(item.get("priority", "")).strip()
        if priority not in {"core", "primary", "secondary", "optional"}:
            errors.append(f"{prefix}.priority must be core, primary, secondary, or optional")
        requirement_ids = item.get("requirement_ids")
        if not isinstance(requirement_ids, list) or not any(
            str(value).strip() for value in requirement_ids
        ):
            errors.append(f"{prefix}.requirement_ids must be a non-empty array")
        else:
            for raw_id in requirement_ids:
                requirement_id = str(raw_id).strip()
                requirement = requirements_by_id.get(requirement_id)
                if requirement is None:
                    errors.append(f"{prefix}.requirement_ids references unknown {requirement_id}")
                elif requirement.get("status") != "active" or requirement.get("priority") != "mandatory":
                    errors.append(
                        f"{prefix}.requirement_ids must reference active mandatory requirements; "
                        f"{requirement_id} is not active mandatory"
                    )
    return errors


def user_design_assets(project_root: Path, trace_payload: object, *, spec_text: str = "") -> List[str]:
    assets: List[str] = []
    root_design = design_md_path(project_root)
    lock = load_frontend_design_lock(project_root)
    lock_source = lock.get("source", {}) if isinstance(lock, dict) else {}
    managed_design = (
        isinstance(lock, dict)
        and isinstance(lock_source, Mapping)
        and lock_source.get("kind") == "awesome-design-md"
        and str(lock.get("design_path", "")) == "DESIGN.md"
        and root_design.is_file()
    )
    if root_design.is_file() and not managed_design:
        assets.append("DESIGN.md")

    if isinstance(trace_payload, Mapping):
        for surface in trace_payload.get("frontend_surfaces", []) or []:
            if not isinstance(surface, Mapping):
                continue
            for raw_ref in surface.get("prototype_refs", []) or []:
                ref = str(raw_ref).strip()
                if not ref:
                    continue
                if ref.startswith(".auto-agents/docs/frontend_prototype/"):
                    continue
                candidate = Path(ref)
                if not candidate.is_absolute():
                    candidate = project_root / candidate
                if candidate.exists() and ref not in assets:
                    assets.append(ref)

    for match in re.findall(r"https?://[^\s)>'\"]*figma\.com/[^\s)>'\"]+", str(spec_text or ""), re.IGNORECASE):
        if match not in assets:
            assets.append(match)
    return assets


def load_frontend_design_lock(project_root: Path) -> dict:
    payload = read_json(frontend_design_lock_path(project_root), default={})
    return payload if isinstance(payload, dict) else {}


def frontend_design_contract_payload(lock_payload: Mapping[str, object]) -> Dict[str, object]:
    return {
        "version": lock_payload.get("version"),
        "source": lock_payload.get("source"),
        "candidates": lock_payload.get("candidates"),
        "design_path": lock_payload.get("design_path"),
        "selection_path": lock_payload.get("selection_path"),
        "prototype": lock_payload.get("prototype"),
        "artifact_sha256": lock_payload.get("artifact_sha256"),
    }


def frontend_design_contract_sha256(lock_payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        frontend_design_contract_payload(lock_payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def approved_frontend_design(project_root: Path) -> bool:
    lock = load_frontend_design_lock(project_root)
    if lock.get("status") != "approved":
        return False
    expected = str(lock.get("contract_sha256", ""))
    if not expected or expected != frontend_design_contract_sha256(lock):
        return False
    return not validate_frontend_design_artifacts(project_root, lock, require_approved=False)


def frontend_design_artifact_hashes(project_root: Path) -> Dict[str, str]:
    root = project_root.resolve()
    paths: List[Path] = []
    design = design_md_path(root)
    if design.is_file():
        paths.append(design)
    design_docs = root / ".auto-agents" / "docs" / "frontend_design"
    if design_docs.is_dir():
        paths.extend(path for path in design_docs.rglob("*") if path.is_file())
    prototype_root = frontend_prototype_dir(root)
    if prototype_root.is_dir():
        paths.extend(path for path in prototype_root.rglob("*") if path.is_file())
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(paths)
    }


def validate_frontend_design_artifacts(
    project_root: Path,
    lock_payload: object,
    *,
    require_approved: bool,
    max_pages: int = 3,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(lock_payload, Mapping):
        return ["frontend design lock must be an object"]
    if int(lock_payload.get("version", 0) or 0) != FRONTEND_DESIGN_LOCK_VERSION:
        errors.append("frontend design lock version must be 1")
    status = str(lock_payload.get("status", ""))
    if status not in {"pending_approval", "approved"}:
        errors.append("frontend design lock status must be pending_approval or approved")
    if require_approved and status != "approved":
        errors.append("frontend design contract has not been approved")

    design = design_md_path(project_root)
    source = lock_payload.get("source")
    design_ref = str(lock_payload.get("design_path", "")).strip()
    if design_ref and not design.is_file():
        errors.append("missing DESIGN.md")
    if not isinstance(source, Mapping):
        errors.append("frontend design lock source must be an object")
    elif source.get("kind") == "awesome-design-md":
        if design_ref != "DESIGN.md":
            errors.append("awesome-design-md source must lock DESIGN.md")
        expected = str(source.get("content_sha256", ""))
        if not expected or (design.is_file() and sha256_file(design) != expected):
            errors.append("DESIGN.md no longer matches the locked upstream bytes")
        license_ref = str(source.get("license_path", "")).strip()
        if not license_ref or not (project_root / license_ref).is_file():
            errors.append("awesome-design-md license notice is missing")

    manifest = frontend_prototype_dir(project_root) / "manifest.json"
    if not manifest.is_file():
        errors.append("missing frontend prototype manifest.json")
    else:
        errors.extend(
            validate_prototype_manifest(
                project_root,
                read_json(manifest, default={}),
                max_pages=max_pages,
            )
        )

    recorded_hashes = lock_payload.get("artifact_sha256")
    if not isinstance(recorded_hashes, Mapping):
        errors.append("frontend design lock artifact_sha256 must be an object")
    else:
        current = frontend_design_artifact_hashes(project_root)
        normalized = {str(key): str(value) for key, value in recorded_hashes.items()}
        if normalized != current:
            errors.append("approved frontend design artifacts have drifted from their locked hashes")

    if status == "approved":
        expected_contract = str(lock_payload.get("contract_sha256", ""))
        if expected_contract != frontend_design_contract_sha256(lock_payload):
            errors.append("frontend design contract_sha256 is stale")
    return errors


def validate_prototype_manifest(project_root: Path, payload: object, *, max_pages: int) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, Mapping):
        return ["frontend prototype manifest must be an object"]
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        return ["frontend prototype manifest pages must be a non-empty array"]
    if len(pages) > max_pages:
        errors.append(f"frontend prototype manifest may contain at most {max_pages} pages")
    viewports = payload.get("viewports")
    if not isinstance(viewports, list) or not viewports or any(
        not isinstance(item, str) or not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", item)
        for item in (viewports or [])
    ):
        errors.append("frontend prototype manifest viewports must be WIDTHxHEIGHT strings")

    prototype_root = frontend_prototype_dir(project_root).resolve()
    seen_ids = set()
    seen_refs = set()
    for index, page in enumerate(pages, start=1):
        prefix = f"frontend prototype pages[{index}]"
        if not isinstance(page, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        page_id = str(page.get("id", "")).strip()
        title = str(page.get("title", "")).strip()
        ref = str(page.get("html_ref", "")).strip()
        if not page_id or page_id in seen_ids:
            errors.append(f"{prefix}.id must be non-empty and unique")
        seen_ids.add(page_id)
        if not title:
            errors.append(f"{prefix}.title must be non-empty")
        if not isinstance(page.get("route", ""), str):
            errors.append(f"{prefix}.route must be a string")
        requirement_ids = page.get("requirement_ids")
        if not isinstance(requirement_ids, list) or not any(
            str(value).strip() for value in (requirement_ids or [])
        ):
            errors.append(f"{prefix}.requirement_ids must be a non-empty array")
        if not ref or ref in seen_refs:
            errors.append(f"{prefix}.html_ref must be non-empty and unique")
            continue
        seen_refs.add(ref)
        candidate = (project_root / ref).resolve()
        try:
            candidate.relative_to(prototype_root)
        except ValueError:
            errors.append(f"{prefix}.html_ref must stay inside the prototype directory")
            continue
        if candidate.suffix.lower() != ".html" or not candidate.is_file():
            errors.append(f"{prefix}.html_ref must reference an existing HTML file")
            continue
        html = read_text(candidate)
        if _REMOTE_ASSET_PATTERN.search(html):
            errors.append(f"{prefix}.html_ref contains a remote or file URL")
        if _SCRIPT_SRC_PATTERN.search(html):
            errors.append(f"{prefix}.html_ref must not load script src dependencies")
        if re.search(r"<meta\b[^>]*name=['\"]viewport['\"]", html, re.IGNORECASE) is None:
            errors.append(f"{prefix}.html_ref must include a viewport meta tag")

    index_ref = str(payload.get("index_ref", "")).strip()
    index_path = (project_root / index_ref).resolve() if index_ref else None
    if index_path is None:
        errors.append("frontend prototype manifest index_ref must be non-empty")
    else:
        try:
            index_path.relative_to(prototype_root)
        except ValueError:
            errors.append("frontend prototype manifest index_ref must stay inside the prototype directory")
        else:
            if index_path.suffix.lower() != ".html" or not index_path.is_file():
                errors.append("frontend prototype manifest index_ref must reference an existing HTML file")
            else:
                html = read_text(index_path)
                if _REMOTE_ASSET_PATTERN.search(html):
                    errors.append("frontend prototype index_ref contains a remote or file URL")
                if _SCRIPT_SRC_PATTERN.search(html):
                    errors.append("frontend prototype index_ref must not load script src dependencies")
                if re.search(r"<meta\b[^>]*name=['\"]viewport['\"]", html, re.IGNORECASE) is None:
                    errors.append("frontend prototype index_ref must include a viewport meta tag")

    allowed_files = {prototype_root / "manifest.json"}
    if index_path is not None:
        allowed_files.add(index_path)
    for page in pages:
        if isinstance(page, Mapping) and str(page.get("html_ref", "")).strip():
            allowed_files.add((project_root / str(page["html_ref"])).resolve())
    if prototype_root.is_dir():
        for artifact in prototype_root.rglob("*"):
            if artifact.is_file() and artifact.resolve() not in allowed_files:
                errors.append(
                    "frontend prototype directory contains an unreferenced or non-standalone "
                    f"artifact: {artifact.relative_to(project_root).as_posix()}"
                )
    return errors


class AwesomeDesignCatalogClient:
    def __init__(
        self,
        project_root: Path,
        *,
        repository: str,
        requested_ref: str,
        timeout_seconds: int,
    ) -> None:
        if repository != "VoltAgent/awesome-design-md":
            raise ValueError("frontend design catalog_repository must be VoltAgent/awesome-design-md")
        self.project_root = project_root.resolve()
        self.repository = repository
        self.requested_ref = requested_ref
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.cache_root = frontend_design_cache_dir(self.project_root)

    def load(self) -> CatalogSnapshot:
        self._ensure_cache_is_ignored()
        try:
            sha = self._resolve_ref()
            root = self._ensure_snapshot(sha)
            return self._snapshot(root, sha, from_cache=False)
        except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError, urllib.error.URLError) as error:
            cached = self._latest_complete_cache()
            if cached is None:
                raise FrontendDesignUnavailable(
                    "awesome-design-md is unavailable and this project has no complete cached snapshot: "
                    f"{error}"
                ) from error
            return self._snapshot(cached, cached.name, from_cache=True)

    def _ensure_cache_is_ignored(self) -> None:
        path = auto_gitignore_path(self.project_root)
        lines = [line.strip() for line in read_text(path).splitlines() if line.strip()]
        if "cache/" in lines:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, "\n".join([*lines, "cache/"]) + "\n")

    def _request_bytes(self, url: str, *, limit: int) -> bytes:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "auto-agents"},
        )
        chunks: List[bytes] = []
        total = 0
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            while True:
                chunk = response.read(min(64 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"download exceeded {limit} bytes")
                chunks.append(chunk)
        return b"".join(chunks)

    def _resolve_ref(self) -> str:
        encoded_ref = urllib.parse.quote(self.requested_ref, safe="")
        raw = self._request_bytes(
            f"https://api.github.com/repos/{self.repository}/commits/{encoded_ref}",
            limit=1024 * 1024,
        )
        payload = json.loads(raw.decode("utf-8"))
        sha = str(payload.get("sha", "")).lower()
        if not _SHA_PATTERN.fullmatch(sha):
            raise ValueError("GitHub commit response did not contain a full SHA")
        return sha

    def _ensure_snapshot(self, sha: str) -> Path:
        target = self.cache_root / sha
        if (target / ".complete").is_file():
            return target
        archive = self._request_bytes(
            f"https://codeload.github.com/{self.repository}/tar.gz/{sha}",
            limit=CATALOG_MAX_ARCHIVE_BYTES,
        )
        self.cache_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        with tempfile.TemporaryDirectory(prefix="awesome-design-md-", dir=str(self.cache_root)) as raw_tmp:
            temp_root = Path(raw_tmp)
            archive_path = temp_root / "snapshot.tar.gz"
            archive_path.write_bytes(archive)
            extracted = temp_root / "content"
            extracted.mkdir()
            self._extract_snapshot(archive_path, extracted)
            if not (extracted / "README.md").is_file() or not (extracted / "LICENSE").is_file():
                raise ValueError("catalog archive is missing README.md or LICENSE")
            extracted.rename(target)
            (target / ".complete").write_text(utc_now_iso() + "\n", encoding="utf-8")
        return target

    @staticmethod
    def _extract_snapshot(archive_path: Path, target: Path) -> None:
        total = 0
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                parts = PurePosixPath(member.name).parts
                if len(parts) < 2:
                    continue
                relative = PurePosixPath(*parts[1:])
                allowed = relative.as_posix() in CATALOG_ALLOWED_FILES
                allowed = allowed or (
                    len(relative.parts) == 3
                    and relative.parts[0] == "design-md"
                    and relative.parts[2] == "DESIGN.md"
                )
                if not allowed:
                    continue
                if member.size < 0 or member.size > CATALOG_MAX_FILE_BYTES:
                    raise ValueError(f"catalog file has invalid size: {relative}")
                total += member.size
                if total > CATALOG_MAX_EXTRACTED_BYTES:
                    raise ValueError("catalog extracted content is too large")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"unable to read catalog file: {relative}")
                content = source.read(CATALOG_MAX_FILE_BYTES + 1)
                if len(content) != member.size:
                    raise ValueError(f"catalog file size changed while reading: {relative}")
                destination = target.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

    def _latest_complete_cache(self) -> Optional[Path]:
        if not self.cache_root.is_dir():
            return None
        requested_sha = self.requested_ref.lower()
        if _SHA_PATTERN.fullmatch(requested_sha):
            exact = self.cache_root / requested_sha
            return exact if exact.is_dir() and (exact / ".complete").is_file() else None
        candidates = [
            path
            for path in self.cache_root.iterdir()
            if path.is_dir() and _SHA_PATTERN.fullmatch(path.name) and (path / ".complete").is_file()
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None

    def _snapshot(self, root: Path, sha: str, *, from_cache: bool) -> CatalogSnapshot:
        entries = parse_catalog_entries(read_text(root / "README.md"), root)
        if not entries:
            raise ValueError("catalog README did not contain any usable DESIGN.md entries")
        return CatalogSnapshot(
            repository=self.repository,
            requested_ref=self.requested_ref,
            commit_sha=sha,
            root=root,
            entries=tuple(entries),
            from_cache=from_cache,
        )


def parse_catalog_entries(readme: str, snapshot_root: Path) -> List[CatalogEntry]:
    category = "Uncategorized"
    entries: List[CatalogEntry] = []
    seen = set()
    for raw_line in readme.splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            category = line[4:].strip()
            continue
        match = _CATALOG_ITEM_PATTERN.match(line)
        if not match:
            continue
        slug = match.group("slug").strip()
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", slug) or slug in seen:
            continue
        design_path = f"design-md/{slug}/DESIGN.md"
        if not (snapshot_root / design_path).is_file():
            continue
        entries.append(
            CatalogEntry(
                name=match.group("name").strip(),
                slug=slug,
                category=category,
                description=match.group("description").strip(),
                design_path=design_path,
            )
        )
        seen.add(slug)
    return entries


def validate_catalog_selection(
    payload: object,
    snapshot: CatalogSnapshot,
    *,
    candidate_count: int = 3,
) -> Tuple[CatalogEntry, List[Dict[str, object]]]:
    if not isinstance(payload, Mapping):
        raise ValueError("prototype selection must be a JSON object")
    raw_candidates = payload.get("candidates")
    selected_slug = str(payload.get("selected_slug", "")).strip()
    if not isinstance(raw_candidates, list) or len(raw_candidates) != candidate_count:
        raise ValueError(f"prototype selection must contain exactly {candidate_count} candidates")
    by_slug = {entry.slug: entry for entry in snapshot.entries}
    normalized: List[Dict[str, object]] = []
    seen = set()
    for index, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"prototype candidate {index} must be an object")
        slug = str(raw.get("slug", "")).strip()
        if slug not in by_slug or slug in seen:
            raise ValueError(f"prototype candidate {index} has an unknown or duplicate slug")
        try:
            score = int(raw.get("score"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"prototype candidate {index} score must be an integer") from error
        if score < 0 or score > 100:
            raise ValueError(f"prototype candidate {index} score must be from 0 to 100")
        rationale = str(raw.get("rationale", "")).strip()
        risks = [str(item).strip() for item in raw.get("risks", []) or [] if str(item).strip()]
        if not rationale:
            raise ValueError(f"prototype candidate {index} rationale must be non-empty")
        normalized.append({"slug": slug, "score": score, "rationale": rationale, "risks": risks})
        seen.add(slug)
    if selected_slug not in seen:
        raise ValueError("selected_slug must name one of the candidates")
    winner_score = max(int(item["score"]) for item in normalized)
    winners = [item for item in normalized if int(item["score"]) == winner_score]
    if len(winners) != 1 or winners[0]["slug"] != selected_slug:
        raise ValueError("selected_slug must be the unique highest-scoring candidate")
    return by_slug[selected_slug], normalized


def selected_surface_specs(trace_payload: object, *, max_pages: int) -> List[Dict[str, object]]:
    surfaces = frontend_scope_surfaces(trace_payload)
    if not surfaces and isinstance(trace_payload, Mapping):
        legacy = trace_payload.get("frontend_surfaces")
        if isinstance(legacy, list):
            legacy_requirement_ids = frontend_fidelity_requirement_ids(trace_payload)
            surfaces = [
                {
                    "id": str(item.get("id", "")).strip() or f"surface-{index}",
                    "name": str(item.get("name", "")).strip() or f"Surface {index}",
                    "route": str(item.get("route", "")).strip(),
                    "priority": "core" if index <= max_pages else "secondary",
                    "purpose": "",
                    "key_states": [],
                    "requirement_ids": list(
                        item.get("requirement_ids", []) or legacy_requirement_ids
                    ),
                }
                for index, item in enumerate(legacy, start=1)
                if isinstance(item, Mapping)
            ]

    def priority(item: Mapping[str, object]) -> Tuple[int, str]:
        raw = str(item.get("priority", "secondary")).strip().lower()
        rank = {"core": 0, "primary": 0, "secondary": 1, "optional": 2}.get(raw, 1)
        return rank, str(item.get("id", item.get("name", "")))

    selected: List[Dict[str, object]] = []
    for index, surface in enumerate(sorted(surfaces, key=priority)[:max_pages], start=1):
        surface_id = str(surface.get("id", "")).strip() or f"surface-{index}"
        selected.append(
            {
                "id": surface_id,
                "name": str(surface.get("name", "")).strip() or surface_id,
                "route": str(surface.get("route", "")).strip(),
                "purpose": str(surface.get("purpose", "")).strip(),
                "key_states": [
                    str(item).strip()
                    for item in surface.get("key_states", []) or []
                    if str(item).strip()
                ],
                "requirement_ids": [
                    str(item).strip()
                    for item in surface.get("requirement_ids", []) or []
                    if str(item).strip()
                ],
            }
        )
    return selected


def derived_frontend_surfaces(lock_payload: Mapping[str, object]) -> List[Dict[str, object]]:
    prototype = lock_payload.get("prototype")
    pages = prototype.get("pages") if isinstance(prototype, Mapping) else None
    viewports = prototype.get("viewports") if isinstance(prototype, Mapping) else None
    if not isinstance(pages, list):
        return []
    result: List[Dict[str, object]] = []
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        result.append(
            {
                "name": str(page.get("title", page.get("id", ""))).strip(),
                "route": str(page.get("route", "")).strip(),
                "prototype_refs": [str(page.get("html_ref", "")).strip()],
                "viewports": list(viewports) if isinstance(viewports, list) else [],
                "fidelity": "layout-and-style",
                "requirement_ids": [
                    str(item).strip()
                    for item in page.get("requirement_ids", []) or []
                    if str(item).strip()
                ],
            }
        )
    return result
