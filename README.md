# Multi-Agent PR Reviewer

**Clear code reviews, without the noise.**

Multi-Agent PR Reviewer helps teams spend less time and effort on code reviews. It takes on the repetitive cognitive work — reading changes, gathering context, checking patterns, and spotting likely issues — so reviewers can focus on the judgment calls that need human experience. It shares only clear, code-backed feedback, and hands uncertain or sensitive findings to a person.

The result is faster reviews, less reviewer fatigue, and responsible automation that keeps people in control.

**Less noise. More signal. Human judgment where it matters.**

## How It Works

1. **GitHub sends a webhook** when a pull request is opened, reopened, or updated.
2. **The ingress verifies the signature** and enqueues a job in Redis. Duplicate deliveries are ignored.
3. **The worker fetches the PR diff** from the GitHub API using `GITHUB_TOKEN`.
4. **Four specialists analyze the added lines** — security, quality, testing, and docs — and the aggregator keeps only grounded, deduplicated findings.
5. **The routing gate decides**: high-confidence findings are posted to the PR, low-confidence findings go to the HITL queue, and critical security findings are never posted publicly.
6. **The GitHub poster writes the comments** and the event/finding history is stored in the local spine.

## Project Layout

```text
├── backend/
│   ├── webhook/         # FastAPI receiver, HMAC security, Redis/ARQ handoff
│   ├── worker.py         # ARQ worker + pipeline builder
│   ├── pipeline.py       # diff -> specialists -> HITL -> GitHub
│   ├── cli.py            # manual `check` and `review` commands
│   ├── github/           # GitHub API client + review-comment poster
│   ├── db/               # PostgreSQL/TimescaleDB schema & local SQLite spine
│   ├── agents/           # Security, Quality, Testing, Docs agents & aggregator
│   ├── orchestrator/     # Abstract WorkflowEngine & LangGraph adapter
│   ├── gates/            # Confidence/severity HITL routing
│   └── observability/    # Append-only event spine & budget guard
├── frontend/             # Next.js operational dashboard
├── evals/                # Golden PR regression dataset
├── tests/                # Offline unit/integration/fault-injection tests
├── Dockerfile            # Backend image (ingress + worker)
├── frontend/Dockerfile   # Dashboard image
└── docker-compose.yml    # Redis + ingress + worker + dashboard
```

## Quick Start

### 1. Install

This repo uses Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

### 2. Configure `.env`

At minimum, set the GitHub values:

```ini
GITHUB_WEBHOOK_SECRET=<openssl rand -hex 32>
GITHUB_TOKEN=<fine-grained PAT or classic PAT>
QUEUE_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
SPINE_SQLITE_PATH=./data/pr_reviewer.sqlite
```

Use **two different values**:

- `GITHUB_WEBHOOK_SECRET` is the shared secret you also paste into GitHub's webhook settings. It only signs webhook deliveries.
- `GITHUB_TOKEN` is the API token used to fetch the PR diff and post review comments.

For a **fine-grained PAT**, grant:

- **Pull requests:** Read and write
- **Contents:** Read
- **Metadata:** Read (usually included automatically)

For a **classic PAT**, use `repo` for private repositories or `public_repo` for public repositories.

To use Gemini for the specialist reviews:

```ini
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_google_ai_studio_key
GEMINI_MODEL_HIGH=gemini-3.6-flash
GEMINI_MODEL_FAST=gemini-3.5-flash-lite
```

The default `LLM_PROVIDER=deterministic` is fully offline and is what the tests and golden evals use.

### 3. Review an existing PR without a webhook

This is the fastest way to verify the whole path against your repository:

```bash
# Verify the token and fetch PR metadata/diff size
.venv/bin/python -m backend.cli check --repo owner/repository --pr 12

# Analyze and route, without posting comments
.venv/bin/python -m backend.cli review --repo owner/repository --pr 12 --no-post

# Analyze and post eligible comments
.venv/bin/python -m backend.cli review --repo owner/repository --pr 12
```

The CLI prints a JSON summary with `findings_count`, `auto_posted`, `hitl_escalated`, `private_security_alerts`, and the posted comment IDs/URLs.

### 4. Run the webhook locally

Start Redis (or use the Docker Compose stack in the next section):

```bash
docker run --rm -p 6379:6379 redis:7-alpine
```

Start the ingress and worker in two terminals:

```bash
.venv/bin/uvicorn backend.webhook.app:app --reload --port 8000
.venv/bin/arq backend.worker.WorkerSettings
```

