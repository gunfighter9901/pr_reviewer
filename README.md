# Multi-Agent PR Reviewer

**Clear code reviews, without the noise.**

Drop a GitHub PR on it and get back **one structured review** — every issue ordered
by risk and explained in plain language — instead of a spray of bot comments.
Four specialist agents (security, quality, testing, docs) read the diff, the
aggregator merges their results into a single verdict, a fifth balanced
SummarizerAgent renders that verdict as one plain-language review, and a
routing gate decides what gets posted publicly, what needs a human, and what
stays private. Uncertain or sensitive findings never touch the public thread.
Clean PRs still get a short “all clear” comment by default.

## How it works

```text
[ GitHub PR opened / updated ]
                │
                ▼
┌────────────────────────────────────────────────────────┐
│ 1. FRONT DOOR — checks every delivery                   │
│  • Confirms it really came from GitHub                  │
│  • Ignores repeat deliveries of the same event          │
│  • Replies instantly, reviews happen in the background  │
└───────────────────────────┬────────────────────────────┘
                            │ queued for review
                            ▼
┌────────────────────────────────────────────────────────┐
│ 2. WAITING LINE — nothing gets lost                     │
│  • Slow reviews run in the background, fast replies up  │
│    front                                                 │
│  • Progress is saved, so crashes pick up where they     │
│    left off                                              │
└───────────────────────────┬────────────────────────────┘
                            │ a reviewer picks up the job
                            ▼
┌────────────────────────────────────────────────────────┐
│ 3. FOUR SPECIALISTS — one combined review               │
│                                                        │
│     ┌──────────┬──────────────┬────────────┐           │
│     ▼          ▼              ▼            ▼           │
│ [Security] [Quality]      [Tests]      [Docs]          │
│     │          │              │            │            │
│     └──────────┴──────────────┴────────────┘           │
│           ▼  (everything merged, worst first)          │
│    [ One review ] ──► duplicates removed, explained     │
│                       in plain words                    │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ 4. SAFETY CHECK — what happens next                     │
│  • Sure and safe ──► posted on the PR as one comment   │
│  • Unsure or critical ──► waits for a human first      │
└───────────────────────────┬────────────────────────────┘
                            │
     ═══════════════════════╪════════════════════════
     ▼                                          ▼
┌────────────────────────────────────────────────────────┐
│ 5. MEMORY — everything is remembered                    │
│ ┌───────────────┐ ┌───────────────┐ ┌────────────────┐ │
│ │ WHAT IT KNOWS │ │ WHAT'S TRUE   │ │ WHAT HAPPENED  │ │
│ │ past code     │ │ reviews       │ │ every step     │ │
│ │ patterns      │ │ findings      │ │ costs + timing │ │
│ └───────────────┘ │ human rulings │ └────────┬───────┘ │
│                   └───────────────┘          │         │
└──────────────────────────────────────────────┼────────┘
                                               ▼
┌────────────────────────────────────────────────────────┐
│ 6. YOUR DASHBOARD — stay in control                     │
│  • Approval queue • Review history • Spending guard    │
└────────────────────────────────────────────────────────┘
```

The technical version of this map (modules, tests, verified behavior) lives in
`project-details/architecture.md`.

## File system

| Path | What lives here |
|---|---|
| `backend/webhook/` | FastAPI ingress: webhook receiver, HMAC security, ARQ queue adapter, dashboard REST API |
| `backend/worker.py` | ARQ background worker: builds the pipeline, runs queued PR reviews |
| `backend/pipeline.py` | Review pipeline: diff → specialists → aggregator → HITL gate → poster |
| `backend/agents/` | 4 specialists (security, quality, testing, docs) + balanced SummarizerAgent, aggregator + single-PR summarizer |
| `backend/orchestrator/` | Workflow engine + LangGraph fan-out adapter |
| `backend/gates/` | HITL routing gate + feedback-decay engine |
| `backend/github/` | GitHub API client + idempotent PR poster (inline + single-summary) |
| `backend/retrieval/` | Code indexer + hybrid retriever (semantic/episodic/procedural context) |
| `backend/db/` | SQLite spine: schema, migrations, immutable audit triggers |
| `backend/config.py`, `backend/cli.py` | Validated settings; `check`/`review` manual CLI |
| `frontend/src/` | Next.js dashboard (PR cards, history, traces, queue + trigger) |
| `evals/` | Golden PR dataset + regression runner |
| `tests/` | Milestone-aligned unit, integration, and fault tests |
| `project-details/`, `docs/` | Milestone status/notes and the full project spec |
| `.env.example` | All configuration with defaults (copy to `.env`) |

## Features

- **Single-comment reviews** — one risk-ordered, plain-language summary per PR.
- **Selectivity first** — only grounded, deduplicated findings; clean PRs get a short “all clear” comment by default (`POST_CLEAN_COMMENT=false` for silent clean PRs).
- **Safety by construction** — critical findings are never posted to public PRs.
- **Human-in-the-loop** — approve / reject decisions persist and calibrate future sensitivity.
- **Live dashboard** — PR cards, review history, trace viewer, Redis queue with manual trigger.
- **Deterministic by default** — fully offline regex specialists; Gemini is opt-in and falls back gracefully.
- **Large-PR guardrails** — source-first filtering with byte budgets and truncation reports.

## Prerequisites

