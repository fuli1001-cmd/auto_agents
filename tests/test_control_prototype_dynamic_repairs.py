"""Restored nodes may appear after a frozen action, but remain bounded."""

import json

import pytest

from auto_agents.control import prototype_scope as scope
from auto_agents.control.prototype_browser import run
from test_control_prototype_contract import HTML, Iteration, setup, write_package


def dynamic_plan():
    return {
        "mode": "reuse", "baseline_pages": ["home.html"], "viewports": ["800x600"],
        "changes": [], "geometry": [], "repair_has_original": True, "repair_original": False,
        "repair_regions": [{"page": "home.html", "selector": "#page-two-card", "kind": "add"}],
        "repair_geometry": [], "checks": [{
            "id": "pagination", "flow": "pagination", "page": "home.html", "kind": "preservation",
            "steps": [{"action": "click", "selector": "#next"}],
            "asserts": [{"selector": "#page-two-card", "property": "text", "equals": "Second page"}],
        }],
    }


def dynamic_html(script):
    return ('<html><meta name="viewport" content="width=device-width"><body>'
            '<section id="stable"><p>Keep</p><p>Existing</p><p>Appearance</p><button>Control</button></section>'
            '<section id="list"></section><button id="next">Next</button>'
            '<script>document.querySelector("#next").onclick=()=>{' + script + '};</script></body></html>').encode()


ADD = "const c=document.createElement('article');c.id='page-two-card';c.textContent='Second page';document.querySelector('#list').append(c);"


def test_dynamic_repair_is_observed_after_pagination_in_each_viewport(tmp_path):
    value = dynamic_plan()
    value["viewports"].append("390x844")
    report = run({"home.html": dynamic_html(ADD)}, value, tmp_path, baseline=True)
    assert report["executed"] == 2 and not report["failures"]
    assert len(report["repair_observations"]) == 4


def test_repair_can_be_present_only_in_a_frozen_query_state(tmp_path):
    value = dynamic_plan()
    value["checks"][0].update(page="home.html?state=second", steps=[])
    html = dynamic_html("").replace(b"</script>", (
        "if(location.search==='?state=second'){" + ADD + "}</script>"
    ).encode())
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert not report["failures"] and report["executed"] == 1


def test_never_observed_repair_is_not_silently_admitted(tmp_path):
    value = dynamic_plan()
    value["checks"][0].update(steps=[], asserts=[{"selector": "#stable", "property": "visible", "equals": True}])
    report = run({"home.html": dynamic_html("")}, value, tmp_path, baseline=True)
    assert any("never observed" in f["error"]["message"] for f in report["failures"])
    assert report["failures"][-1]["error"]["details"]["selector"] == "#page-two-card"


@pytest.mark.parametrize("script", [
    ADD + ADD.replace("const c=", "const duplicate=").replace("c.", "duplicate.").replace("append(c)", "append(duplicate)"),
    "const c=document.createElement('main');c.id='page-two-card';while(document.body.firstElementChild)c.append(document.body.firstElementChild);document.body.append(c);",
])
def test_dynamic_duplicate_or_page_sized_region_remains_rejected(tmp_path, script):
    value = dynamic_plan()
    value["checks"][0]["asserts"] = [{"selector": "#page-two-card", "property": "count", "equals": 2 if "duplicate" in script else 1}]
    report = run({"home.html": dynamic_html(script)}, value, tmp_path, baseline=True)
    assert any("Baseline repair region" in f["error"]["message"] or "entire page" in f["error"]["message"] for f in report["failures"])


def test_original_does_not_need_the_added_dynamic_region(tmp_path):
    value = dynamic_plan()
    value["repair_original"] = True
    value["mode"] = "incremental"
    value["changes"] = [{"id": "restored-card", "page": "home.html", "selector": "#page-two-card", "kind": "change"}]
    report = run({"home.html": dynamic_html("")}, value, tmp_path, baseline=True)
    assert report["failures"] and all("never observed" not in f["error"]["message"] for f in report["failures"])