Expose the ingress to GitHub with a tunnel:

```bash
npx smee-client --url https://smee.io/YOUR_CHANNEL --target http://localhost:8000/webhooks/github
```

Or use `ngrok http 8000` and copy the HTTPS forwarding URL.

Then in your GitHub repository:

1. **Settings → Webhooks → Add webhook**
2. **Payload URL:** `https://YOUR_PUBLIC_URL/webhooks/github`
3. **Content type:** `application/json`
4. **Secret:** the exact `GITHUB_WEBHOOK_SECRET` value from `.env`
5. **Events:** “Let me select individual events” → **Pull requests**
6. **Active:** checked

Open or update a PR. The ingress returns `202` with `{"status":"queued"}`, the worker fetches the diff, and eligible findings appear as PR review comments.

### 5. Host it

#### Docker Compose on a VPS

```bash
cp .env.example .env
# fill in GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN, REDIS_URL, GEMINI_API_KEY, etc.
docker compose up -d --build
```

This starts Redis, the ingress on port `8000`, the worker, and the dashboard on port `3000`. The worker stores findings in the `review-data` volume at `/data/pr_reviewer.sqlite`.

Put Caddy or Nginx in front of port `8000` with HTTPS, then use:

```text
https://your-domain.example/webhooks/github
```

as the GitHub payload URL.

#### Render / Railway / Fly.io

Deploy the repository as **two services** plus a Redis instance:

| Service | Command |
|---|---|
| Ingress (public HTTPS) | `uvicorn backend.webhook.app:app --host 0.0.0.0 --port $PORT` |
| Worker (background) | `arq backend.worker.WorkerSettings` |

Give both services the same environment variables, especially `REDIS_URL`, `QUEUE_BACKEND=redis`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN`, and the Gemini settings. If the platform provides a persistent disk, set `SPINE_SQLITE_PATH` to a path on that disk.

Use the ingress service's public HTTPS URL as the GitHub webhook payload URL. The worker does not need a public URL.

## Tests and Evals

```bash
.venv/bin/python -m pytest -q
.venv/bin/python evals/run_evals.py
npm --prefix frontend run build
```

Tests and golden evals are fully offline: they use in-memory SQLite, fakeredis, deterministic specialists, and mocked HTTP transports. No GitHub, Gemini, Tiger, or Redis credentials are needed.

## Optional: Gemini Embedding 2

Embeddings are deterministic by default. To use Gemini Embedding 2 for semantic memory:

```ini
EMBEDDING_PROVIDER=gemini
EMBEDDING_MODEL=gemini-embedding-2
EMBEDDING_DIM=1536
```

The adapter requests `output_dimensionality=1536` to match the committed `vector(1536)` schema, uses `RETRIEVAL_DOCUMENT` for indexed chunks and `RETRIEVAL_QUERY` for search queries, and raises on any dimension mismatch.

## Frontend Dashboard

```bash
npm --prefix frontend install
npm --prefix frontend run dev
```

Open `http://localhost:3000` for the HITL queue, trace viewer, and cost dashboard.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `401 Bad credentials` or `403` from GitHub | `GITHUB_TOKEN` is missing, expired, or lacks Pull requests/Contents permissions. |
| Webhook returns `401 unauthorized` | `GITHUB_WEBHOOK_SECRET` does not match the secret in GitHub's webhook settings. |
| Webhook returns `202 ignored` | The event is not `pull_request`, or the action is not `opened`/`synchronize`/`reopened`. |
| Webhook returns `202 queued` but nothing happens | Ingress and worker must use the same `REDIS_URL` and `QUEUE_BACKEND=redis`; check the worker logs. |
| No comments on the PR | No finding passed grounding/confidence routing. Run `python -m backend.cli review ... --no-post` to inspect the summary. |
| Inline comment fails with `422` | The line is not part of the diff; the poster automatically falls back to a PR conversation comment. |
| `GITHUB_TOKEN is missing or still a placeholder` | Replace `ghp_fallback_personal_access_token` in `.env` with a real token. |

## Security Notes

- `.env` is gitignored. Never commit tokens, webhook secrets, or private keys.
- `GITHUB_WEBHOOK_SECRET` signs webhook deliveries; `GITHUB_TOKEN` authorizes API calls. Rotate them independently.
- Critical security findings are never posted to public PRs; they are routed privately to the HITL queue.
