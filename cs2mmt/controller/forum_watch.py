"""Watching each mod's Paradox forum thread for new posts.

The first check of a thread only records the newest post id (old posts are not reported).
After that, every post with a higher id by someone other than the mod's author becomes an item
for triage. Posts are untrusted text from the internet and are handled as such downstream.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..common.http import HttpClient
from ..common.registry import ModSpec, Registry
from .store import Store
from .xenforo import ThreadPage, looks_blocked, page_url, parse_thread_page, thread_base_url

__all__ = ["ForumBlocked", "ForumWatcher"]

log = logging.getLogger("cs2mmt.forum")


class ForumBlocked(RuntimeError):
    pass


class ForumWatcher:
    def __init__(
        self,
        store: Store,
        http: HttpClient,
        registry: Registry,
        on_items: Callable[[ModSpec, List[Dict[str, Any]]], Any],
        notify: Callable[..., Any],
        ignore_authors: Optional[List[str]] = None,
        max_pages_back: int = 3,
    ):
        self.store = store
        self.http = http
        self.registry = registry
        self.on_items = on_items
        self.notify = notify
        self.ignore = {a.lower() for a in (ignore_authors or [])}
        self.max_pages_back = max_pages_back

    def fetch(self, url: str) -> ThreadPage:
        r = self.http.get(url, headers={"Accept": "text/html", "Accept-Language": "en"})
        body = r.text
        if looks_blocked(r.status, body):
            raise ForumBlocked("HTTP %d (bot protection?) for %s" % (r.status, url))
        if not r.ok:
            raise RuntimeError("HTTP %d for %s" % (r.status, url))
        return parse_thread_page(body, r.url or url)

    def poll(self) -> int:
        found = 0
        for mod in self.registry.mods:
            for thread in mod.forum_threads:
                try:
                    items = self.poll_thread(mod, thread)
                except ForumBlocked as e:
                    self._report_problem(thread, str(e))
                    continue
                except Exception as e:
                    log.warning("forum %s: %s", thread, e)
                    self._report_problem(thread, str(e))
                    continue
                self.store.kv_delete("forum.problem." + thread_base_url(thread))
                if items:
                    found += len(items)
                    self.on_items(mod, items)
        return found

    def _report_problem(self, thread: str, message: str) -> None:
        key = "forum.problem." + thread_base_url(thread)
        prev = self.store.kv_get(key)
        now = self.store.clock()
        if prev is None:
            self.store.kv_set(key, {"since": now, "notified": False, "message": message})
            return
        if not prev.get("notified") and now - float(prev["since"]) > 86400:
            self.notify("포럼을 하루째 읽지 못하고 있습니다: %s\n%s" % (thread, message), level="warning")
            prev["notified"] = True
            self.store.kv_set(key, prev)

    def poll_thread(self, mod: ModSpec, thread_url: str) -> List[Dict[str, Any]]:
        base = thread_base_url(thread_url)
        key = "forum.last_post." + base
        last = self.store.kv_get(key)
        page = self.fetch(base + "latest")
        pages = [page]
        posts = list(page.posts)
        cur = page
        while last is not None and cur.page > 1 and posts and min(p.id for p in posts) > int(last) and len(pages) <= self.max_pages_back:
            cur = self.fetch(page_url(base, cur.page - 1))
            pages.append(cur)
            posts = list(cur.posts) + posts
        if not posts:
            return []
        newest = max(p.id for p in posts)
        if last is None:
            self.store.kv_set(key, newest)
            self.store.event("forum baseline %s: post %d" % (base, newest))
            return []
        owners = self.ignore | {n.lower() for n in mod.owner_names}
        items: List[Dict[str, Any]] = []
        for p in sorted(posts, key=lambda x: x.id):
            if p.id <= int(last):
                continue
            if p.author.lower() in owners:
                continue
            if not self.store.seen_add("forum", str(p.id)):
                continue
            items.append({
                "source": "forum",
                "mod": mod.id,
                "id": "forum:%d" % p.id,
                "author": p.author,
                "url": p.url,
                "title": page.title,
                "text": p.text[:20000],
                "created": p.timestamp,
                "attachments": p.attachments[:10],
            })
        self.store.kv_set(key, max(int(last), newest))
        return items
