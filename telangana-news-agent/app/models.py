"""Typed models shared across the pipeline."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Priority(StrEnum):
    critical = "critical"
    high = "high"
    normal = "normal"
    low = "low"


PRIORITY_RANK = {Priority.critical: 0, Priority.high: 1, Priority.normal: 2, Priority.low: 3}


class Verification(StrEnum):
    confirmed = "confirmed"
    developing = "developing"
    unverified = "unverified"


VERIFICATION_RANK = {Verification.unverified: 0, Verification.developing: 1, Verification.confirmed: 2}


class FeedItem(BaseModel):
    """A normalized item from RSS/Atom or Tavily, before persistence."""

    source_id: str
    title: str
    url: str  # canonicalized
    excerpt: str = ""
    language: str = "en"
    published_at: datetime | None = None  # UTC, publisher timestamp
    raw_metadata: dict = Field(default_factory=dict)


class StoryAnalysis(BaseModel):
    """Schema Gemini must return (spec section 6). Validated, never free-parsed."""

    story_title: str = Field(description="Concise English headline, using only facts in the evidence.")
    summary: str = Field(description="2-4 sentence English summary grounded only in the evidence.")
    why_it_matters_for_comms: str = Field(description="One or two sentences for a communications team.")
    telangana_relevant: bool = Field(description="True only if the event happens in Telangana or directly concerns its government, people or institutions.")
    political_relevant: bool = Field(description=(
        "True if the story is about politics, government, policy, elections, legislature, courts on public matters, "
        "public administration, or a public issue where government action or accountability is at stake "
        "(protests, disasters, civic failures, welfare schemes, farmers, jobs). False for entertainment, cinema, "
        "sports, lifestyle, astrology, celebrity, gadgets, and routine crime or accidents with no government or political angle."))
    language: Literal["te", "en", "other"] = Field(description="Dominant language of the evidence.")
    districts: list[str] = Field(default_factory=list, description="Telangana districts mentioned; 'Statewide' if state-level.")
    topics: list[str] = Field(default_factory=list)
    category: Literal["government_politics", "public_issues", "business_economy", "other"]
    priority: Literal["critical", "high", "normal", "low"]
    verification_status: Literal["confirmed", "developing", "unverified"]
    politically_sensitive: bool = Field(description="True for contested political claims, allegations, communal/caste angles.")
    evidence_url_ids: list[int] = Field(description="IDs of the evidence items that support the summary. Only IDs given in the input.")
    material_update: bool = Field(description="True only if evidence adds new material facts versus the previous summary.")
    reason: str = Field(description="Short justification of priority and verification.")
