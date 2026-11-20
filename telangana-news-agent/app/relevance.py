"""Keyword-based relevance, geography and fallback classification (config/topics.yaml)."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from app.dedupe import ZERO_WIDTH


def _prep(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").translate(ZERO_WIDTH).lower()


@lru_cache(maxsize=512)
def _en_pattern(word: str) -> re.Pattern:
    # Prefix match allowed for stems ending in a letter like "evacuat"; otherwise whole word.
    return re.compile(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])")


def _match_any(text: str, words: list[str]) -> list[str]:
    hits = []
    for w in words or []:
        w_norm = _prep(w)
        if w_norm.isascii():
            pat = _en_pattern(w_norm) if not w_norm.endswith("at") else re.compile(re.escape(w_norm))
            if pat.search(text):
                hits.append(w)
        elif w_norm in text:
            hits.append(w)
    return hits


def _kw(block: dict[str, list[str]] | list[str] | None) -> list[str]:
    if not block:
        return []
    if isinstance(block, list):
        return block
    return [*block.get("en", []), *block.get("te", [])]


class Tagger:
    def __init__(self, topics: dict[str, Any]):
        self.topics = topics

    def is_relevant(self, text: str, telangana_specific: bool) -> bool:
        if telangana_specific:
            return True
        t = _prep(text)
        return bool(_match_any(t, _kw(self.topics.get("relevance_keywords")))
                    or self.districts(text))

    def political(self, text: str) -> bool:
        return bool(_match_any(_prep(text), _kw((self.topics.get("scope") or {}).get("political_keywords"))))

    def out_of_scope(self, text: str, *, tags: list[str] | None = None, url: str = "") -> str | None:
        """Reason string if the item is non-political (sports, cinema, ...), else None.

        Excluded only when a section/tag/keyword marks it out of scope AND no political keyword
        appears, so political stories that touch cinema or sport are kept.
        """
        scope = self.topics.get("scope") or {}
        sections = {_prep(s) for s in scope.get("exclude_sections") or []}
        path = urlsplit(url).path.lower().split("/") if url else []
        reason = next((f"section '{s}'" for s in [*(_prep(t) for t in tags or []), *path] if s in sections), None)
        if reason is None:
            hits = _match_any(_prep(text), _kw(scope.get("exclude_keywords")))
            reason = f"keyword '{hits[0]}'" if hits else None
        if reason and self.political(text):
            return None
        return reason

    def districts(self, text: str) -> list[str]:
        t = _prep(text)
        return [name for name, words in (self.topics.get("districts") or {}).items() if _match_any(t, words)]

    def category(self, text: str) -> str:
        t = _prep(text)
        scores = {k: len(_match_any(t, _kw(v))) for k, v in (self.topics.get("categories") or {}).items()}
        best = max(scores.items(), key=lambda kv: kv[1], default=("other", 0))
        return best[0] if best[1] > 0 else "other"

    def priority(self, text: str) -> str:
        t = _prep(text)
        pk = self.topics.get("priority_keywords") or {}
        if _match_any(t, _kw(pk.get("critical"))):
            return "high"  # keyword guesses never self-promote to critical without Gemini/human
        if _match_any(t, _kw(pk.get("high"))):
            return "normal"  # heuristic mode is conservative: digest only
        return "normal"

    def sensitive(self, text: str) -> bool:
        return bool(_match_any(_prep(text), _kw(self.topics.get("politically_sensitive_keywords"))))
