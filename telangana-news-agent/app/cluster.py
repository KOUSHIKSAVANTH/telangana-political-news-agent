"""Group related articles from different outlets into one story."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.dedupe import normalize_text, similarity, tokens  # noqa: F401  (tokens re-exported)


# Words so common in Telangana coverage that sharing them says nothing about being the same event.
GENERIC = {
    "telangana", "hyderabad", "tg", "ts", "govt", "government", "state", "district", "city", "cm",
    "chief", "minister", "police", "arrest", "arrested", "held", "caught", "today", "news", "india",
    "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "lakh", "crore", "rs",
    "case", "demand", "urge", "slam", "red", "handed", "official", "people", "man", "woman",
    "తెలంగాణ", "హైదరాబాద్", "ప్రభుత్వం", "సీఎం", "పోలీసు", "కేసు",
}


def match_tokens(title: str) -> set[str]:
    return tokens(title) - GENERIC


@dataclass
class Candidate:
    story_id: int
    tokens: set[str]
    first_published_at: datetime | None


def story_key(title: str) -> str:
    """Normalized title stored on the story and compared against later articles."""
    return normalize_text(title)


def best_match(title: str, published_at: datetime | None, candidates: list[Candidate], *,
               threshold: float, window: timedelta, min_shared_tokens: int = 2) -> int | None:
    """Return the story id this article belongs to, or None to start a new story."""
    t = match_tokens(title)
    best_id, best_score = None, 0.0
    for c in candidates:
        if published_at and c.first_published_at and abs(published_at - c.first_published_at) > window:
            continue
        if len(t & c.tokens) < min_shared_tokens:
            continue
        score = similarity(t, c.tokens)
        shared = len(t & c.tokens)
        # Containment: a short headline almost fully contained in a longer one is the same story.
        containment = shared / min(len(t), len(c.tokens))
        if containment >= 0.8 and shared >= 4:
            score = max(score, threshold)
        if score >= threshold and score > best_score:
            best_id, best_score = c.story_id, score
    return best_id
