"""Preview/apply a reviewed v5 config; existing frozen sessions stay intact."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.verification_migration import apply_migration, prepare_migration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text()) if args.manifest else None
    path, original, candidate = prepare_migration(args.project, manifest)
    if args.apply:
        apply_migration(args.project, manifest)
    print(json.dumps({'applied':args.apply, 'config':str(path), 'before_sha256':hashlib.sha256(original).hexdigest(),
        'policy_version':5, 'final_proof_ids':[step['proof_id'] for step in candidate['gates']['steps']],
        'reviewed_updates':(manifest or {}).get('updates', {}), 'existing_bindings':'preserved'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
