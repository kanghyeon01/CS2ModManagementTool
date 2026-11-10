"""Redundancy between two (or more) controllers.

Each controller (say the Synology NAS and a Debian/Ubuntu server) runs the same watchers on its
own and keeps its own queue, so either one alone notices a game update and wakes the PC. They
do not share a database; instead:

* the PC agent asks every controller for work and runs a job only once per ``dedupe_key``
  (``game_update:<build>``, ``bugfix:...``), then tells every controller it is done;
* the primary speaks (notifications, Discord commands); a secondary speaks only after the
  primary has been unreachable for ``failover_after_minutes``, and says so;
* each controller checks the others and reports one that stops answering;
* approval decisions are forwarded to the peers, with an outbox for peers that are down.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..common.http import HttpClient
from .store import Store

__all__ = ["Peer", "PeerMonitor"]

log = logging.getLogger("cs2mmt.peers")


@dataclass
class Peer:
    id: str
    url: str
    role: str = "secondary"
    up: Optional[bool] = None
    down_since: Optional[float] = None
    last_ok: Optional[float] = None
    alerted: bool = False
    last_error: str = ""
    info: Dict[str, Any] = field(default_factory=dict)


class PeerMonitor:
    def __init__(
        self,
        store: Store,
        http: HttpClient,
        self_id: str,
        role: str,
        peers: List[Dict[str, Any]],
        token: str,
        notify: Callable[..., Any],
        failover_after_minutes: float = 10,
        alert_after_minutes: float = 15,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        # Health checks must be quick and never pile up retries on a peer that is down.
        self.http = HttpClient(timeout=8, retries=0, user_agent=http.user_agent) if isinstance(http, HttpClient) else http
        self.self_id = self_id
        self.role = role
        self.peers = [Peer(id=p["id"], url=p["url"].rstrip("/"), role=p.get("role", "secondary" if role == "primary" else "primary")) for p in peers]
        self.token = token
        self.notify = notify
        self.failover_after = failover_after_minutes * 60
        self.alert_after = alert_after_minutes * 60
        self.clock = clock
        self._was_active = role == "primary"

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer " + self.token}

    def tick(self) -> None:
        now = self.clock()
        for p in self.peers:
            ok = False
            try:
                r = self.http.get(p.url + "/api/v1/health", headers=self._headers(), timeout=10)
                ok = r.ok
                if ok:
                    p.info = r.json()
                else:
                    p.last_error = "HTTP %d" % r.status
            except Exception as e:
                p.last_error = str(e)[:200]
            if ok:
                if p.up is False and p.alerted:
                    self.notify("이중화: '%s' 시스템이 다시 응답합니다." % p.id, level="success", force=True)
                p.up, p.down_since, p.last_ok, p.alerted = True, None, now, False
            else:
                if p.up is not False:
                    p.down_since = now
                p.up = False
                if not p.alerted and p.down_since is not None and now - p.down_since >= self.alert_after:
                    p.alerted = True
                    self.store.event("peer %s down since %s: %s" % (p.id, time.ctime(p.down_since), p.last_error), "warning")
                    # Whoever notices speaks, even a secondary: this is exactly the case it is there for.
                    self.notify("이중화 경고: '%s' 시스템이 %d분째 응답하지 않습니다 (%s). 이쪽('%s')이 계속 감시합니다."
                                % (p.id, int((now - p.down_since) // 60), p.last_error, self.self_id), level="warning", force=True)
        active = self.is_active_notifier()
        if active != self._was_active:
            self._was_active = active
            if self.role != "primary":
                if active:
                    self.notify("이중화: 주 시스템이 응답하지 않아 '%s'가 알림과 명령 처리를 넘겨받았습니다." % self.self_id, level="warning", force=True)
                else:
                    self.store.event("primary is back; %s returns to standby" % self.self_id)
        self._flush_outbox()

    def is_active_notifier(self) -> bool:
        if self.role == "primary":
            return True
        primaries = [p for p in self.peers if p.role == "primary"]
        if not primaries:
            return True
        now = self.clock()
        for p in primaries:
            if p.up is None:
                return False  # not checked yet: assume the primary is fine
            if p.up or p.down_since is None or now - p.down_since < self.failover_after:
                return False
        return True

    # ---- approval decisions ----
    def forward_decision(self, approval_id: str, decision: str, by: str) -> None:
        outbox = self.store.kv_get("peer.outbox", [])
        for p in self.peers:
            outbox.append({"peer": p.id, "id": approval_id, "decision": decision, "by": by, "at": self.clock()})
        self.store.kv_set("peer.outbox", outbox)
        self._flush_outbox()

    def _flush_outbox(self) -> None:
        outbox = self.store.kv_get("peer.outbox", [])
        if not outbox:
            return
        remaining = []
        for item in outbox:
            peer = next((p for p in self.peers if p.id == item["peer"]), None)
            if peer is None:
                continue
            if self.clock() - float(item.get("at", 0)) > 14 * 86400:
                continue  # give up after two weeks
            try:
                r = self.http.post(peer.url + "/api/v1/peer/approval-decision", headers=self._headers(),
                                   json_body={"id": item["id"], "decision": item["decision"], "by": item["by"], "from": self.self_id}, timeout=10)
                if r.ok or r.status == 404:
                    continue
            except Exception as e:
                log.info("forward to %s: %s", peer.id, e)
            remaining.append(item)
        self.store.kv_set("peer.outbox", remaining)

    def status(self) -> List[Dict[str, Any]]:
        return [{"id": p.id, "role": p.role, "up": p.up, "down_since": p.down_since, "last_ok": p.last_ok, "error": p.last_error} for p in self.peers]
