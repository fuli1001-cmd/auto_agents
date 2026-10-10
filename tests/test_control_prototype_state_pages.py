"""State URLs execute against one frozen file and keep visual scope guards."""

import pytest

from auto_agents.control import ControlError
from auto_agents.control import prototype_scope as scope
from auto_agents.control.prototype_browser import run
from auto_agents.control.prototype_pages import page_file
from test_control_prototype_contract import HTML, plan


@pytest.mark.parametrize("page", ["home.html?state=error", "home.html#loading", "nested/home.html?state=error#modal"])
def test_state_url_maps_to_its_package_file(page):
    assert page_file(page) == page.split("?", 1)[0].split("#", 1)[0]


@pytest.mark.parametrize("page", ["https://example.invalid/home.html", "//example.invalid/home.html", "/home.html", "../home.html", "%2e%2e/home.html", "nested%2f..%2fhome.html", "home.html\\other.html", "home.html\n?state=error", "http://[invalid/home.html", "%00home.html"])
def test_state_url_cannot_leave_the_package(page):
    with pytest.raises(ControlError):
        page_file(page)


def test_check_state_url_stays_on_its_flow_html_file():
    value = plan()
    value["checks"][0]["page"] = "home.html?state=error"
    baseline = {"has_frontend": True, "original": scope.encoded({"home.html": HTML.encode()}),
                "history": {}, "active_requirements": ["REQ-001"], "permissions": "Add retry only"}
    inputs = {".auto-agents/docs/requirements_trace.json": "frozen"}
    assert scope.check_schema(value, baseline, inputs) == value
    value["checks"][0]["page"] = "other.html?state=error"
    with pytest.raises(ControlError, match="same HTML file"):
        scope.check_schema(value, baseline, inputs)


def state_plan():
    return {
        "mode": "incremental", "baseline_pages": ["home.html"], "viewports": ["800x600"],
        "changes": [{"id": "retry", "page": "home.html", "selector": "#status", "kind": "change"}],
        "geometry": [], "checks": [
            {"id": "preserved", "flow": "status", "page": "home.html?state=error", "kind": "preservation",
             "steps": [], "asserts": [{"selector": "#stable", "property": "text", "equals": "Retained"}]},
            {"id": "new-state", "flow": "retry", "page": "home.html?state=error", "kind": "new",
             "steps": [], "asserts": [{"selector": "#status", "property": "text", "equals": "new-error"}]},
        ],
    }


def state_html(prefix, drift=False):
    return ('<html><meta name="viewport" content="width=device-width"><body>'
        '<section id="stable">Retained</section><section id="status"></section>'
        '<script>const error=new URLSearchParams(location.search).get("state")==="error";'
        'document.querySelector("#status").textContent=' + repr(prefix) + '+(error?"-error":"-default");'
        + ('if(error)document.querySelector("#stable").style.color="red";' if drift else '')
        + '</script></body></html>').encode()


@pytest.mark.parametrize("preserved_state", [False, True])
def test_browser_executes_query_states_and_keeps_region_masks(tmp_path, preserved_state):
    value = state_plan()
    if not preserved_state:
        value["checks"][0]["page"] = "home.html"
    original = run({"home.html": state_html("old")}, value, tmp_path / "original", baseline=True)
    candidate = run({"home.html": state_html("new")}, value, tmp_path / "candidate")
    assert original["executed"] == 1 and candidate["executed"] == 2
    assert not original["failures"] and not candidate["failures"]
    assert "800x600/home.html?state=error/initial" in original["observations"]
    assert original["observations"] == candidate["observations"]


def test_query_state_style_drift_remains_visible_outside_declared_regions(tmp_path):
    value = state_plan()
    original = run({"home.html": state_html("old")}, value, tmp_path / "original", baseline=True)
    candidate = run({"home.html": state_html("new", drift=True)}, value, tmp_path / "candidate")
    assert not candidate["failures"]
    assert original["observations"]["800x600/home.html/initial"] == candidate["observations"]["800x600/home.html/initial"]
    assert original["observations"]["800x600/home.html?state=error/initial"] != candidate["observations"]["800x600/home.html?state=error/initial"]


def test_repair_bounds_use_the_html_file_for_state_checks():
    value = plan()
    value["checks"][0]["page"] = "home.html?state=error"
    value["repairs"] = [{"id": "restore", "flow_ids": [value["flows"][0]["id"]]}]
    regions, _, files = scope.repair_bounds(value)
    assert regions and {r["page"] for r in regions} == {"home.html"}
    assert files == {"manifest.json", "home.html"}


def test_state_urls_reach_the_verified_prototype_gate(tmp_path):
    import json
    from test_control_prototype_contract import Iteration, setup

    class States(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare":
                reply = json.loads(result["text"])
                for check in reply["scope"]["checks"]:
                    check["page"] += "?scenario=" + check["id"]
                result["text"] = json.dumps(reply)
            return result

    _, engine = setup(tmp_path, States())
    work = engine.start("run", "Add retry")
    assert work["status"] == "WAITING" and work["context"]["approval"] == "prototype", work["failure"]
    frozen = scope.get(engine.store, "scope", work["context"]["prototype_scope"])
    assert all("?scenario=" in check["page"] for check in frozen["scope"]["checks"])
    scope.require_certificate(engine, work, engine.context(work), scope.package(engine.context(work).workspace_root, scope.CANONICAL))
