"""Controller browser oracles. No worker JavaScript or executable tests are trusted.

Chrome's debugging pipe needs no provider, network service or Python browser package.
Only the frozen package is served; external requests are failed before transmission.
"""

from contextlib import contextmanager
from pathlib import Path
import base64
import hashlib
import http.server
import json
import os
import select
import shutil
import subprocess
import tempfile
import threading
import time

from .types import ControlError, digest

TEST_ENVIRONMENT = r"""(now => {
 const RealDate=Date;
 function FixedDate(...args){
   if(new.target)return Reflect.construct(RealDate,args.length?args:[now],new.target);
   return new RealDate(now).toString();
 }
 Object.setPrototypeOf(FixedDate,RealDate);FixedDate.prototype=RealDate.prototype;
 FixedDate.now=()=>now;window.Date=FixedDate;
 let seed=1234567;
 Math.random=()=>((seed=Math.imul(seed,1664525)+1013904223>>>0)/4294967296);
 crypto.getRandomValues=a=>{for(let i=0;i<a.length;i++)a[i]=Math.floor(Math.random()*4294967296);return a};
 crypto.randomUUID=()=> '10000000-1000-4000-8000-100000000000'.replace(/[018]/g,c=>(c^crypto.getRandomValues(new Uint8Array(1))[0]&15>>c/4).toString(16));
})(__CLOCK__)"""


class Chrome:
    def __init__(self, scratch):
        binary = (
            shutil.which("google-chrome")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
        )
        if not binary:
            raise ControlError(
                "prototype_browser_environment",
                "Install Chrome or Chromium to verify prototypes",
                category="environment",
            )
        command_read, self.writer = os.pipe()
        self.reader, response_write = os.pipe()
        # Chrome requires descriptors 3 (commands) and 4 (responses). A tiny
        # launcher duplicates them after Python's close_fds, without preexec_fn.
        import sys

        launcher = "import os,sys; a,b=map(int,sys.argv[1:3]); os.dup2(a,3); os.dup2(b,4); os.set_inheritable(3,True); os.set_inheritable(4,True); os.execv(sys.argv[3],sys.argv[3:])"
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                launcher,
                str(command_read),
                str(response_write),
                binary,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-component-update",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-sync",
                "--metrics-recording-only",
                "--disable-extensions",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--remote-debugging-pipe",
                "--user-data-dir=" + str(scratch / "chrome"),
                "about:blank",
            ],
            pass_fds=(command_read, response_write),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(command_read)
        os.close(response_write)
        self.buffer, self.serial, self.session = b"", 0, None
        self.origin = ""
        try:
            target = self.call("Target.createTarget", {"url": "about:blank"})[
                "targetId"
            ]
            self.session = self.call(
                "Target.attachToTarget", {"targetId": target, "flatten": True}
            )["sessionId"]
            self.call("Page.enable")
            self.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        except BaseException:
            self.close()
            raise

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        os.close(self.reader)
        os.close(self.writer)

    def call(self, method, params=None):
        self.serial += 1
        identity = self.serial
        request = {"id": identity, "method": method, "params": params or {}}
        if self.session:
            request["sessionId"] = self.session
        os.write(self.writer, json.dumps(request).encode() + b"\0")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if b"\0" not in self.buffer:
                ready, _, _ = select.select(
                    [self.reader], [], [], max(0, deadline - time.monotonic())
                )
                if not ready:
                    break
                data = os.read(self.reader, 1024 * 1024)
                if not data:
                    break
                self.buffer += data
                continue
            line, self.buffer = self.buffer.split(b"\0", 1)
            if not line:
                continue
            reply = json.loads(line)
            if reply.get("method") == "Fetch.requestPaused":
                event = reply["params"]
                url = event["request"]["url"]
                allowed = url.startswith(self.origin + "/") if self.origin else False
                self.serial += 1
                os.write(
                    self.writer,
                    json.dumps(
                        {
                            "id": self.serial,
                            "sessionId": reply["sessionId"],
                            "method": (
                                "Fetch.continueRequest"
                                if allowed
                                else "Fetch.failRequest"
                            ),
                            "params": {
                                "requestId": event["requestId"],
                                **(
                                    {}
                                    if allowed
                                    else {"errorReason": "BlockedByClient"}
                                ),
                            },
                        }
                    ).encode()
                    + b"\0",
                )
            if reply.get("id") == identity:
                if "error" in reply:
                    raise ControlError(
                        "prototype_browser_environment",
                        str(reply["error"]),
                        category="environment",
                    )
                return reply.get("result", {})
        raise ControlError(
            "prototype_browser_environment",
            "Chrome did not complete " + method,
            category="environment",
        )

    def evaluate(self, expression):
        reply = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if reply.get("exceptionDetails"):
            raise ControlError(
                "prototype_checks_failed",
                "Browser oracle failed: " + str(reply["exceptionDetails"]),
                category="verification",
            )
        return reply.get("result", {}).get("value")

    def navigate(self, url, viewport):
        self.call(
            "Storage.clearDataForOrigin", {"origin": self.origin, "storageTypes": "all"}
        )
        width, height = map(int, viewport.split("x"))
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        self.call("Page.navigate", {"url": url})
        self.evaluate(
            "new Promise((resolve,reject)=>{let n=0;let t=setInterval(()=>{if(document.readyState==='complete'){clearInterval(t);resolve(true)}else if(++n>200){clearInterval(t);reject(Error('load timeout'))}},20)})"
        )
        self.evaluate("document.fonts.ready.then(()=>true)")


