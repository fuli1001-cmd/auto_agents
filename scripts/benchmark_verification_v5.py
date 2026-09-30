"""Four offline v5 scenarios in an isolated disposable project; no provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.models import GateConfig, VerificationStep
from auto_agents.gates import resolve_gate_plan_from_verification_steps, run_gate_plan
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.verification_selection import select_verification_steps
from auto_agents.workers import gate_environment_fingerprint


def main():
    with tempfile.TemporaryDirectory(prefix='v5-benchmark-') as temporary:
        root = Path(temporary)/'project'; root.mkdir(); (root/'tests').mkdir()
        os.environ['AUTO_AGENTS_VERIFICATION_ROOT'] = str(Path(temporary)/'proofs')
        os.environ['AUTO_AGENTS_STATE_HOME'] = str(Path(temporary)/'state')
        os.environ['AUTO_AGENTS_WORKER_ROOT'] = str(Path(temporary)/'worker')
        (root/'.gitignore').write_text('.conda\n.pytest_cache\n__pycache__\n.auto-agents\n')
        (root/'.conda').symlink_to(sys.prefix, target_is_directory=True)
        for name in ('a','b'):
            (root/f'{name}.py').write_text('def value(): return 1\n')
            (root/f'tests/test_{name}.py').write_text(f'import {name}\ndef test_value(): assert {name}.value() > 0\n')
        for arguments in [('init',),('config','user.name','Benchmark'),('config','user.email','benchmark@example.com'),
                          ('add','.'),('commit','-m','baseline')]:
            subprocess.run(['git',*arguments],cwd=root,check=True,capture_output=True)
        steps = [VerificationStep(proof_id=f'proof.{name}',runner='pytest',purpose='Offline contract',
            levels=['affected'],targets=[f'tests/test_{name}.py'],impact_symbols=[f'{name}.py::value'],
            parallel_safe=True,cache_scope='source',result_cache_scope='auto') for name in ('a','b')]
        gates = GateConfig(verification_policy_version=5,steps=steps,parallel_workers=2)
        environment = gate_environment_fingerprint(isolation_mode='git_worktree',environment_id='local',distributed=False,project_root=root)
        rows = []
        for index,(name,level,changed) in enumerate([('cold','release',[]),('local-edit','affected',['a.py']),
                                                   ('unchanged-resume','affected',['a.py']),('final-release','release',[]) ]):
            if name=='local-edit': (root/'a.py').write_text('def value(): return 2\n')
            selection = select_verification_steps(steps,root,gates,level=level,changed_paths=changed,preserve_release_targets=True)
            plan = resolve_gate_plan_from_verification_steps(selection.steps,root)
            started = time.monotonic()
            with LocalGatePlanExecutor(root,gates,run_id=f'benchmark-{index}',metadata=plan.metadata,
                    environment_fingerprint=environment,result_context_fingerprint='fixed-input-contract',
                    record_pytest_execution=True,input_reuse_mode='on') as executor:
                result = run_gate_plan(plan.commands,plan.parallel_groups,root,collect_all=True,
                    parallel_workers=2,command_timeout_seconds=60,gate_executor=executor)
            rows.append({'scenario':name,'seconds':round(time.monotonic()-started,3),'ok':result.ok,
                         'proofs':selection.proof_ids,'batches':len(result.commands),
                         'executed':sum(not item.cached for item in result.commands),'cache_hits':sum(item.cached for item in result.commands),
                         'passed_nodes':sorted({node for item in result.commands for node in item.executed_tests}),
                         **({'failure':result.summary[-2000:]} if not result.ok else {})})
        print(json.dumps({'scope':'isolated offline reference workload','runs':rows},indent=2))
        return 0 if all(row['ok'] for row in rows) else 1


if __name__=='__main__':
    raise SystemExit(main())
