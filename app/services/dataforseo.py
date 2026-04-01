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

def _average_interest(items: list[dict]) -> float:
    """Compute the mean interest value across all time buckets for one keyword."""
    values = [
        point.get("value", 0)
        for item in items
        for point in item.get("data", [])
    ]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _parse_related_queries(related_queries: list[dict]) -> list[TopicItem]:
    """Extract TopicItem records from DataForSEO related_queries.

    Each element has:
      {
        "seed_keyword": "...",
        "type": "rising" | "top",
        "items": [ {"query": "...", "value": 123, ...}, ... ]
      }
    """
    topics: list[TopicItem] = []

    for group in related_queries:
        query_type = group.get("type", "top")
        for item in group.get("items") or []:
            query = item.get("query", "").strip()
            if not query:
                continue
            raw_value = item.get("value", 0)

            # "rising" values can be huge (e.g. 50000 = breakout) — cap at 100
            if query_type == "rising":
                rising_pct = float(raw_value)
                # Normalise score: breakout (very high) → 100, else proportional
                score = 100.0 if raw_value == 0 else min(100.0, raw_value / 100)
            else:
                rising_pct = None
                score = min(100.0, float(raw_value))

            topics.append(
                TopicItem(
                    title=query,
                    score=round(score, 2),
                    rising_pct=rising_pct if query_type == "rising" else None,
                    sources=["dataforseo"],
                    snippet=f"{'Rising' if query_type == 'rising' else 'Top'} related search: {query}",
                    related_queries=None,
                )
            )

    return topics


def _parse_result(result: dict) -> tuple[list[TopicItem], str]:
    """Parse a single DataForSEO task result into (topics, task_id)."""
    task_id = result.get("id", "unknown")
    items: list[TopicItem] = []

    for res in result.get("result") or []:
        # 1. Seed keyword scores from interest_over_time
        interest_items = res.get("items") or []
        for interest_item in interest_items:
            keyword = interest_item.get("keyword", "").strip()
            if not keyword:
                continue
            score = _average_interest([interest_item])
            # Collect all related query strings for the snippet
            related_q_raw = []
            for rq_group in res.get("related_queries") or []:
                for rq in rq_group.get("items") or []:
                    q = rq.get("query", "").strip()
                    if q:
                        related_q_raw.append(q)

            items.append(
                TopicItem(
                    title=keyword,
                    score=score,
                    rising_pct=None,
                    sources=["dataforseo"],
                    snippet=f"Avg interest over time: {score:.0f}/100",
                    related_queries=related_q_raw[:10] or None,
                )
            )

        # 2. Related queries (rising + top)
        items.extend(_parse_related_queries(res.get("related_queries") or []))

    return items, task_id


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
