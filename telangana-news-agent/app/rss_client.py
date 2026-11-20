"""RSS/Atom collection with conditional GET and per-feed error isolation."""
from __future__ import annotations

import calendar
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import httpx

from app.dedupe import canonicalize_url
from app.models import FeedItem

log = logging.getLogger(__name__)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class FetchResult:
    source_id: str
    ok: bool
    not_modified: bool = False
    items: list[FeedItem] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


def clean_excerpt(raw: str, max_chars: int) -> str:
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars].rsplit(" ", 1)[0] + "…"
    return text


def _entry_time(entry: Any) -> datetime | None:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    # feedparser normalizes to UTC struct_time
    return datetime.fromtimestamp(calendar.timegm(st), tz=UTC)


def feed_tags(entry: Any) -> list[str]:
    """Publisher category tags, split (some feeds pack 'Sports,Cricket' into one tag)."""
    out: list[str] = []
    for t in entry.get("tags") or []:
        for part in str(t.get("term") or "").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out[:20]


def parse_feed(content: bytes, source: dict[str, Any], *, max_age: timedelta, excerpt_chars: int,
               now: datetime | None = None) -> list[FeedItem]:
    """Parse a feed into FeedItems (age-filtered, URL-canonicalized)."""
    now = now or datetime.now(UTC)
    parsed = feedparser.parse(content)
    items: list[FeedItem] = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = clean_excerpt(e.get("title") or "", 500)
        if not link or not title or not link.startswith(("http://", "https://")):
            continue
        published = _entry_time(e)
        if published and published > now + timedelta(hours=1):
            published = None  # bogus future timestamp: don't trust it
        if published and now - published > max_age:
            continue
        excerpt = clean_excerpt(e.get("summary") or e.get("description") or "", excerpt_chars)
        if excerpt == title:
            excerpt = ""
        items.append(FeedItem(
            source_id=source["id"],
            title=title,
            url=canonicalize_url(link),
            excerpt=excerpt,
            language=source.get("language", "en"),
            published_at=published,
            raw_metadata={"link": link, "guid": e.get("id"), "feed": source["feed_url"],
                          "via": "rss", "tags": feed_tags(e)},
        ))
    return items


def fetch_source(client: httpx.Client, source: dict[str, Any], *, etag: str | None,
                 last_modified: str | None, max_age: timedelta, excerpt_chars: int) -> FetchResult:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        resp = client.get(source["feed_url"], headers=headers)
        if resp.status_code == 304:
            return FetchResult(source["id"], ok=True, not_modified=True, etag=etag, last_modified=last_modified)
        resp.raise_for_status()
        items = parse_feed(resp.content, source, max_age=max_age, excerpt_chars=excerpt_chars)
        if not items and b"<rss" not in resp.content[:2000] and b"<feed" not in resp.content[:2000]:
            return FetchResult(source["id"], ok=False, error="response is not an RSS/Atom feed")
        return FetchResult(source["id"], ok=True, items=items,
                           etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified"))
    except Exception as exc:  # one bad feed must never break the batch
        log.warning("feed %s failed: %s", source["id"], exc)
        return FetchResult(source["id"], ok=False, error=f"{type(exc).__name__}: {exc}")


def make_client(user_agent: str, timeout: float) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
