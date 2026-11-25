"""Shared building blocks for the monitor and digest jobs."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.database import Database, as_utc, utcnow
from app.email_sender import EmailSender, SendResult
from app.gemini_processor import GeminiProcessor, LLMProcessor, LLMUnavailable
from app.groq_processor import GroqProcessor
from app.models import PRIORITY_RANK, Priority
from app.relevance import Tagger
from app.settings import Settings
from app.verification import EvidenceSummary, assess, cap_status

log = logging.getLogger(__name__)


@dataclass
class LLMBudget:
    db: Database
    provider: str
    per_run: int
    per_day: int
    used_this_run: int = 0

    def ok(self) -> bool:
        return self.used_this_run < self.per_run and self.db.get_usage(self.provider) < self.per_day

    def record(self) -> None:
        self.used_this_run += 1
        self.db.add_usage(self.provider)


def make_llm(settings: Settings, db: Database) -> LLMProcessor | None:
    provider = settings.llm_provider
    if provider is None:
        log.info("AI off (LLM_PROVIDER=none or no GROQ_API_KEY/GEMINI_API_KEY): "
                 "using keyword-only analysis with publisher excerpts")
        return None
    cfg = settings.pipeline[provider]
    budget = LLMBudget(db, provider, cfg["max_calls_per_run"], cfg["max_calls_per_day"])
    common = dict(temperature=cfg.get("temperature", 0.1), budget_ok=budget.ok, on_call=budget.record)
    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("LLM_PROVIDER=groq but GROQ_API_KEY is not set")
        llm: LLMProcessor = GroqProcessor(settings.groq_api_key, settings.groq_model, **common)
        model = settings.groq_model
    else:
        if not settings.gemini_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
        llm = GeminiProcessor(settings.gemini_api_key, settings.gemini_model, **common)
        model = settings.gemini_model
    llm.glossary = dict(settings.topics.get("translation_glossary") or {})
    log.info("AI analysis via %s (%s)", provider, model)
    return llm


def is_ai(source: str | None) -> bool:
    return source not in (None, "heuristic")


def evidence_summary(settings: Settings, evidence: list[dict[str, Any]], priority: str = "high") -> EvidenceSummary:
    ev = settings.alert_rules.get("evidence", {})
    return assess(
        evidence,
        copy_threshold=ev.get("copy_similarity_threshold", 0.85),
        min_independent=(ev.get("min_independent_sources") or {}).get(priority, 2),
        official_satisfies=ev.get("official_source_satisfies", True),
    )


def heuristic_analysis(evidence: list[dict[str, Any]], tagger: Tagger, summ: EvidenceSummary) -> dict[str, Any]:
    """No-AI fallback: publisher's own headline/excerpt, keyword tags. Never invents text.

    Conservative by design: status comes purely from deterministic evidence, and priority is
    raised only when an alarming keyword appears AND >=2 independent outlets carry the story.
    """
    first_en = next((e for e in evidence if e["language"] == "en"), None)
    lead = first_en or evidence[0]
    title = lead.get("title_en") or lead["title"]
    summary = lead.get("excerpt") or lead["title"]
    blob = " ".join(f"{e['title']} {e.get('title_en') or ''} {e.get('excerpt') or ''}" for e in evidence)
    political = tagger.political(blob)
    if not political:
        priority = "low"  # political scope: no political keyword -> kept out of alerts and the digest
    elif tagger.priority(blob) == "high" and summ.independent_sources >= 2:
        priority = "high"
    else:
        priority = "normal"
    return {
        "title": title,
        "summary": summary,
        "why_it_matters": None,
        "category": tagger.category(blob),
        "districts": tagger.districts(blob),
        "topics": [],
        "priority": priority,
        "verification_status": summ.max_status.value,
        "politically_sensitive": tagger.sensitive(blob),
        "reason": "Keyword heuristic (AI analysis unavailable)." if political
                  else "No political/government keyword found; outside political scope.",
        "evidence_article_ids": [e["id"] for e in evidence],
        "analysis_source": "heuristic",
        "material_update": False,
    }


@dataclass
class AnalysisStats:
    ai: int = 0
    heuristic: int = 0
    errors: list[str] = field(default_factory=list)
    analyzed_ids: list[int] = field(default_factory=list)


def analyze_pending(settings: Settings, db: Database, llm: LLMProcessor | None, tagger: Tagger,
                    *, limit: int = 500) -> AnalysisStats:
    stats = AnalysisStats()
    # With AI on, recent keyword-mode stories are upgraded too (most important first).
    since = utcnow() - timedelta(hours=settings.pipeline["clustering"]["window_hours"]) if llm else None
    budget_out = False
    for story in db.stories_needing_analysis(limit, ai_upgrade_since=since):
        already = story.get("analysis_source")
        if budget_out and already:
            continue  # AI budget used up: keep the existing analysis, upgrade on a later run
        evidence = db.story_evidence(story["id"])
        if not evidence:
            db.save_analysis(story["id"])
            continue
        result: dict[str, Any] | None = None
        if llm is not None and not budget_out:
            try:
                a = llm.analyze_story(evidence, story.get("summary") if is_ai(story.get("analysis_source")) else None)
                result = {
                    "title": a.story_title, "summary": a.summary, "why_it_matters": a.why_it_matters_for_comms,
                    "category": a.category, "districts": a.districts, "topics": a.topics,
                    # Not about Telangana, or not political -> low: excluded from alerts and the digest.
                    "priority": a.priority if a.telangana_relevant and a.political_relevant else "low",
                    "verification_status": a.verification_status, "politically_sensitive": a.politically_sensitive,
                    "reason": a.reason, "evidence_article_ids": a.evidence_url_ids, "analysis_source": llm.name,
                    "material_update": a.material_update,
                }
                stats.ai += 1
            except LLMUnavailable as exc:
                if "budget" in str(exc):
                    budget_out = True
                    log.info("AI call budget reached; remaining stories wait for the next run")
                else:
                    stats.errors.append(f"story {story['id']}: {exc}")
                    log.warning("AI analysis failed for story %s: %s", story["id"], exc)
        if result is None:
            if is_ai(already) or (llm is not None and already):
                # Keep the earlier analysis (AI, or keyword-mode awaiting an AI upgrade); retry next run.
                continue
            result = heuristic_analysis(evidence, tagger, evidence_summary(settings, evidence))
            stats.heuristic += 1

        # Deterministic guards on top of the model.
        summ = evidence_summary(settings, evidence, result["priority"])
        result["verification_status"] = cap_status(result["verification_status"], summ.max_status).value
        result["politically_sensitive"] = bool(result["politically_sensitive"]) or tagger.sensitive(
            " ".join(e["title"] for e in evidence))
        material = bool(result.pop("material_update")) and bool(story.get("summary"))
        times = [e["published_at"] for e in evidence if e["published_at"]]
        values = {k: v for k, v in result.items()}
        values["first_published_at"] = min(times) if times else as_utc(story.get("first_published_at"))
        # Keep the story pending AI analysis if an LLM is configured but was skipped (budget).
        values["needs_analysis"] = llm is not None and not is_ai(result["analysis_source"])
        if material:
            values["material_update_count"] = (story.get("material_update_count") or 0) + 1
            values["last_material_update_at"] = utcnow()
        db.save_analysis(story["id"], **values)
        stats.analyzed_ids.append(story["id"])
    return stats


def translate_backlog(settings: Settings, db: Database, llm: LLMProcessor | None, *, limit: int = 120) -> int:
    """Translate recent Telugu headlines that have no English title yet (20 per AI call)."""
    if llm is None:
        return 0
    since = utcnow() - timedelta(hours=settings.pipeline["clustering"]["window_hours"])
    items = db.untranslated_telugu(since, limit)
    done = 0
    for i in range(0, len(items), 20):
        try:
            got = llm.translate_titles(items[i:i + 20])
        except LLMUnavailable as exc:
            log.warning("Telugu title translation stopped: %s", exc)
            break
        for article_id, title_en in got.items():
            db.set_article_title_en(article_id, title_en)
        done += len(got)
    if items:
        log.info("translated %d of %d pending Telugu headlines", done, len(items))
    return done


def cited_sources(story: dict[str, Any], evidence: list[dict[str, Any]], n: int = 3) -> list[dict[str, Any]]:
    """Up to n cited articles, one per independence group, earliest first. Only stored records."""
    cited_ids = set(story.get("evidence_article_ids") or [])
    pool = [e for e in evidence if e["id"] in cited_ids] or evidence
    seen, out = set(), []
    for e in pool:
        if e["independence_group"] in seen:
            continue
        seen.add(e["independence_group"])
        out.append(e)
        if len(out) == n:
            break
    return out


def rank_key(story: dict[str, Any], summary: EvidenceSummary) -> tuple:
    return (PRIORITY_RANK[Priority(story["priority"])], -summary.independent_sources,
            -(as_utc(story["updated_at"]).timestamp()))


def deliver(settings: Settings, db: Database, sender: EmailSender, *, key: str, type_: str,
            recipient_group: str, recipients: list[str], subject: str, html: str, text: str,
            story_id: int | None) -> SendResult | None:
    """Idempotent delivery: claim key -> send -> record. Never sends twice for one key."""
    if settings.dry_run:
        key = f"dryrun:{key}"
    if not db.reserve_notification(key=key, type_=type_, recipient_group=recipient_group,
                                   subject=subject, story_id=story_id):
        existing = db.get_notification(key)
        log.info("notification %s already %s; not resending", key, existing and existing["status"])
        return None
    try:
        result = sender.send(recipients=recipients, subject=subject, html=html, text=text, idempotency_key=key)
    except Exception as exc:
        db.finish_notification(key, status="failed", error=f"{type(exc).__name__}: {exc}")
        log.error("send failed for %s: %s", key, exc)
        raise
    db.finish_notification(key, status=result.status, provider_message_id=result.provider_message_id)
    return result


TELEGRAM_KEY_PREFIX = "tg:"


def make_senders(settings: Settings) -> dict[str, Any]:
    """One sender per enabled channel (DELIVERY_CHANNELS)."""
    senders: dict[str, Any] = {}
    if "email" in settings.delivery_channels:
        senders["email"] = EmailSender(settings)
    if "telegram" in settings.delivery_channels:
        from app.telegram_sender import TelegramSender

        senders["telegram"] = TelegramSender(settings)
    return senders


def deliver_all(settings: Settings, db: Database, senders: dict[str, Any], *, key: str, type_: str,
                recipient_group: str, subject: str, html: str, text: str, telegram_html: str,
                story_id: int | None) -> list[SendResult]:
    """Deliver to every enabled channel. Each channel has its own idempotency key
    (Telegram: 'tg:' + key), so a failure on one channel never blocks or re-sends the other.
    Raises after trying all channels if any of them failed."""
    results, failures = [], []
    for channel, sender in senders.items():
        if channel == "telegram":
            chats = (settings.telegram_reviewer_chat_ids if recipient_group == "reviewer"
                     else settings.telegram_team_chat_ids)
            args = dict(key=TELEGRAM_KEY_PREFIX + key, recipients=chats, html=telegram_html)
        else:
            recips = settings.reviewer_recipients if recipient_group == "reviewer" else settings.team_recipients
            args = dict(key=key, recipients=recips, html=html)
        try:
            res = deliver(settings, db, sender, type_=type_, recipient_group=recipient_group, subject=subject,
                          text=text, story_id=story_id, **args)
        except Exception as exc:  # already logged and recorded as failed by deliver()
            failures.append(f"{channel}: {type(exc).__name__}: {exc}")
            continue
        if res is not None:
            results.append(res)
    if failures:
        raise RuntimeError("delivery failed on " + "; ".join(failures))
    return results


def window_since(hours: float):
    return utcnow() - timedelta(hours=hours)
