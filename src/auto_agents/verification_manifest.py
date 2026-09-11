"""Trusted read-only manifest replay in the verification namespace."""
import hashlib
import json
from pathlib import Path
import sys

if __package__:
    from .verification_probes import PREFIX, matches
else:
    from verification_probes import PREFIX, matches


def path_digest(path):
    try:
        if path.is_symlink():
            return 'link:' + str(path.readlink())
        if path.is_dir():
            data = json.dumps(sorted(p.name for p in path.iterdir()), ensure_ascii=False, sort_keys=True).encode()
            return 'dir:' + hashlib.sha256(data).hexdigest()
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            return 'file:' + digest.hexdigest()
    except OSError:
        pass
    return None


def manifest_matches(root, manifest):
    if not manifest:
        return False
    for raw_path, expected in manifest.items():
        if str(raw_path).startswith(PREFIX):
            if not matches(root, str(raw_path), expected):
                return False
            continue
        relative = str(raw_path).replace("\\", "/").strip()
        denied = relative.startswith("?")
        if denied:
            relative = relative[1:]
        missing = relative.startswith("!")
        if missing:
            relative = relative[1:]
        external = relative.startswith("@/")
        if (
            not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            return False
        path = Path(relative[1:]) if external else root / relative
        if denied:
            try:
                path.stat()
                return False  # Host visibility differs; do not read content.
            except PermissionError as error:
                if str(error.errno) == str(expected):
                    continue
                return False
            except OSError:
                return False
        if missing:
            if path.exists():
                return False
            continue
        if path_digest(path) != str(expected):
            return False
    return True


if __name__ == '__main__':
    root, request = map(Path, sys.argv[1:])
    try:
        valid = manifest_matches(root.resolve(), json.loads(request.read_text()))
    except (OSError, ValueError, TypeError):
        valid = False
    print(json.dumps({'matches': valid}))
