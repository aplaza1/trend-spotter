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

import base64
import logging
from typing import Any

import httpx

from app.config import Settings, get_settings, resolve_region, CATEGORIES
from app.models import TopicItem

logger = logging.getLogger(__name__)

# DataForSEO limits to 5 keywords per task — we chunk the seeds automatically.
_MAX_KEYWORDS_PER_TASK = 5


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
    """Extract per-keyword average interest scores from a google_trends_graph item."""
    keywords: list[str] = item.get("keywords") or []
    data_points: list[dict] = item.get("data") or []
    if not keywords or not data_points:
        return []

    # Accumulate values per keyword index
    sums = [0.0] * len(keywords)
    counts = [0] * len(keywords)
    for point in data_points:
        values = point.get("values") or []
        for idx, val in enumerate(values):
            if idx < len(keywords) and val is not None:
                sums[idx] += val
                counts[idx] += 1

    topics = []
    for idx, kw in enumerate(keywords):
        score = round(sums[idx] / counts[idx], 2) if counts[idx] else 0.0
        topics.append(TopicItem(
            title=kw,
            score=score,
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
            score = min(100.0, raw_value / 100) if raw_value else 100.0
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
            # google_trends_topics_* have a different shape; skip for now

    item_types_found = []
    for res in result.get("result") or []:
        for item in res.get("items") or []:
            item_types_found.append(item.get("type"))
            logger.warning("Item keys: %s", list(item.keys()))
    logger.warning("Task %s item types: %s — parsed %d topics", task_id, item_types_found, len(topics))
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

    # Build payload — chunk seeds into groups of _MAX_KEYWORDS_PER_TASK
    tasks_payload = []
    for i in range(0, len(seeds), _MAX_KEYWORDS_PER_TASK):
        chunk = seeds[i : i + _MAX_KEYWORDS_PER_TASK]
        task: dict = {
            "keywords": chunk,
            "type": "web",
            "language_name": "English",
            # date_from / date_to omitted → DataForSEO uses its default (last 12 months)
        }
        if location_name:
            task["location_name"] = location_name
        tasks_payload.append(task)

    logger.info(
        "Calling DataForSEO for category=%s location=%s tasks=%d",
        category,
        location_name or "Worldwide",
        len(tasks_payload),
    )

    async with _make_client(settings) as client:
        response = await client.post(
            "/v3/keywords_data/google_trends/explore/live",
            json=tasks_payload,
        )
        response.raise_for_status()
        payload: dict = response.json()

    # DataForSEO wraps errors inside the payload even on HTTP 200
    status_code = payload.get("status_code", 0)
    if status_code not in (20000, 20100):
        raise RuntimeError(
            f"DataForSEO error {status_code}: {payload.get('status_message', 'unknown')}"
        )

    all_topics: list[TopicItem] = []
    last_task_id = "unknown"

    for task in payload.get("tasks") or []:
        task_status = task.get("status_code", 0)
        if task_status not in (20000, 20100):
            logger.warning(
                "DataForSEO task %s failed: %s",
                task.get("id"),
                task.get("status_message"),
            )
            continue
        topics, task_id = _parse_result(task)
        all_topics.extend(topics)
        last_task_id = task_id

    ranked = _deduplicate_and_rank(all_topics)
    logger.info("DataForSEO returned %d unique topics for category=%s", len(ranked), category)
    return ranked, last_task_id
