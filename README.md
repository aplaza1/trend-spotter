# Trend Spotter API

Single-user serverless API that returns **current trending topics** for a given category (travel, tech, food, etc.) to generate blog post ideas. Backed by DataForSEO Google Trends data, FastAPI on AWS Lambda, and DynamoDB.

---

## Architecture

```
Client → API Gateway (x-api-key) → Lambda (FastAPI + Mangum)
                                        ├── GET /v1/trends/current → DynamoDB read
                                        └── POST /v1/trends/refresh → DataForSEO → DynamoDB write
```

- **No cron jobs.** Data is only fetched from DataForSEO when you explicitly call `POST /refresh`.
- **GET /current** is a pure DynamoDB read — fast and free.
- **Single-table DynamoDB** design: `pk = category#<name>`, `sk = snapshot#latest`.
- **TTL:** Items expire after 90 days automatically.
- **Auth:** A single API Gateway key (`x-api-key` header) is enforced at the gateway level. No app-level key duplication.

---

## Project Structure

```
trend-spotter/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + Mangum Lambda handler
│   ├── models.py            # Pydantic v2 request/response models
│   ├── config.py            # Category seeds + Settings
│   ├── dependencies.py      # Rate limiter
│   └── services/
│       ├── __init__.py
│       ├── dataforseo.py    # Async DataForSEO client
│       └── dynamodb.py      # Async DynamoDB repository
├── cdk_app.py               # CDK root app
├── cdk_stack.py             # CDK stack (Lambda, APIGW, DynamoDB, SSM)
├── cdk.json                 # CDK config
├── requirements.txt         # Lambda runtime deps
├── requirements-cdk.txt     # CDK-only deps
├── .env.example             # Local dev env vars template
└── README.md
```

---

## Supported Categories

| Key             | Example Seeds                                   |
|-----------------|-------------------------------------------------|
| `travel`        | vacation, solo travel, budget travel            |
| `technology`    | artificial intelligence, ChatGPT, quantum computing |
| `food`          | healthy recipes, meal prep, air fryer recipes   |
| `health`        | weight loss, mental health, gut health          |
| `finance`       | personal finance, investing, passive income     |
| `fitness`       | home workout, HIIT, yoga for beginners          |
| `beauty`        | skincare routine, makeup trends, clean beauty   |
| `parenting`     | parenting tips, toddler activities, homeschooling |
| `pets`          | dog training, cat care, puppy tips              |
| `sustainability`| zero waste, sustainable living, solar panels    |

---

## Local Development

### 1. Prerequisites

- Python 3.12+
- AWS credentials configured (`~/.aws/credentials` or env vars)
- A DynamoDB table named `trend-spotter` in your AWS account (or use DynamoDB Local)
- DataForSEO account credentials

### 2. Setup

```bash
cd trend-spotter

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env — fill in DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD
```

### 3. Run locally

```bash
uvicorn app.main:app --reload --port 8000
```

Interactive docs: http://localhost:8000/docs  
*(No API key required locally — auth is enforced at the API Gateway level, not in the app.)*

### 4. Test locally

```bash
# Health check
curl http://localhost:8000/health

# List categories
curl http://localhost:8000/v1/categories

# Refresh trends for travel (calls DataForSEO — costs credits)
curl -X POST http://localhost:8000/v1/trends/refresh \
  -H "Content-Type: application/json" \
  -d '{"category": "travel", "region": "global"}'

# Get cached trends (fast DynamoDB read)
curl "http://localhost:8000/v1/trends/current?category=travel&limit=10"
```

---

## AWS Deployment

### 1. Install CDK dependencies

```bash
pip install -r requirements-cdk.txt
npm install -g aws-cdk          # CDK CLI (requires Node.js)
```

### 2. Bootstrap CDK (once per account/region)

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2
cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
```

### 3. Create SSM parameters BEFORE deploying

```bash
aws ssm put-parameter \
  --name /trend-spotter/dataforseo-login \
  --value "your_dataforseo_email" \
  --type String --overwrite

aws ssm put-parameter \
  --name /trend-spotter/dataforseo-password \
  --value "your_dataforseo_password" \
  --type String --overwrite
