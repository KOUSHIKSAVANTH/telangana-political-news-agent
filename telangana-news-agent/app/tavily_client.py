"""Optional Tavily search with hard daily/monthly credit caps enforced in code."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.dedupe import canonicalize_url
from app.models import FeedItem
from app.rss_client import clean_excerpt

log = logging.getLogger(__name__)
API_URL = "https://api.tavily.com/search"
TAVILY_SOURCE_PREFIX = "tavily:"  # articles from Tavily get a per-domain pseudo-source


class TavilyBudgetExceeded(RuntimeError):
    pass


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for parser in (lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")), parsedate_to_datetime):
        try:
            dt = parser(raw)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except (ValueError, TypeError):
            continue
    return None


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class TavilyClient:
    def __init__(self, api_key: str, cfg: dict[str, Any], db, http: httpx.Client | None = None):
        self.api_key = api_key
        self.cfg = cfg
        self.db = db
        self.http = http or httpx.Client(timeout=30)
        self.allowed = {d.lower() for d in cfg.get("allowed_domains", [])}

    def _check_budget(self, credits: int) -> None:
        today = datetime.now(UTC).date()
        used_today = self.db.get_usage("tavily", today)
        used_month = self.db.get_usage_since("tavily", today.replace(day=1))
        if used_today + credits > self.cfg["daily_credit_cap"]:
            raise TavilyBudgetExceeded(f"daily cap {self.cfg['daily_credit_cap']} reached ({used_today})")
        if used_month + credits > self.cfg["monthly_credit_cap"]:
            raise TavilyBudgetExceeded(f"monthly cap {self.cfg['monthly_credit_cap']} reached ({used_month})")

    def search(self, query: str) -> list[FeedItem]:
        credits = 2 if self.cfg.get("search_depth") == "advanced" else 1
        self._check_budget(credits)
        self.db.add_usage("tavily", credits)  # count before the call so failures can't loop for free
        resp = self.http.post(API_URL, headers={"Authorization": f"Bearer {self.api_key}"}, json={
            "query": query,
            "search_depth": self.cfg.get("search_depth", "basic"),
            "topic": "news",
            "days": 2,
            "max_results": self.cfg.get("max_results", 5),
            "include_domains": sorted(self.allowed),
        })
        resp.raise_for_status()
        items = []
        for r in resp.json().get("results", []):
            url = r.get("url") or ""
            dom = domain_of(url)
            if not url or not any(dom == a or dom.endswith("." + a) for a in self.allowed):
                continue
            items.append(FeedItem(
                source_id=TAVILY_SOURCE_PREFIX + dom,
                title=clean_excerpt(r.get("title") or "", 500),
                url=canonicalize_url(url),
                # Tavily snippets are search extracts, not verified reporting; keep short.
                excerpt=clean_excerpt(r.get("content") or "", 300),
                language="te" if any("ఀ" <= ch <= "౿" for ch in (r.get("title") or "")) else "en",
                published_at=_parse_date(r.get("published_date")),
                raw_metadata={"link": url, "via": "tavily", "query": query, "score": r.get("score")},
            ))
        return items


def tavily_source_record(source_id: str, configured: list[dict[str, Any]]) -> dict[str, Any]:
    """Map a Tavily result domain onto the configured publisher's independence group when known."""
    dom = source_id[len(TAVILY_SOURCE_PREFIX):]
    for s in configured:
        site = s.get("website") or ""
        if site and domain_of(site) == dom:
            return {"id": source_id, "name": f"{s['name']} (search)", "language": s.get("language", "en"),
                    "type": s.get("type", "publisher"), "independence_group": s.get("independence_group", s["id"]),
                    "website": site, "feed_url": None, "enabled": False, "telangana_specific": False}
    return {"id": source_id, "name": dom, "language": "en", "type": "publisher", "independence_group": dom,
            "website": f"https://{dom}", "feed_url": None, "enabled": False, "telangana_specific": False}
