"""Small HTTP client on top of urllib (no third-party packages).

Retries transient failures with backoff, sends a descriptive User-Agent, and returns status,
headers and body together so callers can react to rate limits and bot protection.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from .. import USER_AGENT

__all__ = ["HttpError", "Response", "HttpClient"]


class HttpError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes
    url: str

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return self.body.decode(charset, errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpClient:
    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 2.0,
        user_agent: str = USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.user_agent = user_agent
        self._sleep = sleep

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        json_body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        retry_statuses: tuple = (429, 500, 502, 503, 504),
    ) -> Response:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        hdrs = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        if headers:
            hdrs.update(headers)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        last_error: Optional[Exception] = None
        attempts = max(1, self.retries + 1)
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=body, method=method.upper(), headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    data = resp.read()
                    return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, data, resp.geturl())
            except urllib.error.HTTPError as e:
                data = e.read() if hasattr(e, "read") else b""
                response = Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data, url)
                if e.code in retry_statuses and attempt + 1 < attempts:
                    self._sleep(self._delay(attempt, response))
                    continue
                return response
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
                last_error = e
                if attempt + 1 < attempts:
                    self._sleep(self.backoff * (2 ** attempt))
                    continue
        raise HttpError("%s %s failed: %s" % (method, url, last_error))

    def _delay(self, attempt: int, response: Response) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 300.0)
        return self.backoff * (2 ** attempt)

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Response:
        return self.request("POST", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        r = self.get(url, **kw)
        if not r.ok:
            raise HttpError("GET %s: HTTP %d" % (url, r.status), r.status, r.text[:500])
        return r.json()
