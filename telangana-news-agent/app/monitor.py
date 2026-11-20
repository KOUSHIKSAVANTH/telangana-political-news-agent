"""`python -m app.monitor`: one bounded collect -> cluster -> analyze -> alert run."""
from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.alerts import decide
from app.cluster import Candidate, best_match, match_tokens, story_key
from app.database import Database, as_utc, utcnow
from app.dedupe import content_hash
from app.email_sender import EmailSender, render
from app.gemini_processor import LLMUnavailable
from app.logging_setup import setup_logging
from app.models import FeedItem
from app.pipeline import (analyze_pending, cited_sources, deliver, evidence_summary, make_llm)
from app.relevance import Tagger
from app.rss_client import fetch_source, make_client
from app.settings import Settings, get_settings
from app.tavily_client import (TAVILY_SOURCE_PREFIX, TavilyBudgetExceeded, TavilyClient,
                               tavily_source_record)

log = logging.getLogger("app.monitor")
IST = ZoneInfo("Asia/Kolkata")


def collect_rss(settings: Settings, db: Database) -> list[FeedItem]:
    coll = settings.pipeline["collection"]
    max_age = timedelta(hours=coll["max_item_age_hours"])
    items: list[FeedItem] = []
    with make_client(coll["user_agent"], coll["request_timeout_seconds"]) as client:
        def one(src: dict[str, Any]):
            state = db.get_source(src["id"]) or {}
            return src, fetch_source(client, src, etag=state.get("etag"), last_modified=state.get("last_modified"),
                                     max_age=max_age, excerpt_chars=coll["excerpt_max_chars"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            for src, res in pool.map(one, settings.enabled_sources):
                db.record_poll(src["id"], ok=res.ok, etag=res.etag, last_modified=res.last_modified, error=res.error)
                status = "304 not modified" if res.not_modified else (f"{len(res.items)} items" if res.ok else f"ERROR {res.error}")
                log.info("feed %-26s %s", src["id"], status)
                items.extend(res.items)
    return items


def collect_tavily_scheduled(settings: Settings, db: Database) -> list[FeedItem]:
    cfg = settings.pipeline["tavily"]
    if not (cfg.get("enabled") and settings.tavily_api_key):
        return []
    hour = utcnow().astimezone(IST).hour
    if hour not in cfg.get("scheduled_hours_ist", []):
        return []
    marker = f"tavily_sched_h{hour}"
    if db.get_usage(marker) > 0:  # already ran this scheduled slot today
        return []
    db.add_usage(marker)
    client = TavilyClient(settings.tavily_api_key, cfg, db)
    out: list[FeedItem] = []
    for q in cfg.get("scheduled_queries", []):
        try:
            out.extend(client.search(q))
        except TavilyBudgetExceeded as exc:
            log.warning("Tavily budget: %s", exc)
            break
        except Exception as exc:
            log.warning("Tavily query failed (%s): %s", q, exc)
    log.info("tavily scheduled searches returned %d items", len(out))
    return out


def ingest(settings: Settings, db: Database, tagger: Tagger, items: list[FeedItem]) -> list[dict[str, Any]]:
    """Persist new relevant-or-not articles; return the new relevant ones."""
    group_of = {s["id"]: s.get("independence_group", s["id"]) for s in settings.sources}
    specific = {s["id"]: bool(s.get("telangana_specific")) for s in settings.sources}
    tavily_sources = {i.source_id for i in items if i.source_id.startswith(TAVILY_SOURCE_PREFIX)}
    if tavily_sources:
        recs = [tavily_source_record(sid, settings.sources) for sid in tavily_sources]
        db.sync_sources(recs)
        group_of.update({r["id"]: r["independence_group"] for r in recs})

    new: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for item in items:
        text = f"{item.title} {item.excerpt}"
        relevant = tagger.is_relevant(text, specific.get(item.source_id, False))
        if relevant:
            why = tagger.out_of_scope(text, tags=item.raw_metadata.get("tags"),
                                      url=item.raw_metadata.get("link") or item.url)
            if why:
                relevant = False  # non-political (sports, cinema, lifestyle...): stored, never analysed
                item.raw_metadata["out_of_scope"] = why
        art_id = db.insert_article(item, content_hash=content_hash(item.title, group_of.get(item.source_id, item.source_id)),
                                   relevant=relevant)
        if art_id and relevant:
            new.append({"id": art_id, "title": item.title, "language": item.language,
                        "published_at": item.published_at, "title_en": None})
        elif art_id and "out_of_scope" in item.raw_metadata:
            skipped[item.source_id] = skipped.get(item.source_id, 0) + 1
    if skipped:
        log.info("skipped %d new non-political items (sports/cinema/lifestyle): %s",
                 sum(skipped.values()), skipped)
    return new


def translate_titles(llm, db: Database, new: list[dict[str, Any]]) -> None:
    te = [(a["id"], a["title"]) for a in new if a["language"] == "te"]
    if not llm or not te:
        return
    for i in range(0, len(te), 40):
        try:
            got = llm.translate_titles(te[i:i + 40])
        except LLMUnavailable as exc:
            log.warning("title translation skipped: %s", exc)
            return
        for a in new:
            if a["id"] in got:
                a["title_en"] = got[a["id"]]
                db.set_article_title_en(a["id"], got[a["id"]])


def cluster_new(settings: Settings, db: Database, new: list[dict[str, Any]]) -> int:
    cfg = settings.pipeline["clustering"]
    window = timedelta(hours=cfg["window_hours"])
    cands = [Candidate(c["id"], match_tokens(c["normalized_title"]), as_utc(c["first_published_at"]))
             for c in db.candidate_stories(utcnow() - window)]
    created = 0
    for a in sorted(new, key=lambda x: (x["published_at"] is None, x["published_at"] or utcnow())):
        title = a["title_en"] or a["title"]
        sid = best_match(title, a["published_at"], cands, threshold=cfg["similarity_threshold"], window=window)
        if sid is None:
            sid = db.create_story(title=title, normalized_title=story_key(title), first_published_at=a["published_at"])
            cands.append(Candidate(sid, match_tokens(title), a["published_at"]))
            created += 1
            db.attach_article(sid, a["id"], role="primary")
        else:
            db.attach_article(sid, a["id"])
    return created


def corroborate(settings: Settings, db: Database, tagger: Tagger, story_ids: list[int]) -> list[int]:
    """Targeted Tavily searches for important stories lacking independent sources."""
    cfg = settings.pipeline["tavily"]
    if not (cfg.get("enabled") and settings.tavily_api_key) or not story_ids:
        return []
    client = TavilyClient(settings.tavily_api_key, cfg, db)
    touched, done = [], 0
    for sid in story_ids:
        if done >= cfg.get("max_corroborations_per_run", 2):
            break
        story = db.get_story(sid)
        if not story or story["priority"] not in cfg.get("corroborate_priorities", []):
            continue
        ev = db.story_evidence(sid)
        if evidence_summary(settings, ev, story["priority"]).independent_sources >= 2:
            continue
        try:
            items = client.search(story["title"])
        except TavilyBudgetExceeded as exc:
            log.warning("Tavily budget: %s", exc)
            break
        except Exception as exc:
            log.warning("Tavily corroboration failed for story %s: %s", sid, exc)
            continue
        done += 1
        new = ingest(settings, db, tagger, items)
        if new:
            cluster_new(settings, db, new)
            touched.append(sid)
    return touched


def next_step(story: dict[str, Any], route: str) -> str:
    if route == "reviewer":
        return "Reviewer: check the sources below, then forward to the team or discard."
    if story["priority"] == "critical":
        return "Escalate to comms leads now; prepare a holding line pending official confirmation."
    return "Monitor for official statements; assess whether a response or briefing note is needed."


def send_alerts(settings: Settings, db: Database, sender: EmailSender, story_ids: list[int]) -> int:
    rules = settings.alert_rules
    now = utcnow()
    day_start = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    sent = 0
    for sid in dict.fromkeys(story_ids):
        story = db.get_story(sid)
        if not story or story["priority"] not in rules.get("alert_priorities", []):
            continue
        for k in ("first_published_at", "last_material_update_at", "updated_at"):
            story[k] = as_utc(story[k])
        evidence = db.story_evidence(sid)
        summ = evidence_summary(settings, evidence, story["priority"])
        decision = decide(
            story, summ, rules=rules, now=now,
            last_alert=db.last_alert_for_story(sid, dry_run=settings.dry_run),
            alerts_today=db.count_alerts_since(day_start), alerts_this_run=sent,
            reviewer_configured=bool(settings.reviewer_recipients) or settings.dry_run,
        )
        log.info("story %s [%s/%s]: %s", sid, story["priority"], story["verification_status"], decision.reason)
        if decision.route == "none":
            continue
        review = decision.route == "reviewer"
        prefix = "[REVIEW]" if review else ""
        subject = f"{prefix}[TELANGANA ALERT][{story['priority'].upper()}] {story['title']}"[:250]
        ctx = dict(story=story, evidence=summ, sources=cited_sources(story, evidence), review=review,
                   decision_reason=decision.reason, next_step=next_step(story, decision.route),
                   published=story["first_published_at"], detected=min(e["discovered_at"] for e in evidence),
                   checked=now, alert_id=decision.idempotency_key)
        recipients = settings.reviewer_recipients if review else settings.team_recipients
        try:
            res = deliver(settings, db, sender, key=decision.idempotency_key, type_="review" if review else "breaking",
                          recipient_group="reviewer" if review else "team", recipients=recipients, subject=subject,
                          html=render("breaking.html", **ctx), text=render("breaking.txt", **ctx), story_id=sid)
        except Exception:
            continue  # logged + recorded as failed; other alerts still proceed
        if res:
            sent += 1
    return sent


def run(settings: Settings) -> int:
    db = Database(settings.database_url)
    if db.is_sqlite:
        db.create_all()
    db.sync_sources(settings.sources)
    run_id = db.start_run("monitor")
    seen = added = 0
    try:
        tagger = Tagger(settings.topics)
        llm = make_llm(settings, db)
        items = collect_rss(settings, db) + collect_tavily_scheduled(settings, db)
        seen = len(items)
        new = ingest(settings, db, tagger, items)
        added = len(new)
        translate_titles(llm, db, new)
        created = cluster_new(settings, db, new)
        log.info("ingest: %d seen, %d new relevant articles, %d new stories", seen, added, created)

        stats = analyze_pending(settings, db, llm, tagger)
        log.info("analysis: %d via AI, %d heuristic", stats.ai, stats.heuristic)
        touched = corroborate(settings, db, tagger, stats.analyzed_ids)
        if touched:
            analyze_pending(settings, db, llm, tagger)

        sender = EmailSender(settings)
        n_alerts = send_alerts(settings, db, sender, stats.analyzed_ids + touched)
        log.info("alerts delivered: %d (dry_run=%s)", n_alerts, settings.dry_run)

        failing = db.failing_sources()
        for f in failing:
            log.error("source %s failing %d consecutive polls: %s", f["id"], f["consecutive_failures"], f["last_error"])
        ret = settings.pipeline["retention"]
        db.purge(articles_days=ret["articles_days"], runs_days=ret["runs_days"])
        errors = stats.errors[:10]
        db.finish_run(run_id, status="ok" if not errors else "partial", items_seen=seen, items_added=added,
                      error_summary="; ".join(errors) or None)
        return 0
    except Exception as exc:
        log.exception("monitor run failed")
        db.finish_run(run_id, status="failed", items_seen=seen, items_added=added,
                      error_summary=f"{type(exc).__name__}: {exc}")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one monitor cycle")
    parser.parse_args()
    setup_logging()
    sys.exit(run(get_settings()))


if __name__ == "__main__":
    main()
