"""Versioned boundary between the running verifier and the engine under repair.

The selected worker/controller is immutable for one repair generation. Candidate
imports cannot activate another outer supervisor. Candidate compatibility is
tested by the existing acceptance gates, not advertised as a runtime capability.
"""
from pathlib import Path
import subprocess

from .repair_control import digest

VERSION = 1
FILES = ('repair_validation_protocol.py', 'repair_capability_checks.py', 'repair_runtime.py',
         'verification_sandbox.py', 'verification_metadata.py', 'verification_input_trace.py',
         'verification_supervisor_checks.py', 'gate_verification.py', 'verification_worker.py')


def controller_runtime():
    return Path(__file__).resolve().parents[2]


def binding(runner):
    root = controller_runtime()
    modules = root / 'src/auto_agents'
    files = {name: digest((modules / name).read_bytes().hex()) for name in FILES}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    return {'version': VERSION, 'runtime': str(root), 'runtime_commit': revision,
            'observer': digest(files), 'environment': digest(runner._full_suite_environment_fingerprint()),
            'generation': getattr(runner, '_repair_control_binding', None),
            'acceptance_proof': False,
            'activation': 'immutable selected controller; candidate changes require independently verified version handoff'}


def retain(runner, observed, identity):
    """Keep the negotiated observation independently of component plan versions."""
    if not hasattr(runner, '_experiment_store') or not hasattr(runner, '_experiment'):
        return
    from .repair_memory import save_record
    key = 'validation-protocol:' + digest(identity)
    if key not in runner._experiment.planning_receipts:
        reference = save_record(runner, 'validation_protocol', {'binding': identity, 'observation': observed})
        runner._experiment.planning_receipts[key] = {'reference': reference, 'acceptance_proof': False}
        runner._experiment_store.save(runner._experiment)