@contextmanager
def package_server(files):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            import mimetypes
            import urllib.parse

            name = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).lstrip(
                "/"
            )
            data = files.get(name)
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(name)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' data: blob: 'unsafe-inline'; connect-src 'none'; form-action 'none'; frame-src 'self'; object-src 'none'; base-uri 'none'; sandbox allow-scripts allow-same-origin",
            )
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# The snapshot walks *all* elements, including currently hidden controls. Mutable
# regions are removed before sibling numbering so adding one cannot hide changes
# elsewhere. Computed geometry is checked separately from appearance.
SNAPSHOT = r"""(options => {
 const mutable=options.mutable, geometry=options.geometry;
 const matches=(el,selectors)=>selectors.some(s=>el.matches(s));
 const styles=['color','background-color','background-image','background-size','font-family','font-size','font-weight','font-style','line-height','letter-spacing','text-decoration','text-transform','display','visibility','opacity','border','border-radius','box-shadow','outline','padding','margin','text-align','position','flex-direction','flex-wrap','align-items','justify-content','gap','grid-template-columns','grid-template-rows','overflow','cursor','object-fit','z-index'];
 const appearance=css=>Object.fromEntries(styles.map(p=>[p,css.getPropertyValue(p)]));
 const anchor=(el,p)=>{let a=el.parentElement;while(a){if(geometry.some(g=>a.matches(g.selector)&&g.properties.includes(p)))return a.getBoundingClientRect()[p];a=a.parentElement}return 0};
 const walk=(el)=>{
   if(['SCRIPT','STYLE','META','LINK','TITLE'].includes(el.tagName)||matches(el,mutable))return null;
   const css=getComputedStyle(el),r=el.getBoundingClientRect();
   const ignored=new Set(geometry.filter(x=>el.matches(x.selector)).flatMap(x=>x.properties));
   return {tag:el.tagName,attrs:Array.from(el.attributes).map(a=>[a.name,a.value]).sort(),
     text:Array.from(el.childNodes).filter(n=>n.nodeType===3).map(n=>n.textContent).join(''),
     value:el.value===undefined?null:el.value,checked:el.checked===undefined?null:el.checked,
     styles:appearance(css),
     pseudo:Object.fromEntries(['::before','::after'].map(p=>{const s=getComputedStyle(el,p);return [p,s.content==='none'||s.content==='normal'?null:{content:s.content,styles:appearance(s)}]})),
     geometry:Object.fromEntries(['x','y','width','height'].filter(p=>!ignored.has(p)).map(p=>[p,Math.round((r[p]-(p==='x'||p==='y'?anchor(el,p):0))*10)/10])),
     children:Array.from(el.children).map(walk).filter(Boolean)};
 }; return walk(document.documentElement);
})"""


def element_expression(selector):
    return (
        "(()=>{const es=document.querySelectorAll("
        + json.dumps(selector)
        + ");if(es.length!==1)throw Error('Expected exactly one element: '+"
        + json.dumps(selector)
        + ");return es[0]})()"
    )


def snapshot(browser, options):
    def normalize(value):
        if isinstance(value, str):
            return value.replace(browser.origin, "@origin")
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        return value

    return normalize(browser.evaluate(SNAPSHOT + "(" + json.dumps(options) + ")"))


