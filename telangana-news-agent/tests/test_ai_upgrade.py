"""Switching AI on after keyword-only runs: upgrade the most important stories first, respect the
call budget without re-processing the rest, and translate pending Telugu headlines."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.database import Database
from app.gemini_processor import LLMProcessor, LLMUnavailable
from app.models import FeedItem, StoryAnalysis
from app.pipeline import analyze_pending, translate_backlog
from app.relevance import Tagger
from app.settings import get_settings

NOW = datetime.now(UTC)
GROUPS = ["a", "b", "c"]


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 't.db'}")
    d.create_all()
    d.sync_sources([{"id": g, "name": g.upper(), "language": "te" if g == "c" else "en", "type": "publisher",
                     "enabled": True, "independence_group": g, "feed_url": f"https://{g}.example/feed"}
                    for g in GROUPS])
    return d


@pytest.fixture
def settings():
    return replace(get_settings(), dry_run=True)


def add_story(db, title, n_articles, priority="normal", language="en", title_prefix=""):
    sid = db.create_story(title=title, normalized_title=title.lower(), first_published_at=NOW)
    ids = []
    for i in range(n_articles):
        src = GROUPS[i] if language == "en" else "c"
        aid = db.insert_article(
            FeedItem(source_id=src, title=f"{title_prefix}{title} {i}", url=f"https://{src}.example/{sid}-{i}",
                     language=language, published_at=NOW - timedelta(hours=1)),
            content_hash=f"{sid}-{i}", relevant=True)
        db.attach_article(sid, aid)
        ids.append(aid)
    db.save_analysis(sid, title=title, summary="excerpt", priority=priority, verification_status="developing",
                     analysis_source="heuristic", evidence_article_ids=ids)
    return sid


class FakeLLM(LLMProcessor):
    name = "groq"

    def __init__(self, budget):
        self.budget, self.calls = budget, []

    def _generate(self, *, system, prompt, schema):
        if len(self.calls) >= self.budget:
            raise LLMUnavailable("groq call budget exhausted for this run/day")
        self.calls.append(schema.__name__)
        if schema is StoryAnalysis:
            import json
            ids = [e["id"] for e in json.loads(prompt.split("EVIDENCE:\n", 1)[1])]
            return StoryAnalysis(story_title="AI title", summary="AI summary", why_it_matters_for_comms="w",
                                 telangana_relevant=True, political_relevant=True, language="en", category="other",
                                 priority="normal", verification_status="developing", politically_sensitive=False,
                                 evidence_url_ids=ids, material_update=False, reason="r")
        import json
        return schema.model_validate({"items": [{"id": x["id"], "title_en": "English " + x["title"]}
                                                for x in json.loads(prompt)]})


def test_upgrade_queue_only_when_ai_on_and_ordered_by_importance(db):
    minor = add_story(db, "Minor civic notice", 1)
    big = add_story(db, "Assembly session row", 3)
    urgent = add_story(db, "Flood alert issued", 1, priority="high")
    assert db.stories_needing_analysis(10) == []  # AI off: keyword stories are done
    queue = [s["id"] for s in db.stories_needing_analysis(10, ai_upgrade_since=NOW - timedelta(hours=36))]
    assert queue == [urgent, big, minor]


def test_budget_upgrades_top_stories_and_leaves_the_rest_untouched(db, settings):
    minor = add_story(db, "Minor civic notice", 1)
    big = add_story(db, "Assembly session row", 3)
    llm = FakeLLM(budget=1)
    stats = analyze_pending(settings, db, llm, Tagger(settings.topics))
    assert stats.ai == 1 and stats.heuristic == 0
    assert db.get_story(big)["analysis_source"] == "groq" and db.get_story(big)["summary"] == "AI summary"
    untouched = db.get_story(minor)
    assert untouched["analysis_source"] == "heuristic" and untouched["summary"] == "excerpt"
    # Next run (fresh budget) picks up the remaining story.
    analyze_pending(settings, db, FakeLLM(budget=5), Tagger(settings.topics))
    assert db.get_story(minor)["analysis_source"] == "groq"


def test_translate_backlog_only_pending_telugu(db, settings):
    add_story(db, "తెలంగాణ వార్త", 1, language="te")
    add_story(db, "English story", 1)
    llm = FakeLLM(budget=5)
    assert translate_backlog(settings, db, llm) == 1
    assert llm.calls == ["TitleTranslations"]
    assert db.untranslated_telugu(NOW - timedelta(hours=36)) == []
    assert translate_backlog(settings, db, llm) == 0 and llm.calls == ["TitleTranslations"]


def test_no_ai_means_no_translation_calls(db, settings):
    add_story(db, "తెలంగాణ వార్త", 1, language="te")
    assert translate_backlog(settings, db, None) == 0
