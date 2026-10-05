"""An engine fault pauses business execution; it never starts an internal repair."""
from pathlib import Path
import hashlib
import json


class EngineFault(RuntimeError):
    def __init__(self, message, *, step_id='engine', evidence=None):
        super().__init__(message)
        self.step_id = step_id
        self.evidence = dict(evidence or {})


def engine_root():
    import os
    override = os.environ.get('AUTO_AGENTS_ENGINE_SOURCE_ROOT')
    if override:
        return Path(override)
    source = Path(__file__).resolve().parents[2]
    if (source / '.git').exists():
        return source
    # A regular local pip installation retains its source URL. Maintenance
    # edits that Git checkout, never the interpreter's site-packages directory.
    from importlib.metadata import distribution, PackageNotFoundError
    from urllib.parse import urlsplit, unquote
    try:
        raw = distribution('auto-agents').read_text('direct_url.json')
        url = urlsplit(json.loads(raw or '{}').get('url', ''))
        origin = Path(unquote(url.path))
        if url.scheme == 'file' and origin.is_dir() and (origin / '.git').exists():
            return origin
    except (PackageNotFoundError, OSError, ValueError):
        pass
    return source


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def request_engine_repair(orchestrator, payload):
    """Only an explicitly engine-owned repository route becomes an engine fault."""
    from .execution_binding import route_sources
    target = next((row.get('target_repository') for row in route_sources(payload)
                   if row.get('target_repository')), '')
    if not target:
        return False
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path(orchestrator.project_root) / path
    if path.resolve() != engine_root().resolve():
        return False
    raise EngineFault(payload.get('reason') or payload.get('summary') or 'Engine execution failed',
                      step_id='engine-route', evidence=payload)
