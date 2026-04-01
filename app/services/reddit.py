"""
app/services/reddit.py
──────────────────────
Fetch trending topics from Reddit's OAuth API for a category.

Reddit blocks unauthenticated requests from data-centre IPs (AWS included).
We use the client_credentials OAuth flow (script app, read-only) which works
from any IP and has a generous rate limit (100 req/min).

Setup (one-time, free):
  1. Go to https://www.reddit.com/prefs/apps and create a "script" app.
  2. Set the redirect URI to http://localhost (unused for client_credentials).
  3. Note the client_id (shown under the app name) and client_secret.
  4. Store them in SSM:
       aws ssm put-parameter --name /trend-spotter/reddit-client-id   --value "..." --type String --overwrite
       aws ssm put-parameter --name /trend-spotter/reddit-client-secret --value "..." --type String --overwrite
  5. Add REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET as GitHub repo secrets.

Auth:   OAuth2 client_credentials (access token fetched once per cold-start)
Limit:  100 requests/min (authenticated)
Docs:   https://www.reddit.com/dev/api/#GET_top
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

_USER_AGENT = "TrendSpotter/1.0 (by /u/trendspotter_bot)"
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_BASE = "https://oauth.reddit.com"


async def _get_access_token(client_id: str, client_secret: str) -> str:
    """Fetch a client_credentials OAuth token from Reddit."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def fetch_reddit_topics(
    category: str,
    client_id: str = "",
    client_secret: str = "",
) -> list[TopicItem]:
    """Return trending TopicItems from Reddit for the given category.

    Fetches the top posts of the week from each mapped subreddit in parallel
    using OAuth client_credentials (required from cloud/data-centre IPs).

    Args:
        category:      Category key matching SUBREDDITS dict (e.g. "travel").
        client_id:     Reddit OAuth app client ID.
        client_secret: Reddit OAuth app client secret.

    Returns:
        List of TopicItem with sources=["reddit"]. Empty list if credentials
        are missing, category is unknown, or all subreddit fetches fail.
    """
    if not client_id or not client_secret:
        logger.info("Reddit credentials not configured — skipping Reddit source")
        return []

    subreddits = SUBREDDITS.get(category.lower(), [])
    if not subreddits:
        logger.info("No Reddit subreddits configured for category=%s", category)
        return []

    try:
        token = await _get_access_token(client_id, client_secret)
    except Exception as exc:
        logger.warning("Reddit OAuth token fetch failed: %s", exc)
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
    }

    async def _fetch_sub(client: httpx.AsyncClient, sub: str) -> list[TopicItem]:
        url = f"{_API_BASE}/r/{sub}/top?t=week&limit=10"
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
            # Divisor of 200 so ~20k combined engagement → score 100 (typical top weekly post).
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

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
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
