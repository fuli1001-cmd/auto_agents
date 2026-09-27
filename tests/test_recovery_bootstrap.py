from pathlib import Path

import pytest

from auto_agents.recovery import KernelStore


@pytest.mark.parametrize('arguments,target', [(['collab','--project','/project','--session','original'],'active'),
                                              (['repair','upgrade','--runtime','/candidate'],'trusted')])
def test_existing_console_launcher_selects_a_fresh_runtime(tmp_path,monkeypatch,arguments,target):
    store = KernelStore(tmp_path/'control')
    store.set_meta('mode','active')
    store.set_meta('active_runtime',{'path':str(tmp_path/'active')})
    store.set_meta('trusted_verifier_runtime',{'path':str(tmp_path/'trusted')})
    monkeypatch.setenv('AUTO_AGENTS_RECOVERY_CONTROL',str(store.root))
    validated = []
    monkeypatch.setattr('auto_agents.repair_v2.runtime_artifact.verify',lambda artifact:validated.append(artifact['path']))
    class Selected(BaseException): pass
    execution = []
    def execve(binary,argv,environment):
        execution.append((binary,argv,environment))
        raise Selected()
    monkeypatch.setattr('auto_agents.bootstrap.os.execve',execve)
    from auto_agents.cli import main
    with pytest.raises(Selected): main(arguments)
    assert validated == [str(tmp_path/target)]
    assert execution[0][1][3:] == arguments
    assert execution[0][2]['PYTHONPATH'] == str(tmp_path/target/'src')
    assert execution[0][2]['AUTO_AGENTS_RECOVERY_CONTROL'] == str(store.root)


def test_status_remains_available_when_adopted_artifact_is_broken(tmp_path,monkeypatch,capsys):
    store = KernelStore(tmp_path/'control')
    store.set_meta('mode','active')
    store.set_meta('active_runtime',{'path':'/missing-runtime'})
    monkeypatch.setenv('AUTO_AGENTS_RECOVERY_CONTROL',str(store.root))
    from auto_agents.cli import main
    assert main(['repair','status','--json']) == 0
    assert 'missing-runtime' in capsys.readouterr().out


def test_copied_supervisor_bootstrap_advertises_its_pinned_kernel(tmp_path,monkeypatch):
    from auto_agents import repair_control
    implementation = Path(repair_control.__file__).resolve().parents[2]
    supervisor = repair_control.Supervisor({'root':str(tmp_path/'control'),
        'source_root':str(tmp_path/'source'),'implementation_root':str(implementation)})
    monkeypatch.setattr(repair_control,'__file__',str(tmp_path/'control/bootstrap.py'))
    response = supervisor.dispatch({'version':1,'op':'ping'},[])
    assert 'recovery-kernel-v1' in response['capabilities']
    assert supervisor.dispatch({'version':2,'op':'kernel-status'},[])['protocol'] == 2