@pytest.mark.parametrize("baseline", [True, False])
def test_changed_or_added_region_can_appear_after_a_frozen_action(tmp_path, baseline):
    value = dynamic_plan()
    value.pop("repair_regions")
    value["mode"] = "incremental"
    value["changes"] = [{"id": "card", "page": "home.html", "selector": "#page-two-card", "kind": "change" if baseline else "add"}]
    value["checks"][0]["kind"] = "preservation" if baseline else "new"
    report = run({"home.html": dynamic_html(ADD)}, value, tmp_path, baseline=baseline)
    assert report["executed"] == 1 and not report["failures"]


def test_changed_region_can_be_absent_in_empty_list_state(tmp_path):
    value = dynamic_plan()
    value.pop("repair_regions")
    value["mode"] = "incremental"
    value["changes"] = [{"id": "card", "page": "home.html", "selector": "#page-two-card", "kind": "change"}]
    value["checks"][0].update(steps=[], asserts=[{"selector": "#next", "property": "visible", "equals": True}])
    value["checks"].append({**value["checks"][0], "id": "empty", "page": "home.html?state=empty"})
    html = dynamic_html("").replace(b"</script>", ("if(!location.search){" + ADD + "}</script>").encode())
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert report["executed"] == 2 and not report["failures"]


def test_removed_region_must_stay_absent_after_actions(tmp_path):
    value = dynamic_plan()
    value.pop("repair_regions")
    value["mode"] = "incremental"
    value["changes"] = [{"id": "card", "page": "home.html", "selector": "#page-two-card", "kind": "remove"}]
    report = run({"home.html": dynamic_html(ADD)}, value, tmp_path)
    assert any("not removed" in f["error"]["message"] for f in report["failures"])


def test_missing_change_is_not_silently_admitted(tmp_path):
    value = dynamic_plan()
    value.pop("repair_regions")
    value["mode"] = "incremental"
    value["changes"] = [{"id": "card", "page": "home.html", "selector": "#page-two-card", "kind": "change"}]
    value["checks"][0].update(steps=[], asserts=[{"selector": "#next", "property": "visible", "equals": True}])
    report = run({"home.html": dynamic_html("")}, value, tmp_path, baseline=True)
    assert any("Changed region was never observed" in f["error"]["message"] for f in report["failures"])


def test_dynamic_baseline_restoration_reaches_the_certified_prototype_gate(tmp_path):
    class Paginated(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare":
                html = HTML.replace("document.querySelector('#page').textContent='2'", (
                    "document.querySelector('#page').textContent='2';"
                    "const card=document.createElement('article');card.id='page-two-card';"
                    "card.textContent='Second page';document.querySelector('#pagination').append(card)"
                ))
                write_package(ctx.workspace_root, scope.PREPARED, html)
                reply = json.loads(result["text"])
                reply["scope"]["repairs"] = [{
                    "id": "restore-page-two", "flow_ids": ["pagination"],
                    "evidence": [scope.CANONICAL + "/home.html"], "reason": "Restore second-page card",
                    "regions": [
                        {"page": "home.html", "selector": "#pagination", "kind": "change"},
                        {"page": "home.html", "selector": "#page-two-card", "kind": "add"},
                    ], "files": ["home.html"],
                }]
                next(c for c in reply["scope"]["checks"] if c["id"] == "pagination")["asserts"].append(
                    {"selector": "#page-two-card", "property": "text", "equals": "Second page"},
                )
                result["text"] = json.dumps(reply)
            return result

    _, engine = setup(tmp_path, Paginated())
    work = engine.start("run", "Add retry and restore existing pagination")
    assert work["status"] == "WAITING" and work["context"]["approval"] == "prototype", work["failure"]
    scope.require_certificate(engine, work, engine.context(work), scope.package(engine.context(work).workspace_root, scope.CANONICAL))
