"""``exposure panel PID``: a debug panel in the browser.

Client side only: a small stdlib HTTP server on 127.0.0.1 that holds one
connection to the agent and serves a single self-contained page
(``panel.html``, no CDN). The page polls at about 10 Hz.

Security, since a browser can be pointed at any local port:

* binds 127.0.0.1 only, on a random port by default;
* a random token in the URL; every request must carry it (``?token=`` for the
  page, ``X-Exposure-Token`` for the API) or it gets 403;
* the Host header must be 127.0.0.1 / localhost on our port (defeats DNS
  rebinding: a hostile page can't reach us through its own domain name);
* changes are POST-only with ``Content-Type: application/json`` (a plain HTML
  form can't send that), and an Origin header, if present, must be ours;
* no CORS headers at all, so other origins can't read responses.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .client import RemoteError

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")


class PanelServer:
    def __init__(self, client, preset=None, port=0):
        self.c = client
        self.lock = threading.Lock()          # one request at a time on the agent connection
        self.token = secrets.token_urlsafe(24)
        self.preset_name = preset
        self.config = self._build_config(preset)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                       name="exposure-panel")
        self._fps = {"t": None, "it": None, "fps": None}

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- what the page shows --------------------------------------------------------------
    def _call(self, fn, *a, **kw):
        with self.lock:
            return fn(*a, **kw)

    def _build_config(self, preset):
        presets = self._call(self.c.presets)
        scope = self._call(self.c.scope)
        p = presets.get(preset, {}) if preset else {}
        if preset and preset not in presets:
            raise LookupError(f"no preset {preset!r} (have: {sorted(presets)})")
        sliders = list(p.get("sliders", []))
        for path in p.get("tweak", []):
            rng = p.get("range", {}).get(path)
            sliders.append({"label": path, "read": path, "set": path, "range": rng,
                            "step": p.get("step", {}).get(path)})
        return {"pid": self.c.pid, "preset": preset, "watch": list(p.get("watch", [])),
                "sliders": sliders, "actions": scope.get("actions", []),
                "scope": scope.get("scope"), "read_only": scope.get("read_only"),
                "rate": p.get("rate", 10)}

    def state(self):
        paths = list(dict.fromkeys(self.config["watch"]
                                   + [s["read"] for s in self.config["sliders"]]))
        sample = self._call(self.c.sample, paths) if paths else {"values": []}
        try:
            paused = self._call(self.c.paused)
        except RemoteError:
            paused = {"paused": False}
        now = time.monotonic()
        it = paused.get("iteration")
        if it is not None and self._fps["it"] is not None and now > self._fps["t"]:
            if it >= self._fps["it"] and not paused.get("paused"):
                self._fps["fps"] = round((it - self._fps["it"]) / (now - self._fps["t"]), 1)
        if it is not None:
            self._fps.update(t=now, it=it)
        return {"t": time.time(), "values": sample["values"],
                "paused": bool(paused.get("paused")), "iteration": it, "fps": self._fps["fps"]}

    # -- HTTP -------------------------------------------------------------------------------
    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "exposure-panel"

            def log_message(self, *a):        # quiet
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(data)

            def _host_ok(self):
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

            def _token_ok(self, query):
                given = self.headers.get("X-Exposure-Token") or query.get("token", [""])[0]
                return hmac.compare_digest(given.encode(), server.token.encode())

            def _guard(self, query):
                if not self._host_ok():
                    self._send(403, {"error": "bad Host header"})
                    return False
                if not self._token_ok(query):
                    self._send(403, {"error": "missing or wrong token"})
                    return False
                return True

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._guard(q):
                    return
                try:
                    if u.path == "/":
                        with open(PAGE, "rb") as f:
                            self._send(200, f.read(), "text/html; charset=utf-8")
                    elif u.path == "/api/config":
                        self._send(200, server.config)
                    elif u.path == "/api/state":
                        self._send(200, server.state())
                    elif u.path == "/api/events":
                        since = int(q.get("since", ["0"])[0])
                        self._send(200, server._call(server.c.probe_events, since=since))
                    else:
                        self._send(404, {"error": "not found"})
                except (RemoteError, LookupError, ValueError) as exc:
                    self._send(400, {"error": str(exc)})

            def do_POST(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._guard(q):
                    return
                origin = self.headers.get("Origin")
                if origin and origin not in (f"http://127.0.0.1:{server.port}",
                                             f"http://localhost:{server.port}"):
                    self._send(403, {"error": "cross-origin request refused"})
                    return
                if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
                    self._send(415, {"error": "changes need Content-Type: application/json"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    self._send(200, server.post(u.path, body))
                except (RemoteError, LookupError, ValueError, TypeError) as exc:
                    self._send(400, {"error": str(exc)})

            def do_PUT(self):
                self._send(405, {"error": "use POST"})

            do_DELETE = do_PATCH = do_OPTIONS = do_PUT

        return Handler

    def post(self, path, body):
        c = self.c
        if path == "/api/set":
            return self._call(c.set, str(body["target"]), _literal(body["value"]))
        if path == "/api/slider":
            s = self.config["sliders"][int(body["index"])]
            value = _literal(body["value"])
            if s.get("action"):
                args = [str(a) for a in s.get("args", [])] + [value]
                return self._call(c.do, s["action"], *args)
            return self._call(c.set, s["set"], value)
        if path == "/api/nudge":
            return self._call(c.nudge, str(body["target"]), float(body["delta"]))
        if path == "/api/do":
            values = [_literal(v) for v in body.get("values", [])]
            return self._call(c.do, str(body["action"]), *values)
        if path == "/api/pause":
            return self._call(c.pause)
        if path == "/api/resume":
            return self._call(c.resume)
        if path == "/api/step":
            return self._call(c.step, int(body.get("n", 1)))
        if path == "/api/break":
            return self._call(c.break_, None, when=str(body["when"]), confirmed=True)
        if path == "/api/unbreak":
            return self._call(c.unprobe, "all")
        raise LookupError(f"unknown action {path}")


def _literal(value):
    """A JSON value from the page -> literal source for the agent."""
    if isinstance(value, str):
        return value          # already source text from an input box ("0.5", "'x'", "True")
    return repr(value)


def run(client, preset=None, port=0, open_browser=True, out=None):
    import sys
    import webbrowser
    out = out or sys.stdout
    panel = PanelServer(client, preset, port).start()
    print(f"Exposure panel for pid {client.pid}: {panel.url}", file=out, flush=True)
    print("(keep this window open; Ctrl+C stops the panel)", file=out, flush=True)
    if open_browser:
        webbrowser.open(panel.url)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        panel.stop()
    return 0
