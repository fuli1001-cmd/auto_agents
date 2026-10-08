"""Optional native Docker acceptance of the public, credential-free protocol."""

import json
import os
from pathlib import Path
import shutil
import pytest
from auto_agents.control import Store, ControlError
from auto_agents.control.engine import Engine
from auto_agents.control import api
from auto_agents.config import save_project_config
from auto_agents.models import ProjectConfig
from test_control_engine import project, Worker


def test_docker_reproduces_original_exception_and_clears_only_real_progress(tmp_path):
    image = os.environ.get("AUTO_AGENTS_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip(
            "Set AUTO_AGENTS_TEST_DOCKER_IMAGE to an existing local tool image for native integration"
        )
    from auto_agents_watch.sandbox import Docker

    root = project(tmp_path)
    save_project_config(root, ProjectConfig("offline"))

    class Broken(Engine):
        def step(self, work, context):
            if work["phase"] == "classify":
                self.model(work, context, "classify")
                raise KeyError("missing_classification_field")
            return super().step(work, context)

    store = Store(root)
    broken = Broken(
        root,
        store,
        ProjectConfig("offline"),
        transport=Worker(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    work = broken.start("fix", "Set value to one", max_calls=6)
    assert work["failure"]["category"] == "engine"
    fault = api.checkpoint(
        root,
        ["fix", "--project", str(root), "--goal", "Set value to one"],
        work["id"],
        ControlError(
            "engine_exception",
            "missing_classification_field",
            category="engine",
            details={"exception_type": "KeyError"},
        ),
    )
    host_identity = api.status(root)["identity"]
    snapshot = tmp_path / "snapshot"
    api.snapshot(root, snapshot)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "resume-token.json").write_bytes(
        Path(fault["resume_token"]).read_bytes()
    )
    engine_source = Path(__file__).resolve().parents[1]
    old = tmp_path / "old-engine"
    (old / "src").mkdir(parents=True)
    shutil.copytree(
        engine_source / "src/auto_agents",
        old / "src/auto_agents",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    file = old / "src/auto_agents/control/engine.py"
    source = file.read_text()
    marker = "    def step(self, work, ctx):\n"
    assert source.count(marker) == 1
    file.write_text(
        source.replace(
            marker,
            marker
            + "        if work['phase'] == 'classify':\n            raise KeyError('missing_classification_field')\n",
        )
    )
    sandbox = Docker(tmp_path / "watch/sandboxes/offline", image=image)
    sandbox.prepare()
    reports = {}
    for label, code in [("original", old), ("repaired", engine_source)]:
        private = tmp_path / (label + "-project")
        shutil.copytree(snapshot, private, symlinks=True)
        log = tmp_path / (label + ".log")
        sandbox.verify(
            [
                "python",
                "-m",
                "auto_agents",
                "resume-check",
                "--project",
                str(root),
                "--resume-token",
                "/evidence/resume-token.json",
            ],
            code,
            evidence,
            tmp_path / (label + "-output"),
            log,
            project=private,
        )
        values = []
        for line in log.read_text().splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                values.append(value)
        assert values, log.read_text()
        reports[label] = values[-1]
    assert (
        reports["original"]["category"] == "engine"
        and reports["original"]["type"] == "KeyError"
    )
    assert not reports["original"]["blocked_step_cleared"]
    assert reports["repaired"]["ok"] and reports["repaired"]["blocked_step_cleared"]
    assert (
        reports["repaired"]["retained_constraints"]
        and reports["repaired"]["external_calls"] == 0
    )
    assert (
        api.status(root)["identity"] == host_identity
        and store.workflow(work["workflow"])["calls"] == 1
    )
