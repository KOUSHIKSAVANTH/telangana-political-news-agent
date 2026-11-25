"""Runtime settings: secrets from environment, policy from config/*.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:  # local development convenience only; Cloud Run uses real env vars
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", ROOT / "config"))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class Settings:
    # --- mode ---
    dry_run: bool
    output_dir: Path
    # --- storage ---
    database_url: str
    # --- AI / search ---
    llm_provider: str | None  # gemini | groq | None (heuristic)
    gemini_api_key: str | None
    gemini_model: str
    groq_api_key: str | None
    groq_model: str
    tavily_api_key: str | None
    # --- email ---
    email_sender: str | None
    team_recipients: list[str]
    reviewer_recipients: list[str]
    recipient_allowlist: list[str]
    gmail_client_id: str | None
    gmail_client_secret: str | None
    gmail_refresh_token: str | None
    gmail_service_account_json: str | None
    gmail_delegated_user: str | None
    # --- delivery channels ---
    delivery_channels: list[str]  # any of: email, telegram
    telegram_bot_token: str | None
    telegram_team_chat_ids: list[str]
    telegram_reviewer_chat_ids: list[str]
    # --- config files ---
    sources: list[dict[str, Any]] = field(repr=False)
    topics: dict[str, Any] = field(repr=False)
    alert_rules: dict[str, Any] = field(repr=False)
    pipeline: dict[str, Any] = field(repr=False)

    @property
    def enabled_sources(self) -> list[dict[str, Any]]:
        return [s for s in self.sources if s.get("enabled") and s.get("feed_url")]


CHANNELS = ("email", "telegram")


def _channels() -> list[str]:
    """DELIVERY_CHANNELS=email,telegram (default: email)."""
    chosen = [c.lower() for c in _list("DELIVERY_CHANNELS")] or ["email"]
    bad = [c for c in chosen if c not in CHANNELS]
    if bad:
        raise ValueError(f"DELIVERY_CHANNELS has unknown channel(s) {bad}; use email and/or telegram")
    return list(dict.fromkeys(chosen))


def _llm_provider() -> str | None:
    """LLM_PROVIDER wins; otherwise use whichever key is present (Gemini first)."""
    explicit = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if explicit in {"none", "heuristic"}:
        return None
    if explicit:
        if explicit not in {"gemini", "groq"}:
            raise ValueError(f"LLM_PROVIDER must be gemini, groq or none, not {explicit!r}")
        return explicit
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    pipeline = load_yaml("pipeline.yaml")
    return Settings(
        # DRY_RUN defaults to TRUE: nothing is emailed until explicitly switched off.
        dry_run=_bool("DRY_RUN", True),
        output_dir=Path(os.environ.get("OUTPUT_DIR") or ROOT / "out"),
        database_url=(os.environ.get("DATABASE_URL") or "").strip() or f"sqlite:///{ROOT / 'local.db'}",
        llm_provider=_llm_provider(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
        gemini_model=os.environ.get("GEMINI_MODEL") or pipeline["gemini"]["model"],
        groq_api_key=os.environ.get("GROQ_API_KEY") or None,
        groq_model=os.environ.get("GROQ_MODEL") or pipeline["groq"]["model"],
        tavily_api_key=os.environ.get("TAVILY_API_KEY") or None,
        email_sender=os.environ.get("EMAIL_SENDER") or None,
        team_recipients=_list("EMAIL_TEAM_RECIPIENTS"),
        reviewer_recipients=_list("EMAIL_REVIEWER_RECIPIENTS"),
        recipient_allowlist=[x.lower() for x in _list("EMAIL_RECIPIENT_ALLOWLIST")],
        gmail_client_id=os.environ.get("GMAIL_CLIENT_ID") or None,
        gmail_client_secret=os.environ.get("GMAIL_CLIENT_SECRET") or None,
        gmail_refresh_token=os.environ.get("GMAIL_REFRESH_TOKEN") or None,
        gmail_service_account_json=os.environ.get("GMAIL_SERVICE_ACCOUNT_JSON") or None,
        gmail_delegated_user=os.environ.get("GMAIL_DELEGATED_USER") or None,
        delivery_channels=_channels(),
        telegram_bot_token=(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None,
        telegram_team_chat_ids=_list("TELEGRAM_TEAM_CHAT_IDS"),
        telegram_reviewer_chat_ids=_list("TELEGRAM_REVIEWER_CHAT_IDS"),
        sources=load_yaml("sources.yaml").get("sources", []),
        topics=load_yaml("topics.yaml"),
        alert_rules=load_yaml("alert_rules.yaml"),
        pipeline=pipeline,
    )
