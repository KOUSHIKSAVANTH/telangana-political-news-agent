import json
from datetime import UTC, datetime

import httpx
import pytest

from app import groq_processor
from app.gemini_processor import LLMUnavailable
from app.groq_processor import GroqProcessor
from app.settings import _llm_provider

EVIDENCE = [{"id": 11, "publisher": "The Hindu", "source_type": "publisher", "language": "en",
             "published_at": datetime(2026, 11, 19, 4, tzinfo=UTC), "title": "Rain lashes Hyderabad",
             "title_en": None, "excerpt": "Heavy rain flooded low-lying areas."}]


def analysis(**kw):
    base = {"story_title": "Rain lashes Hyderabad", "summary": "Heavy rain flooded low-lying areas.",
            "why_it_matters_for_comms": "Civic response may draw questions.", "telangana_relevant": True,
            "political_relevant": True,
            "language": "en", "districts": ["Hyderabad"], "topics": ["weather"], "category": "public_issues",
            "priority": "normal", "verification_status": "developing", "politically_sensitive": False,
            "evidence_url_ids": [11], "material_update": False, "reason": "Single outlet."}
    base.update(kw)
    return base


def chat(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})


def make(handler, **kw) -> GroqProcessor:
    return GroqProcessor("k", "test-model", http=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(groq_processor.time, "sleep", lambda s: None)


def test_schema_mode_request_and_parse():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return chat(analysis())

    a = make(handler).analyze_story(EVIDENCE, None)
    assert a.evidence_url_ids == [11]
    assert seen["response_format"]["type"] == "json_schema"
    assert "EVIDENCE" in seen["messages"][1]["content"]


def test_falls_back_to_json_object_when_schema_unsupported():
    modes = []

    def handler(req):
        body = json.loads(req.content)
        modes.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, text='{"error":"response_format json_schema not supported"}')
        assert "JSON Schema" in body["messages"][0]["content"]
        return chat(analysis())

    make(handler).analyze_story(EVIDENCE, None)
    assert modes == ["json_schema", "json_object"]


def test_rate_limit_retries_with_retry_after():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "2"}) if len(calls) == 1 else chat(analysis())

    make(handler).analyze_story(EVIDENCE, None)
    assert len(calls) == 2


def test_long_retry_after_gives_up_for_this_run():
    with pytest.raises(LLMUnavailable, match="retry-after"):
        make(lambda req: httpx.Response(429, headers={"retry-after": "3600"})).analyze_story(EVIDENCE, None)


def test_invented_evidence_ids_rejected():
    with pytest.raises(LLMUnavailable, match="no valid evidence"):
        make(lambda req: chat(analysis(evidence_url_ids=[999]))).analyze_story(EVIDENCE, None)


def test_invalid_json_eventually_fails():
    bad = httpx.Response(200, json={"choices": [{"message": {"content": "{not json"}}]})
    with pytest.raises(LLMUnavailable, match="invalid JSON"):
        make(lambda req: bad).analyze_story(EVIDENCE, None)


def test_budget_exhausted_makes_no_request():
    def handler(req):
        raise AssertionError("must not call the API")

    with pytest.raises(LLMUnavailable, match="budget"):
        make(handler, budget_ok=lambda: False).analyze_story(EVIDENCE, None)


def test_translate_titles_ignores_unknown_ids():
    p = make(lambda req: chat({"items": [{"id": 1, "title_en": "Heavy rain in Telangana"},
                                         {"id": 77, "title_en": "made up"}]}))
    assert p.translate_titles([(1, "తెలంగాణలో భారీ వర్షాలు")]) == {1: "Heavy rain in Telangana"}


@pytest.mark.parametrize("env,expected", [
    ({"GROQ_API_KEY": "x"}, "groq"),
    ({"GEMINI_API_KEY": "x", "GROQ_API_KEY": "y"}, "gemini"),
    ({"GEMINI_API_KEY": "x", "GROQ_API_KEY": "y", "LLM_PROVIDER": "groq"}, "groq"),
    ({"GROQ_API_KEY": "x", "LLM_PROVIDER": "none"}, None),
    ({}, None),
])
def test_provider_selection(monkeypatch, env, expected):
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert _llm_provider() == expected
