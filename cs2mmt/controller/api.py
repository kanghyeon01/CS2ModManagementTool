"""The controller's HTTP API for the PC agent, the peer controller and the ``cs2mmt`` command.

Plain JSON over HTTP on the home network, protected by a shared bearer token. Keep the port
inside the LAN (or behind a VPN); it is never meant to face the internet.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

__all__ = ["ApiServer"]

log = logging.getLogger("cs2mmt.api")

Route = Tuple[str, "re.Pattern[str]", str, bool]  # method, path regex, app method, needs auth

ROUTES = [
    ("GET", r"^/api/v1/health$", "api_health", False),
    ("GET", r"^/api/v1/status$", "api_status", True),
    ("GET", r"^/api/v1/boot-decision$", "api_boot_decision", True),
    ("POST", r"^/api/v1/jobs/claim$", "api_claim", True),
    ("POST", r"^/api/v1/jobs$", "api_enqueue", True),
    ("GET", r"^/api/v1/jobs/(?P<job_id>[A-Za-z0-9_-]+)$", "api_get_job", True),
    ("POST", r"^/api/v1/jobs/(?P<job_id>[A-Za-z0-9_-]+)/heartbeat$", "api_heartbeat", True),
    ("POST", r"^/api/v1/jobs/(?P<job_id>[A-Za-z0-9_-]+)/complete$", "api_complete", True),
    ("POST", r"^/api/v1/dedupe/complete$", "api_dedupe_complete", True),
    ("POST", r"^/api/v1/approvals$", "api_add_approval", True),
    ("GET", r"^/api/v1/approvals/(?P<approval_id>[A-Za-z0-9_-]+)$", "api_get_approval", True),
    ("POST", r"^/api/v1/peer/approval-decision$", "api_peer_decision", True),
    ("POST", r"^/api/v1/notify$", "api_notify", True),
    ("POST", r"^/api/v1/control/update$", "api_update_tool", True),
]
_COMPILED = [(m, re.compile(p), fn, auth) for m, p, fn, auth in ROUTES]
MAX_BODY = 4 * 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "cs2mmt"
    app: Any = None
    token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # route to logging, not stderr
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, data: Optional[Dict[str, Any]]) -> None:
        body = b"" if data is None else json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        if data is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not self.token:
            return False
        return hmac.compare_digest(header[7:].strip().encode(), self.token.encode())

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        for m, rx, fn, needs_auth in _COMPILED:
            if m != method:
                continue
            match = rx.match(parsed.path)
            if not match:
                continue
            authed = self._authorized()
            if needs_auth and not authed:
                self._send(401, {"error": "unauthorized"})
                return
            body: Dict[str, Any] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._send(413, {"error": "body too large"})
                    return
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        self._send(400, {"error": "invalid JSON"})
                        return
                    if not isinstance(body, dict):
                        self._send(400, {"error": "expected a JSON object"})
                        return
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            try:
                status, data = getattr(self.app, fn)(body=body, query=query, authed=authed, **match.groupdict())
            except Exception as e:  # never let one bad request take the server down
                log.exception("API %s %s failed", method, parsed.path)
                status, data = 500, {"error": str(e)[:300]}
            self._send(status, data)
            return
        self._send(404, {"error": "not found"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


class ApiServer:
    def __init__(self, app: Any, host: str, port: int, token: str):
        handler = type("BoundHandler", (_Handler,), {"app": app, "token": token})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="api", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
