#!/usr/bin/env python3
"""Screenshot the HUD in a real browser and dump its console — dev tool.

The HUD is WebGL-heavy (network map, moon terrain, bloom), and none of that
can be verified by reading source. This drives headless Chromium over the
DevTools protocol so a change can actually be *looked at*, and so page
errors surface instead of being invisible.

    apt install chromium          # one-time, on the dev box
    scripts/screenshot-hud.py /tmp/hud.png
    scripts/screenshot-hud.py /tmp/map.png --click '#netmapToggleBtn'

Options:
    [WIDTH HEIGHT]        viewport size (default 1920x1080)
    --wait SECONDS        settle time before the shot (default 8)
    --click SELECTOR      click an element, then wait; repeatable
    --eval JS             evaluate an expression and print its value.
                          Interleaved with --click in the order given, so a
                          probe reads the state at that point in the
                          sequence, not the end state. For checking STATE a
                          screenshot cannot show — what a <select> was
                          populated with, or what document.elementFromPoint()
                          returns over a control (the only way to catch a
                          click-blocking overlay, since .click() bypasses
                          pointer-events entirely).
    --url URL             default https://127.0.0.1/hud/
    --throttle KBPS       emulate a slow network (catches load-order races,
                          e.g. code that runs before deferred modules land)

The HUD token comes from $LOOKING_GLASS_HUD_TOKEN, or /etc/looking-glass/env.
It is set as a cookie so the PIN gate doesn't cover the page. Never hardcode
the token here — this file is committed.

Gotchas this script encodes:
  * --virtual-time-budget deadlocks on the map's requestAnimationFrame loop
    (virtual clock + endless rAF never goes idle), so it waits in real time.
  * websockets' sync client wraps its socket; poking .socket.settimeout()
    raises EBADF. Use recv(timeout=...).
  * Never `pkill -f` a pattern containing "remote-debugging-port" — the
    shell running it matches itself and dies.
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".venv/lib/python3.11/site-packages"))
import websockets.sync.client as wsclient  # noqa: E402


def hud_token() -> str:
    token = os.environ.get("LOOKING_GLASS_HUD_TOKEN")
    if token:
        return token
    env = Path("/etc/looking-glass/env")
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("LOOKING_GLASS_HUD_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "hud.png"
    args = sys.argv[2:]
    width, height, wait_s = 1920, 1080, 8.0
    # One ordered list, not a list of clicks and a list of evals: a probe is
    # only meaningful at its own point in the sequence, and running every
    # eval after every click reports the end state for all of them.
    actions: list[tuple[str, str]] = []
    url = "https://127.0.0.1/hud/"
    throttle_kbps = 0.0

    # Single pass. A flag's VALUE must never be mistaken for a positional —
    # collecting "every arg not starting with --" swallowed '#netmapToggleBtn'
    # from `--click` and tried to parse it as a width.
    i = 0
    leading: list[str] = []
    while i < len(args):
        if args[i] == "--wait":
            wait_s = float(args[i + 1]); i += 2
        elif args[i] == "--click":
            actions.append(("click", args[i + 1])); i += 2
        elif args[i] == "--eval":
            actions.append(("eval", args[i + 1])); i += 2
        elif args[i] == "--url":
            url = args[i + 1]; i += 2
        elif args[i] == "--throttle":
            throttle_kbps = float(args[i + 1]); i += 2
        elif args[i].startswith("--"):
            i += 1
        else:
            leading.append(args[i]); i += 1
    if len(leading) >= 2 and leading[0].isdigit() and leading[1].isdigit():
        width, height = int(leading[0]), int(leading[1])

    port, profile = 9222, "/tmp/hud-shot-profile"
    subprocess.run(["rm", "-rf", profile], check=False)
    chrome = subprocess.Popen([
        "chromium", "--headless=new", f"--remote-debugging-port={port}",
        "--no-sandbox", "--disable-gpu", "--ignore-certificate-errors",
        "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
        "--disable-dev-shm-usage", f"--window-size={width},{height}",
        f"--user-data-dir={profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(60):
        try:
            for target in json.load(urlopen(f"http://127.0.0.1:{port}/json")):
                if target.get("type") == "page":
                    ws_url = target["webSocketDebuggerUrl"]
                    break
            if ws_url:
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ws_url:
        print("FATAL: devtools never came up", file=sys.stderr)
        chrome.kill()
        return 1

    ws = wsclient.connect(ws_url, max_size=100_000_000)
    logs: list[str] = []
    msg_id = [0]

    def collect(msg: dict) -> None:
        method, params = msg.get("method", ""), msg.get("params", {})
        if method == "Log.entryAdded":
            e = params.get("entry", {})
            logs.append(f"[{e.get('level')}] {e.get('text')} ({e.get('url','')}:{e.get('lineNumber','')})")
        elif method == "Runtime.consoleAPICalled":
            body = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
            logs.append(f"[console.{params.get('type')}] {body}")
        elif method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails", {})
            logs.append(f"[EXCEPTION] {d.get('exception',{}).get('description') or d.get('text')}")

    def send(method: str, params: dict | None = None) -> dict:
        msg_id[0] += 1
        mid = msg_id[0]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg
            collect(msg)

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                collect(json.loads(ws.recv(timeout=0.4)))
            except Exception:
                pass

    for m in ("Log.enable", "Runtime.enable", "Network.enable", "Page.enable"):
        send(m)
    token = hud_token()
    if token:
        from urllib.parse import urlparse
        send("Network.setCookie", {
            "name": "looking_glass_token", "value": token,
            "domain": urlparse(url).hostname or "127.0.0.1", "path": "/", "secure": True,
        })
    if throttle_kbps:
        bps = throttle_kbps * 1024 / 8
        send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 150,
            "downloadThroughput": bps, "uploadThroughput": bps,
        })
    send("Page.navigate", {"url": url})
    pump(wait_s)

    for kind, arg in actions:
        if kind == "click":
            expression = (f"(()=>{{const e=document.querySelector({json.dumps(arg)});"
                          f"if(!e) return 'NOT FOUND'; e.click(); return 'clicked';}})()")
        else:
            expression = (f"(()=>{{try{{return JSON.stringify({arg});}}"
                          f"catch(e){{return 'ERROR: '+e.message;}}}})()")
        result = send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        print(f"{kind} {arg}: {result.get('result',{}).get('result',{}).get('value')}")
        # A click can kick off a fetch/render; a probe only reads what is
        # already there, so it does not need the settle time.
        if kind == "click":
            pump(5.0)

    shot = send("Page.captureScreenshot", {"format": "png"})
    data = shot.get("result", {}).get("data")
    if not data:
        print("NO SCREENSHOT", shot, file=sys.stderr)
    else:
        Path(out).write_bytes(base64.b64decode(data))
        print(f"wrote {out} ({os.path.getsize(out)} bytes)")

    print("\n===== CONSOLE / ERRORS =====")
    seen = set()
    for line in logs:
        if line not in seen:
            seen.add(line)
            print(line)
    if not logs:
        print("(none)")

    ws.close()
    chrome.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
