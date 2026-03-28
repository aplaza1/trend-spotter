"""
app/dependencies.py
───────────────────
FastAPI dependency providers:
  • API key authentication (X-API-Key header)
  • Simple in-memory rate-limiter for POST /refresh

Both are designed for a single-user internal API and therefore intentionally
lightweight — no Redis, no distributed state.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# API Key authentication
# ──────────────────────────────────────────────────────────────────────────────

async def verify_api_key(
    x_api_key: Annotated[Optional[str], Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """Raise 401 if X-API-Key header is missing or does not match configured key."""
    if not x_api_key or x_api_key != settings.api_key:
        logger.warning("Rejected request with invalid or missing API key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )


# ──────────────────────────────────────────────────────────────────────────────
# In-memory rate limiter (sliding window, per-Lambda instance)
# ──────────────────────────────────────────────────────────────────────────────

# Timestamps (float seconds) of recent refresh calls kept in a module-level deque.
# Works well for single-user single-Lambda; across warm instances it is per-instance
# but that is acceptable for this use-case.
_refresh_timestamps: deque[float] = deque()
_WINDOW_SECONDS = 60


def check_refresh_rate_limit(settings: Settings = Depends(get_settings)) -> None:
    """Sliding-window rate limiter: max `settings.refresh_rate_limit` calls per minute.

    Raises HTTP 429 if the limit is exceeded.
    """
    now = time.monotonic()
    cutoff = now - _WINDOW_SECONDS

    # Drop timestamps outside the sliding window
    while _refresh_timestamps and _refresh_timestamps[0] < cutoff:
        _refresh_timestamps.popleft()

    if len(_refresh_timestamps) >= settings.refresh_rate_limit:
        logger.warning("Refresh rate limit exceeded (%d calls/min)", settings.refresh_rate_limit)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit: max {settings.refresh_rate_limit} refreshes per minute",
        )

    _refresh_timestamps.append(now)
