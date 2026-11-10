"""Watching each mod's GitHub repository for new issues and issue comments."""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable, Dict, List, Optional

from ..common.http import HttpClient
from ..common.registry import ModSpec, Registry
from .store import Store

__all__ = ["GitHubWatcher", "iso_now"]

log = logging.getLogger("cs2mmt.github")


def iso_now(clock: Callable[[], float]) -> str:
    return _dt.datetime.fromtimestamp(clock(), tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()


class GitHubWatcher:
    def __init__(
        self,
        store: Store,
        http: HttpClient,
        registry: Registry,
        on_items: Callable[[ModSpec, List[Dict[str, Any]]], Any],
        token: Optional[str] = None,
        ignore_authors: Optional[List[str]] = None,
        api: str = "https://api.github.com",
    ):
        self.store = store
        self.http = http
        self.registry = registry
        self.on_items = on_items
        self.token = token
        self.ignore = {a.lower() for a in (ignore_authors or [])}
        self.api = api.rstrip("/")

    def _get(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        r = self.http.get(self.api + path, headers=headers, params=params)
        if r.status == 304:
            return []
        if not r.ok:
            raise RuntimeError("GitHub %s: HTTP %d %s" % (path, r.status, r.text[:200]))
        data = r.json()
        return data if isinstance(data, list) else []

    def poll(self) -> int:
        total = 0
        for mod in self.registry.mods:
            slug = mod.github_slug
            if not mod.github_issues or not slug:
                continue
            try:
                items = self.poll_repo(mod, slug)
            except Exception as e:
                log.warning("github %s: %s", slug, e)
                continue
            if items:
                total += len(items)
                self.on_items(mod, items)
        return total

    def poll_repo(self, mod: ModSpec, slug: str) -> List[Dict[str, Any]]:
        key = "github.since." + slug
        since = self.store.kv_get(key)
        if since is None:
            self.store.kv_set(key, iso_now(self.store.clock))
            return []
        owners = self.ignore | {n.lower() for n in mod.owner_names}
        owner_login = slug.split("/")[0].lower()
        owners.add(owner_login)
        items: List[Dict[str, Any]] = []
        newest = since
        titles: Dict[str, str] = {}
        issues = self._get("/repos/%s/issues" % slug, {"state": "all", "since": since, "sort": "updated", "direction": "asc", "per_page": 50})
        for it in issues:
            titles[it.get("url", "")] = it.get("title", "")
            newest = max(newest, it.get("updated_at") or newest)
            if "pull_request" in it:
                continue
            author = ((it.get("user") or {}).get("login") or "").lower()
            if author in owners or author.endswith("[bot]"):
                continue
            if _ts(it.get("created_at")) < _ts(since):
                continue
            if not self.store.seen_add("github", "%s#%s" % (slug, it.get("number"))):
                continue
            items.append({
                "source": "github",
                "mod": mod.id,
                "id": "github:%s#%s" % (slug, it.get("number")),
                "author": author,
                "url": it.get("html_url"),
                "title": it.get("title", ""),
                "text": (it.get("body") or "")[:20000],
                "created": _ts(it.get("created_at")),
                "issue_number": it.get("number"),
                "repo": slug,
            })
        comments = self._get("/repos/%s/issues/comments" % slug, {"since": since, "sort": "updated", "direction": "asc", "per_page": 100})
        for c in comments:
            newest = max(newest, c.get("updated_at") or newest)
            author = ((c.get("user") or {}).get("login") or "").lower()
            if author in owners or author.endswith("[bot]"):
                continue
            if _ts(c.get("created_at")) < _ts(since):
                continue
            if not self.store.seen_add("github-comment", str(c.get("id"))):
                continue
            issue_url = c.get("issue_url", "")
            number = issue_url.rsplit("/", 1)[-1] if issue_url else ""
            items.append({
                "source": "github",
                "mod": mod.id,
                "id": "github:%s#%s/c%s" % (slug, number, c.get("id")),
                "author": author,
                "url": c.get("html_url"),
                "title": titles.get(issue_url, "comment on #%s" % number),
                "text": (c.get("body") or "")[:20000],
                "created": _ts(c.get("created_at")),
                "issue_number": int(number) if number.isdigit() else None,
                "repo": slug,
            })
        self.store.kv_set(key, newest)
        return items
