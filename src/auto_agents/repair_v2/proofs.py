"""Reuse verified stage results only under identical, explicit dependencies."""
from dataclasses import asdict
import json

from .store import atomic_json, digest
from .types import PhaseProof


def once(store, stage, inputs, execute):
    key = digest([stage, inputs, 1])
    marker = store.root / 'phase-proofs' / (key + '.json')
    if marker.is_file():
        try:
            proof = store.read(json.loads(marker.read_text()))
            if (proof['stage'] == stage and proof['inputs'] == inputs and proof['policy'] == 1
                    and proof['result'].get('ok') and not proof['result'].get('infrastructure')
                    and not proof['result'].get('cancelled')):
                store.event('phase_reused', stage=stage, inputs=inputs)
                return proof['result']
        except (OSError, ValueError, KeyError, TypeError):
            pass
    result = execute()
    reference = store.artifact('phase-proof', asdict(PhaseProof(stage, inputs, result)))
    if result.get('ok') and not result.get('infrastructure') and not result.get('cancelled'):
        atomic_json(marker, reference)
    return result