| Need | Why | Get it |
|---|---|---|
| Python 3.12 + Node 20 | Backend + dashboard | `python3.12 --version`, `node --version` |
| GitHub fine-grained PAT | Fetch diffs, post comments | GitHub → Settings → Developer settings → **Pull requests: Read+Write**, **Contents: Read**, **Metadata: Read** |
| Redis URL | Webhook → worker queue (only for webhook mode) | Local `redis-server`, or any hosted Redis |

Optional: Google AI Studio key for Gemini (`LLM_PROVIDER=gemini`); a public URL
(`smee.io`/`ngrok`) so GitHub can reach your ingress.

## Startup

Pick **one** path. Option A is the fastest way to try it (2 minutes, no Redis).
Option B auto-reviews every PR via webhook + dashboard.

### 0. One-time setup (both options)

```bash
# Python 3.12 is required (tests, CLI, and worker all assume it)
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Create config once, then edit it
cp .env.example .env
```

Now open `.env` and set at minimum:

```text
GITHUB_TOKEN=ghp_...   # classic PAT with `repo` scope, or fine-grained PAT with
                       # Pull requests: Read+Write, Contents: Read, Metadata: Read
```

> `check` / `review` exit with code 2 while `GITHUB_TOKEN` is still a placeholder.
> Tests and evals need no token.

### Option A — Review one PR from the terminal (no Redis, no webhook)

```bash
# 1. Connectivity check — verifies the token, prints head SHA + diff size. Posts nothing.
.venv/bin/python -m backend.cli check --repo owner/repo --pr 1

# 2. Dry run — runs all 4 agents + routing, prints the review JSON. Posts nothing.
.venv/bin/python -m backend.cli review --repo owner/repo --pr 1 --no-post

# 3. Live run — same analysis, then posts eligible comments to the PR.
.venv/bin/python -m backend.cli review --repo owner/repo --pr 1
```

Add `--post-summary` to step 2 or 3 to post one combined comment instead of
one inline comment per finding.

### Option B — Full stack: webhook + worker + dashboard

Use this only if you want `pull_request` events from GitHub to trigger reviews
automatically. You need Redis for this path — **either** hosted **or** local.
If `REDIS_URL` already points to hosted Redis (e.g. Upstash), skip local Redis
entirely: no `redis-server` needed.

1. In `.env`, set:

   ```text
   QUEUE_BACKEND=redis
   # Hosted (current setup): worker + ingress connect directly, nothing to run.
   # Example: REDIS_URL=rediss://default:<password>@<host>:<port>
   # Local alternative: REDIS_URL=redis://localhost:6379/0
   REDIS_URL=<your hosted URL or redis://localhost:6379/0>
   GITHUB_WEBHOOK_SECRET=<same secret you enter in GitHub repo Settings → Webhooks>
   NEXT_PUBLIC_API_URL=http://localhost:8000   # already the default
   ```

2. Only if `REDIS_URL` is local (`redis://localhost...`): install and run it.
   Skip this step for hosted Redis.

   ```bash
   brew install redis          # macOS, only if `redis-server` is missing
   redis-server --port 6379    # Terminal 1 — queue (local only)
   ```

3. Run each command in its **own terminal**:

   ```bash
   # Terminal 1 — ingress API (receives webhooks, serves the dashboard API)
   .venv/bin/uvicorn backend.webhook.app:app --port 8000
   ```

   ```bash
   # Terminal 2 — background worker (does the actual review)
   .venv/bin/arq backend.worker.WorkerSettings
   ```

   ```bash
   # Terminal 3 — dashboard (run `npm --prefix frontend install` once first)
   npm --prefix frontend install
   npm --prefix frontend run dev
   ```

   For GitHub to reach your ingress you need a public URL
   (`smee.io` / `ngrok` / VPS tunnel) forwarding to `localhost:8000`, then add it
   as repo Settings → Webhooks → Payload URL:
   `https://<your-public-url>/webhooks/github` (event: Pull requests).

| Link | What |
|---|---|
| `http://localhost:3000` | Dashboard — PR cards, history, traces, Redis queue + manual trigger |
| `http://localhost:8000/health` | Ingress health |
| `http://localhost:8000/webhooks/github` | GitHub webhook URL (repo Settings → Webhooks → Pull requests) |

Key knobs in `.env`: `LLM_PROVIDER=deterministic|gemini`, `POST_PR_SUMMARY_COMMENT=true`
(one comment) or unset (inline comments), `POST_CLEAN_COMMENT=true` (default: clean
PRs get an “all clear” comment; `false` restores silent clean PRs),
`CONFIDENCE_THRESHOLD=0.75`, `MAX_REVIEW_DIFF_BYTES=120000`. API: `GET /api/stats|hitl|pr-summaries|reviews|traces|queue`,
`POST /api/hitl/{id}/decision`, `POST /api/queue/trigger`.

## Verify

```bash
.venv/bin/python -m pytest -q        # full suite, offline
.venv/bin/python evals/run_evals.py  # golden PR regression gate
npm --prefix frontend run build      # dashboard build
```

## Tech stack

| Layer | Tech |
|---|---|
| API / ingress | Python, FastAPI, Uvicorn, Pydantic |
| Agents / orchestration | LangGraph (Send fan-out), asyncio |
| Queue / checkpoints | Redis, ARQ |
| Storage | SQLite (Tiger/TSDB-compatible schema) |
| GitHub | GitHub REST API, httpx |
| Dashboard | Next.js 14, React, Tailwind CSS |
| LLM (opt-in) | Gemini (reviews + embeddings), deterministic offline default |
| Tests / evals | pytest, golden PR dataset |
