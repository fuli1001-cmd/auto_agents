"""Required release gate: real local tools, no model account or paid media."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from auto_agents.control import Store
from auto_agents.config import save_project_config
from auto_agents.models import (
    ProjectConfig,
    ProviderConfig,
    SMART_TIMEOUT_PROGRESS_PROTOCOL,
)
from auto_agents.control.workspace import git
from auto_agents_watch.store import Store as WatchStore
from test_control_engine import project


def test_real_runtime_failure_fix_and_same_project_resume(tmp_path):
    if os.environ.get("AUTO_AGENTS_RUNTIME_ACCEPTANCE") != "1":
        pytest.skip(
            "Release gate requires AUTO_AGENTS_RUNTIME_ACCEPTANCE=1 and pinned local Next/Chrome tools"
        )
    modules = Path(os.environ["AUTO_AGENTS_TEST_NEXT_MODULES"])
    chrome = Path(os.environ["AUTO_AGENTS_TEST_CHROME"])
    assert chrome.is_file() and (modules / "next/package.json").is_file()
    assert (
        json.loads((modules / "next/package.json").read_text())["version"] == "15.2.4"
    )
    engine = Path(__file__).resolve().parents[1]
    root = project(tmp_path)
    fixture = Path(__file__).parent / "fixtures/runtime_app"
    shutil.copytree(fixture, root, dirs_exist_ok=True)
    (root / ".gitignore").write_text(
        ".data/\n.tmp/\n.tmp-tests/\nnode_modules/\nnext-env.d.ts\n"
    )
    (root / "capability.json").write_text('{"ready":false}\n')
    (root / "tests").mkdir()
    (root / "tests/test_capability.py").write_text(
        "import json\nfrom pathlib import Path\ndef test_capability(): assert json.loads(Path('capability.json').read_text())['ready'] is True\n"
    )
    (root / "node_modules").symlink_to(modules.resolve(), target_is_directory=True)
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "runtime fixture",
    )
    config = ProjectConfig("runtime-fixture")
    config.active_provider = "fixture"
    config.providers = {
        "fixture": ProviderConfig(
            kind="shell",
            binary=sys.executable,
            extra_args=["./fixture_provider.py"],
            progress_protocol=SMART_TIMEOUT_PROGRESS_PROTOCOL,
        )
    }
    config.repo_map.enabled = False
    save_project_config(root, config)
    binary = tmp_path / "bin"
    binary.mkdir()
    launcher = binary / "auto-agents-watch"
    launcher.write_text(
        f"#!{sys.executable}\nfrom auto_agents_watch.cli import main\nraise SystemExit(main())\n"
    )
    launcher.chmod(0o700)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(engine / "src"), str(engine / "supervisor/src"))
        ),
        "AUTO_AGENTS_WATCH_ROOT": str(tmp_path / "watch"),
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
    }
    env.pop("AUTO_AGENTS_NO_SUPERVISOR", None)
    args = [
        sys.executable,
        "-m",
        "auto_agents",
        "collab",
        "--project",
        str(root),
        "--provider",
        "fixture",
        "--auto-approve",
    ]
    first = subprocess.run(
        [*args, "--goal", "验收已有功能，不新增产品需求，不重新设计或规划项目"],
        cwd=engine,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    business = Store(root)
    watch = WatchStore(tmp_path / "watch")
    jobs = watch.list()
    assert len(jobs) == 1, first.stdout + first.stderr
    original = business.work(jobs[0]["root_id"])
    assert original["failure"]["code"] == "acceptance", first.stdout + first.stderr
    assert (
        original["context"]["acceptance_blocker"]["external_ids"]["project_id"]
        == "project-1"
    )
    assert "[run] clarify" not in first.stderr and "[run] prototype" not in first.stderr
    # A stale supervisor observation cannot prevent an explicit recovery.
    watch.save(
        jobs[0],
        "STOPPED",
        fault={"category": "model", "message": "obsolete scope rejection"},
    )
    second = subprocess.run(
        [*args, "--session", original["id"]],
        cwd=engine,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    completed = business.work(original["id"])
    assert (
        completed["status"] == "COMPLETED"
        and completed["contract"] == original["contract"]
    )
    assert len(watch.list()) == 1 and watch.get(jobs[0]["id"])["model_calls"] == 0
    assert all(w["mode"] != "run" for w in business.works())
    assert completed["result"]["acceptance"]["external_ids"] == {
        "project_id": "project-1",
        "provider_task_id": "local-provider-1",
    }
    observed = json.loads(
        (root / ".auto-agents/docs/acceptance/runtime/observation.json").read_text()
    )
    assert observed["creates"] == 1 and observed["submits"] == 1
    assert (
        root / ".auto-agents/docs/acceptance/runtime/browser.png"
    ).stat().st_size > 1000
    assert not (root / "next-env.d.ts").exists()
    assert not (root / ".data").exists()
    assert (root / "value.py").read_text() == "VALUE = 0\n"
    assert json.loads((root / "capability.json").read_text())["ready"] is True
    assert completed["result"]["verification"]["checks"][0]["executed"] == 1
