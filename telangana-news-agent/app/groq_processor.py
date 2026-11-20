"""Groq provider (OpenAI-compatible chat completions). Same prompts/validation as Gemini.

Check your key and pick a model:
    python -m app.groq_processor            # lists models + runs one Telugu translation test
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import httpx
from pydantic import BaseModel, ValidationError

from app.gemini_processor import LLMProcessor, LLMUnavailable

log = logging.getLogger(__name__)
API_BASE = "https://api.groq.com/openai/v1"


class GroqProcessor(LLMProcessor):
    name = "groq"

    def __init__(self, api_key: str, model: str, *, temperature: float = 0.1,
                 budget_ok: Callable[[], bool] = lambda: True,
                 on_call: Callable[[], None] = lambda: None, max_retries: int = 4,
                 max_wait_seconds: float = 60, http: httpx.Client | None = None):
        self.model = model
        self.temperature = temperature
        self.budget_ok = budget_ok
        self.on_call = on_call
        self.max_retries = max_retries
        self.max_wait = max_wait_seconds
        self.http = http or httpx.Client(timeout=90, headers={"Authorization": f"Bearer {api_key}"})
        self._schema_mode = True  # flips to json_object if the model rejects json_schema

    def _body(self, system: str, prompt: str, schema: type[BaseModel]) -> dict:
        body = {"model": self.model, "temperature": self.temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if self._schema_mode:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema()}}
        else:
            # JSON mode: describe the schema in the prompt; we still validate locally with pydantic.
            body["messages"][0]["content"] = (
                f"{system}\n\nRespond with ONLY a JSON object matching this JSON Schema:\n"
                f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}")
            body["response_format"] = {"type": "json_object"}
        return body

    def _generate(self, *, system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        if not self.budget_ok():
            raise LLMUnavailable("Groq call budget exhausted for this run/day")
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            self.on_call()
            try:
                resp = self.http.post(f"{API_BASE}/chat/completions", json=self._body(system, prompt, schema))
            except httpx.HTTPError as exc:
                if attempt == self.max_retries:
                    raise LLMUnavailable(f"Groq network error: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 400 and self._schema_mode and "response_format" in resp.text:
                log.info("model %s does not support json_schema; switching to json_object mode", self.model)
                self._schema_mode = False
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == self.max_retries:
                    raise LLMUnavailable(f"Groq API error {resp.status_code}: {resp.text[:300]}")
                wait = delay
                try:
                    wait = float(resp.headers.get("retry-after", delay))
                except ValueError:
                    pass
                if wait > self.max_wait:  # e.g. daily token limit hit: give up for this run
                    raise LLMUnavailable(f"Groq rate limit: retry-after {wait:.0f}s exceeds {self.max_wait:.0f}s")
                log.info("Groq %s; waiting %.1fs", resp.status_code, wait)
                time.sleep(wait)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise LLMUnavailable(f"Groq API error {resp.status_code}: {resp.text[:300]}")

            content = resp.json()["choices"][0]["message"].get("content") or ""
            try:
                return schema.model_validate_json(content)
            except ValidationError as exc:
                if attempt == self.max_retries:
                    raise LLMUnavailable(f"Groq returned invalid JSON: {exc}") from exc
                time.sleep(delay)
                delay *= 2
        raise LLMUnavailable("Groq: retries exhausted")


def main() -> None:
    """Smoke test: verify the key, list models, translate one Telugu headline."""
    import sys

    from app.settings import get_settings

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    s = get_settings()
    if not s.groq_api_key:
        sys.exit("GROQ_API_KEY is not set (put it in .env)")
    http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {s.groq_api_key}"})
    r = http.get(f"{API_BASE}/models")
    if r.status_code != 200:
        sys.exit(f"key check failed: HTTP {r.status_code} {r.text[:200]}")
    ids = sorted(m["id"] for m in r.json().get("data", []))
    print("Models available to this key:\n  " + "\n  ".join(ids))
    print(f"\nConfigured model: {s.groq_model} ({'OK' if s.groq_model in ids else 'NOT AVAILABLE - set GROQ_MODEL'})")
    if s.groq_model not in ids:
        sys.exit(1)
    g = GroqProcessor(s.groq_api_key, s.groq_model, http=http)
    sample = "తెలంగాణలో భారీ వర్షాలు.. హైదరాబాద్‌కు ఆరెంజ్ అలర్ట్"
    print(f"\nTest translation:\n  {sample}\n  -> {g.translate_titles([(1, sample)]).get(1)}")


if __name__ == "__main__":
    main()
