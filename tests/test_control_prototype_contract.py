"""Actual browser and ledger regressions for incremental prototype contracts."""

import copy
import json
from pathlib import Path

import pytest

from auto_agents.control import ControlError, Store
from auto_agents.control.engine import Engine
from auto_agents.control.effects import Verifier
from auto_agents.control import prototype_scope as scope, prototypes
from auto_agents.control.prototype_browser import run
from auto_agents.control.workspace import git
from auto_agents.models import ProjectConfig
from test_control_engine import Worker, project

HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width"><style>
body{font-family:Arial;color:rgb(20,20,20)} section{padding:8px} #recovery{height:40px}
</style></head><body>
<section id="duration"><select id="duration-mode"><option value="auto" selected>Smart estimate</option><option value="manual">Manual</option></select>
<button id="assess" onclick="document.querySelector('#confirmation').hidden=false">Assess</button>
<div id="confirmation" hidden><button id="confirm" onclick="document.querySelector('#effective').textContent='12'">Accept recommendation</button></div></section>
<section id="information"><span id="requested">10</span><span id="effective">10</span><span id="actual">11</span></section>
<section id="pagination"><span id="page">1</span><button id="next" onclick="document.querySelector('#page').textContent='2'">Next</button></section>
<section id="creation"><input id="story"><button id="create" onclick="const c=document.createElement('article');c.className='card';c.textContent=document.querySelector('#story').value;document.querySelector('#cards').append(c)">Create</button><div id="cards"><article class="card">Existing</article></div><span id="toast"></span></section>
<section id="player"><input id="seek" type="range" min="0" max="100" value="0" oninput="document.querySelector('#position').textContent=this.value"><output id="position">0</output></section>
<section id="recovery"><p>Existing status</p></section>
</body></html>"""
NEW = '<button id="retry" onclick="document.querySelector(\'#retry-state\').textContent=\'retrying\'">Retry</button><output id="retry-state">idle</output>'


def rule(selector, prop, expected):
    return {"selector": selector, "property": prop, "equals": expected}


def plan(mode="incremental"):
    flows, checks = [], []

    def check(identity, steps, assertions):
        flows.append(
            {
                "id": identity,
                "page": "home.html",
                "requirement_ids": ["REQ-001"],
                "evidence": [scope.CANONICAL + "/home.html"],
                "checks": [identity],
            }
        )
        checks.append(
            {
                "id": identity,
                "flow": identity,
                "page": "home.html",
                "kind": "preservation",
                "steps": steps,
                "asserts": assertions,
            }
        )

    check(
        "smart-duration",
        [],
        [
            rule("#duration-mode", "value", "auto"),
            rule("#duration-mode option", "count", 2),
        ],
    )
    check(
        "duration-confirmation",
        [
            {"action": "click", "selector": "#assess"},
            {"action": "click", "selector": "#confirm"},
        ],
        [rule("#effective", "text", "12")],
    )
    check(
        "three-durations",
        [],
        [
            rule("#requested", "text", "10"),
            rule("#effective", "text", "10"),
            rule("#actual", "text", "11"),
        ],
    )
    check(
        "pagination",
        [{"action": "click", "selector": "#next"}],
        [rule("#page", "text", "2")],
    )
    check(
        "creation",
        [
            {"action": "fill", "selector": "#story", "value": "New story"},
            {"action": "click", "selector": "#create"},
        ],
        [rule(".card", "count", 2), rule(".card:last-child", "text", "New story")],
    )
    check(
        "seeking",
        [{"action": "fill", "selector": "#seek", "value": "60"}],
        [rule("#seek", "value", "60"), rule("#position", "text", "60")],
    )
    changes = []
    if mode != "reuse":
        flows.append(
            {
                "id": "recovery",
                "page": "home.html",
                "requirement_ids": ["REQ-001"],
                "evidence": ["goal"],
                "checks": ["recovery"],
            }
        )
        checks.append(
            {
                "id": "recovery",
                "flow": "recovery",
                "page": "home.html",
                "kind": "new",
                "steps": [{"action": "click", "selector": "#retry"}],
                "asserts": [rule("#retry-state", "text", "retrying")],
            }
        )
        changes = [
            {
                "id": "recovery",
                "kind": "change",
                "page": "home.html",
                "selector": "#recovery",
                "files": ["home.html"],
                "requirement_ids": ["REQ-001"],
                "evidence": ["goal"],
                "because": [],
                "reason": "Add retry in the existing status region",
            }
        ]
    return {
        "version": 1,
        "mode": mode,
        "reason": "Preserve existing flows; add only recovery",
        "repairs": [],
        "changes": changes,
        "flows": flows,
        "checks": checks,
        "geometry": [],
        "viewports": ["1000x800"],
    }


def write_package(root, prefix, html=HTML):
    directory = root / prefix
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "home.html").write_text(html)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "id": "home",
                        "title": "Workbench",
                        "route": "/",
                        "requirement_ids": ["REQ-001"],
                        "html_ref": prefix + "/home.html",
                    }
                ],
                "index_ref": prefix + "/home.html",
                "viewports": ["1000x800"],
            }
        )
    )


class Iteration(Worker):
    def __init__(self, *, mode="incremental", repair=False, mutation=None, review=True):
        super().__init__()
        self.mode, self.repair, self.mutation, self.review = (
            mode,
            repair,
            mutation,
            review,
        )
        self.scope_seen = []

    def run(self, ctx, phase, prompt, output, **kwargs):
        if phase in {
            "prototype_prepare",
            "prototype_scope_review",
            "prototype",
            "prototype_review",
        }:
            self.calls.append((ctx.work_id, phase))
            if phase == "prototype_prepare":
                value = plan(self.mode)
                if self.repair:
                    write_package(ctx.workspace_root, scope.PREPARED)
                    value["repairs"] = [
                        {
                            "id": "restore-auto",
                            "flow_ids": ["smart-duration"],
                            "evidence": ["workbench/src/app/page.tsx"],
                            "reason": "Implementation still offers automatic duration",
                            "files": ["home.html"],
                            "geometry": [
                                {
                                    "page": "home.html",
                                    "selector": "#assess",
                                    "properties": ["x"],
                                }
                            ],
                        }
                    ]
                result = {"scope": value}
            elif phase == "prototype":
                prefix = scope.CANONICAL + (
                    "_variants/" + ctx.contract.inputs["variant_only"]
                    if ctx.contract.inputs.get("variant_only")
                    else ""
                )
                path = ctx.workspace_root / prefix / "home.html"
                content = path.read_text().replace("<p>Existing status</p>", NEW)
                if self.mutation:
                    content = self.mutation(content)
                path.write_text(content)
                result = {
                    "artifacts": [prefix + "/home.html", prefix + "/manifest.json"]
                }
            else:
                self.scope_seen.append(prompt)
                result = {
                    "decision": "pass" if self.review else "revise",
                    "coverage_complete": self.review,
                    "reason": "Compared all six source flows, permitted recovery region, assertions and browser evidence",
                }
            return {"text": json.dumps(result)}
        result = super().run(ctx, phase, prompt, output, **kwargs)
        if phase == "clarify":
            value = json.loads(result["text"])
            value["frontend"] = True
            result["text"] = json.dumps(value)
        return result


def setup(tmp_path, worker=None, *, original=HTML):
    root = project(tmp_path)
    write_package(root, scope.CANONICAL, original)
    source = root / "workbench/src/app/page.tsx"
    source.parent.mkdir(parents=True)
    source.write_text(
        "// Existing requirements: auto/manual duration; assessment confirmation; requested/effective/actual durations; pagination; card creation; seeking\nexport default function Home(){return <main/>}"
    )
    git(root, "add", "-f", scope.CANONICAL, "workbench")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "Existing frontend",
    )
    config = ProjectConfig("incremental")
    config.frontend_design.mode = "off"
    engine = Engine(
        root,
        Store(root),
        config,
        transport=worker or Iteration(),
        auto_approve=True,
        print_fn=lambda *args: None,
    )
    return root, engine


@pytest.fixture(scope="module")
def baseline_observations(tmp_path_factory):
    root = tmp_path_factory.mktemp("prototype-browser")
    value = plan()
    value["baseline_pages"] = ["home.html"]
    report = run({"home.html": HTML.encode()}, value, root / "baseline", baseline=True)
    assert not report["failures"] and report["executed"] == 6
    return root, value, report


@pytest.mark.parametrize(
    "mutation",
    [
        lambda html: html.replace(
            '<option value="auto" selected>Smart estimate</option>', ""
        ),
        lambda html: html.replace('id="assess"', 'id="assess" disabled'),
        lambda html: html.replace('<span id="actual">11</span>', ""),
        lambda html: html.replace('id="next"', 'id="next" disabled'),
        lambda html: html.replace(
            "document.querySelector('#cards').append(c)",
            "document.querySelector('#toast').textContent='success'",
        ),
        lambda html: html.replace('id="seek"', 'id="seek" disabled'),
        lambda html: html.replace('value="auto" selected', 'value="auto"').replace(
            'value="manual"', 'value="manual" selected'
        ),
    ],
)
def test_browser_catches_six_lost_flows_and_changed_default(
    baseline_observations, tmp_path, mutation
):
    _, value, original = baseline_observations
    candidate = mutation(HTML.replace("<p>Existing status</p>", NEW))
    result = run({"home.html": candidate.encode()}, value, tmp_path, baseline=False)
    assert result["failures"]
    assert result["executed"] == 7


def test_browser_rejects_unrelated_style_but_allows_causal_geometry(
    baseline_observations, tmp_path
):
    _, value, original = baseline_observations
    files = {"home.html": HTML.replace("<p>Existing status</p>", NEW).encode()}
    result = run(files, value, tmp_path / "allowed")
    assert not result["failures"]
    assert result["observations"] == original["observations"]
    for identity, before, after in (
        ("font", "font-family:Arial", "font-family:serif"),
        ("color", "rgb(20,20,20)", "rgb(50,50,50)"),
    ):
        changed = {
            "home.html": files["home.html"].replace(before.encode(), after.encode())
        }
        report = run(changed, value, tmp_path / identity)
        assert (
            not report["failures"]
            and report["observations"] != original["observations"]
        )
    # Height is explicitly authorized only for the affected ancestor. Appearance
    # properties remain compared, and cannot be exempted by a geometry entry.
    value = copy.deepcopy(value)
    value["geometry"] = [
        {
            "page": "home.html",
            "selector": "#recovery",
            "properties": ["height"],
            "change_ids": ["recovery"],
        }
    ]
    report = run(
        {"home.html": files["home.html"].replace(b"height:40px", b"height:70px")},
        value,
        tmp_path / "geometry",
    )
    # The untouched body also grows, requiring its causal geometry declaration;
    # whole-page style changes are never excused by a local region.
    assert report["observations"] != original["observations"]
    value["geometry"] += [
        {
            "page": "home.html",
            "selector": selector,
            "properties": ["height"],
            "change_ids": ["recovery"],
        }
        for selector in ("html", "body")
    ]
    baseline = run(
        {"home.html": HTML.encode()},
        value,
        tmp_path / "geometry-baseline",
        baseline=True,
    )
    report = run(
        {"home.html": files["home.html"].replace(b"height:40px", b"height:70px")},
        value,
        tmp_path / "geometry-permitted",
    )
    assert not report["failures"] and report["observations"] == baseline["observations"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(checks=[]),
        lambda s: s.update(flows=[]),
        lambda s: s["changes"][0].update(selector="body, *"),
        lambda s: s["changes"][0].update(evidence=[]),
        lambda s: s["changes"][0].update(kind="linkage", because=[]),
        lambda s: s["changes"][0].update(kind="remove"),
        lambda s: s.update(mode="redesign"),
        lambda s: s["checks"][0].update(asserts=[]),
        lambda s: s["checks"][0]["asserts"][0].update(property="javascript"),
        lambda s: s["checks"][0].update(
            steps=[{"action": "evaluate", "selector": "#recovery", "value": "true"}]
        ),
        lambda s: s["geometry"].append(
            {
                "page": "home.html",
                "selector": "#pagination",
                "properties": ["color"],
                "change_ids": ["recovery"],
            }
        ),
        lambda s: s["checks"][0].update(kind=None),
    ],
)
def test_scope_rejects_empty_weakened_unbounded_or_unauthorized_contracts(mutation):
    value = plan()
    mutation(value)
    baseline = {
        "has_frontend": True,
        "original": scope.encoded({"home.html": HTML.encode()}),
        "history": {},
        "active_requirements": ["REQ-001"],
        "permissions": "Add retry only.",
    }
    with pytest.raises(ControlError):
        scope.check_schema(
            value, baseline, {".auto-agents/docs/requirements_trace.json": "frozen"}
        )


def test_explicit_redesign_preserves_flow_oracles():
    value = plan()
    value["mode"] = "redesign"
    permission = "Redesign the entire workbench while retaining all existing behaviors."
    value["changes"][0]["permission"] = permission
    baseline = {
        "has_frontend": True,
        "original": scope.encoded({"home.html": HTML.encode()}),
        "history": {},
        "active_requirements": ["REQ-001"],
        "permissions": permission,
    }
    assert (
        scope.check_schema(
            value, baseline, {".auto-agents/docs/requirements_trace.json": "frozen"}
        )
        == value
    )
    value["checks"] = [c for c in value["checks"] if c["kind"] == "new"]
    value["flows"] = [f for f in value["flows"] if f["id"] == "recovery"]
    with pytest.raises(ControlError, match="preserved behavior"):
        scope.check_schema(
            value, baseline, {".auto-agents/docs/requirements_trace.json": "frozen"}
        )


def test_browser_rejects_whole_page_container_disguised_as_local_region(tmp_path):
    value = plan()
    value["changes"][0]["selector"] = "#whole-page"
    files = {
        "home.html": HTML.replace("<body>", '<body><main id="whole-page">')
        .replace("</body>", "</main></body>")
        .encode()
    }
    with pytest.raises(ControlError, match="entire page"):
        run(files, value, tmp_path, baseline=True)


def test_unchanged_files_are_byte_preserved():
    value = {
        **plan(),
        "baseline": scope.encoded(
            {"logo.svg": b"original", "home.html": HTML.encode()}
        ),
    }
    with pytest.raises(ControlError, match="undeclared file"):
        scope.verify_package(
            {"logo.svg": b"changed", "home.html": HTML.encode()}, value
        )


def test_incremental_pipeline_freezes_scope_and_certificate_before_gate(tmp_path):
    root, engine = setup(tmp_path)
    work = engine.start("run", "Add failed project retry in the recovery status region")
    assert work["status"] == "WAITING", work["failure"]
    assert [p for _, p in engine.transport.calls] == [
        "clarify",
        "prototype_prepare",
        "prototype_scope_review",
        "prototype",
        "prototype_review",
    ]
    ctx = engine.context(work)
    frozen = scope.get(engine.store, "scope", work["context"]["prototype_scope"])
    assert frozen["scope"]["changes"][0]["selector"] == "#recovery"
    assert len(frozen["scope"]["flows"]) == 7
    scope.require_certificate(
        engine, work, ctx, scope.package(ctx.workspace_root, scope.CANONICAL)
    )
    assert not any(
        "prototype_preparation" in n for n in work["context"]["candidate"]["paths"]
    )
    assert (root / scope.CANONICAL / "home.html").read_text() == HTML
    gallery, _ = prototypes.preview_gallery(engine, work)
    assert b"repaired-baseline" in gallery
    html = ctx.workspace_root / scope.CANONICAL / "home.html"
    html.write_text(html.read_text().replace("Smart estimate", "Removed"))
    with pytest.raises(ControlError):
        prototypes.approve(engine, work)
    assert engine.store.work(work["id"])["status"] == "WAITING"


def test_incomplete_baseline_is_repaired_and_reuse_skips_generation(tmp_path):
    original = HTML.replace('<option value="auto" selected>Smart estimate</option>', "")
    root, engine = setup(
        tmp_path, Iteration(mode="reuse", repair=True), original=original
    )
    work = engine.start(
        "run", "Repair the missing automatic duration option; no new UI requested"
    )
    assert work["status"] == "WAITING", work["failure"]
    assert "prototype" not in [p for _, p in engine.transport.calls]
    frozen = scope.get(engine.store, "scope", work["context"]["prototype_scope"])
    assert frozen["scope"]["repairs"][0]["id"] == "restore-auto"
    assert scope.decoded(frozen["original"])["home.html"].decode() == original
    assert (
        engine.context(work).workspace_root / scope.CANONICAL / "home.html"
    ).read_text() == HTML
    assert (root / scope.CANONICAL / "home.html").read_text() == original


def test_budget_shortage_cannot_silently_approve_or_increase_limits(tmp_path):
    _, engine = setup(tmp_path)
    work = engine.start("run", "Add retry", max_calls=3)
    assert work["status"] == "BLOCKED" and work["failure"]["category"] == "budget"
    workflow = engine.store.workflow(work["workflow"])
    assert workflow["calls"] == workflow["max_calls"] == 3
    assert "prototype_certificate" not in work["context"]


def test_independent_review_and_zero_browser_execution_cannot_reach_gate(tmp_path):
    _, engine = setup(tmp_path)

    class Empty(Verifier):
        def prototype(self, *args, **kwargs):
            return {
                "executed": 0,
                "failures": [],
                "observations": {},
                "screenshots": {},
            }

    engine.verifier = Empty()
    work = engine.start("run", "Add retry")
    assert work["status"] == "BLOCKED"
    assert "prototype_certificate" not in work["context"]


def test_scope_record_tampering_is_detected(tmp_path):
    _, engine = setup(tmp_path)
    identity = scope.put(engine.store, "scope", {"checks": ["strong"]})
    engine.store.set_meta("prototype_scope:" + identity, {"checks": []})
    with pytest.raises(ControlError, match="missing or changed"):
        scope.get(engine.store, "scope", identity)


def test_missing_browser_is_an_environment_failure_not_a_pass(tmp_path, monkeypatch):
    from auto_agents.control import prototype_browser

    _, engine = setup(tmp_path)
    monkeypatch.setattr(prototype_browser.shutil, "which", lambda name: None)
    work = engine.start("run", "Add retry")
    assert work["status"] == "BLOCKED"
    assert work["failure"]["category"] == "environment"
    assert [p for _, p in engine.transport.calls] == ["clarify", "prototype_prepare"]
    assert "prototype_certificate" not in work["context"]


def test_result_review_is_mandatory_and_repairs_stay_bounded(tmp_path):
    class RejectResult(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_review":
                result["text"] = json.dumps(
                    {
                        "decision": "revise",
                        "coverage_complete": False,
                        "reason": "The semantic result still needs revision",
                    }
                )
            return result

    _, engine = setup(tmp_path, RejectResult())
    work = engine.start("run", "Add retry")
    assert work["status"] == "BLOCKED"
    assert len([p for _, p in engine.transport.calls if p == "prototype"]) == 3
    assert len([p for _, p in engine.transport.calls if p == "prototype_prepare"]) == 1
    assert "prototype_certificate" not in work["context"]


def test_legacy_gate_is_rechecked_without_replaying_its_producer(tmp_path):
    _, engine = setup(tmp_path)
    work = engine.start("run", "Add retry")
    assert work["status"] == "WAITING", work["failure"]
    source_contract = work["contract"]
    old_producer = next(
        o for o in engine.store.operations(work["id"]) if o["kind"] == "model:prototype"
    )
    native = engine.store.outcome(old_producer["id"])
    state = {k: v for k, v in work["context"].items() if not k.startswith("prototype_")}
    state["approval_phase"] = "prototype"
    state.pop("frontend", None)
    work = engine.store.transition(work, "WAITING", context=state)
    with pytest.raises(ControlError, match="resume the session"):
        prototypes.approve(engine, work)
    resumed = engine.resume(work["id"])
    assert resumed["status"] == "WAITING", resumed["failure"]
    assert resumed["contract"] == source_contract
    assert len([p for _, p in engine.transport.calls if p == "prototype"]) == 1
    assert engine.store.outcome(old_producer["id"])["payload"] == native["payload"]
    assert resumed["context"]["prototype_certificate"]


def test_confirmed_browser_receipt_survives_interrupted_settlement(
    tmp_path, monkeypatch
):
    _, engine = setup(tmp_path)
    settle = engine.store.settle
    interrupted = []
    browser_calls = []
    verify = engine.verifier.prototype

    def tracked(*args, **kwargs):
        browser_calls.append(kwargs.get("baseline", False))
        return verify(*args, **kwargs)

    def stop_after_native(identity, result, **kwargs):
        operation = next(o for o in engine.store.operations() if o["id"] == identity)
        if operation["kind"] == "prototype_browser:candidate" and not interrupted:
            interrupted.append(identity)
            raise KeyboardInterrupt()
        return settle(identity, result, **kwargs)

    monkeypatch.setattr(engine.verifier, "prototype", tracked)
    monkeypatch.setattr(engine.store, "settle", stop_after_native)
    with pytest.raises(KeyboardInterrupt):
        engine.start("run", "Add retry")
    root = engine.store.meta("active_root")
    calls = list(browser_calls)
    native = engine.store.outcome(interrupted[0])
    assert native["native_state"] == "confirmed"
    resumed = engine.resume(root)
    assert resumed["status"] == "WAITING", resumed["failure"]
    assert browser_calls == calls
    assert engine.store.outcome(interrupted[0])["payload"] == native["payload"]
    assert len([p for _, p in engine.transport.calls if p == "prototype"]) == 1


@pytest.mark.parametrize("input_failure", [False, True])
def test_answer_cannot_bypass_human_approval(tmp_path, capsys, input_failure):
    from auto_agents.config import save_project_config
    from auto_agents.control.cli import main

    root, engine = setup(tmp_path)
    save_project_config(root, engine.config)
    work = engine.start("run", "Add retry")
    if input_failure:
        work = engine.store.transition(
            work,
            "WAITING",
            failure={
                "code": "operator_input_changed",
                "category": "operator",
                "message": "Changed binding",
            },
        )
    before = engine.store.work(work["id"])
    assert (
        main(
            [
                "answer",
                "--project",
                str(root),
                "--session",
                work["id"],
                "--value",
                "yes",
                "--no-resume",
            ]
        )
        == 3
    )
    capsys.readouterr()
    assert engine.store.work(work["id"]) == before


def test_original_scope_is_frozen_again_only_after_explicit_expansion_answer(
    tmp_path, capsys
):
    from auto_agents.config import save_project_config
    from auto_agents.control.cli import main

    class AskExpansion(Iteration):
        asked = False

        def run(self, ctx, phase, *args, **kwargs):
            if phase == "prototype" and not self.asked:
                self.asked = True
                self.calls.append((ctx.work_id, phase))
                return {
                    "text": json.dumps(
                        {
                            "questions": [
                                "Should the pagination controls also move? Otherwise keep the existing layout."
                            ]
                        }
                    )
                }
            return super().run(ctx, phase, *args, **kwargs)

    root, engine = setup(tmp_path, AskExpansion())
    save_project_config(root, engine.config)
    work = engine.start("run", "Add retry")
    assert work["status"] == "WAITING" and work["context"]["waiting_for"] == "user"
    old_scope = work["context"]["prototype_scope"]
    assert (
        main(
            [
                "answer",
                "--project",
                str(root),
                "--session",
                work["id"],
                "--value",
                "Keep the existing pagination layout.",
                "--no-resume",
            ]
        )
        == 0
    )
    capsys.readouterr()
    ready = engine.store.work(work["id"])
    assert (
        ready["phase"] == "prototype_prepare"
        and "prototype_scope" not in ready["context"]
    )
    resumed = engine.resume(work["id"])
    assert resumed["status"] == "WAITING", resumed["failure"]
    assert resumed["context"]["prototype_scope"] != old_scope
    assert len([p for _, p in engine.transport.calls if p == "prototype_prepare"]) == 2


def test_new_frontend_freezes_catalog_choice_without_iteration_reselection(
    tmp_path, monkeypatch
):
    from auto_agents.control.effects import Provider
    from auto_agents.frontend_design import (
        AwesomeDesignCatalogClient,
        CatalogEntry,
        CatalogSnapshot,
    )
    from test_control_domains import Frontend

    root = project(tmp_path)
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    (catalog_root / "LICENSE").write_text("MIT\n")
    entries = []
    for slug in ("alpha", "beta", "gamma"):
        path = catalog_root / (slug + "/DESIGN.md")
        path.parent.mkdir()
        path.write_text("# " + slug + "\nPreserve exact bytes.\n")
        entries.append(
            CatalogEntry(
                slug=slug,
                name=slug,
                category="SaaS",
                description="fixture",
                design_path=slug + "/DESIGN.md",
            )
        )
    snapshot = CatalogSnapshot(
        "fixture/catalog", "", "c" * 40, catalog_root, tuple(entries), True
    )
    loads = []

    def catalog(client):
        loads.append(client)
        return snapshot

    monkeypatch.setattr(AwesomeDesignCatalogClient, "load", catalog)
    selection = {
        "selected_slug": "alpha",
        "candidates": [
            {"slug": slug, "score": score, "rationale": "Fixture fit", "risks": []}
            for slug, score in (("alpha", 95), ("beta", 80), ("gamma", 70))
        ],
    }

    class Choice(Frontend):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase in {"prototype_prepare", "prototype"}:
                value = json.loads(result["text"])
                value["selection"] = selection
                result["text"] = json.dumps(value)
            return result

    class LocalProvider(Provider):
        def run(self, *args, **kwargs):
            return Choice().run(*args, **kwargs)

    config = ProjectConfig("catalog fixture")
    engine = Engine(
        root,
        Store(root),
        config,
        transport=LocalProvider(config),
        auto_approve=True,
        print_fn=lambda *args: None,
    )
    work = engine.start("run", "Create a frontend")
    assert work["status"] == "WAITING", work["failure"]
    assert len(loads) == 1
    frozen = scope.get(engine.store, "scope", work["context"]["prototype_scope"])
    assert frozen["selection"] == selection
    assert (engine.context(work).workspace_root / "DESIGN.md").read_bytes() == (
        catalog_root / "alpha/DESIGN.md"
    ).read_bytes()
    cert = scope.get(
        engine.store, "certificate", work["context"]["prototype_certificate"]
    )
    forged = scope.put(
        engine.store, "certificate", {**cert, "review": frozen["scope_review"]}
    )
    with pytest.raises(ControlError, match="another candidate"):
        scope.require_certificate(
            engine,
            work,
            engine.context(work),
            scope.package(engine.context(work).workspace_root, scope.CANONICAL),
            forged,
        )


def test_writer_boundary_protects_source_and_frozen_original(tmp_path):
    import subprocess
    import sys
    from auto_agents.control.confinement import Boundary

    root = project(tmp_path)
    frozen = tmp_path / "frozen-original.html"
    frozen.write_text("original")
    boundary = Boundary(
        root, tmp_path / "scratch", writable_paths=[scope.CANONICAL, ".tmp"]
    )
    boundary.check()
    code = "from pathlib import Path; import sys; denied=0\nfor name in sys.argv[1:]:\n try: Path(name).write_text('changed')\n except PermissionError: denied+=1\nassert denied==2\nPath('.auto-agents/docs/frontend_prototype/home.html').write_text('candidate')"
    result = subprocess.run(
        boundary.command(
            [sys.executable, "-B", "-c", code, str(root / "value.py"), str(frozen)]
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert frozen.read_text() == "original"
    assert (root / "value.py").read_text() == "VALUE = 0\n"
    assert (root / scope.CANONICAL / "home.html").read_text() == "candidate"


def test_paused_alternative_resumes_from_root_and_keeps_its_own_review(
    tmp_path, capsys
):
    from auto_agents.config import save_project_config
    from auto_agents.control.cli import main
    from auto_agents.control.interaction import pending_decisions
    from test_control_domains import Frontend

    class AskOnce(Frontend):
        asked = False

        def run(self, ctx, phase, *args, **kwargs):
            if (
                phase == "prototype"
                and ctx.contract.inputs.get("variant_only")
                and not self.asked
            ):
                self.asked = True
                return {
                    "text": json.dumps(
                        {
                            "questions": [
                                "Keep the same bounded scope for the alternative?"
                            ]
                        }
                    )
                }
            return super().run(ctx, phase, *args, **kwargs)

    root = project(tmp_path)
    config = ProjectConfig("alternative continuation")
    config.frontend_design.mode = "off"
    save_project_config(root, config)
    engine = Engine(
        root,
        Store(root),
        config,
        transport=AskOnce(),
        auto_approve=True,
        print_fn=lambda *args: None,
    )
    work = engine.start("run", "Create a frontend")
    waiting = prototypes.generate(engine, work, "Alternative inside the same scope")
    assert waiting["status"] == "WAITING"
    child = waiting["context"]["prototype_addon"]
    decisions = pending_decisions(engine.store, waiting)
    assert (
        len(decisions) == 1
        and decisions[0]["work_id"] == child
        and decisions[0]["kind"] == "user"
    )
    assert (
        main(
            [
                "answer",
                "--project",
                str(root),
                "--session",
                child,
                "--value",
                "Keep the same scope.",
                "--no-resume",
            ]
        )
        == 0
    )
    capsys.readouterr()
    resumed = engine.resume(work["id"])
    assert resumed["status"] == "WAITING", resumed["failure"]
    assert "prototype_addon" not in resumed["context"]
    rows = prototypes.candidates(engine, resumed)
    assert len(rows) == 2 and rows[-1]["certificate"]
    state = prototypes.approve(engine, resumed, rows[-1]["id"])
    approved = engine.store.transition(resumed, "WAITING", context=state)
    prototypes.preview_gallery(engine, approved)


def test_binary_baseline_and_full_dom_reports_stay_out_of_model_prompts(tmp_path):
    class Capture(Iteration):
        prompts = []

        def run(self, ctx, phase, prompt, *args, **kwargs):
            self.prompts.append(prompt)
            return super().run(ctx, phase, prompt, *args, **kwargs)

    root, engine = setup(tmp_path, Capture())
    asset = root / scope.CANONICAL / "background.svg"
    asset.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><!--'
        + "large asset " * 50000
        + "--></svg>"
    )
    manifest = root / scope.CANONICAL / "manifest.json"
    data = json.loads(manifest.read_text())
    data["artifacts"] = [scope.CANONICAL + "/background.svg"]
    manifest.write_text(json.dumps(data))
    git(root, "add", "-f", scope.CANONICAL)
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "Large supporting asset",
    )
    work = engine.start("run", "Add retry")
    assert work["status"] == "WAITING", work["failure"]
    assert max(map(len, engine.transport.prompts)) < 100000
    frozen = scope.get(engine.store, "scope", work["context"]["prototype_scope"])
    assert scope.decoded(frozen["baseline"])["background.svg"] == asset.read_bytes()
    report = scope.report_input(engine, work["context"]["prototype_browser"])
    assert all(isinstance(v, str) for v in report["observations"].values())
    assert Path(report["report_path"]).is_file()


def test_declared_ancestor_shift_preserves_descendant_positions(
    baseline_observations, tmp_path
):
    _, value, _ = baseline_observations
    value = copy.deepcopy(value)
    value["geometry"] = [
        {
            "page": "home.html",
            "selector": selector,
            "properties": ["y"],
            "change_ids": ["recovery"],
        }
        for selector in ("#creation", "#player")
    ]
    original = run(
        {"home.html": HTML.encode()}, value, tmp_path / "original", baseline=True
    )
    block = '<section id="recovery"><p>Existing status</p></section>'
    candidate = HTML.replace(block, "").replace(
        '<section id="creation">',
        '<section id="recovery">' + NEW + '</section><section id="creation">',
    )
    result = run({"home.html": candidate.encode()}, value, tmp_path / "shifted")
    assert not result["failures"] and result["observations"] == original["observations"]


@pytest.mark.parametrize("record", ["valid", "missing", "changed"])
def test_historical_approval_requires_matching_admitted_bytes(tmp_path, record):
    from auto_agents.frontend_design import sha256_file
    from auto_agents.prototype_variants import artifact_bundle_sha256

    root, engine = setup(tmp_path)
    prefix = ".auto-agents/docs/frontend_prototype_variants/legacy-approved"
    write_package(root, prefix + "/frontend_prototype")
    design = root / prefix / "DESIGN.md"
    design.write_text("# Existing approved style\n")
    hashes = {
        p.relative_to(root / prefix).as_posix(): sha256_file(p)
        for p in (root / prefix).rglob("*")
        if p.is_file()
    }
    declared = dict(hashes)
    if record == "missing":
        declared = {}
    elif record == "changed":
        declared["DESIGN.md"] = "sha256:" + "0" * 64
    registry = root / ".auto-agents/state/frontend_prototype_variants.json"
    registry.write_text(
        json.dumps(
            {
                "variants": [
                    {
                        "id": "legacy-approved",
                        "status": "approved",
                        "artifact_sha256": declared,
                        "bundle_sha256": artifact_bundle_sha256(declared),
                    }
                ]
            }
        )
    )
    git(root, "add", "-f", prefix)
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "Historical prototype evidence",
    )
    work = engine.start("run", "Add retry", max_calls=1)
    assert work["status"] == "BLOCKED" and work["failure"]["category"] == "budget"
    source = scope.get(engine.store, "seed", work["context"]["prototype_seed"])
    assert source["historical_approvals"][0]["verified"] is (record == "valid")


def test_explicit_page_retirement_preserves_the_remaining_frontend(tmp_path):
    permission = "Remove the Settings page."

    class RetirePage(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare":
                value = json.loads(result["text"])
                value["scope"]["changes"].append(
                    {
                        "id": "retire-settings",
                        "kind": "remove",
                        "page": "settings.html",
                        "selector": "body",
                        "whole_page": True,
                        "files": ["settings.html"],
                        "requirement_ids": ["REQ-001"],
                        "evidence": ["goal"],
                        "permission": permission,
                        "because": [],
                        "reason": "Explicitly retired by the current goal",
                    }
                )
                result["text"] = json.dumps(value)
            if phase == "prototype":
                package = ctx.workspace_root / scope.CANONICAL
                (package / "settings.html").unlink()
                manifest = json.loads((package / "manifest.json").read_text())
                manifest["pages"] = [
                    p for p in manifest["pages"] if p["id"] != "settings"
                ]
                (package / "manifest.json").write_text(json.dumps(manifest))
            return result

    root, engine = setup(tmp_path, RetirePage())
    package = root / scope.CANONICAL
    (package / "settings.html").write_text(
        '<html><head><meta name="viewport" content="width=device-width"></head><body><main>Retired settings</main></body></html>'
    )
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["pages"].append(
        {
            "id": "settings",
            "title": "Settings",
            "route": "/settings",
            "requirement_ids": ["REQ-001"],
            "html_ref": scope.CANONICAL + "/settings.html",
        }
    )
    (package / "manifest.json").write_text(json.dumps(manifest))
    git(root, "add", "-f", scope.CANONICAL)
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "Existing settings page",
    )
    work = engine.start("run", "Add retry. " + permission)
    assert work["status"] == "WAITING", work["failure"]
    assert (package / "settings.html").is_file()
    assert not (
        engine.context(work).workspace_root / scope.CANONICAL / "settings.html"
    ).exists()


def test_baseline_repair_cannot_hide_an_unrelated_restyle(tmp_path):
    class RestyleDuringRepair(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare":
                path = ctx.workspace_root / scope.PREPARED / "home.html"
                path.write_text(
                    path.read_text().replace("rgb(20,20,20)", "rgb(200,10,10)")
                )
            return result

    original = HTML.replace('<option value="auto" selected>Smart estimate</option>', "")
    _, engine = setup(
        tmp_path, RestyleDuringRepair(mode="reuse", repair=True), original=original
    )
    work = engine.start("run", "Restore the missing automatic duration option")
    assert work["status"] == "BLOCKED"
    assert "outside its bounded repairs" in work["failure"]["message"]
    assert not any(p == "prototype" for _, p in engine.transport.calls)


def test_revised_scope_cannot_weaken_a_frozen_assertion():
    value = plan()
    required = copy.deepcopy(value["checks"][0])
    value["checks"][0]["asserts"][0]["equals"] = "manual"
    baseline = {
        "has_frontend": True,
        "original": scope.encoded({"home.html": HTML.encode()}),
        "history": {},
        "active_requirements": ["REQ-001"],
        "permissions": "Add retry",
        "required_checks": [required],
    }
    with pytest.raises(ControlError, match="weaken frozen"):
        scope.check_schema(
            value, baseline, {".auto-agents/docs/requirements_trace.json": "frozen"}
        )


def test_alternative_after_reuse_freezes_a_new_scope_and_preserves_old_checks(tmp_path):
    class AddToReusedBaseline(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare" and ctx.contract.inputs.get("variant_only"):
                result["text"] = json.dumps({"scope": plan()})
            return result

    root, engine = setup(tmp_path, AddToReusedBaseline(mode="reuse"))
    work = engine.start("run", "Retain the existing video workbench")
    assert work["status"] == "WAITING", work["failure"]
    original = scope.get(engine.store, "scope", work["context"]["prototype_scope"])
    alternative = prototypes.generate(
        engine, work, "Add a retry control in the recovery status region"
    )
    assert alternative["status"] == "WAITING", alternative["failure"]
    row = prototypes.candidates(engine, alternative)[-1]
    cert = scope.get(engine.store, "certificate", row["certificate"])
    frozen = scope.get(engine.store, "scope", cert["scope"])
    assert frozen["scope"]["mode"] == "incremental"
    assert all(c in frozen["scope"]["checks"] for c in original["scope"]["checks"])
    assert (
        b'id="retry"'
        in (engine.context(alternative).workspace_root / row["path"]).read_bytes()
    )
