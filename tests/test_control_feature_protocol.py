"""Version negotiation is read-only and precedes business admission."""

import json
import os
from pathlib import Path
import sys

from auto_agents import bootstrap
from auto_agents.control.cli import main
from auto_agents_watch.cli import main as watch_main
from auto_agents.control.policy import FEATURES


def test_capabilities_do_not_create_project_or_supervisor_state(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("AUTO_AGENTS_WATCH_ROOT", str(tmp_path / "absent"))
    assert main(["capabilities", "--json"]) == 0
    assert set(FEATURES).issubset(json.loads(capsys.readouterr().out)["features"])
    assert watch_main(["capabilities", "--json"]) == 0
    assert set(FEATURES).issubset(json.loads(capsys.readouterr().out)["features"])
    assert not (tmp_path / "absent").exists()


def test_incompatible_supervisor_refused_before_business_dispatch(
    tmp_path, monkeypatch, capsys
):
    binary = tmp_path / "bin"
    binary.mkdir()
    marker = tmp_path / "business-dispatched"
    watcher = binary / "auto-agents-watch"
    watcher.write_text(
        f"#!{sys.executable}\nimport sys,json\nfrom pathlib import Path\n"
        f"if sys.argv[1]!='capabilities':Path({str(marker)!r}).touch()\n"
        "print(json.dumps({'ok':True,'protocol':2,'features':[]}))\n"
    )
    watcher.chmod(0o700)
    monkeypatch.delenv("AUTO_AGENTS_NO_SUPERVISOR", raising=False)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(bootstrap, "select_runtime", lambda *_: None)
    project = tmp_path / "project"
    assert bootstrap.main(["collab", "--project", str(project), "--goal", "Goal"]) == 3
    assert "upgrade both packages" in capsys.readouterr().err
    assert not marker.exists() and not project.exists()


def test_invalid_legacy_preview_port_has_a_user_error(tmp_path):
    from types import SimpleNamespace
    import pytest
    from auto_agents.control.project_tools import dispatch

    with pytest.raises(ValueError, match="--port must be between"):
        dispatch(
            SimpleNamespace(
                command="prototype-preview", project=str(tmp_path), port=65536
            )
        )
