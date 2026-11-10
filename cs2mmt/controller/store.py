"""Persistent state of the controller in one SQLite file.

* ``kv``: watcher state (last seen build, last forum post id, ...);
* ``jobs``: the queue (queued -> dispatched -> running -> succeeded / failed / cancelled);
* ``approvals``: changes waiting for the owner's yes or no;
* ``seen``: items already reported, so a restart never reports them twice;
* ``events``: a short history shown by !status and the weekly report.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["Store", "Job", "Approval", "JOB_STATES", "ACTIVE_STATES", "FINAL_STATES"]

JOB_STATES = ("queued", "dispatched", "running", "succeeded", "failed", "cancelled")
ACTIVE_STATES = ("dispatched", "running")
FINAL_STATES = ("succeeded", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    payload TEXT NOT NULL,
    dedupe_key TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    not_before REAL NOT NULL DEFAULT 0,
    host TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    wake_attempts INTEGER NOT NULL DEFAULT 0,
    dispatched_at REAL,
    claim_deadline REAL,
    started_at REAL,
    lease_until REAL,
    finished_at REAL,
    stage TEXT,
    message TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, priority, created);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key, state);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    policy TEXT NOT NULL,
    created REAL NOT NULL,
    auto_at REAL,
    expires REAL,
    decided REAL,
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS approvals_state ON approvals(state);
CREATE TABLE IF NOT EXISTS seen (source TEXT NOT NULL, item TEXT NOT NULL, seen REAL NOT NULL, PRIMARY KEY (source, item));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, level TEXT NOT NULL, text TEXT NOT NULL);
"""


@dataclass
class Job:
    id: str
    kind: str
    state: str
    priority: int
    payload: Dict[str, Any]
    dedupe_key: Optional[str]
    created: float
    updated: float
    not_before: float
    host: Optional[str]
    attempts: int
    wake_attempts: int
    dispatched_at: Optional[float]
    claim_deadline: Optional[float]
    started_at: Optional[float]
    lease_until: Optional[float]
    finished_at: Optional[float]
    stage: Optional[str]
    message: Optional[str]
    result: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Approval:
    id: str
    job_id: Optional[str]
    kind: str
    title: str
    body: str
    payload: Dict[str, Any]
    state: str  # pending / approved / rejected / expired
    policy: str
    created: float
    auto_at: Optional[float]
    expires: Optional[float]
    decided: Optional[float]
    decided_by: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _new_id(prefix: str) -> str:
    return "%s-%s-%s" % (prefix, time.strftime("%Y%m%d"), uuid.uuid4().hex[:6])