```

### 4. Deploy

```bash
cdk synth    # preview CloudFormation template
cdk deploy   # requires Docker for Lambda bundling
```

The deployment outputs:
- `ApiBaseUrl` — your API Gateway URL
- `DynamoTableName` — DynamoDB table name
- `GatewayApiKeyId` — API Gateway key ID

### 5. Retrieve your API key

```bash
aws apigateway get-api-keys \
  --include-values \
  --query "items[?name=='trend-spotter-key'].value" \
  --output text --region us-west-2
```

### 6. Call the deployed API

```bash
BASE_URL="https://<api-id>.execute-api.us-west-2.amazonaws.com/prod"
GW_KEY="<your-gateway-key>"

# Health check (no key needed)
curl $BASE_URL/health

# List categories
curl -H "x-api-key: $GW_KEY" $BASE_URL/v1/categories

# Refresh trends (costs DataForSEO credits)
curl -X POST "$BASE_URL/v1/trends/refresh" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $GW_KEY" \
  -d '{"category": "technology"}'

# Read cache
curl "$BASE_URL/v1/trends/current?category=technology&limit=20" \
  -H "x-api-key: $GW_KEY"
```

---

## CI/CD (GitHub Actions)

Push to `main` triggers the deploy workflow: Lint & Test → CDK Synth → CDK Deploy.

Required GitHub repository secrets:
| Secret | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM user key with CDK deploy permissions |
| `AWS_SECRET_ACCESS_KEY` | Corresponding secret |
| `DATAFORSEO_LOGIN` | Written to SSM before deploy |
| `DATAFORSEO_PASSWORD` | Written to SSM before deploy |

---

## API Reference

### Authentication

All endpoints except `/health` require the API Gateway key:
```
x-api-key: <your-gateway-key>
```

### GET /v1/trends/current

Returns the latest cached trends. **No DataForSEO credits used.**

| Parameter  | Type   | Default  | Description                        |
|------------|--------|----------|------------------------------------|
| `category` | string | required | Category key (e.g. `travel`)       |
| `limit`    | int    | 20       | Max topics returned (1-50)         |
| `region`   | string | `global` | Region: `global`, `us`, `uk`, etc. |

**Returns 404** if the category has never been refreshed.

### POST /v1/trends/refresh

Calls DataForSEO, persists results, returns fresh data. **Costs API credits.**

```json
{
  "category": "travel",
  "region": "global"
}
```

### Response shape (both endpoints)

```json
{
  "category": "travel",
  "region": "global",
  "generated_at": "2026-03-28T12:00:00Z",
  "source_note": "Powered by DataForSEO Google Trends",
  "topics": [
    {
      "title": "solo travel",
      "score": 87.5,
      "rising_pct": 450.0,
      "sources": ["dataforseo"],
      "snippet": "Rising related search: solo travel",
      "related_queries": ["solo travel tips", "solo travel safety"]
    }
  ]
}
```

### GET /v1/categories

Lists all supported categories and their seed keywords.

### GET /health

Public health check. Returns `{"status": "ok"}`. No API key required.

---

## How an Orchestrator Should Call This API

1. **Weekly:** Call `POST /v1/trends/refresh` for each category to populate the cache.
2. **During blog generation:** Call `GET /v1/trends/current?category=<cat>&limit=10` (no DataForSEO cost).

```
categories = ["travel", "technology", "food"]
for category in categories:
    POST /v1/trends/refresh {"category": category}   # ~once per week

# On-demand during writing:
GET /v1/trends/current?category=travel&limit=5
```

---

## Cost Estimate

| Resource       | Usage           | Cost                |
|----------------|-----------------|---------------------|
| Lambda         | ~100 calls/mo   | ~$0.00              |
| API Gateway    | ~100 calls/mo   | ~$0.00              |
| DynamoDB       | PAY_PER_REQUEST | ~$0.01/mo           |
| DataForSEO     | Per refresh     | ~$0.01–0.05/refresh |

For single-user weekly refreshes of 10 categories: **< $2/month total**.

---

## Teardown

```bash
cdk destroy
```

> The DynamoDB table has `RemovalPolicy.RETAIN` — it will NOT be deleted automatically. Delete it manually in the console if needed.
