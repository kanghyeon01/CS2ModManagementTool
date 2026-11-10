"""Reading posts from a XenForo 2 thread page (forum.paradoxplaza.com runs XenForo).

Each post is an ``<article data-content="post-N" data-author="...">``; its text is the first
``div.bbWrapper`` inside it (signatures have their own wrapper, which is skipped). Quotes are
kept, prefixed with ``> `` so a report that quotes another post still reads clearly.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

__all__ = ["ForumPost", "ThreadPage", "parse_thread_page", "thread_base_url", "page_url", "looks_blocked"]

_BLOCK_TAGS = {"div", "p", "br", "li", "ul", "ol", "blockquote", "pre", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass
class ForumPost:
    id: int
    author: str
    timestamp: Optional[float]
    text: str
    url: str = ""
    attachments: List[str] = field(default_factory=list)


@dataclass
class ThreadPage:
    posts: List[ForumPost]
    page: int
    last_page: int
    title: str = ""


def thread_base_url(url: str) -> str:
    """https://host/forum/threads/slug.123/page-4#post-9 -> https://host/forum/threads/slug.123/"""
    m = re.match(r"^(https?://[^/]+/(?:.*?/)?threads/[^/]+?\.\d+)(?:/.*)?$", url.strip())
    if not m:
        raise ValueError("not a XenForo thread URL: %r" % url)
    return m.group(1) + "/"


def page_url(base: str, page: int) -> str:
    return base if page <= 1 else "%spage-%d" % (base, page)


def looks_blocked(status: int, body: str) -> bool:
    """Bot protection (e.g. Cloudflare) instead of the forum page."""
    if status in (403, 429, 503):
        return True
    head = body[:4000].lower()
    return "just a moment..." in head or "cf-browser-verification" in head or "attention required!" in head or "challenge-platform" in head


def _parse_time(attrs: Dict[str, str]) -> Optional[float]:
    for key in ("data-timestamp", "data-time"):
        v = attrs.get(key, "")
        if v.isdigit():
            return float(v)
    iso = attrs.get("datetime")
    if iso:
        try:
            iso = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", iso)
            return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class _ThreadParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.posts: List[ForumPost] = []
        self.title = ""
        self.canonical = ""
        self.page_links: List[str] = []
        self._in_title = False
        # post state
        self._post: Optional[ForumPost] = None
        self._article_depth = 0  # nesting of <article> inside the current post
        self._have_body = False
        self._in_signature = 0
        # body state
        self._div_depth = 0
        self._body_depth: Optional[int] = None
        self._quote_depth = 0
        self._skip_depth: Optional[int] = None
        self._buf: List[str] = []

    # -- helpers --
    def _emit(self, text: str) -> None:
        if self._body_depth is None or self._skip_depth is not None:
            return
        # HTML collapses whitespace in text; line breaks come only from <br> and block elements.
        text = re.sub(r"\s+", " ", text)
        if not text.strip() and (not self._buf or self._buf[-1].endswith(("\n", " ", "> "))):
            return
        self._buf.append(text)

    def _newline(self) -> None:
        if self._body_depth is not None and self._skip_depth is None:
            self._buf.append("\n> " if self._quote_depth else "\n")

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        cls = attrs.get("class", "")
        if tag == "title":
            self._in_title = True
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href", "")
        if tag == "a" and "href" in attrs:
            href = attrs["href"]
            if "/page-" in href:
                self.page_links.append(href)
            if self._post is not None and "/attachments/" in href:
                full = href if href.startswith("http") else re.sub(r"^(https?://[^/]+).*$", r"\1", self.base_url) + href
                if full not in self._post.attachments:
                    self._post.attachments.append(full)
        if tag == "article":
            content = attrs.get("data-content", "")
            if self._post is None and content.startswith("post-") and content[5:].isdigit():
                pid = int(content[5:])
                self._post = ForumPost(id=pid, author=attrs.get("data-author", ""), timestamp=None, text="",
                                       url="%spost-%d" % (self.base_url, pid))
                self._article_depth = 1
                self._have_body = False
                return
            if self._post is not None:
                self._article_depth += 1
        if self._post is None:
            return
        if tag == "aside" and "message-signature" in cls:
            self._in_signature += 1
        if tag == "time" and self._post.timestamp is None:
            self._post.timestamp = _parse_time(attrs)
        if tag == "div":
            self._div_depth += 1
            if self._body_depth is None and not self._have_body and not self._in_signature and "bbWrapper" in cls.split():
                self._body_depth = self._div_depth
                self._buf = []
                return
            if self._body_depth is not None and self._skip_depth is None and ("bbCodeBlock-expandLink" in cls or "bbCodeBlock-title" in cls):
                self._skip_depth = self._div_depth
        if self._body_depth is None:
            return
        if tag == "blockquote":
            self._quote_depth += 1
            self._newline()
        elif tag == "img":
            alt = attrs.get("alt", "")
            self._emit("[image%s]" % (": " + alt if alt and len(alt) < 40 else ""))
        elif tag in _BLOCK_TAGS:
            self._newline()

    def handle_startendtag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag in ("div", "article", "aside", "blockquote"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._post is None:
            return
        if tag == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                self._finish_post()
            return
        if tag == "aside" and self._in_signature:
            self._in_signature -= 1
        if tag == "div":
            if self._skip_depth is not None and self._div_depth == self._skip_depth:
                self._skip_depth = None
            elif self._body_depth is not None and self._div_depth == self._body_depth:
                self._post.text = _clean("".join(self._buf))
                self._body_depth = None
                self._have_body = True
            elif self._body_depth is not None:
                self._newline()
            self._div_depth -= 1
            return
        if self._body_depth is None:
            return
        if tag == "blockquote":
            self._quote_depth = max(0, self._quote_depth - 1)
            self._newline()
        elif tag in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._post is not None and self._body_depth is not None:
            self._emit(data)

    def _finish_post(self) -> None:
        post = self._post
        if post is not None:
            if self._body_depth is not None:
                post.text = _clean("".join(self._buf))
            self.posts.append(post)
        self._post = None
        self._body_depth = None
        self._skip_depth = None
        self._quote_depth = 0
        self._in_signature = 0
        self._div_depth = 0
        self._buf = []


def _clean(text: str) -> str:
    out: List[str] = []
    for raw in text.replace("\r", "").split("\n"):
        quoted = raw.lstrip().startswith(">")
        body = raw.lstrip()[1:] if quoted else raw
        body = " ".join(body.split())
        line = ("> " + body) if quoted and body else body
        if not line:
            # keep at most one empty line, and none right after a quote's last line twice
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out)


def parse_thread_page(html: str, url: str) -> ThreadPage:
    base = thread_base_url(url)
    p = _ThreadParser(base)
    p.feed(html)
    p.close()
    thread_id = re.search(r"\.(\d+)/$", base).group(1)
    pages = [int(m.group(1)) for href in p.page_links if ("." + thread_id + "/") in href for m in [re.search(r"/page-(\d+)", href)] if m]
    current = 1
    for candidate in (p.canonical, url):
        m = re.search(r"/page-(\d+)", candidate or "")
        if m:
            current = int(m.group(1))
            break
    last = max([current] + pages)
    title = re.sub(r"\s*\|.*$", "", p.title.strip())
    return ThreadPage(posts=p.posts, page=current, last_page=last, title=title)
