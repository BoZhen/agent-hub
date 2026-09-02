#!/usr/bin/env python3
"""Dependency-light Chrome/CDP checks for the dashboard's browser contracts."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect


def _json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


class CDP:
    def __init__(self, websocket_url: str) -> None:
        self.socket = connect(websocket_url, origin="http://127.0.0.1")
        self.next_id = 1

    def close(self) -> None:
        self.socket.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        call_id = self.next_id
        self.next_id += 1
        self.socket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        payload = result.get("result", {})
        if payload.get("subtype") == "error":
            raise RuntimeError(payload.get("description", "browser evaluation failed"))
        return payload.get("value")


def _wait_for_page(port: int, target_url: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            targets = _json(f"http://127.0.0.1:{port}/json/list")
            for target in targets:
                if target.get("type") == "page" and target.get("url", "").startswith(target_url):
                    return target
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Chrome debugging target did not become ready")


def _wait_for_contract(cdp: CDP) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready = cdp.evaluate(
            "document.readyState === 'complete' && "
            "typeof previewState !== 'undefined' && !!previewState.sid && "
            "document.querySelectorAll('#workspace-tabs [role=tab]').length > 0"
        )
        if ready:
            return
        time.sleep(0.1)
    raise RuntimeError("Dashboard contract did not become ready")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Dashboard URL with a selected session")
    parser.add_argument("--tmux", required=True, help="tmux session that must survive Close view")
    parser.add_argument("--chrome", default=shutil.which("google-chrome") or "google-chrome")
    parser.add_argument("--debug-port", type=int, default=19222)
    args = parser.parse_args()

    profile = Path(tempfile.mkdtemp(prefix="agent-hub-cdp-"))
    process = subprocess.Popen(
        [
            args.chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={args.debug_port}",
            f"--user-data-dir={profile}",
            "--window-size=1440,900",
            args.url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp: CDP | None = None
    try:
        target = _wait_for_page(args.debug_port, args.url.split("?", 1)[0])
        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        _wait_for_contract(cdp)

        no_tmux = cdp.evaluate(
            "(() => {"
            "const pending=[...document.querySelectorAll('.session-row[data-pending=\"1\"]')];"
            "const without=pending.filter(card => !card.dataset.tmux).length;"
            "return {without, details:document.querySelectorAll('#needs-list a[href^=\"/sessions/\"]').length, approves:document.querySelectorAll('#needs-list .approve').length};"
            "})()"
        )
        assert no_tmux["details"] == no_tmux["without"], no_tmux

        stale = cdp.evaluate(
            "(() => {"
            "const frame=document.getElementById('preview-iframe');const dot=document.getElementById('terminal-status-dot');"
            "dot.className='dot dot-active';"
            "window.dispatchEvent(new MessageEvent('message',{origin:TERMINAL_ORIGIN,source:frame.contentWindow,data:{source:'web-terminal',type:'webterminal:disconnected',session:'stale-session'}}));"
            "return {dot:dot.className,current:previewState.tmux};"
            "})()"
        )
        assert "dot-active" in stale["dot"], stale

        row_count = cdp.evaluate("document.querySelectorAll('.session-row').length")
        if row_count < 2:
            raise RuntimeError("browser contract requires at least two rendered session rows")

        arrow_focus = cdp.evaluate(
            "(async () => {"
            "const original=previewState.sid;"
            "const second=[...document.querySelectorAll('.session-row')].find(card=>card.id!==('card-'+original));"
            "const footer=second.querySelector('[id^=actions-]');const previousTmux=second.dataset.tmux;const previousFooterTmux=footer.dataset.tmux;second.dataset.tmux=previewState.tmux;footer.dataset.tmux=previewState.tmux;previewSession(second);previewSession(document.getElementById('card-'+original));"
            "const first=document.querySelector('#workspace-tabs [role=tab]');first.focus();first.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));"
            "await new Promise(resolve=>requestAnimationFrame(resolve));"
            "const selected=document.querySelector('#workspace-tabs [role=tab][aria-selected=\"true\"]');"
            "const result={focused:document.activeElement===selected,active:document.activeElement?.dataset?.sid||null,selected:selected?.dataset?.sid||null};"
            "closeWorkspaceTab(second.id.slice(5));second.dataset.tmux=previousTmux;footer.dataset.tmux=previousFooterTmux;await new Promise(resolve=>requestAnimationFrame(resolve));return result;"
            "})()"
        )
        assert arrow_focus["focused"] and arrow_focus["active"] == arrow_focus["selected"], arrow_focus

        cdp.evaluate("window.__visibilityMessages=[];postToTerminal=(message)=>window.__visibilityMessages.push(message)")
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 600, "height": 800, "deviceScaleFactor": 1, "mobile": True})
        time.sleep(0.25)
        hidden = cdp.evaluate("window.__visibilityMessages.filter(m=>m.type==='agent-hub:visibility').at(-1)")
        assert hidden == {"type": "agent-hub:visibility", "visible": False}, hidden
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.25)
        visible = cdp.evaluate("window.__visibilityMessages.filter(m=>m.type==='agent-hub:visibility').at(-1)")
        assert visible == {"type": "agent-hub:visibility", "visible": True}, visible

        closed = cdp.evaluate(
            "(() => {const tab=document.querySelector('#workspace-tabs [role=tab]');"
            "tab.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true}));"
            "return new Promise(resolve=>requestAnimationFrame(()=>resolve({tabs:document.querySelectorAll('#workspace-tabs [role=tab]').length,sid:previewState.sid,src:document.getElementById('preview-iframe').src})));})()"
        )
        assert closed["tabs"] == 0 and closed["sid"] is None and closed["src"] == "about:blank", closed
        tmux_check = subprocess.run(
            ["tmux", "has-session", "-t", args.tmux],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        assert tmux_check.returncode == 0, f"Close view ended tmux session {args.tmux}"

        print(json.dumps({"ok": True, "no_tmux": no_tmux, "stale_message": stale, "arrow_focus": arrow_focus, "hidden": hidden, "visible": visible, "closed": closed}))
        return 0
    finally:
        if cdp is not None:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
