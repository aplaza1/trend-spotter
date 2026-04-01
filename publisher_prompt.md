# Build the Publisher Service

## System Overview

You are building the **Publisher** service — one of three components in an automated blog content pipeline:

- **Trend Spotter** (EXISTS at `/Users/plaza/repositories/make-money-projects/trend-spotter`): discovers trending topics by category. REST API on AWS Lambda + API Gateway.
- **Content Writer** (EXISTS): generates blog articles from trending topics. REST API on AWS Lambda.
- **Publisher** (YOU ARE BUILDING THIS): orchestrates the daily pipeline and posts finished articles to a headless CMS.

## Publisher Responsibilities

1. **Daily topic refresh** (once/day at 06:00 UTC): call the Trend Spotter's refresh endpoint for each of the 10 categories to populate the cache with fresh topics.
2. **Article generation** (3× per day at 09:00, 14:00, 19:00 UTC): trigger the Content Writer to generate and return one new article per slot.
3. **Publishing**: post each returned article to a headless CMS via its API.

## Scheduling

The Publisher has **no always-on process**. Use AWS EventBridge scheduled rules (cron expressions) that invoke Lambda functions directly.

- **Refresh rule** (06:00 UTC daily): one EventBridge rule per category, each invoking the same Lambda function with a different category payload. Or one rule that fans out via SNS/SQS.
- **Write rules** (09:00, 14:00, 19:00 UTC daily): 3 EventBridge rules, each invoking a Lambda function that calls the Content Writer for a rotating category.

EventBridge → Lambda (direct invoke, no API Gateway needed for scheduled triggers).

## Trend Spotter API

**Base URL**: `https://srxz6apgd2.execute-api.us-west-2.amazonaws.com/prod`
**Auth**: `x-api-key` header — retrieve value via:
```bash
aws apigateway get-api-keys --include-values --region us-west-2 \
  --query "items[?name=='trend-spotter-key'].value" --output text
```

### POST /v1/trends/refresh
Refresh topics for one category. Call once per category per day.
```json
{ "category": "travel", "region": "global" }
```
Valid categories: `travel`, `technology`, `food`, `health`, `finance`, `fitness`, `beauty`, `parenting`, `pets`, `sustainability`

### GET /v1/trends/current?category=travel&limit=3
Get cached trending topics. No DataForSEO credits used. Returns up to 3 topics by default.

## Content Writer API

See `content_writer_prompt.md` for the full spec. Relevant endpoint:

### POST /v1/articles/generate
```json
{ "category": "travel", "region": "global" }
```
Returns:
```json
{
  "topic": "Hidden gem destinations for budget travellers",
  "title": "7 Hidden Gem Destinations Nobody Talks About",
  "body": "...",
  "meta_description": "...",
  "tags": ["travel", "budget", "destinations"],
  "generated_at": "2026-04-01T09:00:00Z"
}
```

## Headless CMS Integration

Build the CMS client as a **pluggable module** (`app/services/cms.py`) so it can be swapped without touching orchestration logic. The CMS API endpoint and credentials will be provided when you start building.

The CMS module should expose a single function:
```python
async def publish_article(article: ArticleResponse) -> str:
    """Publish article to CMS. Returns the published article URL."""
```

## Tech Stack

Same pattern as Trend Spotter: **Python 3.12, FastAPI, Mangum, AWS Lambda, CDK v2 (Python), DynamoDB**.

Study the Trend Spotter structure before starting:
- `cdk_stack.py` — CDK stack pattern (Lambda, API Gateway, SSM, IAM)
- `app/config.py` — Settings via pydantic-settings + SSM
- `app/main.py` — FastAPI + Mangum handler pattern
- `.github/workflows/deploy.yml` — CI/CD pattern

## Credential Management

All secrets via **SSM Parameter Store** + Lambda env vars (same pattern as Trend Spotter):
- `/publisher/trend-spotter-api-key` → `TREND_SPOTTER_API_KEY`
- `/publisher/content-writer-api-key` → `CONTENT_WRITER_API_KEY`
- `/publisher/cms-api-key` → `CMS_API_KEY`
- `/publisher/cms-base-url` → `CMS_BASE_URL`

## Key Constraints

- **API Gateway timeout**: 29s hard limit — any endpoint that fans out must use Lambda async invoke (`InvocationType="Event"`) and return 202 immediately.
- **Lambda timeout**: set to 60s for orchestration functions.
- **Category rotation**: for the 3 daily article slots, rotate through categories (e.g. slot 1 = travel, slot 2 = technology, slot 3 = food, then wrap around). Store the rotation state in DynamoDB or derive it from the time of day.
