"""Native form events work while prototype network submissions stay blocked."""

import pytest

from auto_agents.control.prototype_browser import run


def form_plan(steps):
    return {
        "mode": "reuse", "baseline_pages": ["home.html"], "viewports": ["800x600"],
        "changes": [], "geometry": [], "checks": [{
            "id": "submit", "flow": "submit", "page": "home.html", "kind": "preservation",
            "steps": steps, "asserts": [{"selector": "#result", "property": "text", "equals": "submitted"}],
        }],
    }


@pytest.mark.parametrize("submit", [
    {"action": "click", "selector": "#submit"},
    {"action": "press", "selector": "#query", "value": "Enter"},
    {"action": "press", "selector": "#submit", "value": "Space"},
])
def test_native_form_submit_handler_runs_with_mouse_and_enter(tmp_path, submit):
    html = b'''<html><meta name="viewport" content="width=device-width"><body>
    <form onsubmit="event.preventDefault();document.querySelector('#result').textContent='submitted'">
    <input id="query"><button id="submit" type="submit">Submit</button></form>
    <output id="result">idle</output></body></html>'''
    value = form_plan([{"action": "fill", "selector": "#query", "value": "story"}, submit])
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert report["executed"] == 1 and not report["failures"]


def test_default_form_navigation_is_still_blocked(tmp_path):
    html = b'''<html><meta name="viewport" content="width=device-width"><body>
    <form action="home.html?submitted=yes" onsubmit="document.querySelector('#result').textContent='submitted'">
    <button id="submit" type="submit">Submit</button></form><output id="result">idle</output>
    </body></html>'''
    report = run({"home.html": html}, form_plan([{"action": "click", "selector": "#submit"}]), tmp_path, baseline=True)
    assert report["executed"] == 1 and not report["failures"]


def test_native_async_submit_can_reach_its_frozen_final_state(tmp_path):
    html = b'''<html><meta name="viewport" content="width=device-width"><body>
    <form onsubmit="event.preventDefault();setTimeout(()=>document.querySelector('#result').textContent='submitted',150)">
    <button id="submit" type="submit">Submit</button></form><output id="result">idle</output>
    </body></html>'''
    report = run({"home.html": html}, form_plan([{"action": "click", "selector": "#submit"}]), tmp_path, baseline=True)
    assert report["executed"] == 1 and not report["failures"]


def test_waiting_cannot_combine_assertions_from_mutually_exclusive_states(tmp_path):
    html = b'''<html><meta name="viewport" content="width=device-width"><body>
    <button id="submit" onclick="document.querySelector('#first').textContent='ready';setTimeout(()=>{document.querySelector('#first').textContent='idle';document.querySelector('#second').textContent='ready'},200)">Submit</button>
    <output id="first">idle</output><output id="second">idle</output></body></html>'''
    value = form_plan([{"action": "click", "selector": "#submit"}])
    value["checks"][0]["asserts"] = [
        {"selector": "#first", "property": "text", "equals": "ready"},
        {"selector": "#second", "property": "text", "equals": "ready"},
    ]
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert report["failures"] and report["executed"] == 1


@pytest.mark.parametrize("disabled", [False, True])
def test_next_action_waits_for_a_new_or_disabled_control_without_replaying_submit(tmp_path, disabled):
    initial = '<button id="next" disabled>Next</button>' if disabled else ''
    html = ('''<html><meta name="viewport" content="width=device-width"><body>
    <button id="start">Start</button><section id="slot">''' + initial + '''</section>
    <output id="result">idle</output><output id="started">0</output><output id="clicked">0</output>
    <script>document.querySelector('#start').onclick=()=>{
      document.querySelector('#started').textContent=String(Number(document.querySelector('#started').textContent)+1);
      setTimeout(()=>{const next=document.createElement('button');next.id='next';next.textContent='Next';
        next.onclick=()=>{document.querySelector('#result').textContent='submitted';document.querySelector('#clicked').textContent=String(Number(document.querySelector('#clicked').textContent)+1)};
        document.querySelector('#slot').replaceChildren(next)},150)
    };</script></body></html>''').encode()
    value = form_plan([{"action": "click", "selector": "#start"}, {"action": "click", "selector": "#next"}])
    value["checks"][0]["asserts"].extend([
        {"selector": "#started", "property": "text", "equals": "1"},
        {"selector": "#clicked", "property": "text", "equals": "1"},
    ])
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert report["executed"] == 1 and not report["failures"]


def test_check_waits_for_an_async_radio_option(tmp_path):
    html = b'''<html><meta name="viewport" content="width=device-width"><body>
    <button id="start" onclick="setTimeout(()=>{const radio=document.createElement('input');radio.id='option';radio.type='radio';document.body.append(radio)},150)">Assess</button>
    </body></html>'''
    value = form_plan([{"action": "click", "selector": "#start"}, {"action": "check", "selector": "#option", "value": True}])
    value["checks"][0]["asserts"] = [{"selector": "#option", "property": "checked", "equals": True}]
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert report["executed"] == 1 and not report["failures"]


@pytest.mark.parametrize("clear_cover", [False, True])
def test_click_checks_its_actual_hit_target_without_replaying_input(tmp_path, clear_cover):
    import json
    script = "setTimeout(()=>document.querySelector('#cover').hidden=true,200)" if clear_cover else ""
    html = ('''<html><meta name="viewport" content="width=device-width"><body>
    <button id="start">Start</button><button id="submit">Submit</button>
    <output id="result">idle</output><output id="count">0</output>
    <div id="cover" hidden style="position:fixed;inset:0;z-index:10;background:white"></div>
    <script>document.querySelector('#start').onclick=()=>{document.querySelector('#cover').hidden=false;'''
    + script + '''};document.querySelector('#submit').onclick=()=>{
      document.querySelector('#result').textContent='submitted';
      document.querySelector('#count').textContent=String(Number(document.querySelector('#count').textContent)+1)
    };</script></body></html>''').encode()
    value = form_plan([{"action": "click", "selector": "#start"}, {"action": "click", "selector": "#submit"}])
    value["checks"][0]["asserts"].append({"selector": "#count", "property": "text", "equals": "1"})
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    if clear_cover:
        assert not report["failures"]
    else:
        assert "obscured click target" in json.dumps(report["failures"])


def test_seek_cannot_fake_media_progress_on_a_range_slider(tmp_path):
    import json
    html = b'<html><meta name="viewport" content="width=device-width"><input id="slider" type="range" min="0" max="30" value="0"></html>'
    value = form_plan([{"action": "seek", "selector": "#slider", "value": 15}])
    value["checks"][0]["asserts"] = [{"selector": "#slider", "property": "currentTime", "equals": 15}]
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert "seek requires a video or audio element" in json.dumps(report["failures"])


def test_seek_observes_the_real_media_time(tmp_path):
    import base64
    import io
    import wave
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 8000 * 30)
    html = ('<html><meta name="viewport" content="width=device-width"><audio id="media" preload="auto" src="data:audio/wav;base64,'
            + base64.b64encode(stream.getvalue()).decode() + '"></audio></html>').encode()
    value = form_plan([{"action": "seek", "selector": "#media", "value": 15}])
    value["checks"][0]["asserts"] = [{"selector": "#media", "property": "currentTime", "equals": 15}]
    report = run({"home.html": html}, value, tmp_path, baseline=True)
    assert report["executed"] == 1 and not report["failures"]
