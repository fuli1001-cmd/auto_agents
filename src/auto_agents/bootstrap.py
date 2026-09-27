"""Small trusted selector; import the adopted runtime only after validation."""
import os
from pathlib import Path
import sys


def select_runtime(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:2] in (['repair','status'], ['repair','migrate']): return
    from .recovery.authority import installation_root
    from .recovery.store import KernelStore
    root = installation_root()
    if root is not None:
        store = KernelStore(root, readonly=True)
        if arguments[:2] == ['repair','upgrade']:
            artifact = store.meta('trusted_verifier_runtime') if store.meta('active_runtime') else None
        else:
            artifact = store.meta('active_runtime') if store.meta('mode') == 'active' else None
        if artifact:
            from .repair_v2.runtime_artifact import verify
            verify(artifact)
            expected = Path(artifact['path']).resolve()
            current = Path(__file__).resolve().parents[2]
            if current != expected:
                # Fresh interpreter: no controller/candidate module mixture.
                script = ("import sys; sys.path.insert(0, " + repr(str(expected/'src')) + "); "
                          "from auto_agents.cli import main; raise SystemExit(main(sys.argv[1:]))")
                env = {**os.environ, 'PYTHONPATH': str(expected/'src'), 'AUTO_AGENTS_RECOVERY_CONTROL': str(root)}
                os.execve(sys.executable, [sys.executable, '-c', script, *arguments], env)


def main(argv=None):
    select_runtime(argv)
    from .cli import main as dispatch
    return dispatch(argv)
