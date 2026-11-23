# Multi-Agent PR Reviewer

**Clear code reviews, without the noise.**

Drop a GitHub PR on it and get back **one structured review** — every issue ordered
by risk and explained in plain language — instead of a spray of bot comments.
Four specialist agents (security, quality, testing, docs) read the diff, the
aggregator merges their results into a single verdict, and a routing gate decides
what gets posted publicly, what needs a human, and what stays private. Uncertain
or sensitive findings never touch the public thread.

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
| `backend/agents/` | 4 specialists (security, quality, testing, docs), aggregator + single-PR summarizer |
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
- **Selectivity first** — only grounded, deduplicated findings; clean PRs get zero comments.
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

```bash
# 1. Install
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then set GITHUB_TOKEN in .env

# 2. Review any PR (no webhook, no Redis needed)
.venv/bin/python -m backend.cli check --repo owner/repo --pr 1
.venv/bin/python -m backend.cli review --repo owner/repo --pr 1 --no-post  # dry run
.venv/bin/python -m backend.cli review --repo owner/repo --pr 1            # post it
```

Full stack — webhook, worker, dashboard:

```bash
# .env: QUEUE_BACKEND=redis, REDIS_URL=redis://localhost:6379/0
redis-server --port 6379                                                       # Redis
.venv/bin/uvicorn backend.webhook.app:app --port 8000                         # ingress
.venv/bin/arq backend.worker.WorkerSettings                                   # worker
npm --prefix frontend install && npm --prefix frontend run dev                # dashboard
```

| Link | What |
|---|---|
| `http://localhost:3000` | Dashboard — PR cards, history, traces, Redis queue + manual trigger |
| `http://localhost:8000/health` | Ingress health |
| `http://localhost:8000/webhooks/github` | GitHub webhook URL (repo Settings → Webhooks → Pull requests) |

Key knobs in `.env`: `LLM_PROVIDER=deterministic|gemini`, `POST_PR_SUMMARY_COMMENT=true`
(one comment) or unset (inline comments), `CONFIDENCE_THRESHOLD=0.75`,
`MAX_REVIEW_DIFF_BYTES=120000`. API: `GET /api/stats|hitl|pr-summaries|reviews|traces|queue`,
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
