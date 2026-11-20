"""Breaking-alert decision policy (spec section 7). Pure functions: no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from app.verification import EvidenceSummary

Route = Literal["none", "team", "reviewer"]


@dataclass(frozen=True)
class AlertDecision:
    route: Route
    reason: str
    idempotency_key: str | None = None


def alert_key(story: dict[str, Any]) -> str:
    """One alert per (story, material-update version, priority). Retries reuse the same key."""
    return f"alert:{story['id']}:v{story.get('material_update_count') or 0}:{story['priority']}"


def _key_version(key: str) -> int:
    try:
        return int(key.split(":")[2].lstrip("v"))
    except (IndexError, ValueError):
        return -1


def decide(story: dict[str, Any], evidence: EvidenceSummary, *, rules: dict[str, Any], now: datetime,
           last_alert: dict[str, Any] | None, alerts_today: int, alerts_this_run: int,
           reviewer_configured: bool) -> AlertDecision:
    priority = story["priority"]
    if priority not in rules.get("alert_priorities", ["critical", "high"]):
        return AlertDecision("none", f"priority {priority} is digest-only")

    status = story["verification_status"]
    if status == "unverified":
        return AlertDecision("none", "unverified: held for digest 'Emerging Stories'")

    material_n = story.get("material_update_count") or 0
    ref_time = story.get("last_material_update_at") if material_n else story.get("first_published_at")
    if ref_time is None:
        return AlertDecision("none", "no publisher timestamp: cannot establish this is breaking")
    if now - ref_time > timedelta(hours=rules.get("max_story_age_hours", 6)):
        return AlertDecision("none", f"story is older than {rules.get('max_story_age_hours', 6)}h")

    key = alert_key(story)
    if last_alert:
        last_key = last_alert["idempotency_key"].removeprefix("dryrun:")
        if last_key == key:
            return AlertDecision("none", "already alerted for this version", key)
        is_material = material_n > _key_version(last_key)
        in_cooldown = now - last_alert["created_at"] < timedelta(hours=rules.get("cooldown_hours", 5))
        if in_cooldown and not is_material:
            return AlertDecision("none", "cooldown active and no material update", key)

    if alerts_this_run >= rules.get("max_alerts_per_run", 3):
        return AlertDecision("none", "per-run alert cap reached", key)
    if alerts_today >= rules.get("max_alerts_per_day", 12):
        return AlertDecision("none", "daily alert cap reached", key)

    ev = rules.get("evidence", {})
    min_ind = (ev.get("min_independent_sources") or {}).get(priority, 2)
    enough_evidence = evidence.independent_sources >= min_ind or (
        ev.get("official_source_satisfies", True) and evidence.has_official)

    reasons = []
    if rules.get("mode", "review") != "auto":
        reasons.append("review mode")
    if status != "confirmed":
        reasons.append(f"status {status}")
    if not enough_evidence:
        reasons.append(f"{evidence.independent_sources}/{min_ind} independent sources")
    if story.get("politically_sensitive") and rules.get("block_sensitive_auto_send", True):
        reasons.append("politically sensitive")

    if not reasons:
        return AlertDecision("team", "passed all automatic checks", key)
    if not reviewer_configured:
        return AlertDecision("none", "needs human review but no reviewer configured: " + ", ".join(reasons), key)
    return AlertDecision("reviewer", "routed to reviewer: " + ", ".join(reasons), key)
