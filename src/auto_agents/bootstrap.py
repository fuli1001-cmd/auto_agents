"""Small trusted selector; import the adopted runtime only after validation."""
import os
from pathlib import Path
import sys


def select_runtime(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    from .runtime_entry import handoff
    if handoff(arguments): return
    maintenance = (not arguments or any(a in {'-h','--help'} for a in arguments)
                   or arguments[0] in {'status','stop','cancel','storage'}
                   or arguments[:2] in (['repair','status'], ['repair','migrate'], ['repair','cancel']))
    if maintenance: return
    from .recovery.authority import installation_root
    from .recovery.store import KernelStore
    root = installation_root()
    if root is not None:
        store = KernelStore(root, readonly=True)
        active = store.meta('active_runtime')
        manager = Path(active['path']) / 'src/auto_agents/recovery/runtime_manager.py' if active else None
        if manager is not None and manager.is_file() and store.meta('mode') in {'active', 'draining'}:
            if os.environ.get('AUTO_AGENTS_RUNTIME_USE'):
                from .recovery.runtime_manager import inherited
                runtime = inherited(store)
                if Path(runtime['path']).resolve() == Path(__file__).resolve().parents[2]: return
                artifact = runtime
            else:
                # Dispatch management from the adopted implementation; source
                # edits do not replace their own independent verification.
                env = {**os.environ, 'PYTHONPATH': str(Path(active['path']) / 'src'),
                       'AUTO_AGENTS_RECOVERY_CONTROL': str(root)}
                script = ('import sys,runpy; sys.path.insert(0,' + repr(str(Path(active['path']) / 'src'))
                          + "); runpy.run_module('auto_agents.recovery.runtime_manager',run_name='__main__')")
                os.execve(sys.executable, [sys.executable, '-c', script, str(root), *arguments], env)
                return
        else:
            artifact = None
        if arguments[:2] == ['repair','upgrade']:
            artifact = store.meta('trusted_verifier_runtime') if store.meta('active_runtime') else None
        elif artifact is None:
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
    from .cli_impl import main as dispatch
    try: return dispatch(argv)
    finally:
        if os.environ.get('AUTO_AGENTS_RUNTIME_USE'):
            from .recovery.runtime_manager import release_inherited
            release_inherited()
