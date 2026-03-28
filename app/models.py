"""
app/models.py
─────────────
All Pydantic v2 request / response models for the Trend Spotter API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel, Field, model_validator


# ──────────────────────────────────────────────────────────────────────────────
# Core domain models
# ──────────────────────────────────────────────────────────────────────────────

class TopicItem(BaseModel):
    """A single trending topic returned to callers."""

    title: str = Field(..., description="Trending keyword / topic title")
    score: float = Field(
        ...,
        ge=0,
        le=100,
        description="Normalised popularity score 0-100 (from DataForSEO interest_over_time)",
    )
    rising_pct: Optional[float] = Field(
        default=None,
        description="Rising percentage if DataForSEO flagged this as a breakout query (e.g. 450 = +450%)",
    )
    sources: list[str] = Field(
        default_factory=lambda: ["dataforseo"],
        description="Data source identifiers",
    )
    snippet: Optional[str] = Field(
        default=None,
        description="Short human-readable context sentence",
    )
    related_queries: Optional[list[str]] = Field(
        default=None,
        description="Related search queries extracted from DataForSEO response",
    )


class TrendResponse(BaseModel):
    """Envelope returned by both GET /current and POST /refresh."""

    category: str
    region: str
    topics: list[TopicItem]
    generated_at: datetime
    source_note: str = "Powered by DataForSEO Google Trends"


# ──────────────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────────────

class RefreshRequest(BaseModel):
    """Body for POST /v1/trends/refresh."""

    category: str = Field(..., description="Category key (must exist in CATEGORIES config)")
    region: str = Field(default="global", description="Region slug or DataForSEO location_name")

    @model_validator(mode="after")
    def lower_region(self) -> "RefreshRequest":
        self.region = self.region.lower()
        return self


# ──────────────────────────────────────────────────────────────────────────────
# DynamoDB item shape (internal – not exposed via API)
# ──────────────────────────────────────────────────────────────────────────────

class SnapshotItem(BaseModel):
    """DynamoDB item shape stored under pk=category#<name>, sk=snapshot#latest."""

    pk: str
    sk: str = "snapshot#latest"
    topics: list[dict]          # serialised TopicItem dicts
    last_updated: str           # ISO-8601 datetime string
    region: str
    dataforseo_task_id: str
    ttl: int                    # Unix epoch – 90 days from now
