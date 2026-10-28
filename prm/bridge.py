"""Optional loopback endpoint for the companion Chrome extension (disabled by default).

* Binds 127.0.0.1 only; every request needs the pairing token (X-PRM-Token header).
* Requests carrying a web-page Origin (http/https) are rejected, so websites cannot talk to it.
* Accepts only download *hints* {file_name, url, referrer}; never cookies, credentials or file content.
* No CORS headers are sent.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .logsetup import get_logger

log = get_logger("bridge")
HINT_TTL = 15 * 60
MAX_HINTS = 200
MAX_BODY = 8192


class Bridge:
    def __init__(self, port: int, token: str, repo):
        self.port, self.token, self.repo = port, token, repo
        self._hints: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    def start(self):
        bridge = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default stderr logging
                pass

            def _deny(self, code=403):
                self.send_response(code)
                self.end_headers()

            def _auth(self) -> bool:
                origin = self.headers.get("Origin", "")
                if origin and not origin.startswith("chrome-extension://"):
                    return False
                tok = self.headers.get("X-PRM-Token", "")
                return bool(tok) and hmac.compare_digest(tok, bridge.token)

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self._auth():
                    return self._deny()
                if self.path == "/v1/status":
                    rows = bridge.repo.jobs(limit=10)
                    return self._json({"jobs": [{"file_name": r["file_name"], "state": r["state"],
                                                 "error": r["error"], "finished_ns": r["finished_ns"]}
                                                for r in rows]})
                if self.path == "/v1/ping":
                    return self._json({"ok": True})
                self._deny(404)

            def do_POST(self):
                if not self._auth():
                    return self._deny()
                if self.path != "/v1/hint":
                    return self._deny(404)
                n = int(self.headers.get("Content-Length", "0") or 0)
                if n <= 0 or n > MAX_BODY:
                    return self._deny(413)
                try:
                    body = json.loads(self.rfile.read(n))
                    bridge.add_hint(str(body["file_name"]), str(body.get("url", "")), str(body.get("referrer", "")))
                except Exception:
                    return self._deny(400)
                self._json({"ok": True})

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self._thread = threading.Thread(target=self._server.serve_forever, name="prm-bridge", daemon=True)
        self._thread.start()
        log.info("bridge listening on 127.0.0.1:%s", self.port)

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def add_hint(self, file_name: str, url: str, referrer: str):
        if urlparse(url).scheme not in ("http", "https", "blob", ""):
            return
        name = file_name.replace("\\", "/").split("/")[-1]
        with self._lock:
            now = time.time()
            for k in [k for k, v in self._hints.items() if now - v["t"] > HINT_TTL]:
                del self._hints[k]
            if len(self._hints) >= MAX_HINTS:
                self._hints.pop(next(iter(self._hints)))
            self._hints[name.lower()] = {"url": url, "referrer": referrer, "t": now}

    def hint_for(self, file_name: str) -> dict | None:
        with self._lock:
            h = self._hints.get(file_name.lower())
            if h and time.time() - h["t"] <= HINT_TTL:
                return h
        return None
