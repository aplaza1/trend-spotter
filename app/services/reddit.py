"""
app/services/reddit.py
──────────────────────
Fetch trending topics from Reddit's top posts of the week for a category.

Auth:  None required for read-only access (unauthenticated JSON endpoints).
Limit: ~10 requests/min unauthenticated — fine for weekly refreshes.
Docs:  https://www.reddit.com/dev/api/#GET_top
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.models import TopicItem

logger = logging.getLogger(__name__)

# Subreddits to poll per category (2-3 communities per category)
SUBREDDITS: dict[str, list[str]] = {
    "travel":         ["travel", "solotravel", "backpacking"],
    "technology":     ["technology", "MachineLearning", "artificial"],
    "food":           ["food", "EatCheapAndHealthy", "MealPrepSunday"],
    "health":         ["health", "loseit", "nutrition"],
    "finance":        ["personalfinance", "investing", "financialindependence"],
    "fitness":        ["fitness", "bodyweightfitness", "running"],
    "beauty":         ["SkincareAddiction", "MakeupAddiction", "HairCare"],
    "parenting":      ["Parenting", "beyondthebump", "Mommit"],
    "pets":           ["dogs", "cats", "Pets"],
    "sustainability": ["ZeroWaste", "sustainability", "ClimateActionPlan"],
}

_HEADERS = {"User-Agent": "TrendSpotter/1.0 (blog topic aggregator; contact via GitHub)"}
_TOP_POSTS_URL = "https://www.reddit.com/r/{sub}/top.json?t=week&limit=10"


async def fetch_reddit_topics(category: str) -> list[TopicItem]:
    """Return trending TopicItems from Reddit for the given category.

    Fetches the top posts of the week from each mapped subreddit in parallel.
    Failures for individual subreddits are logged and skipped — other subreddits
    still contribute results.

    Args:
        category: Category key matching SUBREDDITS dict (e.g. "travel").

    Returns:
        List of TopicItem with sources=["reddit"]. Empty list if category is
        unknown or all subreddit fetches fail.
    """
    subreddits = SUBREDDITS.get(category.lower(), [])
    if not subreddits:
        logger.info("No Reddit subreddits configured for category=%s", category)
        return []

    async def _fetch_sub(client: httpx.AsyncClient, sub: str) -> list[TopicItem]:
        url = _TOP_POSTS_URL.format(sub=sub)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            posts = resp.json().get("data", {}).get("children", [])
        except Exception as exc:
            logger.warning("Reddit fetch failed for r/%s: %s", sub, exc)
            return []

        topics = []
        for post in posts:
            data = post.get("data", {})
            title = (data.get("title") or "").strip()
            if not title or data.get("over_18"):
                continue
            upvotes = data.get("score", 0) or 0
            num_comments = data.get("num_comments", 0) or 0
            # Engagement score: upvotes + comments weighted 2×, normalised to 0-100.
            # Divisor of 200 means ~20k combined engagement → score 100 (typical top weekly post).
            engagement = upvotes + num_comments * 2
            score = round(min(100.0, engagement / 200), 2)
            topics.append(TopicItem(
                title=title,
                score=score,
                rising_pct=None,
                sources=["reddit"],
                snippet=f"Trending on r/{sub}: {upvotes:,} upvotes, {num_comments:,} comments",
                related_queries=None,
            ))
        return topics

    async with httpx.AsyncClient(headers=_HEADERS, timeout=15.0) as client:
        results = await asyncio.gather(
            *[_fetch_sub(client, sub) for sub in subreddits],
            return_exceptions=True,
        )

    topics: list[TopicItem] = []
    for sub, result in zip(subreddits, results):
        if isinstance(result, Exception):
            logger.warning("Reddit gather error for r/%s: %s", sub, result)
        else:
            topics.extend(result)

    logger.info("Reddit returned %d topics for category=%s", len(topics), category)
    return topics
