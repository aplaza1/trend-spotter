"""
app/services/dynamodb.py
────────────────────────
Async DynamoDB repository using aioboto3.

Single-table design:
  Table:  trend-spotter
  PK:     category#<name>        e.g. "category#travel"
  SK:     snapshot#latest        always the same — we only keep one snapshot per category
  TTL:    90 days from last write (epoch seconds)

We serialise TopicItem objects to plain dicts before storing and deserialise on
read so that DynamoDB remains our sole persistence layer with no ORM overhead.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import aioboto3

from app.config import Settings, get_settings
from app.models import SnapshotItem, TopicItem, TrendResponse

logger = logging.getLogger(__name__)

_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days


def _pk(category: str) -> str:
    return f"category#{category}"


SK = "snapshot#latest"


# ──────────────────────────────────────────────────────────────────────────────
# Repository class
# ──────────────────────────────────────────────────────────────────────────────

class TrendRepository:
    """Thin async wrapper around a DynamoDB table for trend snapshots."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._session = aioboto3.Session()

    def _table_resource(self):
        """Return an async context manager for the DynamoDB Table resource."""
        return self._session.resource(
            "dynamodb",
            region_name=self._settings.aws_region,
        )

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_snapshot(self, category: str) -> TrendResponse | None:
        """Fetch the latest cached snapshot for *category*.

        Returns None if no snapshot exists (caller should trigger a refresh).
        """
        async with self._table_resource() as dynamodb:
            table = await dynamodb.Table(self._settings.dynamodb_table_name)
            response = await table.get_item(
                Key={"pk": _pk(category), "sk": SK},
                ConsistentRead=False,  # eventual consistency is fine for reads
            )

        item = response.get("Item")
        if not item:
            logger.info("No cached snapshot found for category=%s", category)
            return None

        topics = [TopicItem(**t) for t in item.get("topics", [])]
        return TrendResponse(
            category=category,
            region=item.get("region", "global"),
            topics=topics,
            generated_at=datetime.fromisoformat(item["last_updated"]),
        )

    # ── Write ─────────────────────────────────────────────────────────────────

    async def upsert_snapshot(
        self,
        category: str,
        region: str,
        topics: list[TopicItem],
        dataforseo_task_id: str,
    ) -> TrendResponse:
        """Persist (or overwrite) the latest snapshot for *category*.

        Returns the TrendResponse that was saved so callers can return it
        immediately without a redundant read.
        """
        now = datetime.now(timezone.utc)
        ttl = int(time.time()) + _TTL_SECONDS

        def _to_decimal(obj: Any) -> Any:
            """Recursively convert floats to Decimal (DynamoDB requirement)."""
            if isinstance(obj, float):
                return Decimal(str(obj))
            if isinstance(obj, dict):
                return {k: _to_decimal(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_to_decimal(v) for v in obj]
            return obj

        serialised_topics = [_to_decimal(t.model_dump()) for t in topics]

        item: dict[str, Any] = {
            "pk": _pk(category),
            "sk": SK,
            "topics": serialised_topics,
            "last_updated": now.isoformat(),
            "region": region,
            "dataforseo_task_id": dataforseo_task_id,
            "ttl": ttl,
        }

        async with self._table_resource() as dynamodb:
            table = await dynamodb.Table(self._settings.dynamodb_table_name)
            await table.put_item(Item=item)

        logger.info(
            "Upserted snapshot for category=%s region=%s topics=%d task_id=%s",
            category,
            region,
            len(topics),
            dataforseo_task_id,
        )

        return TrendResponse(
            category=category,
            region=region,
            topics=topics,
            generated_at=now,
        )
