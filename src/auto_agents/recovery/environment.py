"""Prepare verification software from the installed controller's recipes."""
import json
import sys
from pathlib import Path


def prepare(store, trusted_root):
    from ..repair_worker import engine_environment
    from ..repair_dependencies import prepare_verification_dependency
    operator = store.root/'operator.json'
    config = json.loads(operator.read_text()) if operator.is_file() else {}
    config = {**config,'root':str(store.root),'python':config.get('python') or sys.executable}
    python, fingerprint = engine_environment(config,Path(trusted_root))
    # The engine's own compatibility corpus includes real JavaScript selection.
    # This recipe is owned by the trusted executor, never chosen by a candidate.
    prepare_verification_dependency(config,python,'vitest')
    return python