def action(browser, step):
    op, selector = step["action"], step["selector"]
    element = element_expression(selector)
    value = json.dumps(step.get("value"))
    if op == "click":
        # Real input dispatch, rather than invoking a handler on a disabled or
        # invisible element, catches the common prototype success-toast proxy.
        box = browser.evaluate(
            "(()=>{const e="
            + element
            + ";if(e.disabled)throw Error('disabled');e.scrollIntoView({block:'center',inline:'center'});const r=e.getBoundingClientRect();if(!r.width||!r.height||getComputedStyle(e).visibility==='hidden')throw Error('invisible');return {x:r.x+r.width/2,y:r.y+r.height/2}})()"
        )
        for kind in ("mousePressed", "mouseReleased"):
            browser.call(
                "Input.dispatchMouseEvent",
                {"type": kind, **box, "button": "left", "clickCount": 1},
            )
    elif op in {"fill", "select", "check", "seek"}:
        browser.evaluate(
            "(()=>{const e="
            + element
            + ";if(e.disabled||e.readOnly&&e.type!=='range')throw Error('disabled or readonly');if("
            + json.dumps(op)
            + "!=='seek'){e.scrollIntoView({block:'center'});if(!e.getClientRects().length)throw Error('invisible')}return true})()"
        )
        if op == "select":
            browser.evaluate(
                "(()=>{const e="
                + element
                + ";const o=Array.from(e.options||[]).find(o=>o.value==="
                + value
                + ");if(!o||o.disabled||o.parentElement.disabled)throw Error('unavailable option');return true})()"
            )
        property_name = (
            "checked" if op == "check" else "currentTime" if op == "seek" else "value"
        )
        browser.evaluate(
            "(()=>{const e="
            + element
            + ";if(e.disabled)throw Error('disabled');e["
            + json.dumps(property_name)
            + "]="
            + value
            + ";e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()"
        )
    elif op == "press":
        browser.evaluate("(" + element + ").focus()")
        for kind in ("keyDown", "keyUp"):
            browser.call(
                "Input.dispatchKeyEvent",
                {
                    "type": kind,
                    "key": step["value"],
                    "text": step["value"] if len(step["value"]) == 1 else "",
                },
            )
    browser.evaluate(
        "new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(true))))"
    )


def assertion(browser, rule):
    selector, prop = rule["selector"], rule["property"]
    if prop == "count":
        expression = "document.querySelectorAll(" + json.dumps(selector) + ").length"
    else:
        expressions = {
            "text": "e.textContent.trim()",
            "value": "e.value",
            "checked": "e.checked",
            "enabled": "!e.disabled",
            "visible": "!!(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && getComputedStyle(e).display!=='none')",
            "currentTime": "e.currentTime",
            "attribute": "e.getAttribute(" + json.dumps(rule.get("name", "")) + ")",
        }
        expression = (
            "(()=>{const e="
            + element_expression(selector)
            + ";return "
            + expressions[prop]
            + "})()"
        )
    actual = browser.evaluate(expression)
    expected = rule["equals"]
    if actual != expected or type(actual) is not type(expected):
        raise ControlError(
            "prototype_checks_failed",
            "Browser assertion failed",
            category="verification",
            details={"assertion": rule, "actual": actual},
        )


