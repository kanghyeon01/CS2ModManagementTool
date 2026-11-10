"""Talking to one or more controllers.

With a redundant pair, the agent asks every controller for work and keeps the work unique by
``work_key``: the same game update queued on both the NAS and the server is run once, and both
are told it is done. Every call is best effort per controller; one being down never blocks the
other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..common.http import HttpClient

__all__ = ["ControllerRef", "Controllers", "ClaimedJob"]

log = logging.getLogger("cs2mmt.client")


@dataclass
class ControllerRef:
    id: str
    url: str


@dataclass
class ClaimedJob:
    controller: ControllerRef
    job: Dict[str, Any]
    resumed: bool = False

    @property
    def id(self) -> str:
        return self.job["id"]

    @property
    def kind(self) -> str:
        return self.job["kind"]

    @property
    def payload(self) -> Dict[str, Any]:
        return self.job.get("payload") or {}

    @property
    def work_key(self) -> str:
        return str(self.payload.get("work_key") or self.id)


class Controllers:
    def __init__(self, refs: List[ControllerRef], token: str, http: Optional[HttpClient] = None):
        if not refs:
            raise ValueError("no controllers configured")
        self.refs = refs
        self.token = token
        self.http = http or HttpClient(timeout=20, retries=2)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer " + self.token}

    def _get(self, ref: ControllerRef, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        r = self.http.get(ref.url.rstrip("/") + path, headers=self._headers(), params=params)
        try:
            return r.status, r.json()
        except ValueError:
            return r.status, {}

    def _post(self, ref: ControllerRef, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        r = self.http.post(ref.url.rstrip("/") + path, headers=self._headers(), json_body=body)
        try:
            return r.status, r.json()
        except ValueError:
            return r.status, {}

    def reachable(self) -> List[ControllerRef]:
        out = []
        for ref in self.refs:
            try:
                status, _ = self._get(ref, "/api/v1/health")
                if status == 200:
                    out.append(ref)
            except Exception as e:
                log.info("controller %s unreachable: %s", ref.id, e)
        return out

    def boot_decision(self, boot_time: float, force: bool = False) -> Dict[str, Any]:
        """'windows' if any controller woke the PC for a job (``force``: for any active job,
        used when the PC was already running when the job came)."""
        best: Dict[str, Any] = {"action": "none"}
        params: Dict[str, Any] = {"boot_time": int(boot_time)}
        if force:
            params["force"] = "1"
        for ref in self.refs:
            try:
                status, data = self._get(ref, "/api/v1/boot-decision", params)
            except Exception as e:
                log.info("boot decision from %s: %s", ref.id, e)
                continue
            if status != 200:
                continue
            data["controller"] = ref.id
            if data.get("action") == "windows":
                return data
            if "unattended" in data:
                best.setdefault("unattended", data["unattended"])
                best["unattended"] = best["unattended"] and data["unattended"]
        return best

    def claim(self, host: str, opportunistic: bool = False, kinds: Optional[List[str]] = None) -> Optional[ClaimedJob]:
        for ref in self.refs:
            try:
                status, data = self._post(ref, "/api/v1/jobs/claim", {"host": host, "opportunistic": opportunistic, "kinds": kinds})
            except Exception as e:
                log.info("claim from %s: %s", ref.id, e)
                continue
            if status == 200 and data.get("job"):
                return ClaimedJob(ref, data["job"], bool(data.get("resumed")))
        return None

    def heartbeat(self, claimed: ClaimedJob, stage: str, message: str = "") -> bool:
        """Returns True when the owner asked to cancel the job."""
        try:
            status, data = self._post(claimed.controller, "/api/v1/jobs/%s/heartbeat" % claimed.id, {"stage": stage, "message": message[:900]})
            return status == 200 and bool(data.get("cancel"))
        except Exception as e:
            log.info("heartbeat: %s", e)
            return False

    def complete(self, claimed: ClaimedJob, state: str, result: Dict[str, Any], summary: str) -> None:
        try:
            self._post(claimed.controller, "/api/v1/jobs/%s/complete" % claimed.id, {"state": state, "result": result, "summary": summary})
        except Exception as e:
            log.warning("complete on %s: %s", claimed.controller.id, e)
        if result.get("retry"):
            return  # the job is queued again for later; other controllers keep their copies too
        self.broadcast_done(claimed.work_key, state, summary, exclude=None, via=claimed.controller.id)

    def broadcast_done(self, work_key: str, state: str, summary: str = "", exclude: Optional[str] = None, via: str = "") -> None:
        for ref in self.refs:
            if exclude and ref.id == exclude:
                continue
            try:
                self._post(ref, "/api/v1/dedupe/complete", {"key": work_key, "state": state, "summary": summary, "via": via})
            except Exception as e:
                log.info("dedupe/complete on %s: %s", ref.id, e)

    def request_approval(self, approval: Dict[str, Any]) -> int:
        """Sends the same approval (same id) to every controller; returns how many got it."""
        ok = 0
        for ref in self.refs:
            try:
                status, _ = self._post(ref, "/api/v1/approvals", approval)
                ok += status == 200
            except Exception as e:
                log.info("approval to %s: %s", ref.id, e)
        return ok

    def approval_state(self, approval_id: str) -> str:
        """'rejected' if any controller says so, else 'approved' if any does, else the first state seen."""
        states: List[str] = []
        for ref in self.refs:
            try:
                status, data = self._get(ref, "/api/v1/approvals/%s" % approval_id)
            except Exception:
                continue
            if status == 200 and data.get("approval"):
                states.append(data["approval"]["state"])
        if "rejected" in states:
            return "rejected"
        if "approved" in states:
            return "approved"
        return states[0] if states else "unknown"

    def notify(self, text: str, level: str = "info") -> None:
        for ref in self.refs:
            try:
                status, _ = self._post(ref, "/api/v1/notify", {"text": text, "level": level})
                if status == 200:
                    return  # the controllers decide among themselves who speaks; one copy is enough
            except Exception as e:
                log.info("notify via %s: %s", ref.id, e)
