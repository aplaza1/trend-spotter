"""
app/services/googlenews.py
──────────────────────────
Fetch trending news headlines from Google News RSS for a category.

No auth, no API key, no registration required.
Google News RSS is a public feed — works from any IP including Lambda.

Endpoint: https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en

Parsing strategy
────────────────
For each category we run 2-3 focused queries in parallel (e.g. "digital nomad
travel tips" for the travel category). Each query returns up to 10 recent
articles. We extract the <title> of each article as a topic and score by
recency (most recent = highest score, linearly decaying to 0).
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.models import TopicItem

logger = logging.getLogger(__name__)

# Per-category Google News search queries.
# Use specific, actionable phrases that surface current articles.
NEWS_QUERIES: dict[str, list[str]] = {
    "travel":         ["hidden gem travel destinations", "budget travel tips", "solo travel guide"],
    "technology":     ["AI tools 2025", "open source AI models", "tech trends"],
    "food":           ["viral food recipes", "healthy meal prep", "food trends"],
    "health":         ["weight loss tips", "mental health advice", "gut health"],
    "finance":        ["investing tips", "passive income ideas", "personal finance"],
    "fitness":        ["workout routines", "strength training tips", "fitness trends"],
    "beauty":         ["skincare routine tips", "makeup trends", "beauty hacks"],
    "parenting":      ["parenting tips", "toddler activities", "baby sleep advice"],
    "pets":           ["dog training tips", "cat care advice", "pet health"],
    "sustainability": ["zero waste tips", "sustainable living", "eco friendly home"],
}

_BASE_URL = "https://news.google.com/rss/search"
_MAX_AGE_DAYS = 30  # ignore articles older than this


def _parse_feed(xml_text: str, query: str) -> list[TopicItem]:
    """Parse a Google News RSS feed and return TopicItems scored by recency."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Failed to parse RSS feed for query '%s': %s", query, exc)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    items = channel.findall("item")
    now = datetime.now(timezone.utc)
    topics = []

    for rank, item in enumerate(items[:10]):
        title_el = item.find("title")
        if title_el is None or not title_el.text:
            continue
        # Google News titles are often "Headline - Source Name"; strip the source.
        title = title_el.text.rsplit(" - ", 1)[0].strip()
        if not title:
            continue

        # Score by recency: most recent article in the feed scores 100,
        # linearly decaying to ~30 for the 10th item.
        pub_date_el = item.find("pubDate")
        age_score = 100.0 - rank * 7  # position-based fallback
        if pub_date_el is not None and pub_date_el.text:
            try:
                pub_dt = parsedate_to_datetime(pub_date_el.text)
                age_hours = (now - pub_dt).total_seconds() / 3600
                if age_hours > _MAX_AGE_DAYS * 24:
                    continue  # skip stale articles
                # Decay from 100 → 10 over 30 days
                age_score = max(10.0, 100.0 - (age_hours / (_MAX_AGE_DAYS * 24)) * 90)
            except Exception:
                pass  # use position-based score

        source_el = item.find("source")
        source_name = source_el.text if source_el is not None and source_el.text else "Google News"

        topics.append(TopicItem(
            title=title,
            score=round(age_score, 2),
            rising_pct=None,
            sources=["googlenews"],
            snippet=f"In the news ({source_name}): {title}",
            related_queries=None,
        ))

    return topics


async def fetch_news_topics(category: str) -> list[TopicItem]:
    """Return trending TopicItems from Google News for the given category.

    Runs all queries for the category in parallel. Individual query failures
    are logged and skipped.

    Args:
        category: Category key matching NEWS_QUERIES dict.

    Returns:
        List of TopicItem with sources=["googlenews"]. Empty if category unknown
        or all queries fail.
    """
    queries = NEWS_QUERIES.get(category.lower(), [])
    if not queries:
        logger.info("No Google News queries configured for category=%s", category)
        return []

    async def _fetch_query(client: httpx.AsyncClient, query: str) -> list[TopicItem]:
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        try:
            resp = await client.get(_BASE_URL, params=params)
            resp.raise_for_status()
            return _parse_feed(resp.text, query)
        except Exception as exc:
            logger.warning("Google News fetch failed for query '%s': %s", query, exc)
            return []

    async with httpx.AsyncClient(timeout=15.0) as client:
        results = await asyncio.gather(
            *[_fetch_query(client, q) for q in queries],
            return_exceptions=True,
        )

    topics: list[TopicItem] = []
    for query, result in zip(queries, results):
        if isinstance(result, Exception):
            logger.warning("Google News gather error for query '%s': %s", query, result)
        else:
            topics.extend(result)

    logger.info("Google News returned %d topics for category=%s", len(topics), category)
    return topics
