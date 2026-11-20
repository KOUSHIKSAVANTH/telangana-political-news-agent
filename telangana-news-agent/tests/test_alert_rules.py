from datetime import UTC, datetime, timedelta

import pytest

from app.alerts import alert_key, decide
from app.models import Verification
from app.verification import EvidenceSummary

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
RULES = {
    "mode": "auto",
    "alert_priorities": ["critical", "high"],
    "evidence": {"min_independent_sources": {"critical": 2, "high": 2}, "official_source_satisfies": True},
    "max_story_age_hours": 6,
    "cooldown_hours": 5,
    "max_alerts_per_run": 3,
    "max_alerts_per_day": 12,
    "block_sensitive_auto_send": True,
}
STRONG = EvidenceSummary(independent_sources=3, publishers=["A", "B", "C"], has_official=False,
                         copies_collapsed=0, max_status=Verification.confirmed)
WEAK = EvidenceSummary(independent_sources=1, publishers=["A"], has_official=False,
                       copies_collapsed=0, max_status=Verification.developing)


def story(**kw):
    base = dict(id=7, priority="critical", verification_status="confirmed", politically_sensitive=False,
                first_published_at=NOW - timedelta(hours=1), last_material_update_at=None, material_update_count=0)
    base.update(kw)
    return base


def run(s, ev=STRONG, rules=RULES, last_alert=None, today=0, this_run=0, reviewer=True):
    return decide(s, ev, rules=rules, now=NOW, last_alert=last_alert, alerts_today=today,
                  alerts_this_run=this_run, reviewer_configured=reviewer)


def test_confirmed_critical_auto_mode_goes_to_team():
    d = run(story())
    assert d.route == "team" and d.idempotency_key == "alert:7:v0:critical"


@pytest.mark.parametrize("priority", ["normal", "low"])
def test_normal_and_low_are_digest_only(priority):
    assert run(story(priority=priority)).route == "none"


def test_unverified_never_alerts():
    assert run(story(verification_status="unverified")).route == "none"


def test_politically_sensitive_never_auto_sends():
    d = run(story(politically_sensitive=True))
    assert d.route == "reviewer" and "politically sensitive" in d.reason


def test_sensitive_without_reviewer_is_not_sent_at_all():
    assert run(story(politically_sensitive=True), reviewer=False).route == "none"


def test_developing_goes_to_reviewer_not_team():
    assert run(story(verification_status="developing")).route == "reviewer"


def test_insufficient_independent_sources_goes_to_reviewer():
    assert run(story(), ev=WEAK).route == "reviewer"


def test_review_mode_routes_everything_to_reviewer():
    assert run(story(), rules={**RULES, "mode": "review"}).route == "reviewer"


def test_old_story_is_not_breaking():
    assert run(story(first_published_at=NOW - timedelta(hours=7))).route == "none"


def test_missing_timestamp_is_not_breaking():
    assert run(story(first_published_at=None)).route == "none"


def test_same_version_never_realerts():
    s = story()
    last = {"idempotency_key": alert_key(s), "created_at": NOW - timedelta(hours=20)}
    assert run(s, last_alert=last).route == "none"


def test_dry_run_prefixed_key_recognised():
    s = story()
    last = {"idempotency_key": "dryrun:" + alert_key(s), "created_at": NOW - timedelta(hours=1)}
    assert run(s, last_alert=last).route == "none"


def test_priority_escalation_blocked_during_cooldown_but_allowed_after():
    s = story(priority="critical")
    last = {"idempotency_key": "alert:7:v0:high", "created_at": NOW - timedelta(hours=1)}
    assert run(s, last_alert=last).route == "none"
    last["created_at"] = NOW - timedelta(hours=6)
    assert run(s, last_alert=last).route == "team"


def test_material_update_bypasses_cooldown():
    s = story(material_update_count=1, last_material_update_at=NOW - timedelta(minutes=10))
    last = {"idempotency_key": "alert:7:v0:critical", "created_at": NOW - timedelta(hours=1)}
    d = run(s, last_alert=last)
    assert d.route == "team" and d.idempotency_key == "alert:7:v1:critical"


def test_run_and_daily_caps():
    assert run(story(), this_run=3).route == "none"
    assert run(story(), today=12).route == "none"
