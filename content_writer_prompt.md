# Build the Content Writer Service

## System Overview

You are building the **Content Writer** service — one of three components in an automated blog content pipeline:

- **Trend Spotter** (EXISTS at `/Users/plaza/repositories/make-money-projects/trend-spotter`): discovers trending topics by category. REST API on AWS Lambda + API Gateway.
- **Content Writer** (YOU ARE BUILDING THIS): generates blog articles from trending topics using the Claude API.
- **Publisher** (EXISTS): orchestrates the pipeline and publishes finished articles to a headless CMS.

## Content Writer Responsibilities

1. Receive a `POST /v1/articles/generate` request with a `category`
2. Call the Trend Spotter to get the top 10 trending topics for that category
3. Check its own DynamoDB table to find the first topic **not already written about** (deduplication)
4. Call the Claude API to generate a full blog article for that topic
5. Store the article in DynamoDB
6. Return the article to the caller (Publisher)

## API Endpoints

### POST /v1/articles/generate
**Request:**
```json
{ "category": "travel", "region": "global" }
```
**Response:**
```json
{
  "topic": "Hidden gem destinations for budget travellers",
  "title": "7 Hidden Gem Destinations Nobody Talks About",
  "body": "## Introduction\n\n...",
  "meta_description": "Discover 7 underrated travel destinations...",
  "tags": ["travel", "budget", "destinations"],
  "generated_at": "2026-04-01T09:00:00Z",
  "category": "travel",
  "topic_score": 68.5,
  "topic_sources": ["googlenews", "dataforseo"]
}
```
Returns **404** if all topics for the category have already been written about (caller should trigger a Trend Spotter refresh first).

### GET /v1/articles/list?category=travel&limit=20
Returns the list of previously written articles for a category (for deduplication inspection and review).

### GET /health
Public health check.

## Trend Spotter API

**Base URL**: `https://srxz6apgd2.execute-api.us-west-2.amazonaws.com/prod`
**Auth**: `x-api-key` header — store in SSM as `/content-writer/trend-spotter-api-key`

### GET /v1/trends/current?category={category}&limit=10
Returns up to 10 cached trending topics. Use `limit=10` (not the default 3) to have enough candidates for deduplication.

**Response shape:**
```json
{
  "category": "travel",
  "topics": [
    {
      "title": "Hidden gem destinations for budget travellers",
      "score": 68.5,
      "snippet": "In the news (BBC Travel): Hidden gem destinations...",
      "sources": ["googlenews"]
    }
  ]
}
```

**Deduplication strategy**: iterate topics in score order; skip any whose `title` already exists as a DynamoDB SK in the articles table; write about the first available one.

## Article Generation

**Model**: `claude-sonnet-4-6` (preferred) or `claude-haiku-4-5-20251001` (faster/cheaper for testing)
**SDK**: `anthropic>=0.40` — add to `requirements.txt`

**Prompt template** (adapt as needed):
```
You are an expert blog writer. Write an 800-word SEO-optimised blog post about the following trending topic.

Topic: {title}
Context: {snippet}

Requirements:
- Engaging H1 title (not the raw topic title)
- 3-4 H2 subheadings
- Natural keyword integration
- Actionable advice
- Conversational but authoritative tone
- End with a clear call to action

Also provide:
- meta_description: 150-160 character SEO meta description
- tags: 3-5 relevant tags as a JSON array
```

**Timeout note**: Claude generation takes 10-20s. The API Gateway hard limit is 29s. Use `claude-haiku-4-5-20251001` during development to stay well within the limit. Switch to `claude-sonnet-4-6` if generation quality needs to improve — but if it consistently exceeds 25s, consider using Lambda async invoke (return 202 + store result in DynamoDB, poll via a separate endpoint).

## DynamoDB Schema

**Table name**: `content-writer`
**Single-table design** (same pattern as Trend Spotter):

| Field | Value |
|---|---|
| PK | `category#travel` |
| SK | `article#2026-04-01T09:00:00Z` |
| topic_title | "Hidden gem destinations for budget travellers" |
| title | "7 Hidden Gem Destinations Nobody Talks About" |
| body | full article markdown |
| meta_description | SEO meta description |
| tags | list of strings |
| generated_at | ISO timestamp |
| topic_score | float |
| topic_sources | list of strings |
| ttl | epoch seconds (90 days from write) |

**For deduplication lookup**, also maintain a secondary record:

| Field | Value |
|---|---|
| PK | `category#travel` |
| SK | `topic#hidden-gem-destinations-for-budget-travellers` (slugified title) |
| article_sk | `article#2026-04-01T09:00:00Z` (pointer to the article) |
| written_at | ISO timestamp |

This makes "has this topic been written?" a fast `get_item` on `(category#travel, topic#<slug>)` without a scan.

## Tech Stack

Same pattern as Trend Spotter: **Python 3.12, FastAPI, Mangum, AWS Lambda, CDK v2 (Python), DynamoDB**.

Study the Trend Spotter before starting:
- `cdk_stack.py` — CDK stack pattern (Lambda, API Gateway, SSM, IAM, DynamoDB)
- `app/config.py` — Settings via pydantic-settings
- `app/main.py` — FastAPI + Mangum handler
- `app/services/dynamodb.py` — DynamoDB repository pattern (Decimal serialisation, TTL)
- `.github/workflows/deploy.yml` — CI/CD pattern

## Credential Management

All secrets via SSM + Lambda env vars:
- `/content-writer/anthropic-api-key` → `ANTHROPIC_API_KEY`
- `/content-writer/trend-spotter-api-key` → `TREND_SPOTTER_API_KEY`

## Key Constraints

- API Gateway timeout: 29s — if Claude generation is slow, fall back to Haiku or async pattern
- Lambda timeout: 60s
- DynamoDB: use `Decimal` for all float fields (see Trend Spotter's `_to_decimal()` helper in `app/services/dynamodb.py`)
- Never retry a failed Claude generation automatically — log and return 502 so the Publisher can reschedule
