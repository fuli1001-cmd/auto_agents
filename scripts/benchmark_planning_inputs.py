#!/usr/bin/env python3
"""Compare archived and current working-input sizes without invoking providers."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.repair_planning_input import working_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--request', action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.experiment.resolve().parent
    if args.output.resolve().is_relative_to(root):
        parser.error('write the report outside retained experiment evidence')
    rows = []
    for path in sorted((root / 'planning').glob('*/request.json')):
        identity = path.parent.name
        if args.request and identity not in args.request:
            continue
        request = json.loads(path.read_text())
        if request.get('stage') != 'self_repair_component_plan':
            continue
        saved = path.with_name('working_input.json')
        if not saved.is_file():
            continue
        context = json.loads(path.with_name('input.json').read_text())
        old = json.loads(saved.read_text())
        new = working_input(context, path.parent, request['stage'])
        size = lambda value: len(json.dumps(value, ensure_ascii=False))
        rows.append({'request': identity, 'old_chars': size(old), 'new_chars': size(new),
                     'incremental': bool(context.get('previous_revision'))})
    args.output.write_text(json.dumps({'requests': rows,
        'interpretation': 'Offline input reconstruction only. Complete evidence remains referenced. '
                          'No provider calls, job mutations or end-to-end latency prediction.'}, indent=2) + '\n')
    print(args.output)


if __name__ == '__main__':
    main()
