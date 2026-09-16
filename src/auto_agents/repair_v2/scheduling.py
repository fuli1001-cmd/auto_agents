"""Measured scheduling hints. Timings never substitute for test evidence."""
import json
import math
from pathlib import Path
import shlex

from .store import atomic_json, digest
from .types import ValidationUnit


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


class Timings:
    def __init__(self, root):
        self.path = Path(root) / 'timings.json'
        try:
            saved = json.loads(self.path.read_text())
            self.nodes = {k: float(v) for k, v in saved.get('nodes', {}).items()
                          if isinstance(k, str) and positive(v)}
        except (OSError, ValueError, TypeError, AttributeError): self.nodes = {}

    def observe(self, checks):
        for check in checks:
            if check.get('cache_hit') or check.get('cancelled') or check.get('infrastructure'): continue
            nodes = check.get('collected', [])
            durations = check.get('node_seconds', {})
            # Old receipts recorded batch timings only. They remain useful
            # conservative estimates until individual measurements replace them.
            fallback = check.get('seconds', 0) / len(nodes) if nodes else 0
            for node in nodes:
                value = durations.get(node, fallback)
                if positive(value): self.nodes[node] = float(value)

    def save(self):
        try: atomic_json(self.path, {'version': 1, 'nodes': self.nodes})
        except OSError: pass  # A scheduling optimization cannot block acceptance.

    def estimate(self, nodes):
        return 2 + sum(self.nodes.get(node, 1.0) for node in nodes)

    def batches(self, grouped, *, prefix='suite', fresh=False, pack=True):
        """Retain all nodes, keep large files sharded, combine measured fast files."""
        units, small = [], []

        def unit(identity, nodes, targets):
            return ValidationUnit(identity, shlex.join(['python', '-m', 'pytest', '-q', *targets]),
                                  expected_nodes=tuple(nodes), fresh=fresh, profile='sandbox')

        for file, nodes in sorted(grouped.items()):
            nodes = list(nodes)
            if (pack and len(nodes) <= 32 and all(node in self.nodes for node in nodes)
                    and self.estimate(nodes) <= 30):
                small.append((file, nodes))
            else:
                for index in range(0, len(nodes), 32):
                    batch = nodes[index:index + 32]
                    targets = [file] if prefix == 'suite' and len(nodes) <= 32 else batch
                    units.append(unit(prefix + ':' + file + ':' + str(index // 32), batch, targets))

        files, nodes = [], []
        def flush():
            if nodes:
                units.append(unit(prefix + ':batch:' + digest(files)[:16], nodes, files))
        for file, batch in small:
            if files and (len(files) >= 8 or len(nodes) + len(batch) > 128
                          or self.estimate([*nodes, *batch]) > 60):
                flush(); files, nodes = [], []
            files.append(file); nodes.extend(batch)
        flush()
        # Long jobs start first to avoid leaving a single expensive tail. The
        # controller subsequently moves previous failures ahead of this order.
        return sorted(units, key=lambda item: -self.estimate(item.expected_nodes))
