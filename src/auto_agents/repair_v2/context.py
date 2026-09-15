"""Bounded controller-collected source context, avoiding repeated CLI discovery."""
import json
from pathlib import Path
import subprocess

from .workspace import git, inventory


def source_context(root, base, *, limit=32000):
    root = Path(root)
    files = inventory(root)
    try:
        changed = git(root, 'diff', '--name-only', base, '--').splitlines()
        patch = git(root, 'diff', '--no-ext-diff', '--no-textconv', base, '--')
    except subprocess.CalledProcessError:
        changed, patch = [], ''
    selected = list(files) if len(files) <= 12 else [name for name in changed if name in files]
    contents, used = {}, 0
    for name in selected:
        path = root / name
        if files[name][0] != 'file' or path.stat().st_size > limit - used:
            continue
        try: text = path.read_text()
        except (UnicodeError, OSError): continue
        if '\0' in text or len(text) + used > limit: continue
        contents[name] = text
        used += len(text)
    return json.dumps({'base': base, 'file_count': len(files), 'changed_files': changed,
        'files': contents, 'complete_repository_contents': len(contents) == len(files),
        'diff': patch[:limit], 'diff_truncated': len(patch) > limit,
        'note': 'These are actual bytes collected by the controller from the current candidate. '
                'Files not included remain available in the repository. Inspect omitted dependencies when needed.'},
        ensure_ascii=False)
