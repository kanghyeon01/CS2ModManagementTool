"""Getting queued work to the PC: Wake-on-LAN, waiting for the agent, retries, timeouts.

A job is ``dispatched`` once the PC has been woken for it. The agent then has
``claim_timeout_minutes`` to boot (Linux, switch to Windows, log in) and claim it. If it does
not, the PC is woken again; after ``max_wake_attempts`` an optional power-cycle command runs
(e.g. a smart plug turned off and on, with the BIOS set to power on after power loss). A running
job that stops sending heartbeats is treated as crashed and queued again, a limited number of
times.
"""

from __future__ import annotations

import logging
import shlex
import time
from typing import Any, Callable, Dict, List, Optional

from ..common import proc
from .store import Job, Store
from .wol import send_magic_packet

__all__ = ["Dispatcher"]

log = logging.getLogger("cs2mmt.dispatch")


class Dispatcher:
    def __init__(
        self,
        store: Store,
        hosts: List[Dict[str, Any]],
        notify: Callable[..., Any],
        claim_timeout_minutes: float = 25,
        max_wake_attempts: int = 3,
        lease_grace_minutes: float = 5,
        max_job_attempts: int = 3,
        retry_after_minutes: float = 30,
        power_cycle_command: Optional[str] = None,
        controller_kinds: Optional[List[str]] = None,
        wake: Callable[..., int] = send_magic_packet,
        run=proc.run,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.hosts = hosts
        self.notify = notify
        self.claim_timeout = claim_timeout_minutes * 60
        self.max_wake_attempts = max_wake_attempts
        self.lease_grace = lease_grace_minutes * 60
        self.max_job_attempts = max_job_attempts
        self.retry_after = retry_after_minutes * 60
        self.power_cycle_command = power_cycle_command
        self.controller_kinds = set(controller_kinds or [])
        self._wake = wake
        self._run = run
        self.clock = clock

    @property
    def paused(self) -> bool:
        return bool(self.store.kv_get("control.paused", False))

    @property
    def owner_home(self) -> bool:
        return bool(self.store.kv_get("control.owner_home", False))

    def wake_host(self, host: Optional[Dict[str, Any]] = None) -> int:
        sent = 0
        for h in ([host] if host else self.hosts):
            if not h or not h.get("mac"):
                continue
            try:
                sent += self._wake(h["mac"], h.get("broadcast", "255.255.255.255"), int(h.get("port", 9)), int(h.get("repeat", 3)), h.get("extra_targets", []))
            except OSError as e:
                log.warning("WoL to %s failed: %s", h.get("id"), e)
        return sent

    def tick(self) -> None:
        now = self.clock()
        for job in self.store.jobs(["dispatched", "running"], limit=10):
            self._check_active(job, now)
        if self.store.active_job() is not None:
            return
        # !pause and home mode hold automatic work; a remote session the owner asked for still goes.
        job = self._next_pc_job(only_kinds=["maintenance"] if (self.paused or self.owner_home) else None)
        if job is None:
            return
        sent = self.wake_host()
        ok = self.store.transition(job.id, ["queued"], "dispatched", dispatched_at=now, claim_deadline=now + self.claim_timeout,
                                   wake_attempts=1, stage="waking PC", message="WoL sent (%d packets)" % sent)
        if ok:
            self.store.event("job %s (%s) dispatched, WoL sent" % (job.id, job.kind))

    def _next_pc_job(self, only_kinds: Optional[List[str]] = None) -> Optional[Job]:
        for job in self.store.jobs(["queued"], limit=20):
            if job.kind in self.controller_kinds:
                continue
            if only_kinds is not None and job.kind not in only_kinds:
                continue
            if job.not_before and job.not_before > self.clock():
                continue
            return job
        return None

    def _check_active(self, job: Job, now: float) -> None:
        if job.state == "dispatched" and job.claim_deadline and now > job.claim_deadline:
            if job.wake_attempts < self.max_wake_attempts:
                sent = self.wake_host()
                self.store.update_job(job.id, wake_attempts=job.wake_attempts + 1, claim_deadline=now + self.claim_timeout,
                                      message="no answer from the PC; WoL sent again (%d packets)" % sent)
                self.store.event("job %s: PC did not answer, WoL #%d" % (job.id, job.wake_attempts + 1), "warning")
                return
            if self.power_cycle_command and not job.payload.get("_power_cycled"):
                payload = dict(job.payload)
                payload["_power_cycled"] = now
                r = self._run(shlex.split(self.power_cycle_command), timeout=120)
                self.store.update_job(job.id, payload=payload, claim_deadline=now + self.claim_timeout,
                                      message="power cycle command run (exit %s)" % r.code)
                self.notify("PC가 WoL에 응답하지 않아 전원 재투입 명령을 실행했습니다 (결과 코드 %s)." % r.code, level="warning")
                return
            attempts = job.attempts + 1
            if attempts < self.max_job_attempts:
                self.store.transition(job.id, ["dispatched"], "queued", attempts=attempts, not_before=now + self.retry_after * attempts,
                                      wake_attempts=0, message="PC did not wake; retrying later")
                self.notify("PC가 깨어나지 않습니다 (작업 %s). %d분 뒤 다시 시도합니다." % (job.id, int(self.retry_after * attempts // 60)), level="warning")
            else:
                self.store.transition(job.id, ["dispatched"], "failed", finished_at=now, message="PC never woke up")
                self.notify("PC를 깨우지 못해 작업 %s(%s)를 포기했습니다. 전원·랜선·BIOS의 WoL 설정을 확인해야 합니다." % (job.id, job.kind), level="error")
            return
        if job.state == "running" and job.lease_until and now > job.lease_until + self.lease_grace:
            attempts = job.attempts + 1
            if attempts < self.max_job_attempts:
                self.store.transition(job.id, ["running"], "queued", attempts=attempts, not_before=now + self.retry_after,
                                      wake_attempts=0, message="agent stopped reporting during '%s'; queued again" % (job.stage or "?"))
                self.notify("작업 %s가 '%s' 단계에서 응답이 끊겼습니다 (PC 멈춤?). 다시 시도합니다." % (job.id, job.stage or "?"), level="warning")
            else:
                self.store.transition(job.id, ["running"], "failed", finished_at=now, message="agent stopped reporting")
                self.notify("작업 %s가 %d번 모두 중간에 끊겨 포기했습니다." % (job.id, attempts), level="error")
