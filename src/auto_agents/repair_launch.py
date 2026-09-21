"""Load a verified runtime in a fresh interpreter, preserving workflow identity."""
import json
import os
from pathlib import Path
import sys
import subprocess

if __package__:
    from .repair_runtime import RuntimeCompatibilityError, verify_runtime
    from .repair_runtime_identity import RuntimeIdentityError, observe_engine
else:
    from repair_runtime import RuntimeCompatibilityError, verify_runtime
    from repair_runtime_identity import RuntimeIdentityError, observe_engine


_sanitize_failure = None


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    subscriber, result = request["subscriber"], request["result"]
    runtime = Path(result["runtime"]).resolve()
    budget_reconcile, budget_anchors = None, {}
    if result.get('engine') == 'v2':
        # Validate with the immutable launch/controller implementation before
        # importing any approved candidate module into this interpreter.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        global _sanitize_failure
        from auto_agents.repair_environment_log import sanitize as _sanitize_failure
        from auto_agents.repair_v2.integration import verify_receipt
        from auto_agents.repair_v2.transaction import transaction_root
        acceptance = verify_receipt(result, expected_root=transaction_root(request['config'], subscriber['payload']['repair']))
        if result.get('recovery_protocol') not in (1, 2):
            raise RuntimeError('旧运行产物必须迁移并通过恢复检查后再启动')
        from auto_agents.repair_v2.budget_recovery import reconcile as budget_reconcile
        from auto_agents.repair_v2.store import Store
        if acceptance.get('budget_anchors'):
            budget_anchors = Store(result['v2_transaction']).read(acceptance['budget_anchors'])
        # The business process below must import the delivered engine, not the
        # controller modules used to check its receipt.
        for name in list(sys.modules):
            if name == 'auto_agents' or name.startswith('auto_agents.'):
                del sys.modules[name]
    loaded = subprocess.run(["git", "-C", str(runtime), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    changed = subprocess.run(["git", "-C", str(runtime), "diff", "HEAD", "--exit-code"],
                             capture_output=True, timeout=60)
    if loaded != result["commit"] or changed.returncode:
        raise RuntimeError("approved runtime changed before workflow launch")
    verify_runtime(runtime, sys.executable, phase="resume")
    sys.path.insert(0, str(runtime / "src"))
    from auto_agents.run_lock import ProjectRunLock
    from auto_agents.config import load_run_state
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.cli import main as cli_main
    observation = {'engine_runtime': observe_engine(runtime, expected_commit=result['commit']),
                   'invocation': subscriber['payload']['repair'].get('invocation', {})}
    # Keep this evidence in the controller's request directory, not in the
    # product session or its original structured failure.
    report = Path(sys.argv[1]).with_suffix('.runtime.json')
    report.write_text(json.dumps(observation, ensure_ascii=False, sort_keys=True))
    project = Path(subscriber["project"])
    payload = subscriber["payload"]["repair"]
    inherited = int(os.environ["AUTO_AGENTS_RUN_LOCK_FD"])
    preparation_fd = os.dup(inherited)
    preparation_env = {**os.environ, "AUTO_AGENTS_RUN_LOCK_FD": str(preparation_fd)}
    with ProjectRunLock(project, environ=preparation_env) as lock:
        invocation = payload["invocation"]
        if budget_reconcile:
            budget_reconcile(project, budget_anchors, invocation)
        if invocation.get("run_id"):
            state = load_run_state(project)
            if state.run_id != invocation["run_id"] or state.active_blocker.get("fingerprint") != payload["fingerprint"]:
                raise RuntimeError("live run no longer matches the approved recovery")
            Orchestrator(project).mark_self_repair_applied(result["commit"], verification=result["proof"])
        argv = payload["resume_argv"]
    return cli_main(argv[2:])


if __name__ == "__main__":
    path = Path(sys.argv[1])
    failure = {}
    try:
        code = main()
    except RuntimeCompatibilityError as error:
        failure = error.to_result()
        code = 3
        print(str(error), file=sys.stderr)
    except RuntimeIdentityError as error:
        failure = {'ok': False, 'category': 'runtime_identity', 'error': str(error),
                   'engine_runtime': error.report}
        code = 3
        print(str(error), file=sys.stderr)
    except Exception as error:
        message = (_sanitize_failure(str(error)) if _sanitize_failure else
                   type(error).__name__ + ': 恢复启动失败，请查看详细日志')
        failure = {'ok': False, 'category': 'recovery_launch', 'code': getattr(error, 'code', 'activation_failed'),
                   'error': message, 'error_type': type(error).__name__}
        code = 3
        print(message, file=sys.stderr)
    finally:
        output = path.with_name(path.stem + "-result.json")
        temporary = output.with_suffix(".tmp")
        with temporary.open("w") as handle:
            json.dump({**failure, "exit_code": locals().get("code", 3)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    raise SystemExit(code)
