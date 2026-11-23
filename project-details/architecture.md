# Architecture — from idea to current structure

Original idea (user sketch) verified against the codebase and tightened to what
actually runs. Four corrections were made:

| # | Original claim | Current reality |
|---|---|---|
| 1 | Ingress returns `200 OK` (<10 s ACK) | Returns `202 queued` / `200 duplicate` / `202 ignored`, `403`/`400` on bad auth; <200 ms SLA enforced by `tests/test_m1_webhook.py` |
| 2 | `build_context ◄── Hybrid RAG` feeds the graph | The pipeline passes the filtered diff + changed files; `HybridRetriever` (vector + FTS, Python-side RRF) is implemented and exercised in golden evals |
| 3 | Spine is Tiger Cloud / managed Postgres; table `hitl_reviews` | Spine runs on a local SQLite file with the Tiger-compatible schema (Timescale/DiskANN/FTS declared in `001_init.sql`, enforced by introspection); review-decision table is `hitl_feedback` |
| 4 | — (didn't exist in the sketch) | Single-PR summary comment, PR-grouped dashboard, and Redis queue index + manual trigger were added on top |

## Runtime diagram

```text
[ GitHub PR opened / updated ]
                │
                ▼
┌────────────────────────────────────────────────────────┐
│ 1. INGRESS (FastAPI, :8000)                            │
│  • Verify HMAC-SHA256 (reject spoofs: 403)             │
│  • Dedup X-GitHub-Delivery, memory + Redis SET NX      │
│  • Answer fast: 202 queued / 200 duplicate / 202        │
│    ignored (<200 ms, covered by tests)                  │
└───────────────────────────┬────────────────────────────┘
                            │ enqueue(review_job)
                            ▼
┌────────────────────────────────────────────────────────┐
│ 2. QUEUE & CHECKPOINTS (Redis + ARQ)                   │
│  • Decouples fast ingress from ~20–70 s agent runs     │
│  • Redis checkpoints per node → resume after crashes   │
│  • Dashboard-visible index + manual trigger (same path)│
└───────────────────────────┬────────────────────────────┘
                            │ worker dequeues job
                            ▼
┌────────────────────────────────────────────────────────┐
│ 3. ORCHESTRATOR (LangGraph StateGraph + Send fan-out)  │
│                                                        │
│    context: filtered diff + changed files              │
│           │                                            │
│     ┌─────┴──────────┬──────────────┬────────────┐      │
│     ▼                ▼              ▼            ▼      │
│ [Security]      [Quality]       [Tests]      [Docs]     │
│     │                │              │            │      │
│     └─────┬──────────┴──────────────┴────────────┘      │
│           ▼  (barrier join, per-node timeouts)         │
│    [ Aggregator ] ──► dedup + risk-ordered findings       │
│    [ SummarizerAgent ] ──► balanced single review with    │
│                       plain explanations (all-clear       │
│                       verdict when zero findings)          │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ 4. SELECTIVITY & HITL GATE                             │
│  • High confidence, non-critical ──► post ONE comment   │
│  • Low confidence ──► human approval queue             │
│  • Critical on public repo ──► private queue, never    │
│    posted publicly                                     │
└───────────────────────────┬────────────────────────────┘
                            │
     ═══════════════════════╪════════════════════════
     ▼ (read before action / write after action)     ▼
┌────────────────────────────────────────────────────────┐
│ 5. UNIFIED DATA SPINE (SQLite file · Tiger-compatible) │
│ ┌───────────────┐ ┌───────────────┐ ┌────────────────┐ │
│ │ MEMORY        │ │ TRUTH         │ │ TIME & ROLLUPS │ │
│ │ code_chunks   │ │ pr_reviews    │ │ agent_events   │ │
│ │ (DiskANN+FTS  │ │ findings      │ │ (views: hourly │ │
│ │ schema-ready) │ │ hitl_feedback │ │ costs, health) │ │
│ └───────────────┘ └───────────────┘ └────────┬───────┘ │
└──────────────────────────────────────────────┼────────┘
                                               ▼
┌────────────────────────────────────────────────────────┐
│ 6. CONTROL PLANE (Next.js dashboard, :3000)             │
│  • One structured card per PR • Queue + manual trigger │
│  • Trace viewer • Spend guard (blocks LLM over budget) │
└────────────────────────────────────────────────────────┘
```

## Stage → code → tests

| Stage | Modules | Guards |
|---|---|---|
| 1. Ingress | `backend/webhook/{app,security,router,queue,api}.py` | `tests/test_m1_webhook.py`, `tests/test_webhook_queue.py` |
| 2. Queue & checkpoints | `backend/worker.py`, `orchestrator/langgraph_adapter.py` (`RedisCheckpointStore`) | `tests/test_m4_orchestrator.py`, `tests/test_queue_api.py` |
| 3. Orchestrator + specialists | `orchestrator/engine.py`, `agents/specialists.py`, `agents/aggregator.py`, `agents/summarizer.py` | `tests/test_m4/m5/m6_*.py`, `tests/test_pr_summary.py` |
| 4. Gate + poster | `gates/hitl.py`, `github/{client,poster}.py`, `pipeline.py` | `tests/test_m7_hitl.py`, `tests/test_review_pipeline.py` |
| 5. Spine | `db/spine.py`, `db/migrations/001_init.sql` | `tests/test_m2_spine.py`, `tests/test_m3_events.py` |
| 6. Dashboard | `frontend/src/app/page.tsx`, `GET /api/*` | `npm run build`, `tests/test_m8_evals.py` (golden gate) |

Verified: 79 pytest passed, golden evals 100% recall/selectivity/grounding/routing,
live webhook → worker → dashboard loop exercised against a real repository.
