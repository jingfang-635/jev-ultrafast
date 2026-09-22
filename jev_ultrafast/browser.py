"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

_LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_AUTOMATION_PROFILE_ROOT = Path(tempfile.gettempdir()) / "jev-ultrafast-automation"
_LAUNCHED_BROWSERS: list[subprocess.Popen] = []


def _devtools_reachable(url, timeout=1.5):
    try:
        urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=timeout)
    except urllib.error.HTTPError:
        return True  # A DevTools listener answered; the harness validates the endpoint itself.
    except OSError:
        return False
    return True


def _automation_browser_binary():
    for key in ("BH_CHROME_PATH", "CHROME_PATH"):
        raw = (os.environ.get(key) or "").strip()
        if raw and Path(raw).expanduser().is_file():
            return raw
    if sys.platform == "win32":
        relative = ("Google\\Chrome\\Application\\chrome.exe", "Microsoft\\Edge\\Application\\msedge.exe")
        roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        candidates = [Path(root) / name for name in relative for root in roots if root]
    elif sys.platform == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    else:
        found = (
            shutil.which("google-chrome-stable"),
            shutil.which("google-chrome"),
            shutil.which("chromium-browser"),
            shutil.which("chromium"),
            shutil.which("microsoft-edge"),
            shutil.which("microsoft-edge-stable"),
        )
        candidates = [Path(path) for path in found if path]
    return next((str(path) for path in candidates if path.is_file()), None)


def _terminate_launched_browsers():
    for process in _LAUNCHED_BROWSERS:
        if process.poll() is None:
            process.terminate()


def _ensure_automation_browser():
    """Start a dedicated headless browser when BU_CDP_URL names a local DevTools
    port with no listener. The demo then shows everything inside the inspector;
    no external browser window opens on the desktop. Set BU_AUTOMATION_BROWSER=0
    to keep the original connect-only behavior."""
    if os.environ.get("BU_AUTOMATION_BROWSER", "").strip().lower() in {"0", "false", "no", "off"}:
        return
    url = os.environ.get("BU_CDP_URL")
    if not url:
        return
    endpoint = urllib.parse.urlparse(url)
    if endpoint.scheme != "http" or endpoint.hostname not in _LOCAL_HOSTS or not endpoint.port:
        return  # Remote or gateway endpoints are provisioned elsewhere.
    if endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment:
        return  # A pathful URL is not a bare DevTools endpoint; keep connect-only behavior.
    if _devtools_reachable(url):
        return
    binary = _automation_browser_binary()
    if binary is None:
        raise RuntimeError(
            f"BU_CDP_URL={url} has no listener, and no Chrome/Edge installation was found to start a "
            "headless automation browser. Install Chrome or Edge, or start the dedicated browser yourself "
            "with --remote-debugging-port=<port> --user-data-dir=<dir>."
        )
    spawn_kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        [
            binary,
            f"--remote-debugging-port={endpoint.port}",
            f"--user-data-dir={_AUTOMATION_PROFILE_ROOT / f'port-{endpoint.port}'}",
            "--headless=new",
            "--window-size=1120,780",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **spawn_kwargs,
    )
    _LAUNCHED_BROWSERS.append(process)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _LAUNCHED_BROWSERS.remove(process)
            raise RuntimeError(
                f"The headless automation browser for BU_CDP_URL={url} exited with code {process.returncode}; "
                "another automation browser may already own this profile."
            )
        if _devtools_reachable(url, timeout=1):
            return
        time.sleep(0.2)
    raise RuntimeError(f"The headless automation browser did not expose {url} within 30s.")


atexit.register(_terminate_launched_browsers)

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url):
        _ensure_automation_browser()
        ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        # Dedicated automation window: keep the tab composited even when it is
        # backgrounded or the window is minimized, otherwise Page.captureScreenshot
        # stalls waiting for a frame and times out. Activate the tab (harmless in
        # a dedicated instance), pin the lifecycle to active, and keep a tiny
        # low-rate screencast running so frames are produced in every state.
        cdp("Target.activateTarget", targetId=self.target)
        try:
            self.call("Page.setWebLifecycleOverride", state="active")
        except RuntimeError:
            pass
        self.call("Page.startScreencast", format="jpeg", quality=30, maxWidth=320, maxHeight=200, everyNthFrame=1)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            try:
                self.call("Page.stopScreencast")
            except RuntimeError:
                pass
            try:
                cdp("Target.closeTarget", targetId=self.target)
            except RuntimeError:
                pass  # The browser may have exited; a fresh demo must still open.
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        # captureBeyondViewport forces a renderer-side capture. Without it a
        # compositor that produces no frame (minimized/occluded window) stalls
        # the call until the harness's 5s timeout kills the whole step.
        info["screenshot"] = call(
            "Page.captureScreenshot", format="jpeg", quality=72, captureBeyondViewport=True
        )["data"]
    return info
