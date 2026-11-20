"""Deterministic evidence checks. AI confidence is never treated as verification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.dedupe import similarity
from app.models import VERIFICATION_RANK, Verification


@dataclass
class EvidenceSummary:
    independent_sources: int
    publishers: list[str]
    has_official: bool
    copies_collapsed: int  # outlets discounted as near-identical copies
    max_status: Verification  # strongest status the evidence can support


def assess(evidence: list[dict[str, Any]], *, copy_threshold: float = 0.85,
           min_independent: int = 2, official_satisfies: bool = True) -> EvidenceSummary:
    """Count independent outlets: distinct independence groups, collapsing copied text.

    `evidence` rows need: independence_group, publisher, source_type, title, excerpt.
    """
    groups: dict[str, dict[str, Any]] = {}
    for row in evidence:
        groups.setdefault(row["independence_group"], row)  # first (earliest) article per group

    reps: list[dict[str, Any]] = []
    copies = 0
    for row in groups.values():
        text = f"{row.get('title_en') or row['title']} {row.get('excerpt') or ''}"
        if any(similarity(text, f"{r.get('title_en') or r['title']} {r.get('excerpt') or ''}") >= copy_threshold
               for r in reps):
            copies += 1  # same wire copy republished: not independent corroboration
            continue
        reps.append(row)

    has_official = any(r.get("source_type") == "official" for r in evidence)
    n = len(reps)
    if n >= min_independent or (official_satisfies and has_official):
        max_status = Verification.confirmed
    elif n >= 1:
        max_status = Verification.developing
    else:
        max_status = Verification.unverified
    return EvidenceSummary(
        independent_sources=n,
        publishers=sorted({r["publisher"] for r in evidence}),
        has_official=has_official,
        copies_collapsed=copies,
        max_status=max_status,
    )


def cap_status(proposed: str, evidence_cap: Verification) -> Verification:
    """Final status = the weaker of the model's claim and what the evidence supports."""
    p = Verification(proposed)
    return p if VERIFICATION_RANK[p] <= VERIFICATION_RANK[evidence_cap] else evidence_cap
