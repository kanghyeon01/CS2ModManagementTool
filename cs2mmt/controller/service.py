"""The controller process: wires the watchers, queue, dispatcher, notifications and API together.

One main loop (a tick every few seconds) runs each periodic task when it is due; every task is
isolated, so one failing source never stops the others. A watchdog thread ends the process if
the loop stops ticking, so Docker's restart policy or systemd brings it back (Docker does not
restart a container that merely hangs). Under systemd, ``WATCHDOG=1`` is also sent.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import __version__
from .. import selfupdate
from ..common.claude import ClaudeRunner
from ..common.config import Config
from ..common.http import HttpClient, HttpError
from ..common.registry import ModSpec, Registry
from ..common.triage import fix_candidates, run_triage
from .api import ApiServer
from .dispatcher import Dispatcher
from .forum_watch import ForumWatcher
from .github_watch import GitHubWatcher
from .notify import APPROVE_EMOJI, REJECT_EMOJI, DiscordBotChannel, Notifier, build_channels
from .peers import PeerMonitor
from .steam_watch import SteamError, SteamWatcher, fetch_patch_notes
from .store import FINAL_STATES, Job, Store

__all__ = ["Controller", "parse_policy"]

log = logging.getLogger("cs2mmt.controller")

KIND_LABELS = {
    "game_update": "게임 업데이트 대응",
    "bugfix": "버그 수정",
    "publish_approved": "승인된 배포",
    "rehearsal": "점검 리허설",
    "triage": "새 글 분류",
    "maintenance": "원격 점검 모드",
}


def parse_policy(text: str) -> Tuple[str, Optional[float]]:
    """'auto' / 'require' / 'veto:12' -> (mode, hours)."""
    t = (text or "require").strip().lower()
    if t.startswith("veto"):
        hours = 12.0
        if ":" in t:
            try:
                hours = float(t.split(":", 1)[1])
            except ValueError:
                pass
        return "veto", hours
    if t in ("auto", "require"):
        return t, None
    return "require", None


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "%d초 전" % seconds
    if seconds < 5400:
        return "%d분 전" % (seconds // 60)
    if seconds < 172800:
        return "%d시간 전" % (seconds // 3600)
    return "%d일 전" % (seconds // 86400)


class _Periodic:
    def __init__(self, name: str, every_s: float, fn: Callable[[], Any], first_delay_s: float = 0):
        self.name = name
        self.every = every_s
        self.fn = fn
        self.next_at = time.monotonic() + first_delay_s
        self.failures = 0
        self.last_error = ""
        self.last_ok: Optional[float] = None

    def run_if_due(self) -> None:
        now = time.monotonic()
        if now < self.next_at:
            return
        self.next_at = now + self.every
        try:
            self.fn()
            self.failures = 0
            self.last_ok = time.time()
        except Exception as e:
            self.failures += 1
            self.last_error = str(e)[:300]
            log.warning("%s failed (%d in a row): %s", self.name, self.failures, e)
            # back off a little on repeated failures, up to 4x the interval
            self.next_at = now + self.every * min(4, 1 + self.failures // 3)


class Controller:
    def __init__(self, cfg: Config, registry: Registry, store: Optional[Store] = None, http: Optional[HttpClient] = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.registry = registry
        self.clock = clock
        self.id = str(cfg.get("controller.id", socket.gethostname()))
        self.role = str(cfg.get("controller.role", "primary"))
        data_dir = cfg.get_path("controller.data_dir", "data") or Path("data")
        self.data_dir = data_dir
        self.store = store or Store(data_dir / ("controller-%s.sqlite3" % self.id), clock=clock)
        self.http = http or HttpClient(timeout=float(cfg.get("controller.http_timeout", 30)))
        secrets = cfg.section("secrets")
        self.token = str(secrets.get("api_token", ""))
        if len(self.token) < 16:
            raise ValueError("secrets.api_token must be at least 16 characters (it protects the agent API)")
        self.peers = PeerMonitor(
            self.store, self.http, self.id, self.role, cfg.get_list("peers"), self.token, self._notify_raw,
            failover_after_minutes=float(cfg.get("redundancy.failover_after_minutes", 10)),
            alert_after_minutes=float(cfg.get("redundancy.alert_after_minutes", 15)),
            clock=clock,
        )
        self.notifier = Notifier(
            build_channels(cfg.section("notify"), secrets, self.http),
            gate=self.peers.is_active_notifier,
            secrets=cfg.secrets(),
            label=str(cfg.get("notify.label", "")),
        )
        self.dispatcher = Dispatcher(
            self.store,
            cfg.get_list("hosts"),
            self.notify,
            claim_timeout_minutes=float(cfg.get("dispatch.claim_timeout_minutes", 25)),
            max_wake_attempts=int(cfg.get("dispatch.max_wake_attempts", 3)),
            max_job_attempts=int(cfg.get("dispatch.max_job_attempts", 3)),
            retry_after_minutes=float(cfg.get("dispatch.retry_after_minutes", 30)),
            power_cycle_command=cfg.get("dispatch.power_cycle_command") or None,
            controller_kinds=["triage"] if self.triage_here else [],
            clock=clock,
        )
        self.lease_s = float(cfg.get("dispatch.lease_minutes", 20)) * 60
        self.steam = SteamWatcher(
            self.store, self.http, self._enqueue_game_update, self.notify,
            sources=cfg.get_list("steam.sources", ["steamcmd_api", "steamcmd"]),
            appid=int(cfg.get("steam.appid", 949230)),
            branch=str(cfg.get("steam.branch", "public")),
            settle_minutes=float(cfg.get("steam.settle_minutes", 30)),
            steamcmd_path=cfg.get("steam.steamcmd_path") or shutil.which("steamcmd"),
            watch_branches=cfg.get_list("steam.watch_branches"),
        )
        self.forum = ForumWatcher(self.store, self.http, registry, self._on_items, self.notify,
                                  ignore_authors=cfg.get_list("forum.ignore_authors"))
        self.github = GitHubWatcher(self.store, self.http, registry, self._on_items, token=secrets.get("github_token"),
                                    ignore_authors=cfg.get_list("github.ignore_authors"))
        self.api: Optional[ApiServer] = None
        self._stop = threading.Event()
        self._last_tick = time.monotonic()
        self._triage_lock = threading.Lock()
        self.periodic: List[_Periodic] = []
        self._build_periodic()

    # ------------------------------------------------------------------ helpers
    @property
    def triage_here(self) -> bool:
        return str(self.cfg.get("triage.run_on", "agent")) == "controller"

    def notify(self, text: str, level: str = "info", approval_id: Optional[str] = None, force: bool = False) -> bool:
        self.store.event(text.splitlines()[0][:300] if text else "", level)
        return self.notifier.send(text, level=level, approval_id=approval_id, force=force)

    def _notify_raw(self, text: str, level: str = "info", force: bool = False) -> bool:
        # Used by PeerMonitor before the notifier exists; it never asks for approval.
        notifier = getattr(self, "notifier", None)
        if notifier is None:
            return False
        return self.notify(text, level=level, force=force)

    def policy_for(self, kind: str) -> str:
        return str(self.cfg.get("approval.%s" % kind, "auto" if kind == "game_update" else "require"))

    def local_now(self) -> _dt.datetime:
        offset = float(self.cfg.get("schedule.utc_offset_hours", 9))
        return _dt.datetime.fromtimestamp(self.clock(), tz=_dt.timezone(_dt.timedelta(hours=offset)))

    # ------------------------------------------------------------------ queueing
    def _enqueue_game_update(self, payload: Dict[str, Any]) -> Job:
        payload = dict(payload)
        payload.setdefault("approval", self.policy_for("game_update"))
        payload["work_key"] = "game_update:%s" % payload.get("new_build")
        existing = [j for j in self.store.jobs(["queued"]) if j.kind == "game_update"]
        if existing:
            # Keep the oldest "old_build" so the API diff covers every patch in between.
            payload["old_build"] = existing[0].payload.get("old_build", payload.get("old_build"))
        return self.store.enqueue("game_update", payload, priority=10, dedupe_key="game_update")

    def _on_items(self, mod: ModSpec, items: List[Dict[str, Any]]) -> None:
        if not items:
            return
        queued = [j for j in self.store.jobs(["queued"]) if j.kind == "triage"]
        if queued:
            job = queued[0]
            merged = list(job.payload.get("items", [])) + items
            self.store.update_job(job.id, payload={"items": merged[-60:], "work_key": job.payload.get("work_key")})
        else:
            self.store.enqueue("triage", {"items": items, "work_key": "triage:%s" % items[0]["id"]}, priority=60, dedupe_key="triage")
        self.store.event("%d new post(s) for %s" % (len(items), mod.id))

    def enqueue_bugfixes(self, classified: List[Dict[str, Any]], items: List[Dict[str, Any]]) -> List[Job]:
        if not bool(self.cfg.get("triage.auto_fix", True)):
            return []
        by_id = {it["id"]: it for it in items}
        min_conf = float(self.cfg.get("triage.min_confidence", 0.6))
        jobs: List[Job] = []
        per_mod: Dict[str, List[Dict[str, Any]]] = {}
        for c in fix_candidates(classified, min_conf):
            item = by_id.get(c["id"])
            if not item:
                continue
            per_mod.setdefault(item["mod"], []).append({"report": item, "triage": c})
        interval = float(self.cfg.get("triage.bugfix_min_interval_hours", 24)) * 3600
        for mod_id, reports in per_mod.items():
            last = self.store.kv_get("bugfix.last_start.%s" % mod_id, 0) or 0
            not_before = max(0.0, float(last) + interval)
            queued = [j for j in self.store.jobs(["queued"]) if j.kind == "bugfix" and j.payload.get("mod") == mod_id]
            if queued:
                job = queued[0]
                payload = dict(job.payload)
                payload["reports"] = (payload.get("reports", []) + reports)[-10:]
                jobs.append(self.store.update_job(job.id, payload=payload))
                continue
            payload = {
                "mod": mod_id,
                "reports": reports,
                "approval": self.policy_for("bugfix"),
                "work_key": "bugfix:%s:%s" % (mod_id, reports[0]["report"]["id"]),
            }
            jobs.append(self.store.enqueue("bugfix", payload, priority=40, not_before=not_before))
            self.store.kv_set("bugfix.last_start.%s" % mod_id, max(self.clock(), not_before))
        return jobs

    def report_triage(self, classified: List[Dict[str, Any]], items: List[Dict[str, Any]]) -> None:
        by_id = {it["id"]: it for it in items}
        labels = {"bug": "버그", "crash": "충돌/튕김", "compatibility": "호환성", "feature_request": "기능 제안", "question": "질문",
                  "feedback": "의견", "off_topic": "무관", "injection_attempt": "⚠️ 지시문 주입 시도"}
        lines = ["새 글 %d건 분류 결과" % len(classified)]
        for c in classified:
            it = by_id.get(c["id"], {})
            lines.append("")
            lines.append("• [%s] %s — %s (%s)" % (labels.get(c.get("kind"), c.get("kind")), it.get("mod", "?"), it.get("author", "?"), it.get("url", "")))
            lines.append("  " + (c.get("summary_ko") or c.get("summary_en") or ""))
            if c.get("suggested_reply_en"):
                lines.append("  답글 초안: " + c["suggested_reply_en"][:600])
        self.notify("\n".join(lines))

    # ------------------------------------------------------------------ periodic tasks
    def _build_periodic(self) -> None:
        c = self.cfg
        self.periodic = [
            _Periodic("peers", 120, self.peers.tick, first_delay_s=20),
            _Periodic("steam", float(c.get("steam.poll_minutes", 15)) * 60, self._poll_steam, first_delay_s=5),
            _Periodic("dispatch", 15, self.dispatcher.tick, first_delay_s=10),
            _Periodic("approvals", 60, self._tick_approvals, first_delay_s=30),
            _Periodic("schedule", 300, self._tick_schedule, first_delay_s=60),
            _Periodic("healthcheck", float(c.get("health.ping_minutes", 5)) * 60, self._ping_healthcheck, first_delay_s=40),
        ]
        if c.get("forum.enabled", True):
            self.periodic.append(_Periodic("forum", float(c.get("forum.poll_minutes", 60)) * 60, self.forum.poll, first_delay_s=90))
        if c.get("github.enabled", True):
            self.periodic.append(_Periodic("github", float(c.get("github.poll_minutes", 30)) * 60, self.github.poll, first_delay_s=120))
        if self.triage_here:
            self.periodic.append(_Periodic("triage", 120, self._triage_worker, first_delay_s=150))

    def _poll_steam(self) -> None:
        try:
            self.steam.poll()
            self.store.kv_delete("steam.failing_since")
        except SteamError as e:
            since = self.store.kv_get("steam.failing_since")
            now = self.clock()
            if since is None:
                self.store.kv_set("steam.failing_since", now)
            elif now - float(since) > 6 * 3600 and not self.store.kv_get("steam.failing_notified"):
                self.store.kv_set("steam.failing_notified", True)
                self.notify("Steam 빌드 정보를 6시간째 가져오지 못합니다: %s\n패치 노트 피드로 대신 감시합니다." % e, level="warning")
            self._news_fallback(since)
            raise
        else:
            self.store.kv_delete("steam.failing_notified")

    def _news_fallback(self, failing_since: Optional[float]) -> None:
        """While every build source fails, a new patch-notes post still starts a game_update job."""
        if failing_since is None or self.clock() - float(failing_since) < 3600:
            return
        try:
            notes = fetch_patch_notes(self.http, int(self.cfg.get("steam.appid", 949230)), since=self.clock() - 7 * 86400)
        except Exception as e:
            log.info("news fallback: %s", e)
            return
        for n in notes:
            key = "%s|%s" % (n.get("date"), n.get("title"))
            if self.store.seen_add("steam-news", key) and float(n.get("date") or 0) > float(failing_since) - 86400:
                self._enqueue_game_update({"old_build": self.store.kv_get("steam.public.buildid"), "new_build": None,
                                           "patch_notes": [n], "detected_by": "steam news (build sources failing)"})
                self.notify("Steam 빌드 정보는 못 가져오지만 패치 노트가 새로 올라와 점검 작업을 예약했습니다: %s" % n.get("title"), level="warning")

    def _tick_approvals(self) -> None:
        now = self.clock()
        for ap in self.store.approvals("pending"):
            mode, _ = parse_policy(ap.policy)
            if mode == "veto" and ap.auto_at and now >= ap.auto_at:
                self.decide(ap.id, "approved", "auto (거절 없음)")
            elif ap.expires and now >= ap.expires:
                if self.store.decide_approval(ap.id, "expired", "timeout"):
                    self.notify("승인 요청 %s(%s)이 기한이 지나 만료되었습니다. 변경은 GitHub 브랜치에 남아 있습니다." % (ap.id, ap.title), level="warning")

    def _tick_schedule(self) -> None:
        now_local = self.local_now()
        self._credential_reminders(now_local)
        self._weekly_report(now_local)
        self._monthly_rehearsal(now_local)

    def _credential_reminders(self, now_local: _dt.datetime) -> None:
        for r in self.cfg.get_list("reminders"):
            try:
                expires = _dt.date.fromisoformat(str(r.get("expires")))
            except (TypeError, ValueError):
                continue
            days = (expires - now_local.date()).days
            for threshold in (60, 30, 14, 7, 3, 1, 0):
                if days <= threshold:
                    key = "reminder.%s.%s.%d" % (r.get("name"), expires.isoformat(), threshold)
                    if self.store.seen_add("reminder", key):
                        what = r.get("how", "")
                        self.notify("인증 정보 만료 %s: %s (만료일 %s)%s" % ("D-%d" % days if days > 0 else "당일/지남", r.get("name"), expires.isoformat(),
                                                                ("\n갱신 방법: " + what) if what else ""), level="warning" if days > 3 else "error")
                    break

    def _weekly_report(self, now_local: _dt.datetime) -> None:
        weekday = int(self.cfg.get("schedule.weekly_report_weekday", 6))
        hour = int(self.cfg.get("schedule.weekly_report_hour", 19))
        if weekday < 0 or now_local.weekday() != weekday or now_local.hour < hour:
            return
        if not self.store.seen_add("weekly-report", now_local.strftime("%G-W%V")):
            return
        self.notify("주간 보고\n" + self.status_text(since=self.clock() - 7 * 86400))

    def _monthly_rehearsal(self, now_local: _dt.datetime) -> None:
        day = int(self.cfg.get("schedule.rehearsal_day", 1))
        hour = int(self.cfg.get("schedule.rehearsal_hour", 11))
        if day <= 0 or now_local.day != day or now_local.hour < hour:
            return
        month = now_local.strftime("%Y-%m")
        if not self.store.seen_add("rehearsal", month):
            return
        self.store.enqueue("rehearsal", {"work_key": "rehearsal:%s" % month, "reason": "monthly"}, priority=80, dedupe_key="rehearsal")
        self.store.event("monthly rehearsal queued")

    def _ping_healthcheck(self) -> None:
        url = self.cfg.get("health.healthcheck_url")
        if not url:
            return
        # Only "healthy" when the game watch itself works; otherwise ping the optional failure URL
        # or stay silent and let the external monitor raise the alarm.
        steam = next((p for p in self.periodic if p.name == "steam"), None)
        if steam is not None and steam.failures >= 8:
            fail_url = self.cfg.get("health.healthcheck_fail_url")
            if fail_url:
                self.http.get(str(fail_url), timeout=15)
            return
        self.http.get(str(url), timeout=15)

    def _triage_worker(self) -> None:
        if not self._triage_lock.acquire(blocking=False):
            return
        try:
            jobs = [j for j in self.store.jobs(["queued"]) if j.kind == "triage"]
            if not jobs:
                return
            job = jobs[0]
            if not self.store.transition(job.id, ["queued"], "running", started_at=self.clock(), host=self.id, stage="triage"):
                return
            items = job.payload.get("items", [])
            try:
                classified = run_triage(self._claude_runner(), self._prompt("triage.md"), items, self.registry,
                                        log_file=self.data_dir / "logs" / ("%s.log" % job.id))
            except Exception as e:
                self.store.transition(job.id, ["running"], "failed", finished_at=self.clock(), message=str(e)[:500])
                self.notify("새 글 분류에 실패했습니다: %s" % e, level="warning")
                return
            self.report_triage(classified, items)
            fixes = self.enqueue_bugfixes(classified, items)
            self.store.transition(job.id, ["running"], "succeeded", finished_at=self.clock(),
                                  result={"classified": classified, "bugfix_jobs": [j.id for j in fixes if j]})
        finally:
            self._triage_lock.release()

    def _claude_runner(self) -> ClaudeRunner:
        secrets = self.cfg.section("secrets")
        env = {}
        if secrets.get("claude_oauth_token"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = secrets["claude_oauth_token"]
        if secrets.get("anthropic_api_key"):
            env["ANTHROPIC_API_KEY"] = secrets["anthropic_api_key"]
        return ClaudeRunner(executable=str(self.cfg.get("claude.executable", "claude")), env=env,
                            work_dir=self.data_dir / "claude", default_model=self.cfg.get("claude.triage_model", "sonnet"))

    def _prompt(self, name: str) -> str:
        base = self.cfg.get_path("controller.prompts_dir", str(Path(__file__).resolve().parents[2] / "prompts"))
        return (Path(base) / name).read_text(encoding="utf-8")

    # ------------------------------------------------------------------ approvals
    def decide(self, aid: str, decision: str, by: str, from_peer: bool = False) -> bool:
        ap = self.store.get_approval(aid)
        if ap is None:
            return False
        if not self.store.decide_approval(aid, decision, by):
            return False
        if not from_peer:
            self.peers.forward_decision(aid, decision, by)
        if decision == "approved":
            payload = dict(ap.payload)
            payload["approval_id"] = aid
            payload["work_key"] = "publish:%s" % aid
            self.store.enqueue("publish_approved", payload, priority=20, dedupe_key="publish:%s" % aid)
            self.notify("승인됨 (%s): %s\n배포 작업을 예약했습니다." % (by, ap.title), level="success")
        elif decision == "rejected":
            self.notify("거절됨 (%s): %s\n변경은 GitHub 브랜치에만 남고 배포하지 않습니다." % (by, ap.title))
        return True

    # ------------------------------------------------------------------ status text
    def status_text(self, since: Optional[float] = None) -> str:
        now = self.clock()
        last = self.store.kv_get("steam.last_check") or {}
        lines = ["CS2 모드 관리 — %s (%s, v%s)" % (self.id, "주 시스템" if self.role == "primary" else "보조 시스템", __version__)]
        if last:
            lines.append("게임 빌드: %s (확인 %s, %s)" % (last.get("buildid"), _ago(now - float(last.get("at", now))), last.get("source")))
        else:
            lines.append("게임 빌드: 아직 확인 전")
        queued = self.store.jobs(["queued"])
        active = self.store.active_job()
        lines.append("대기 작업 %d건 / 진행 중: %s" % (len(queued), "%s %s (%s)" % (active.id, KIND_LABELS.get(active.kind, active.kind), active.stage or active.state) if active else "없음"))
        pend = self.store.approvals("pending")
        lines.append("승인 대기 %d건%s" % (len(pend), "".join("\n  - %s: %s" % (a.id, a.title) for a in pend[:5])))
        for p in self.peers.status():
            mark = "응답" if p["up"] else ("확인 전" if p["up"] is None else "응답 없음 (%s)" % (p["error"] or "?"))
            lines.append("이중화 %s: %s" % (p["id"], mark))
        lines.append("일시정지: %s / 집에 있음 모드: %s" % ("예" if self.dispatcher.paused else "아니오", "예" if self.dispatcher.owner_home else "아니오"))
        for t in self.periodic:
            if t.failures:
                lines.append("문제: %s %d회 연속 실패 — %s" % (t.name, t.failures, t.last_error))
        if since is not None:
            done = [j for j in self.store.jobs(None, limit=100) if j.finished_at and j.finished_at >= since]
            ok = sum(1 for j in done if j.state == "succeeded")
            lines.append("지난 기간 작업: 성공 %d / 실패 %d" % (ok, len(done) - ok))
        upcoming = []
        for r in self.cfg.get_list("reminders"):
            try:
                d = _dt.date.fromisoformat(str(r.get("expires")))
                upcoming.append((d, r.get("name")))
            except (TypeError, ValueError):
                continue
        if upcoming:
            d, name = min(upcoming)
            lines.append("가장 가까운 만료: %s %s (D-%d)" % (name, d.isoformat(), (d - self.local_now().date()).days))
        return "\n".join(lines)

    # ------------------------------------------------------------------ API
    def api_health(self, body, query, authed, **_):
        data: Dict[str, Any] = {"ok": True, "id": self.id, "role": self.role, "version": __version__, "time": self.clock()}
        if authed:
            data["active_notifier"] = self.peers.is_active_notifier()
            data["queued"] = len(self.store.jobs(["queued"]))
            active = self.store.active_job()
            data["active_job"] = active.id if active else None
            data["last_steam_check"] = self.store.kv_get("steam.last_check")
        return 200, data

    def api_status(self, body, query, authed, **_):
        return 200, {
            "text": self.status_text(),
            "jobs": [j.to_dict() for j in self.store.jobs(None, limit=20)],
            "approvals": [a.to_dict() for a in self.store.approvals("pending")],
            "peers": self.peers.status(),
            "events": self.store.recent_events(30),
        }

    def api_boot_decision(self, body, query, authed, **_):
        """Asked by the Linux side of the PC at boot: switch to Windows for a job?"""
        boot_time = float(query.get("boot_time") or 0)
        active = self.store.active_job()
        unattended = bool(self.cfg.get("controller.unattended", True)) and not self.dispatcher.owner_home
        if active is not None and active.state in ("dispatched", "running"):
            woke_for_it = active.dispatched_at is not None and boot_time >= float(active.dispatched_at) - 300
            if woke_for_it or query.get("force") == "1":
                return 200, {"action": "windows", "job_id": active.id, "kind": active.kind, "unattended": unattended}
        return 200, {"action": "none", "unattended": unattended, "queued": len(self.store.jobs(["queued"]))}

    def api_claim(self, body, query, authed, **_):
        host = str(body.get("host") or "pc")
        now = self.clock()
        kinds = body.get("kinds")
        for job in self.store.jobs(["running"]):
            if job.host == host and (not kinds or job.kind in kinds):
                # The agent restarted in the middle of this job (crash or reboot): hand it back.
                self.store.update_job(job.id, lease_until=now + self.lease_s, attempts=job.attempts + 1, message="resumed by the agent")
                return 200, {"job": self.store.get_job(job.id).to_dict(), "resumed": True}
        candidates = self.store.jobs(["dispatched"])
        if body.get("opportunistic"):
            candidates += [j for j in self.store.jobs(["queued"]) if (not j.not_before or j.not_before <= now)]
        for job in candidates:
            if job.kind in self.dispatcher.controller_kinds:
                continue
            if kinds and job.kind not in kinds:
                continue
            if self.store.transition(job.id, ["dispatched", "queued"], "running", host=host, started_at=now, lease_until=now + self.lease_s, stage="claimed"):
                fresh = self.store.get_job(job.id)
                if fresh.kind == "bugfix":
                    self.store.kv_set("bugfix.last_start.%s" % fresh.payload.get("mod"), now)
                return 200, {"job": fresh.to_dict(), "resumed": False}
        return 200, {"job": None}

    def api_enqueue(self, body, query, authed, **_):
        kind = str(body.get("kind") or "")
        if kind not in KIND_LABELS:
            return 400, {"error": "unknown kind %r" % kind}
        payload = dict(body.get("payload") or {})
        if kind == "game_update":
            payload.setdefault("new_build", self.store.kv_get("steam.public.buildid"))
            payload.setdefault("old_build", payload.get("new_build"))
            payload["manual"] = True
            job = self._enqueue_game_update(payload)
        else:
            payload.setdefault("approval", self.policy_for(kind if kind != "publish_approved" else "game_update"))
            payload.setdefault("work_key", "%s:manual:%d" % (kind, int(self.clock())))
            job = self.store.enqueue(kind, payload, priority=int(body.get("priority", 30)), dedupe_key=body.get("dedupe_key"))
        return 200, {"job": job.to_dict()}

    def api_get_job(self, body, query, authed, job_id, **_):
        job = self.store.get_job(job_id)
        return (404, {"error": "no such job"}) if job is None else (200, {"job": job.to_dict()})

    def api_heartbeat(self, body, query, authed, job_id, **_):
        job = self.store.get_job(job_id)
        if job is None or job.state != "running":
            return 409, {"error": "job is not running", "state": job.state if job else None}
        self.store.update_job(job_id, lease_until=self.clock() + self.lease_s, stage=str(body.get("stage") or job.stage or "")[:200],
                              message=str(body.get("message") or "")[:1000])
        return 200, {"ok": True, "cancel": bool(job.payload.get("_cancel"))}

    def _finish_notification(self, job: Optional[Job], key: str, state: str, summary: str) -> None:
        if not summary:
            return
        if not self.store.seen_add("notified", "%s|%s" % (key, state)):
            return
        level = "success" if state == "succeeded" else "error"
        kind = job.kind if job else key.split(":")[0]
        label = KIND_LABELS.get(kind, kind)
        self.notify("%s %s\n%s" % (label, "완료" if state == "succeeded" else "실패", summary), level=level)

    def api_complete(self, body, query, authed, job_id, **_):
        state = str(body.get("state") or "failed")
        if state not in FINAL_STATES:
            return 400, {"error": "bad state"}
        job = self.store.get_job(job_id)
        if job is None:
            return 404, {"error": "no such job"}
        result = body.get("result") or {}
        key = job.payload.get("work_key") or job.id
        max_attempts = int(self.cfg.get("dispatch.max_job_attempts", 3))
        if state == "failed" and result.get("retry") and job.attempts + 1 < max_attempts:
            # Transient trouble (Steam download, Claude usage limit): try again later.
            delay = float(self.cfg.get("dispatch.transient_retry_hours", 6)) * 3600 * (job.attempts + 1)
            self.store.transition(job_id, ["running", "dispatched"], "queued", attempts=job.attempts + 1, not_before=self.clock() + delay,
                                  wake_attempts=0, result=result, message="transient failure; retrying later", stage="retry")
            self.notify("%s 작업이 일시적인 문제로 실패해 %d시간 뒤 다시 시도합니다.\n%s" % (KIND_LABELS.get(job.kind, job.kind), int(delay // 3600),
                                                                      str(body.get("summary") or "")[:1500]), level="warning")
            return 200, {"ok": True, "requeued": True}
        self.store.transition(job_id, ["running", "dispatched", "queued"], state, finished_at=self.clock(), result=result,
                              message=str(body.get("summary") or "")[:1000], stage="done")
        self._finish_notification(job, key, state, str(body.get("summary") or ""))
        if job.kind == "triage" and state == "succeeded":
            classified = result.get("classified") or []
            items = job.payload.get("items", [])
            if classified and not result.get("reported"):
                self.report_triage(classified, items)
            self.enqueue_bugfixes(classified, items)
        return 200, {"ok": True}

    def api_dedupe_complete(self, body, query, authed, **_):
        """The agent finished work for ``key`` (possibly claimed from a peer): close our copies."""
        key = str(body.get("key") or "")
        state = str(body.get("state") or "succeeded")
        if not key:
            return 400, {"error": "key required"}
        closed = []
        for job in self.store.jobs(["queued", "dispatched"], limit=100):
            if job.payload.get("work_key") == key or self._superseded(job, key):
                if self.store.transition(job.id, ["queued", "dispatched"], "succeeded" if state == "succeeded" else state, finished_at=self.clock(),
                                         message="done via %s" % body.get("via", "agent"), result={"duplicate_of": key}):
                    closed.append(job.id)
        self._finish_notification(None, key, state, str(body.get("summary") or ""))
        if key.startswith("game_update:") and state == "succeeded":
            build = key.split(":", 1)[1]
            if build and build != "None":
                known = self.store.kv_get("steam.public.buildid")
                if known is None or (str(build).isdigit() and str(known).isdigit() and int(build) > int(known)):
                    self.store.kv_set("steam.public.buildid", build)
        return 200, {"closed": closed}

    @staticmethod
    def _superseded(job: Job, key: str) -> bool:
        if job.kind != "game_update" or not key.startswith("game_update:"):
            return False
        done = key.split(":", 1)[1]
        mine = str(job.payload.get("new_build"))
        return done.isdigit() and mine.isdigit() and int(done) >= int(mine)

    def api_add_approval(self, body, query, authed, **_):
        aid = str(body.get("id") or "")
        if not aid or len(aid) > 40:
            return 400, {"error": "id required (max 40 chars)"}
        policy = str(body.get("policy") or "require")
        mode, hours = parse_policy(policy)
        now = self.clock()
        auto_at = now + hours * 3600 if mode == "veto" and hours else None
        expires = now + float(self.cfg.get("approval.expire_days", 14)) * 86400
        existed = self.store.get_approval(aid) is not None
        ap = self.store.add_approval(kind=str(body.get("kind") or "publish"), title=str(body.get("title") or aid)[:300],
                                     body=str(body.get("body") or "")[:20000], payload=dict(body.get("payload") or {}),
                                     policy=policy, job_id=body.get("job_id"), auto_at=auto_at, expires=expires, approval_id=aid)
        if not existed:
            when = ""
            if auto_at:
                when = "\n%d시간 안에 거절하지 않으면 자동으로 배포합니다." % int(hours)
            self.notify("승인 요청 %s\n%s\n\n%s%s\n\n이 메시지의 ✅(승인) 또는 ❌(거절) 반응을 누르세요. 명령으로는 !approve %s 또는 !reject %s" % (aid, ap.title, ap.body[:2500], when, aid, aid),
                        approval_id=aid)
        return 200, {"approval": ap.to_dict()}

    def api_get_approval(self, body, query, authed, approval_id, **_):
        ap = self.store.get_approval(approval_id)
        return (404, {"error": "no such approval"}) if ap is None else (200, {"approval": ap.to_dict()})

    def api_peer_decision(self, body, query, authed, **_):
        aid = str(body.get("id") or "")
        decision = str(body.get("decision") or "")
        if decision not in ("approved", "rejected", "expired"):
            return 400, {"error": "bad decision"}
        ap = self.store.get_approval(aid)
        if ap is None:
            return 404, {"error": "unknown approval"}
        if ap.state != "pending":
            return 200, {"ok": True, "state": ap.state}
        if decision == "approved":
            # The peer queued the publish job already; just record the decision here.
            self.store.decide_approval(aid, decision, "%s (via %s)" % (body.get("by"), body.get("from")))
        else:
            self.decide(aid, decision, "%s (via %s)" % (body.get("by"), body.get("from")), from_peer=True)
        return 200, {"ok": True}

    def api_notify(self, body, query, authed, **_):
        text = str(body.get("text") or "")[:8000]
        if text:
            self.notify(text, level=str(body.get("level") or "info"))
        return 200, {"ok": True}

    # ------------------------------------------------------------------ updating the tool
    def update_tool(self, propagate: bool = False, restart: Optional[bool] = None) -> str:
        """Fast-forwards this machine's checkout, checks it, and restarts with the new code.

        ``propagate``: also ask the peer controllers to update. The PC agent updates itself each
        time it starts. The C sentinel on this machine is asked to restart (it rebuilds from the
        updated source when it runs in Docker, see sentinel/docker-entrypoint.sh).
        """
        repo = self.cfg.get_path("update.repo_dir") or selfupdate.repo_root()
        branch = str(self.cfg.get("update.branch", "main"))
        res = selfupdate.self_update(repo, branch, check=selfupdate.default_check())
        lines = ["%s: %s" % (self.id, res.short())]
        if propagate:
            for p in self.peers.peers:
                try:
                    r = self.peers.http.post(p.url + "/api/v1/control/update", headers={"Authorization": "Bearer " + self.token},
                                             json_body={"from": self.id}, timeout=120)
                    lines.append("%s: %s" % (p.id, (r.json() or {}).get("result", "HTTP %d" % r.status)))
                except Exception as e:
                    lines.append("%s: 연결 실패 (%s)" % (p.id, str(e)[:100]))
        self.store.event("tool update: %s" % res.short(), "info" if res.ok else "warning")
        if res.changed:
            sentinel = str(self.cfg.get("update.sentinel_url", "http://127.0.0.1:8766"))
            if sentinel:
                try:
                    self.peers.http.post(sentinel.rstrip("/") + "/api/v1/control", headers={"Authorization": "Bearer " + self.token},
                                         json_body={"action": "restart"}, timeout=10)
                    lines.append("%s의 C 감시기 재시작 요청" % self.id)
                except Exception:
                    pass
            if restart if restart is not None else bool(self.cfg.get("update.restart", True)):
                lines.append("%s 컨트롤러가 새 코드로 다시 시작합니다." % self.id)
                threading.Timer(3.0, self._restart_now).start()
        return "도구 업데이트\n" + "\n".join(lines) + "\nPC 에이전트는 다음에 켜질 때 스스로 업데이트합니다."

    def _restart_now(self) -> None:
        log.info("restarting to load the updated code")
        self.stop()
        logging.shutdown()
        os._exit(0)  # Docker (unless-stopped), systemd (Restart=always) or the keepalive script starts us again

    def api_update_tool(self, body, query, authed, **_):
        text = self.update_tool(propagate=False)
        lines = text.split("\n")
        return 200, {"result": lines[1] if len(lines) > 1 else text}

    # ------------------------------------------------------------------ Discord
    @property
    def owner_user_id(self) -> str:
        return str(self.cfg.get("notify.discord_owner_user_id") or "")

    def _remember_approval_message(self, approval_id: str, message_id: str) -> None:
        msgs = self.store.kv_get("discord.approval_msgs", {}) or {}
        msgs[approval_id] = message_id
        self.store.kv_set("discord.approval_msgs", msgs)

    def handle_discord_message(self, msg: Dict[str, Any]) -> None:
        """A message in the bot's channel: commands count only from the owner's account."""
        author = msg.get("author") or {}
        if author.get("bot") or not self.owner_user_id or str(author.get("id")) != self.owner_user_id:
            return
        text = re.sub(r"^\s*<@!?\d+>\s*", "", str(msg.get("content") or "")).strip()  # "@bot !status" works too
        if not text.startswith("!"):
            return
        reply = self.command(text)
        if reply:
            self.notifier.send(reply, force=True)

    def check_approval_reactions(self, bot: DiscordBotChannel) -> None:
        """✅ / ❌ from the owner on an approval message decides it."""
        msgs = self.store.kv_get("discord.approval_msgs", {}) or {}
        if not msgs or not self.owner_user_id:
            return
        changed = False
        for aid, mid in list(msgs.items()):
            ap = self.store.get_approval(aid)
            if ap is None or ap.state != "pending":
                del msgs[aid]
                changed = True
                continue
            if self.owner_user_id in bot.reaction_user_ids(mid, REJECT_EMOJI):
                self.decide(aid, "rejected", "discord")
            elif self.owner_user_id in bot.reaction_user_ids(mid, APPROVE_EMOJI):
                self.decide(aid, "approved", "discord")
            else:
                continue
            del msgs[aid]
            changed = True
        if changed:
            self.store.kv_set("discord.approval_msgs", msgs)

    def command(self, text: str) -> str:
        parts = text.split()
        if not parts or not parts[0].startswith("!"):
            return ""
        cmd = parts[0][1:].lower()  # commands are "!status": a leading / would open Discord's own command picker
        args = parts[1:]
        if cmd == "help":
            return ("명령어 (이 채널에 입력)\n!status 상태\n!jobs 최근 작업\n!approve <id> / !reject <id> 승인·거절 (✅/❌ 반응으로도 가능)\n"
                    "!pause / !resume 자동 작업 멈춤·재개\n!maintenance [시간] PC를 Windows로 켜 두기(원격 접속용, 기본 2시간)\n"
                    "!done 원격 점검 끝(PC 끄기)\n!update_tool 모든 기기의 도구를 GitHub 최신으로 업데이트\n!clear 대기 중인 작업 모두 취소\n"
                    "!home on|off 집에 있음 모드(PC를 깨우지 않음)\n!run update|rehearsal|check 수동 실행\n!cancel <작업id> 진행 중 작업 취소")
        if cmd == "status":
            return self.status_text()
        if cmd == "jobs":
            rows = ["최근 작업"]
            for j in self.store.jobs(None, limit=10):
                rows.append("%s %s %s — %s" % (j.id, KIND_LABELS.get(j.kind, j.kind), j.state, (j.message or j.stage or "")[:120]))
            return "\n".join(rows)
        if cmd in ("approve", "reject") and args:
            ok = self.decide(args[0], "approved" if cmd == "approve" else "rejected", "discord")
            return "" if ok else "해당 승인 요청이 없거나 이미 처리되었습니다."
        if cmd == "pause":
            self.store.kv_set("control.paused", True)
            return "자동 작업을 멈췄습니다. 감시와 알림은 계속합니다. !resume 으로 재개합니다."
        if cmd == "resume":
            self.store.kv_set("control.paused", False)
            return "자동 작업을 재개합니다."
        if cmd == "home":
            on = bool(args) and args[0].lower() in ("on", "1", "yes", "켜기")
            self.store.kv_set("control.owner_home", on)
            return "집에 있음 모드 %s" % ("켬: PC를 깨우지 않고 작업을 쌓아 둡니다." if on else "끔: 다시 자동으로 PC를 깨웁니다.")
        if cmd == "run" and args:
            what = args[0].lower()
            if what == "update":
                build = self.store.kv_get("steam.public.buildid")
                self._enqueue_game_update({"old_build": build, "new_build": build, "manual": True})
                return "게임 업데이트 대응 작업을 수동으로 예약했습니다 (빌드 %s)." % build
            if what == "rehearsal":
                self.store.enqueue("rehearsal", {"work_key": "rehearsal:manual:%d" % int(self.clock()), "reason": "manual"}, priority=30)
                return "점검 리허설을 예약했습니다 (배포 없이 전체 과정을 시험)."
            if what == "check":
                for t in self.periodic:
                    if t.name in ("steam", "forum", "github"):
                        t.next_at = 0
                return "지금 바로 Steam·포럼·GitHub를 확인합니다."
        if cmd == "maintenance":
            hours = 2.0
            if args:
                try:
                    hours = max(0.1, min(12.0, float(args[0])))
                except ValueError:
                    return "사용법: !maintenance [시간], 예: !maintenance 3"
            open_jobs = [j for j in self.store.jobs(["queued", "dispatched", "running"]) if j.kind == "maintenance"]
            if open_jobs:
                return "원격 점검 모드가 이미 예약되어 있습니다 (%s, %s)." % (open_jobs[0].id, open_jobs[0].state)
            job = self.store.enqueue("maintenance", {"hours": hours, "work_key": "maintenance:%d" % int(self.clock())}, priority=1)
            return ("PC를 깨워 Windows로 켭니다 (%s). 켜지면 알려 드립니다.\n"
                    "%.1f시간 뒤 또는 !done 하면 꺼집니다. 그 사이 다른 대기 작업은 하지 않습니다." % (job.id, hours))
        if cmd == "done":
            for job in self.store.jobs(["queued", "dispatched", "running"]):
                if job.kind != "maintenance":
                    continue
                if job.state == "running":
                    payload = dict(job.payload)
                    payload["_cancel"] = True
                    self.store.update_job(job.id, payload=payload)
                    return "원격 점검을 끝냅니다. PC가 1분 안에 종료 절차를 시작합니다."
                self.store.transition(job.id, ["queued", "dispatched"], "cancelled", finished_at=self.clock(), message="!done")
                return "아직 켜지기 전이라 원격 점검 예약을 취소했습니다."
            return "진행 중인 원격 점검이 없습니다."
        if cmd == "clear":
            n = 0
            for job in self.store.jobs(["queued", "dispatched"], limit=200):
                if self.store.transition(job.id, ["queued", "dispatched"], "cancelled", finished_at=self.clock(), message="!clear"):
                    n += 1
            return "대기 중인 작업 %d건을 취소했습니다 (진행 중인 작업은 !cancel)." % n
        if cmd == "update_tool":
            return self.update_tool(propagate=True)
        if cmd == "cancel" and args:
            job = self.store.get_job(args[0])
            if job is None:
                return "그런 작업이 없습니다."
            if job.state == "running":
                payload = dict(job.payload)
                payload["_cancel"] = True
                self.store.update_job(job.id, payload=payload)
                return "취소를 요청했습니다. PC가 다음 보고 때 멈춥니다."
            if job.state in ("queued", "dispatched"):
                self.store.transition(job.id, ["queued", "dispatched"], "cancelled", finished_at=self.clock(), message="cancelled from Discord")
                return "작업을 취소했습니다."
            return "이미 끝난 작업입니다."
        return "알 수 없는 명령입니다. !help"

    def _discord_loop(self) -> None:
        bot = self.notifier.discord()
        if bot is None:
            return
        if not self.owner_user_id:
            log.warning("notify.discord_owner_user_id is not set: Discord commands and reactions are ignored")
        poll = float(self.cfg.get("notify.discord_poll_seconds", 10))
        last = self.store.kv_get("discord.last_message")
        next_reactions = 0.0
        while not self._stop.is_set():
            if not self.peers.is_active_notifier():
                self._stop.wait(30)
                continue
            try:
                if last is None:
                    last = bot.latest_message_id() or "0"  # start from now: old messages are not commands
                    self.store.kv_set("discord.last_message", last)
                for msg in bot.messages_after(last):
                    last = str(msg.get("id"))
                    self.store.kv_set("discord.last_message", last)
                    try:
                        self.handle_discord_message(msg)
                    except Exception:
                        log.exception("discord message failed")
                if time.monotonic() >= next_reactions:
                    next_reactions = time.monotonic() + 30
                    self.check_approval_reactions(bot)
            except Exception as e:
                log.info("discord: %s", e)
                self._stop.wait(30)
                continue
            self._stop.wait(poll)

    # ------------------------------------------------------------------ lifecycle
    def _sd_notify(self, message: str) -> None:
        addr = os.environ.get("NOTIFY_SOCKET")
        if not addr:
            return
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(addr)
                s.sendall(message.encode())
        except OSError as e:
            log.debug("sd_notify: %s", e)

    def _watchdog_loop(self, limit_s: float) -> None:
        while not self._stop.wait(30):
            if time.monotonic() - self._last_tick > limit_s:
                log.critical("main loop stuck for %.0fs; exiting so the supervisor restarts the controller", limit_s)
                logging.shutdown()
                os._exit(3)

    def tick(self) -> None:
        self._last_tick = time.monotonic()
        for task in self.periodic:
            task.run_if_due()
            self._last_tick = time.monotonic()
        self._sd_notify("WATCHDOG=1")

    def start(self, serve_api: bool = True) -> None:
        if serve_api:
            host = str(self.cfg.get("controller.listen_host", "0.0.0.0"))
            port = int(self.cfg.get("controller.listen_port", 8765))
            self.api = ApiServer(self, host, port, self.token)
            self.api.start()
            log.info("API listening on %s:%d", host, self.api.port)
        bot = self.notifier.discord()
        if bot is not None:
            bot.on_approval_message = self._remember_approval_message
        threading.Thread(target=self._discord_loop, name="discord", daemon=True).start()
        limit = float(self.cfg.get("health.watchdog_minutes", 10)) * 60
        threading.Thread(target=self._watchdog_loop, args=(limit,), name="watchdog", daemon=True).start()
        self.store.event("controller %s (%s) started, v%s" % (self.id, self.role, __version__))
        self._sd_notify("READY=1")
        if not self.store.kv_get("controller.started_once"):
            self.store.kv_set("controller.started_once", self.clock())
            self.notify("CS2 모드 관리 컨트롤러 '%s'(%s)가 처음 시작되었습니다." % (self.id, "주" if self.role == "primary" else "보조"), force=True)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(5)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self.api:
            try:
                self.api.stop()
            except Exception:
                pass
        self._sd_notify("STOPPING=1")
