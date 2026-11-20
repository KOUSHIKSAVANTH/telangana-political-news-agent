"""LLM processing: Telugu title translation and grounded, schema-validated story analysis.

`LLMProcessor` holds the provider-neutral logic (prompts, id validation); providers only
implement `_generate`. Gemini lives here; Groq in app/groq_processor.py.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app.models import StoryAnalysis

log = logging.getLogger(__name__)

SYSTEM_ANALYSIS = """You are a political news analyst for a political communications team in Telangana, India.
You receive EVIDENCE: news items (some in Telugu) about what may be one developing story.
The team tracks ONLY political content: politics, government, policy, elections, legislature, public administration,
and public issues where government action or accountability is at stake. Entertainment, cinema, sports, lifestyle,
astrology, celebrity and gadget news are out of scope: set political_relevant = false and priority = low for them.

Hard rules:
- Use ONLY facts stated in the evidence. Do not add background knowledge, numbers, dates, names, or quotes that are not in the evidence.
- Write in English. Keep Telugu proper names, titles and places accurate (transliterate faithfully; do not anglicise or guess).
- Preserve uncertainty: if the evidence says "alleged", "reportedly", "likely", keep that.
- evidence_url_ids must contain only ids from the EVIDENCE list, and only those that support the summary. Never invent ids or URLs.
- If the items are clearly about different events, summarise the dominant event and cite only its ids.
- priority: critical = immediate public-safety/major government emergency or event demanding same-hour comms attention;
  high = significant state-level development the comms team should know today; normal = routine news; low = minor/soft news.
- verification_status: confirmed only if an official source or multiple independent outlets report it consistently;
  developing if credible but single-source or still unfolding; unverified for claims, allegations, rumours, social media.
- politically_sensitive = true for allegations, contested political claims, communal/caste/religious angles, election disputes.
- material_update = true only if, compared with PREVIOUS SUMMARY, the evidence adds a new material fact (casualty numbers,
  official decision, arrest, reversal). If there is no previous summary, set false.
"""

SYSTEM_TRANSLATE = """Translate each Telugu news headline into concise, neutral English.
Keep proper names (people, parties, places, schemes) accurate and transliterated faithfully. Do not add information.
Return every id exactly once."""


class TitleTranslation(BaseModel):
    id: int
    title_en: str


class TitleTranslations(BaseModel):
    items: list[TitleTranslation]


class LLMUnavailable(RuntimeError):
    pass


GeminiUnavailable = LLMUnavailable  # backwards-compatible name


class LLMProcessor:
    """Shared prompting + validation. Subclasses implement `_generate`."""

    name = "llm"

    def _generate(self, *, system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        raise NotImplementedError

    def translate_titles(self, items: list[tuple[int, str]]) -> dict[int, str]:
        if not items:
            return {}
        prompt = json.dumps([{"id": i, "title": t} for i, t in items], ensure_ascii=False)
        result = self._generate(system=SYSTEM_TRANSLATE, prompt=prompt, schema=TitleTranslations)
        wanted = {i for i, _ in items}
        return {t.id: t.title_en.strip() for t in result.items if t.id in wanted and t.title_en.strip()}

    def analyze_story(self, evidence: list[dict[str, Any]], previous_summary: str | None) -> StoryAnalysis:
        payload = [
            {
                "id": e["id"],
                "publisher": e["publisher"],
                "source_type": e["source_type"],
                "language": e["language"],
                "published_at_utc": e["published_at"].isoformat() if e.get("published_at") else None,
                "title": e["title"],
                "title_en": e.get("title_en"),
                "excerpt": e.get("excerpt") or "",
            }
            for e in evidence
        ]
        prompt = (
            f"PREVIOUS SUMMARY:\n{previous_summary or '(none)'}\n\n"
            f"EVIDENCE:\n{json.dumps(payload, ensure_ascii=False, indent=1)}"
        )
        analysis = self._generate(system=SYSTEM_ANALYSIS, prompt=prompt, schema=StoryAnalysis)
        assert isinstance(analysis, StoryAnalysis)
        valid_ids = {e["id"] for e in evidence}
        cited = [i for i in analysis.evidence_url_ids if i in valid_ids]
        if not cited:
            raise LLMUnavailable(f"{self.name} cited no valid evidence ids; discarding analysis")
        analysis.evidence_url_ids = cited
        return analysis


class GeminiProcessor(LLMProcessor):
    name = "gemini"

    def __init__(self, api_key: str, model: str, *, temperature: float = 0.1,
                 budget_ok: Callable[[], bool] = lambda: True,
                 on_call: Callable[[], None] = lambda: None, max_retries: int = 3):
        from google import genai  # imported lazily so dry runs work without the SDK configured

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.budget_ok = budget_ok
        self.on_call = on_call
        self.max_retries = max_retries

    def _generate(self, *, system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        from google.genai import errors, types

        if not self.budget_ok():
            raise LLMUnavailable("Gemini call budget exhausted for this run/day")
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=self.temperature,
        )
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                self.on_call()
                resp = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
                return schema.model_validate_json(resp.text or "")
            except errors.APIError as exc:
                retryable = getattr(exc, "code", None) in {429, 500, 503, 504}
                if not retryable or attempt == self.max_retries:
                    raise LLMUnavailable(f"Gemini API error: {exc}") from exc
            except ValidationError as exc:
                if attempt == self.max_retries:
                    raise LLMUnavailable(f"Gemini returned invalid JSON: {exc}") from exc
            time.sleep(delay)
            delay *= 2
        raise LLMUnavailable("unreachable")
