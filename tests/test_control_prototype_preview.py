"""Prototype pauses explain previewing, and preview retains comparison views."""

import functools
import http.server
import json
import re
import shlex
import threading
import urllib.error
import urllib.request

import pytest

from auto_agents.config import save_project_config
from auto_agents.control import Store
from auto_agents.control import prototypes
from auto_agents.control.cli import main, parser
from auto_agents.control.engine import Engine
from auto_agents.control.interaction import pending_decisions
from auto_agents.control.workspace import git
from auto_agents.models import ProjectConfig
from test_control_domains import Frontend
from test_control_engine import project


@pytest.fixture
def frontend(tmp_path):
    base = tmp_path / "project with spaces"
    base.mkdir()
    root = project(base)
    prefix = ".auto-agents/docs/frontend_prototype"
    old = root / prefix
    old.mkdir(parents=True)
    (old / "home.html").write_text("<html>Previous prototype</html>")
    (old / "manifest.json").write_text(json.dumps({
        "pages": [{"id": "home", "title": "Previous home", "html_ref": prefix + "/home.html"}],
        "index_ref": prefix + "/home.html",
    }))
    history_prefix = ".auto-agents/docs/frontend_prototype_variants/legacy-old/frontend_prototype"
    history = root / history_prefix
    history.mkdir(parents=True)
    (history / "home.html").write_text("<html>Historical prototype</html>")
    (history / "manifest.json").write_text(json.dumps({
        "pages": [{"id": "home", "title": "Historical home", "html_ref": history_prefix + "/home.html"}],
        "index_ref": history_prefix + "/home.html",
    }))
    git(root, "add", "-f", prefix, history_prefix)
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "previous prototype")
    config = ProjectConfig("preview")
    config.frontend_design.mode = "off"
    save_project_config(root, config)
    store = Store(root)
    (root / ".auto-agents/state/frontend_prototype_variants.json").write_text(json.dumps({
        "variants": [{"id": "legacy-old", "name": "Historical", "status": "approved"}],
    }))

    class Pages(Frontend):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype":
                reply = json.loads(result["text"])
                prefix = reply["artifacts"][0].rsplit("/", 1)[0]
                package = ctx.workspace_root / prefix
                (package / "nested").mkdir(exist_ok=True)
                (package / "nested/home.html").write_text(
                    '<html><meta name="viewport" content="width=device-width">Nested page</html>'
                )
                (package / "nested/report.json").write_text('{"observed": true}')
                manifest = json.loads((package / "manifest.json").read_text())
                manifest["pages"].append({
                    "id": "nested", "title": "Nested page", "route": "/nested",
                    "requirement_ids": ["REQ-001"], "html_ref": prefix + "/nested/home.html",
                })
                reply["artifacts"] += [prefix + "/nested/home.html", prefix + "/nested/report.json"]
                manifest["artifacts"] = reply["artifacts"]
                (package / "manifest.json").write_text(json.dumps(manifest))
                result["text"] = json.dumps(reply)
            return result

    engine = Engine(root, store, config, transport=Pages(), auto_approve=True,
                    print_fn=lambda *args: None)
    work = engine.start("run", "Current prototype")
    assert work["status"] == "WAITING", work["failure"]
    return root, engine, work


def test_prototype_pause_prints_a_runnable_preview_command_without_a_port(frontend, capsys):
    root, engine, work = frontend
    decision = pending_decisions(engine.store, work)[0]
    command = shlex.split(decision["preview_command"])
    args = parser().parse_args(command[1:])
    assert args.project == str(root) and args.session == work["id"] and args.port == 0
    assert "端口自动选择" in decision["preview_instructions"]
    reject = parser().parse_args(shlex.split(decision["reject_command"])[1:])
    assert reject.command == "reject" and reject.gate == "prototype"
    assert reject.project == str(root) and reject.session == work["id"] and reject.reason.strip()
    calls = engine.store.workflow(work["workflow"])["calls"]
    assert main(["run", "--project", str(root), "--session", work["id"], "--auto-approve"]) == 0
    output = capsys.readouterr().out
    assert "查看原型：" + decision["preview_command"] in output
    assert "查看说明：" + decision["preview_instructions"] in output
    assert "拒绝原型：" + decision["reject_command"] in output
    assert "拒绝说明：" + decision["reject_instructions"] in output
    assert engine.store.workflow(work["workflow"])["calls"] == calls


