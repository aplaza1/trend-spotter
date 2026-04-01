"""
app/main.py
───────────
FastAPI application entry point + Mangum Lambda handler.

Endpoints
─────────
GET  /v1/trends/current   – Return latest cached trends (fast DynamoDB read)
POST /v1/trends/refresh   – Call DataForSEO, persist results, return fresh data

All endpoints require X-API-Key header authentication.

Local development
─────────────────
    uvicorn app.main:app --reload

Lambda handler
──────────────
    The `handler` module-level variable is the Mangum ASGI adapter that AWS
    Lambda calls. CDK points the function to `app.main.handler`.
"""

from __future__ import annotations

import logging
from typing import Annotated

import os

import boto3
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from app.config import CATEGORIES, Settings, get_settings
from app.dependencies import check_refresh_rate_limit
from app.models import RefreshRequest, TrendResponse
from app.services.dataforseo import fetch_trends
from app.services.dynamodb import TrendRepository

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Trend Spotter API",
    description="Returns current trending topics per category using DataForSEO Google Trends data.",
    version="1.0.0",
    root_path=os.environ.get("ROOT_PATH", ""),
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — internal API only; adjust origins in production if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# Shared dependency factory
# ──────────────────────────────────────────────────────────────────────────────

def get_repo(settings: Settings = Depends(get_settings)) -> TrendRepository:
    """Dependency that provides a TrendRepository instance."""
    return TrendRepository(settings=settings)


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get(
    "/v1/trends/current",
    response_model=TrendResponse,
    summary="Get latest cached trends",
    tags=["trends"],
)
async def get_current_trends(
    category: Annotated[str, Query(description="Category key (e.g. travel, tech, food)")],
    limit: Annotated[
        int,
        Query(ge=1, le=50, description="Max number of topics to return (default 20, max 50)"),
    ] = 20,
    region: Annotated[
        str,
        Query(description="Region slug: global | us | uk | ca | au | in  (default: global)"),
    ] = "global",
    repo: TrendRepository = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> TrendResponse:
    """Return the **latest cached** trends for *category* — no DataForSEO call is made.

    If no snapshot exists yet (category was never refreshed), returns HTTP 404
    with a helpful message directing the caller to POST /refresh first.
    """
    category = category.lower()

    if category not in CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown category '{category}'. Valid: {sorted(CATEGORIES.keys())}",
        )

    snapshot = await repo.get_snapshot(category)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No cached data for category '{category}'. "
                "Call POST /v1/trends/refresh first to populate it."
            ),
        )

    # Apply limit and return
    snapshot.topics = snapshot.topics[: min(limit, settings.max_limit)]
    return snapshot


@app.post(
    "/v1/trends/refresh",
    response_model=TrendResponse,
    summary="Refresh trends from DataForSEO (costs API credits)",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(check_refresh_rate_limit)],
    tags=["trends"],
)
async def refresh_trends(
    body: RefreshRequest,
    repo: TrendRepository = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> TrendResponse:
    """Synchronously call DataForSEO, process the results, persist to DynamoDB,
    and return the fresh TrendResponse.

    **Note:** Each call consumes DataForSEO API credits. Use sparingly.
    """
    category = body.category.lower()

    if category not in CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown category '{category}'. Valid: {sorted(CATEGORIES.keys())}",
        )

    logger.info("Refresh requested: category=%s region=%s", category, body.region)

    try:
        topics, task_id = await fetch_trends(
            category=category,
            region=body.region,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        logger.exception("DataForSEO call failed for category=%s", category)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"DataForSEO upstream error: {exc}",
        )

    response = await repo.upsert_snapshot(
        category=category,
        region=body.region,
        topics=topics,
        dataforseo_task_id=task_id,
    )

    return response


# ──────────────────────────────────────────────────────────────────────────────
# Health / meta endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health() -> dict:
    """Simple health check — no auth required."""
    return {"status": "ok"}


@app.get("/v1/categories", tags=["meta"])
async def list_categories() -> dict:
    """List all supported category keys and their seed keywords."""
    return {
        "categories": {
            k: {"seeds": v["seeds"], "default_region": v["default_region"]}
            for k, v in CATEGORIES.items()
        }
    }


# ──────────────────────────────────────────────────────────────────────────────
# Mangum Lambda handler
# ──────────────────────────────────────────────────────────────────────────────

# This is the entry point AWS Lambda will call.
# CDK sets handler = "app.main.handler" in the Lambda function configuration.
handler = Mangum(app, lifespan="off")
