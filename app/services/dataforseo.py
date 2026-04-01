"""
app/services/dataforseo.py
──────────────────────────
Async DataForSEO client for the Google Trends Explore Live endpoint.

Endpoint: POST /v3/keywords_data/google_trends/explore/live
Auth:     HTTP Basic (login:password)
Docs:     https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/

Parsing strategy
────────────────
DataForSEO returns a nested structure per task:
  result[0]
    ├── items[] (interest_over_time data per keyword)
    └── related_queries[]  ← breakout / top queries per seed
    └── related_topics[]   ← related topics per seed

We build TopicItem records from two sources and merge them:
  1. Each seed keyword gets a score = average of its interest_over_time values.
  2. Each related_query gets a score derived from its value field (0-100) and
     a rising_pct when its type == "rising".
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from app.config import Settings, get_settings, resolve_region, CATEGORIES
from app.models import TopicItem
from app.services.reddit import fetch_reddit_topics

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP client helpers
# ──────────────────────────────────────────────────────────────────────────────

def _basic_auth_header(login: str, password: str) -> str:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return f"Basic {token}"


def _make_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.dataforseo_base_url,
        headers={
            "Authorization": _basic_auth_header(
                settings.dataforseo_login, settings.dataforseo_password
            ),
            "Content-Type": "application/json",
        },
        timeout=60.0,  # DataForSEO live endpoints can be slow
    )


# ──────────────────────────────────────────────────────────────────────────────
# Response parsing helpers
# ──────────────────────────────────────────────────────────────────────────────
#
# DataForSEO Google Trends Explore response structure:
#
#   task.result[0].items = [
#     { "type": "google_trends_graph",
#       "keywords": ["kw1", "kw2", ...],
#       "data": [{"date_from": ..., "values": [v_kw1, v_kw2, ...]}, ...]
#     },
#     { "type": "google_trends_queries_top",
#       "keywords": ["kw1"],
#       "data": [{"query": "...", "value": 100}, ...]
#     },
#     { "type": "google_trends_queries_rising",
#       "keywords": ["kw1"],
#       "data": [{"query": "...", "value": 350}, ...]   # value = % increase
#     },
#     { "type": "google_trends_topics_top",   (optional)
#       "data": [{"title": "...", "type": "...", "value": 100}, ...]
#     },
#     ...
#   ]


def _parse_graph(item: dict) -> list[TopicItem]:
    """Extract per-keyword average interest scores from a google_trends_graph item.

    Uses the pre-computed `averages` field when available (multi-keyword requests).
    Falls back to computing from time series data (single-keyword requests return
    averages=[]).
    """
    keywords: list[str] = item.get("keywords") or []
    averages: list = item.get("averages") or []
    data_points: list[dict] = item.get("data") or []
    if not keywords:
        return []

    topics = []
    for idx, kw in enumerate(keywords):
        if idx < len(averages) and averages[idx] is not None:
            score = float(averages[idx])
        else:
            # Compute from time series values (single-keyword requests)
            vals = [
                pt["values"][idx]
                for pt in data_points
                if pt.get("values") and idx < len(pt["values"]) and pt["values"][idx] is not None
            ]
            score = round(sum(vals) / len(vals), 2) if vals else 0.0
        topics.append(TopicItem(
            title=kw,
            score=round(score, 2),
            rising_pct=None,
            sources=["dataforseo"],
            snippet=f"Avg interest over time: {score:.0f}/100",
            related_queries=None,
        ))
    return topics


def _parse_queries(item: dict, is_rising: bool) -> list[TopicItem]:
    """Extract TopicItems from google_trends_queries_top / _rising items."""
    topics = []
    for entry in item.get("data") or []:
        query = (entry.get("query") or "").strip()
        if not query:
            continue
        raw_value = entry.get("value") or 0
        if is_rising:
            rising_pct = float(raw_value)
            score = min(100.0, 50.0 + (raw_value ** 0.4)) if raw_value else 100.0
        else:
            rising_pct = None
            score = min(100.0, float(raw_value))
        topics.append(TopicItem(
            title=query,
            score=round(score, 2),
            rising_pct=rising_pct,
            sources=["dataforseo"],
            snippet=f"{'Rising' if is_rising else 'Top'} related search: {query}",
            related_queries=None,
        ))
    return topics


def _parse_topics(item: dict, is_rising: bool) -> list[TopicItem]:
    """Extract TopicItems from google_trends_topics_top / _rising items.

    Topic entities differ from query items: they use a 'title' key instead of
    'query' and may carry an entity type string (e.g. 'Topic', 'City', 'Person').
    """
    topics = []
    for entry in item.get("data") or []:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        raw_value = entry.get("value") or 0
        entity_type = entry.get("type", "")
        if is_rising:
            rising_pct = float(raw_value)
            # raw_value is % increase; map to 0-100 using power curve so high
            # values (e.g. 450%) score meaningfully (≈82) instead of being buried.
            score = min(100.0, 50.0 + (raw_value ** 0.4)) if raw_value else 100.0
        else:
            rising_pct = None
            score = min(100.0, float(raw_value))
        topics.append(TopicItem(
            title=title,
            score=round(score, 2),
            rising_pct=rising_pct,
            sources=["dataforseo"],
            snippet=f"{'Rising' if is_rising else 'Top'} related topic ({entity_type}): {title}",
            related_queries=None,
        ))
    return topics


def _parse_result(result: dict) -> tuple[list[TopicItem], str]:
    """Parse a single DataForSEO task result into (topics, task_id)."""
    task_id = result.get("id", "unknown")
    topics: list[TopicItem] = []

    for res in result.get("result") or []:
        for item in res.get("items") or []:
            item_type = item.get("type", "")
            if item_type == "google_trends_graph":
                topics.extend(_parse_graph(item))
            elif item_type == "google_trends_queries_top":
                topics.extend(_parse_queries(item, is_rising=False))
            elif item_type == "google_trends_queries_rising":
                topics.extend(_parse_queries(item, is_rising=True))
            elif item_type == "google_trends_topics_top":
                topics.extend(_parse_topics(item, is_rising=False))
            elif item_type == "google_trends_topics_rising":
                topics.extend(_parse_topics(item, is_rising=True))

    logger.info("Task %s — parsed %d topics", task_id, len(topics))
    return topics, task_id


def _deduplicate_and_rank(topics: list[TopicItem]) -> list[TopicItem]:
    """Merge duplicate titles (keep highest score) and sort descending by score."""
    seen: dict[str, TopicItem] = {}
    for t in topics:
        key = t.title.lower()
        if key not in seen or t.score > seen[key].score:
            seen[key] = t
    return sorted(seen.values(), key=lambda t: t.score, reverse=True)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_trends(
    category: str,
    region: str,
    settings: Settings | None = None,
) -> tuple[list[TopicItem], str]:
    """Call DataForSEO and return (topics, task_id).

    Args:
        category: Key into CATEGORIES config dict.
        region:   User-facing region slug (resolved internally to DataForSEO location_name).
        settings: Injected settings; defaults to get_settings() for convenience.

    Returns:
        Tuple of (list[TopicItem], dataforseo_task_id string).

    Raises:
        ValueError:     If category is unknown.
        httpx.HTTPError: On network / API errors.
        RuntimeError:   If DataForSEO returns a non-200 status code in its payload.
    """
    if settings is None:
        settings = get_settings()

    if category not in CATEGORIES:
        raise ValueError(f"Unknown category '{category}'")

    cat_cfg = CATEGORIES[category]
    seeds: list[str] = cat_cfg["seeds"]
    resolved = resolve_region(region)
    # "Worldwide" / global → omit location_name entirely (DataForSEO rejects it)
    location_name: str | None = None if resolved == "Worldwide" else resolved

    def _build_task(keyword: str) -> dict:
        task: dict = {
            "keywords": [keyword],
            "type": "web",
            "language_name": "English",
            "date_range": "past_30_days",
        }
        if location_name:
            task["location_name"] = location_name
        return task

    logger.info(
        "Calling DataForSEO for category=%s location=%s seeds=%d",
        category,
        location_name or "Worldwide",
        len(seeds),
    )

    all_topics: list[TopicItem] = []
    last_task_id = "unknown"

    # One API call per seed, all fired in parallel so total latency ≈ max(individual).
    # Batching multiple tasks in one request causes DataForSEO to return 40401
    # "Task Not Found" for all tasks.
    async def _fetch_seed(client: httpx.AsyncClient, seed: str) -> list[TopicItem]:
        nonlocal last_task_id
        response = await client.post(
            "/v3/keywords_data/google_trends/explore/live",
            json=[_build_task(seed)],
        )
        response.raise_for_status()
        payload: dict = response.json()

        status_code = payload.get("status_code", 0)
        if status_code not in (20000, 20100):
            logger.warning(
                "DataForSEO error for seed '%s': %s %s",
                seed, status_code, payload.get("status_message"),
            )
            return []

        results = []
        for task in payload.get("tasks") or []:
            task_status = task.get("status_code", 0)
            if task_status not in (20000, 20100):
                logger.warning(
                    "DataForSEO task %s failed for seed '%s': %s",
                    task.get("id"), seed, task.get("status_message"),
                )
                continue
            topics, task_id = _parse_result(task)
            results.extend(topics)
            last_task_id = task_id
        return results

    async with _make_client(settings) as client:
        results_per_seed = await asyncio.gather(
            *[_fetch_seed(client, seed) for seed in seeds],
            return_exceptions=True,
        )

    for seed, result in zip(seeds, results_per_seed):
        if isinstance(result, Exception):
            logger.warning("DataForSEO call failed for seed '%s': %s", seed, result)
        else:
            all_topics.extend(result)

    # Merge Reddit topics (run after DataForSEO; ~2-3s, well within Lambda timeout)
    try:
        reddit_topics = await fetch_reddit_topics(category)
        all_topics.extend(reddit_topics)
    except Exception as exc:
        logger.warning("Reddit fetch failed for category=%s: %s", category, exc)

    ranked = _deduplicate_and_rank(all_topics)
    logger.info(
        "Merged %d unique topics for category=%s (datasources: dataforseo + reddit)",
        len(ranked), category,
    )
    return ranked, last_task_id