class Store:
    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self.clock = clock

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- kv ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key,value,updated) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (key, json.dumps(value), self.clock()),
            )

    def kv_delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key=?", (key,))

    # ---- seen ----
    def seen_add(self, source: str, item: str) -> bool:
        """True when the item is new (and is now recorded)."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO seen(source,item,seen) VALUES(?,?,?)", (source, item, self.clock()))
            return cur.rowcount == 1

    def seen_has(self, source: str, item: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM seen WHERE source=? AND item=?", (source, item)).fetchone() is not None

    # ---- events ----
    def event(self, text: str, level: str = "info") -> None:
        with self._lock:
            self._db.execute("INSERT INTO events(ts,level,text) VALUES(?,?,?)", (self.clock(), level, text[:4000]))
            self._db.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")

    def recent_events(self, limit: int = 20, since: Optional[float] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if since is None:
                rows = self._db.execute("SELECT ts,level,text FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT ts,level,text FROM events WHERE ts>=? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---- jobs ----
    def _job(self, row: sqlite3.Row) -> Job:
        d = dict(row)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return Job(**d)

    def enqueue(
        self,
        kind: str,
        payload: Dict[str, Any],
        priority: int = 50,
        dedupe_key: Optional[str] = None,
        not_before: float = 0.0,
        merge: bool = True,
    ) -> Job:
        """Adds a job. With a ``dedupe_key``, a still-queued job with the same key is updated instead."""
        now = self.clock()
        with self._lock:
            if dedupe_key:
                row = self._db.execute(
                    "SELECT * FROM jobs WHERE dedupe_key=? AND state='queued' ORDER BY created LIMIT 1", (dedupe_key,)
                ).fetchone()
                if row is not None:
                    existing = self._job(row)
                    new_payload = dict(existing.payload)
                    if merge:
                        new_payload.update(payload)
                    else:
                        new_payload = payload
                    self._db.execute(
                        "UPDATE jobs SET payload=?, updated=?, priority=MIN(priority,?) WHERE id=?",
                        (json.dumps(new_payload), now, priority, existing.id),
                    )
                    return self.get_job(existing.id)
            job_id = _new_id(kind.replace("_", "")[:8])
            self._db.execute(
                "INSERT INTO jobs(id,kind,state,priority,payload,dedupe_key,created,updated,not_before) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, kind, "queued", priority, json.dumps(payload), dedupe_key, now, now, not_before),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return None if row is None else self._job(row)

    def jobs(self, states: Optional[Iterable[str]] = None, limit: int = 50) -> List[Job]:
        with self._lock:
            if states:
                st = list(states)
                q = "SELECT * FROM jobs WHERE state IN (%s) ORDER BY priority, created LIMIT ?" % ",".join("?" * len(st))
                rows = self._db.execute(q, (*st, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self._job(r) for r in rows]

    def next_queued(self) -> Optional[Job]:
        now = self.clock()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE state='queued' AND not_before<=? ORDER BY priority, created LIMIT 1", (now,)
            ).fetchone()
        return None if row is None else self._job(row)

    def update_job(self, job_id: str, **fields: Any) -> Optional[Job]:
        if not fields:
            return self.get_job(job_id)
        for k in ("payload", "result"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated"] = self.clock()
        cols = ", ".join("%s=?" % k for k in fields)
        with self._lock:
            self._db.execute("UPDATE jobs SET %s WHERE id=?" % cols, (*fields.values(), job_id))
        return self.get_job(job_id)

    def transition(self, job_id: str, from_states: Iterable[str], to_state: str, **fields: Any) -> bool:
        """Atomic state change; False when the job was not in one of ``from_states``."""
        st = list(from_states)
        fields["state"] = to_state
        fields["updated"] = self.clock()
        for k in ("payload", "result"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        cols = ", ".join("%s=?" % k for k in fields)
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET %s WHERE id=? AND state IN (%s)" % (cols, ",".join("?" * len(st))),
                (*fields.values(), job_id, *st),
            )
            return cur.rowcount == 1

    def active_job(self) -> Optional[Job]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE state IN ('dispatched','running') ORDER BY created LIMIT 1"
            ).fetchone()
        return None if row is None else self._job(row)

    def last_finished(self, kind: Optional[str] = None) -> Optional[Job]:
        with self._lock:
            if kind:
                row = self._db.execute(
                    "SELECT * FROM jobs WHERE state IN ('succeeded','failed','cancelled') AND kind=? ORDER BY finished_at DESC LIMIT 1", (kind,)
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT * FROM jobs WHERE state IN ('succeeded','failed','cancelled') ORDER BY finished_at DESC LIMIT 1"
                ).fetchone()
        return None if row is None else self._job(row)

    # ---- approvals ----
    def _approval(self, row: sqlite3.Row) -> Approval:
        d = dict(row)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        return Approval(**d)

    def add_approval(
        self,
        kind: str,
        title: str,
        body: str,
        payload: Dict[str, Any],
        policy: str,
        job_id: Optional[str] = None,
        auto_at: Optional[float] = None,
        expires: Optional[float] = None,
        approval_id: Optional[str] = None,
    ) -> Approval:
        """Adds a pending approval. A given ``approval_id`` that already exists is left as it is
        (the agent sends the same request to every controller)."""
        aid = approval_id or _new_id("ap")
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO approvals(id,job_id,kind,title,body,payload,state,policy,created,auto_at,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (aid, job_id, kind, title, body, json.dumps(payload), "pending", policy, self.clock(), auto_at, expires),
            )
        return self.get_approval(aid)

    def get_approval(self, aid: str) -> Optional[Approval]:
        with self._lock:
            row = self._db.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        return None if row is None else self._approval(row)

    def approvals(self, state: Optional[str] = "pending") -> List[Approval]:
        with self._lock:
            if state:
                rows = self._db.execute("SELECT * FROM approvals WHERE state=? ORDER BY created", (state,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT 50").fetchall()
        return [self._approval(r) for r in rows]

    def decide_approval(self, aid: str, decision: str, by: str) -> bool:
        if decision not in ("approved", "rejected", "expired"):
            raise ValueError(decision)
        with self._lock:
            cur = self._db.execute(
                "UPDATE approvals SET state=?, decided=?, decided_by=? WHERE id=? AND state='pending'",
                (decision, self.clock(), by, aid),
            )
            return cur.rowcount == 1