def run(files, scope, evidence, *, baseline=False):
    """Return real observations, or fail closed. Evidence stays outside workers."""
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    checks = [c for c in scope["checks"] if not baseline or c["kind"] == "preservation"]
    if baseline and scope["mode"] == "create":
        return {"executed": 0, "failures": [], "observations": {}, "screenshots": {}}
    if not checks and not (baseline and scope["mode"] == "create"):
        raise ControlError(
            "prototype_checks_missing",
            "Prototype needs executable behavior checks",
            category="verification",
        )
    observations, screenshots, failures, repair_observations = {}, {}, [], {}
    with (
        tempfile.TemporaryDirectory(prefix="prototype-browser-") as scratch,
        package_server(files) as origin,
    ):
        browser = Chrome(Path(scratch))
        browser.origin = origin

        def observe_repair(page, key):
            if "repair_regions" not in scope:
                return
            options = {
                "mutable": [
                    r["selector"] for r in scope["repair_regions"] if r["page"] == page
                ],
                "geometry": [g for g in scope["repair_geometry"] if g["page"] == page],
            }
            repair_observations[key] = digest(snapshot(browser, options))

        try:
            browser.call(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": TEST_ENVIRONMENT.replace(
                        "__CLOCK__", json.dumps(scope.get("clock", 1577836800000))
                    )
                },
            )
            for viewport in scope["viewports"]:
                removed = {
                    c["page"]
                    for c in scope["changes"]
                    if c.get("whole_page") and c["kind"] == "remove"
                }
                pages = sorted(
                    set(scope.get("baseline_pages", []))
                    | {c["page"] for c in checks}
                    | {
                        c["page"]
                        for c in scope["changes"]
                        if not baseline or c["kind"] != "add"
                    }
                )
                if not baseline:
                    pages = [p for p in pages if p not in removed]
                for page in pages:
                    if page not in files:
                        failures.append({"page": page, "reason": "missing page"})
                        continue
                    browser.navigate(origin + "/" + page, viewport)
                    if scope.get("repair_has_original"):
                        for region in scope.get("repair_regions", []):
                            if (
                                region["page"] != page
                                or scope["repair_original"]
                                and region["kind"] == "add"
                                or not scope["repair_original"]
                                and region["kind"] == "remove"
                            ):
                                continue
                            bounded = browser.evaluate(
                                "(()=>{const e="
                                + element_expression(region["selector"])
                                + ";return e!==document.body&&e!==document.documentElement&&e.querySelectorAll('*').length<Math.max(1,document.body.querySelectorAll('*').length*.8)})()"
                            )
                            if not bounded:
                                raise ControlError(
                                    "prototype_scope",
                                    "Baseline repairs cannot authorize the entire page",
                                    category="verification",
                                )
                    for change in scope["changes"]:
                        if (
                            change["page"] != page
                            or baseline
                            and change["kind"] == "add"
                        ):
                            continue
                        count = browser.evaluate(
                            "document.querySelectorAll("
                            + json.dumps(change["selector"])
                            + ").length"
                        )
                        expected = (
                            0 if not baseline and change["kind"] == "remove" else 1
                        )
                        if count != expected:
                            raise ControlError(
                                "prototype_scope",
                                "Changed region is missing, duplicated or not removed",
                                category="verification",
                                details={
                                    "page": page,
                                    "selector": change["selector"],
                                    "expected": expected,
                                    "actual": count,
                                },
                            )
                    if scope["mode"] not in {"create", "redesign"}:
                        for change in scope["changes"]:
                            if change.get("whole_page"):
                                continue
                            if (
                                change["page"] != page
                                or change["kind"] == "add"
                                and baseline
                                or change["kind"] == "remove"
                                and not baseline
                            ):
                                continue
                            bounded = browser.evaluate(
                                "(()=>{const e="
                                + element_expression(change["selector"])
                                + ";return e!==document.body&&e!==document.documentElement&&e.querySelectorAll('*').length<Math.max(1,document.body.querySelectorAll('*').length*.8)})()"
                            )
                            if not bounded:
                                raise ControlError(
                                    "prototype_scope",
                                    "Local changes cannot authorize the entire page",
                                    category="verification",
                                )
                    options = {
                        "mutable": [
                            c["selector"] for c in scope["changes"] if c["page"] == page
                        ],
                        "geometry": [c for c in scope["geometry"] if c["page"] == page],
                    }
                    if baseline or page in scope.get("baseline_pages", []):
                        observations[viewport + "/" + page + "/initial"] = snapshot(
                            browser, options
                        )
                        observe_repair(page, viewport + "/" + page + "/initial")
                    shot = evidence / (digest([viewport, "page", page])[:16] + ".png")
                    shot.write_bytes(
                        base64.b64decode(
                            browser.call(
                                "Page.captureScreenshot",
                                {"format": "png", "captureBeyondViewport": True},
                            )["data"]
                        )
                    )
                    screenshots[shot.name] = hashlib.sha256(
                        shot.read_bytes()
                    ).hexdigest()
                for check in checks:
                    if check["page"] not in files:
                        failures.append(
                            {
                                "check": check["id"],
                                "viewport": viewport,
                                "reason": "missing page",
                            }
                        )
                        continue
                    browser.navigate(origin + "/" + check["page"], viewport)
                    try:
                        for step in check["steps"]:
                            action(browser, step)
                        for rule in check["asserts"]:
                            assertion(browser, rule)
                        options = {
                            "mutable": [
                                c["selector"]
                                for c in scope["changes"]
                                if c["page"] == check["page"]
                            ],
                            "geometry": [
                                c
                                for c in scope["geometry"]
                                if c["page"] == check["page"]
                            ],
                        }
                        if check["kind"] == "preservation":
                            observations[viewport + "/" + check["id"]] = snapshot(
                                browser, options
                            )
                            observe_repair(check["page"], viewport + "/" + check["id"])
                        shot = evidence / (
                            digest([viewport, "check", check["id"]])[:16] + ".png"
                        )
                        shot.write_bytes(
                            base64.b64decode(
                                browser.call(
                                    "Page.captureScreenshot",
                                    {"format": "png", "captureBeyondViewport": True},
                                )["data"]
                            )
                        )
                        screenshots[shot.name] = hashlib.sha256(
                            shot.read_bytes()
                        ).hexdigest()
                    except ControlError as error:
                        if error.category == "environment":
                            raise
                        failures.append(
                            {
                                "check": check["id"],
                                "viewport": viewport,
                                "error": error.to_dict(),
                            }
                        )
        finally:
            browser.close()
    return {
        "executed": len(checks) * len(scope["viewports"]),
        "failures": failures,
        "observations": observations,
        "screenshots": screenshots,
        "repair_observations": repair_observations,
    }