def test_supervisor_status_preserves_preview_instructions(frontend, tmp_path, capsys):
    from auto_agents_watch.cli import readable_result
    from auto_agents_watch.store import Store as WatchStore

    _, engine, work = frontend
    readable_result({
        "state": "STOPPED", "waiting_for": "approval",
        "observation": {"pending_decisions": pending_decisions(engine.store, work)},
    }, WatchStore(tmp_path / "watch"))
    output = capsys.readouterr().out
    assert "查看原型：auto-agents prototype preview" in output
    assert "端口自动选择" in output and "--session " + work["id"] in output
    assert "拒绝原型：auto-agents reject" in output and "--reason " in output
    assert "拒绝后执行恢复命令重新设计" in output


def test_preview_serves_baseline_and_candidates_side_by_side_without_mutation(frontend):
    _, engine, work = frontend
    work = prototypes.generate(engine, work, "Another layout", name="Alternative")
    assert work["status"] == "WAITING", work["failure"]
    variant = work["context"]["variants"][-1]["id"]
    before = engine.store.work(work["id"])
    calls = engine.store.workflow(work["workflow"])["calls"]
    gallery, routes = prototypes.preview_gallery(engine, work)
    assert b'id="f0"' in gallery and b'id="f1"' in gallery
    assert all(name.encode() in gallery for name in ("history-legacy-old", "baseline", "initial", variant))
    handler = functools.partial(prototypes.PrototypeGalleryHandler, gallery=gallery, routes=routes)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:" + str(server.server_port)
    try:
        def read(path):
            with urllib.request.urlopen(url + path, timeout=5) as response:
                return response.read()

        assert read("/") == gallery
        assert read("/variants/baseline/home.html") == b"<html>Previous prototype</html>"
        assert read("/variants/history-legacy-old/home.html") == b"<html>Historical prototype</html>"
        assert b"Current prototype" in read("/variants/initial/home.html")
        assert b"Another layout" in read("/variants/" + variant + "/home.html")
        assert b"Nested page" in read("/variants/initial/nested/home.html")
        assert read("/variants/initial/nested/report.json") == b'{"observed": true}'
        assert read("/home.html") == read("/variants/initial/home.html")
        for path in ("/.git/config", "/variants/initial/../../state/business.sqlite3", "/variants/initial/.env"):
            with pytest.raises(urllib.error.HTTPError) as error:
                read(path)
            assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert engine.store.work(work["id"]) == before
    assert engine.store.workflow(work["workflow"])["calls"] == calls


@pytest.mark.parametrize("command", [["prototype", "preview"], ["prototype-preview"]])
def test_preview_honors_session_when_multiple_frontend_runs_are_pending(frontend, monkeypatch, command):
    root, engine, work = frontend
    other = engine.start("run", "Second run")
    assert other["status"] == "WAITING", other["failure"]
    selected = []
    monkeypatch.setattr(prototypes, "preview", lambda engine, work, **kwargs: selected.append(work["id"]) or 0)
    assert main([*command, "--project", str(root), "--session", work["id"]]) == 0
    assert selected == [work["id"]]


def test_preview_automatically_allocates_a_port_and_prints_the_gallery_url(frontend, monkeypatch, capsys):
    _, engine, work = frontend
    monkeypatch.setattr(http.server.ThreadingHTTPServer, "serve_forever",
                        lambda self: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert prototypes.preview(engine, work) == 0
    output = capsys.readouterr().out
    match = re.search(r"http://127\.0\.0\.1:(\d+)/", output)
    assert match and 0 < int(match[1]) <= 65535
    assert "Ctrl+C" in output


def test_preview_rejects_symlinks_in_live_and_historical_packages(tmp_path):
    root = project(tmp_path)
    ref = ".auto-agents/docs/frontend_prototype/alias.html"
    path = root / ref
    path.parent.mkdir(parents=True)
    path.symlink_to(root / "value.py")
    git(root, "add", "-f", ref)
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "symlink fixture")
    assert prototypes.preview_file(root, ref) is None
    assert prototypes.preview_file(root, ref, git(root, "rev-parse", "HEAD")) is None


@pytest.mark.parametrize("url", ["https://outside.invalid/page.html", "file:///etc/passwd", "javascript:alert(1)"])
def test_gallery_frames_stay_on_the_registered_variant_routes(tmp_path, url):
    from auto_agents.prototype_variants import gallery_html

    gallery = gallery_html(tmp_path, {"variants": [{
        "id": "current", "name": "Current", "status": "candidate",
        "prototype": {"pages": [{"id": "home", "title": "Home", "html_ref": "home.html", "preview_url": url}]},
    }]})
    data = json.loads(gallery.split("const variants=", 1)[1].split(";\nfunction setup", 1)[0])
    assert data[0]["pages"][0]["url"] == "/variants/current/home.html"
