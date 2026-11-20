"""`python -m app.digest`: compile and send the daily 9:00 IST briefing (idempotent per IST date)."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.database import Database, as_utc, utcnow
from app.email_sender import EmailSender, render
from app.logging_setup import setup_logging
from app.pipeline import analyze_pending, cited_sources, deliver, evidence_summary, is_ai, make_llm, rank_key
from app.relevance import Tagger
from app.settings import Settings, get_settings

log = logging.getLogger("app.digest")
IST = ZoneInfo("Asia/Kolkata")


def section_for(story: dict[str, Any]) -> str:
    status = story["verification_status"]
    if status == "unverified" or (status == "developing" and story.get("politically_sensitive")):
        return "emerging"
    cat = story.get("category") or "other"
    districts = [d for d in (story.get("districts") or []) if d not in ("Hyderabad", "Statewide")]
    if cat == "government_politics":
        return cat
    if districts and cat != "business_economy":
        return "district_updates"
    if cat in ("public_issues", "business_economy"):
        return cat
    return "district_updates" if districts else "public_issues"


def build_digest(settings: Settings, db: Database) -> dict[str, Any]:
    cfg = settings.pipeline["digest"]
    end = utcnow()
    start = end - timedelta(hours=cfg["window_hours"])
    stories = [s for s in db.stories_in_window(start, end) if s.get("summary") and s["priority"] != "low"]

    scored = []
    for s in stories:
        s["updated_at"] = as_utc(s["updated_at"])
        ev = db.story_evidence(s["id"])
        summ = evidence_summary(settings, ev, s["priority"])
        scored.append((rank_key(s, summ), s, ev))
    scored.sort(key=lambda x: x[0])
    top = scored[: cfg["max_items"]]

    alerted = db.alerted_story_ids(end - timedelta(days=7))
    follow_ups, by_section = [], {}
    for _, s, ev in top:
        item = {"story": s, "sources": cited_sources(s, ev)}
        if s["id"] in alerted and (s.get("material_update_count") or 0) > 0:
            follow_ups.append(item)
        else:
            by_section.setdefault(section_for(s), []).append(item)

    sections = [{"title": sec["title"], "items": by_section[sec["key"]]}
                for sec in cfg["sections"] if by_section.get(sec["key"])]
    included = [i for sec in sections for i in sec["items"]] + follow_ups
    publishers = {a["publisher"] for i in included for a in i["sources"]}
    credible = [i for i in included if i["story"]["verification_status"] != "unverified"]
    date_label = end.astimezone(IST).strftime("%Y-%m-%d")
    return dict(
        date_label=date_label, window_start=start, window_end=end, sections=sections, follow_ups=follow_ups,
        total=len(included), sources_count=len(publishers),
        light_news=len(credible) < cfg["light_news_threshold"],
        ai_used=all(is_ai(i["story"].get("analysis_source")) for i in included) if included else True,
        source_issues=[f"{f['name']} ({f['last_error']})" for f in db.failing_sources()],
        brief_id=f"digest:{date_label}",
    )


def run(settings: Settings, *, force: bool = False) -> int:
    db = Database(settings.database_url)
    if db.is_sqlite:
        db.create_all()
    run_id = db.start_run("digest")
    try:
        # Make sure everything in the window has a summary (bounded by the AI budget).
        analyze_pending(settings, db, make_llm(settings, db), Tagger(settings.topics))
        ctx = build_digest(settings, db)
        key = ctx["brief_id"] + (f":forced:{utcnow():%H%M%S}" if force else "")
        subject = f"Telangana News Brief | {ctx['date_label']}"
        res = deliver(settings, db, EmailSender(settings), key=key, type_="digest", recipient_group="team",
                      recipients=settings.team_recipients, subject=subject,
                      html=render("digest.html", **ctx), text=render("digest.txt", **ctx), story_id=None)
        if res is None:
            log.info("digest for %s already handled; nothing sent", ctx["date_label"])
        else:
            log.info("digest %s: %s %s", ctx["date_label"], res.status, res.preview_path or res.provider_message_id)
        db.finish_run(run_id, status="ok", items_seen=ctx["total"], items_added=1 if res else 0)
        return 0
    except Exception as exc:
        log.exception("digest run failed")
        db.finish_run(run_id, status="failed", error_summary=f"{type(exc).__name__}: {exc}")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile and send the daily digest")
    parser.add_argument("--force", action="store_true",
                        help="build a new digest even if today's was already sent (testing only)")
    args = parser.parse_args()
    setup_logging()
    sys.exit(run(get_settings(), force=args.force))


if __name__ == "__main__":
    main()
