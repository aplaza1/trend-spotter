"""
app/config.py
─────────────
Central configuration: category seed keywords and application settings.

CATEGORIES is intentionally hardcoded for MVP simplicity. Each entry provides
the seed keywords that DataForSEO will use to discover trending related queries,
and a default region string matching DataForSEO's location_name values.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


# ──────────────────────────────────────────────────────────────────────────────
# Category seed configuration
# ──────────────────────────────────────────────────────────────────────────────

CATEGORIES: dict[str, dict] = {
    "travel": {
        "seeds": [
            "vacation",
            "solo travel",
            "budget travel",
            "family vacation",
            "summer trips",
        ],
        "default_region": "Worldwide",
    },
    "technology": {
        "seeds": [
            "artificial intelligence",
            "machine learning",
            "ChatGPT",
            "Apple Vision Pro",
            "quantum computing",
        ],
        "default_region": "Worldwide",
    },
    "food": {
        "seeds": [
            "healthy recipes",
            "meal prep",
            "air fryer recipes",
            "vegan food",
            "food trends",
        ],
        "default_region": "Worldwide",
    },
    "health": {
        "seeds": [
            "weight loss",
            "mental health",
            "gut health",
            "intermittent fasting",
            "sleep improvement",
        ],
        "default_region": "Worldwide",
    },
    "finance": {
        "seeds": [
            "personal finance",
            "investing for beginners",
            "passive income",
            "crypto",
            "stock market",
        ],
        "default_region": "Worldwide",
    },
    "fitness": {
        "seeds": [
            "home workout",
            "HIIT",
            "yoga for beginners",
            "strength training",
            "running tips",
        ],
        "default_region": "Worldwide",
    },
    "beauty": {
        "seeds": [
            "skincare routine",
            "hair care",
            "makeup trends",
            "clean beauty",
            "anti aging",
        ],
        "default_region": "Worldwide",
    },
    "parenting": {
        "seeds": [
            "parenting tips",
            "toddler activities",
            "baby sleep",
            "homeschooling",
            "screen time kids",
        ],
        "default_region": "Worldwide",
    },
    "pets": {
        "seeds": [
            "dog training",
            "cat care",
            "pet health",
            "puppy tips",
            "exotic pets",
        ],
        "default_region": "Worldwide",
    },
    "sustainability": {
        "seeds": [
            "zero waste",
            "sustainable living",
            "electric vehicles",
            "solar panels",
            "eco friendly products",
        ],
        "default_region": "Worldwide",
    },
}

# Map user-facing region slugs to DataForSEO location_name values
REGION_MAP: dict[str, str] = {
    "global": "Worldwide",
    "us": "United States",
    "uk": "United Kingdom",
    "ca": "Canada",
    "au": "Australia",
    "in": "India",
}


def resolve_region(region: str) -> str:
    """Resolve a user-supplied region slug to a DataForSEO location_name.

    Falls back to the raw value so callers can pass DataForSEO names directly.
    """
    return REGION_MAP.get(region.lower(), region)


# ──────────────────────────────────────────────────────────────────────────────
# Application settings (loaded once at cold-start via pydantic-settings)
# ──────────────────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    """Runtime settings resolved from environment variables.

    For local dev, create a .env file at the project root:

        DATAFORSEO_LOGIN=your_login
        DATAFORSEO_PASSWORD=your_password
        API_KEY=your_secret_key
        DYNAMODB_TABLE_NAME=trend-spotter
        AWS_REGION=us-east-1

    On Lambda, these are injected by the CDK stack (plain env vars or pulled
    from SSM at deploy time via CDK StringParameter.valueForStringParameter).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # DataForSEO credentials
    dataforseo_login: str = ""
    dataforseo_password: str = ""

    # AWS
    dynamodb_table_name: str = "trend-spotter"
    aws_region: str = "us-east-1"

    # DataForSEO base URL (override for testing)
    dataforseo_base_url: str = "https://api.dataforseo.com"

    # Behaviour
    default_limit: int = 20
    max_limit: int = 50

    # Simple in-memory rate-limit for single-user (refresh calls per minute)
    refresh_rate_limit: int = 10


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (created once per Lambda cold-start)."""
    return Settings()
