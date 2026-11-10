"""Noticing a new game build on Steam.

The public branch's build id changes with every game patch. Sources, tried in order:

* ``steamcmd_api``: the public JSON mirror of Steam's app info at api.steamcmd.net
  (``data.<appid>.depots.branches.public.buildid``); no login, nothing to install;
* ``steamcmd``: SteamCMD itself (``+login anonymous +app_info_print``), when installed.

A new build is only acted on after it has stayed for ``settle_minutes`` (patches are often
followed by a hotfix within the hour, and Steam's CDN needs time). The patch notes from the
Steam news feed go into the job, so Claude knows what the developers say they changed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..common import proc, vdf
from ..common.http import HttpClient, HttpError
from ..common.versioning import parse_game_version
from .store import Store

__all__ = ["BuildInfo", "SteamError", "fetch_build_steamcmd_api", "fetch_build_steamcmd_cli", "fetch_patch_notes", "SteamWatcher", "clean_bbcode"]

log = logging.getLogger("cs2mmt.steam")

CS2_APPID = 949230


class SteamError(RuntimeError):
    pass


@dataclass
class BuildInfo:
    buildid: str
    timeupdated: Optional[int]
    source: str
    branch: str = "public"


def _branch_from_app(app: Dict[str, Any], branch: str, source: str) -> BuildInfo:
    depots = vdf.get_ci(app, "depots") or {}
    branches = vdf.get_ci(depots, "branches") or {}
    br = vdf.get_ci(branches, branch)
    if not isinstance(br, dict) or not br.get("buildid"):
        raise SteamError("%s: no build id for branch %r" % (source, branch))
    t = br.get("timeupdated")
    return BuildInfo(str(br["buildid"]), int(t) if str(t or "").isdigit() else None, source, branch)


def fetch_build_steamcmd_api(http: HttpClient, appid: int = CS2_APPID, branch: str = "public") -> BuildInfo:
    try:
        data = http.get_json("https://api.steamcmd.net/v1/info/%d" % appid)
    except (HttpError, ValueError) as e:
        raise SteamError("api.steamcmd.net: %s" % e) from e
    if data.get("status") != "success":
        raise SteamError("api.steamcmd.net: status %r" % data.get("status"))
    app = (data.get("data") or {}).get(str(appid))
    if not isinstance(app, dict):
        raise SteamError("api.steamcmd.net: app %d missing" % appid)
    return _branch_from_app(app, branch, "api.steamcmd.net")


def fetch_build_steamcmd_cli(steamcmd: str, appid: int = CS2_APPID, branch: str = "public", run=proc.run) -> BuildInfo:
    r = run([steamcmd, "+login", "anonymous", "+app_info_update", "1", "+app_info_print", str(appid), "+quit"], timeout=300)
    if r.timed_out:
        raise SteamError("steamcmd timed out")
    block = vdf.find_block(r.stdout, str(appid))
    if not isinstance(block, dict):
        raise SteamError("steamcmd: no app info in its output (exit %s): %s" % (r.code, r.tail(8)))
    return _branch_from_app(block, branch, "steamcmd")


_BB_TAG = re.compile(r"\[/?(?:b|i|u|s|h\d|list|olist|\*|url[^\]]*|img[^\]]*|quote[^\]]*|code|noparse|hr|table|tr|td|th|p|previewyoutube[^\]]*|spoiler|strike)\]", re.I)


def clean_bbcode(text: str) -> str:
    text = re.sub(r"\{STEAM_CLAN_IMAGE\}\S*", "", text)
    text = re.sub(r"\[\*\]", "\n- ", text)
    text = _BB_TAG.sub("", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_patch_notes(http: HttpClient, appid: int = CS2_APPID, since: Optional[float] = None, count: int = 10, max_chars: int = 12000) -> List[Dict[str, Any]]:
    data = http.get_json(
        "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
        params={"appid": appid, "count": count, "maxlength": 0, "format": "json", "feeds": "steam_community_announcements"},
    )
    items = ((data or {}).get("appnews") or {}).get("newsitems") or []
    notes: List[Dict[str, Any]] = []
    for it in items:
        date = float(it.get("date") or 0)
        if since is not None and date < since - 3 * 86400:
            continue
        title = str(it.get("title") or "")
        tags = [str(t).lower() for t in (it.get("tags") or [])]
        if "patchnotes" not in tags and not re.search(r"(patch|hotfix|update|\b\d+\.\d+\.\d+)", title, re.I):
            continue
        body = clean_bbcode(str(it.get("contents") or ""))
        notes.append({
            "title": title,
            "date": date,
            "url": it.get("url"),
            "game_version": str(parse_game_version(title) or parse_game_version(body[:400]) or ""),
            "contents": body[:max_chars],
        })
    notes.sort(key=lambda n: n["date"])
    return notes


class SteamWatcher:
    def __init__(
        self,
        store: Store,
        http: HttpClient,
        enqueue: Callable[[Dict[str, Any]], Any],
        notify: Callable[[str], Any],
        sources: Optional[List[str]] = None,
        appid: int = CS2_APPID,
        branch: str = "public",
        settle_minutes: float = 30,
        steamcmd_path: Optional[str] = None,
        watch_branches: Optional[List[str]] = None,
        clock=None,
        run=proc.run,
    ):
        self.store = store
        self.http = http
        self.enqueue = enqueue
        self.notify = notify
        self.sources = sources or ["steamcmd_api", "steamcmd"]
        self.appid = appid
        self.branch = branch
        self.settle = settle_minutes * 60
        self.steamcmd_path = steamcmd_path
        self.watch_branches = watch_branches or []
        self.clock = clock or store.clock
        self._run = run

    def fetch(self, branch: Optional[str] = None) -> BuildInfo:
        branch = branch or self.branch
        errors = []
        for src in self.sources:
            try:
                if src == "steamcmd_api":
                    return fetch_build_steamcmd_api(self.http, self.appid, branch)
                if src == "steamcmd":
                    if not self.steamcmd_path or not Path(self.steamcmd_path).exists():
                        errors.append("steamcmd: not installed")
                        continue
                    return fetch_build_steamcmd_cli(self.steamcmd_path, self.appid, branch, run=self._run)
                errors.append("%s: unknown source" % src)
            except SteamError as e:
                errors.append(str(e))
        raise SteamError("; ".join(errors))

    def poll(self) -> Optional[str]:
        """One check. Returns the new build id when a game_update job was queued."""
        info = self.fetch()
        key = "steam.%s.buildid" % self.branch
        known = self.store.kv_get(key)
        now = self.clock()
        self.store.kv_set("steam.last_check", {"at": now, "buildid": info.buildid, "source": info.source})
        if known is None:
            self.store.kv_set(key, info.buildid)
            self.store.event("Steam baseline: build %s (%s)" % (info.buildid, info.source))
            return None
        if str(info.buildid) == str(known):
            self.store.kv_delete("steam.pending")
            self._check_other_branches()
            return None
        pending = self.store.kv_get("steam.pending")
        if not pending or pending.get("buildid") != info.buildid:
            self.store.kv_set("steam.pending", {"buildid": info.buildid, "first_seen": now, "timeupdated": info.timeupdated})
            self.store.event("New game build %s seen (was %s); waiting %d min before acting" % (info.buildid, known, self.settle // 60))
            return None
        if now - float(pending["first_seen"]) < self.settle:
            return None
        notes: List[Dict[str, Any]] = []
        try:
            notes = fetch_patch_notes(self.http, self.appid, since=info.timeupdated or pending["first_seen"])
        except Exception as e:  # patch notes are a bonus
            log.warning("patch notes: %s", e)
        game_version = next((n["game_version"] for n in reversed(notes) if n.get("game_version")), "")
        payload = {
            "old_build": str(known),
            "new_build": str(info.buildid),
            "timeupdated": info.timeupdated,
            "game_version_hint": game_version,
            "patch_notes": notes[-3:],
            "detected_by": info.source,
        }
        self.enqueue(payload)
        self.store.kv_set(key, info.buildid)
        self.store.kv_delete("steam.pending")
        self.store.event("Game update %s -> %s queued" % (known, info.buildid))
        title = notes[-1]["title"] if notes else ""
        self.notify("게임 업데이트 감지: 빌드 %s → %s%s\n모드 점검 작업을 예약했습니다." % (known, info.buildid, ("\n" + title) if title else ""))
        return info.buildid

    def _check_other_branches(self) -> None:
        """Beta branches only produce a heads-up message; nothing is built for them."""
        for br in self.watch_branches:
            try:
                info = self.fetch(br)
            except SteamError as e:
                log.info("branch %s: %s", br, e)
                continue
            key = "steam.%s.buildid" % br
            known = self.store.kv_get(key)
            if known != info.buildid:
                self.store.kv_set(key, info.buildid)
                if known is not None:
                    self.notify("베타 브랜치 '%s'에 새 빌드 %s가 올라왔습니다 (정식 배포 전 미리 알림)." % (br, info.buildid))
