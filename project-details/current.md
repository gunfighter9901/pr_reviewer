# project-details/current.md — Milestone Status & Verification Checkpoints

## Active Status: M1–M8 Completed | Live GitHub integration completed | Single-PR summary + live PR-grouped dashboard + Redis queue control completed

### 0. Single-PR Summary Review + Redis Queue Control (latest)
- **Aggregator:** `summarize_pr_findings` orders all specialist results by risk and explains each in one plain sentence (`RISK_LABELS` + `CATEGORY_EXPLAINERS`); `render_pr_summary_markdown` renders the single-comment body (verdict → counts → numbered points → withheld note).
- **GitHub:** `POST_PR_SUMMARY_COMMENT=true` posts the whole review as ONE conversation comment (`post_pr_summary_comment`, idempotent per delivery, critical-guard for public repos) instead of N inline comments; default `false` preserves the per-finding path. Opt-in per run: `backend.cli review --post-summary`.
- **Dashboard:** `GET /api/pr-summaries` groups HITL rows by `(repo, pr_number)` into whole-PR summaries; the review tab renders one structured card per PR with risk badge, headline, ordered points, evidence, fix, and per-point decisions. No demo data.
- **Queue:** `ArqWebhookQueue` keeps a dashboard index (`pr_reviewer:queue:index`) next to ARQ dedup; the worker reports `queued → processing → done/failed` into it. `GET /api/queue` merges the index with live ARQ set membership; `POST /api/queue/trigger {repo, pr_number}` enqueues a manual review through the identical webhook path (deduped, worker-picked). Queue tab lists waiting/recent PRs with a trigger form.
- **Verification:** `tests/test_pr_summary.py` 9 passed; `tests/test_queue_api.py` 5 passed; full suite **79 passed**; golden evals **PASSED**; `npm run build` clean; live `GET /api/pr-summaries` returns grouped summaries and `GET /api/queue` + `POST /api/queue/trigger` drove a real dashboard-triggered review (`chrome_video_scraper#1`, queued → processing → done, 0 findings via strict live mode, 0 posted) from `./data/pr_reviewer.sqlite`.

### 1. Pre-Flight Check
- **Verdict:** Transitioned from `UNBUILT` -> `DONE` across M1..M8; live webhook/worker/poster path added and verified offline.

### 2. Planning & Failure Matrix
- **Engineering Failure Modes Addressed:**
  - Raw-byte HMAC-SHA256 verification before JSON parsing (`INV-1`).
  - Validly signed malformed JSON returning `400 Bad Request` instead of `500` (`INV-1`).
  - Atomic delivery deduplication via `IdempotentWebhookRouter` and Redis `SET NX` (`INV-2`).
  - Per-specialist `asyncio.wait_for` timeout + exponential backoff retry + non-deadlocking fan-in (`INV-6`).
  - External I/O `CircuitBreaker` tripping `OPEN` after consecutive transport failures (`INV-6`).
- **LLM Failure Modes Addressed:**
  - Ungrounded findings filtered by `Finding` Pydantic validators and `FindingAggregator.validate_and_filter_grounded` (`INV-7`).
  - Critical security findings on public repositories withheld from public PR comments and routed to private HITL alerts (`Security Vulnerability Rule`).
  - Feedback loop poisoning prevented via `min_evidence_threshold=3` in `FeedbackDecayEngine`.

### 3. Build & Test Verification
- `pytest -q`: **79 passed** (`test_m1_webhook.py` .. `test_m8_evals.py`, plus `test_pr_summary.py`, `test_queue_api.py`, `test_gemini_provider.py`, `test_gemini_embedding.py`, `test_github_client.py`, `test_review_pipeline.py`, `test_webhook_queue.py`, `test_diff_selection.py`, and `test_specialist_fallback.py`).
- `python evals/run_evals.py`: **100% category recall, 100% grounded evidence rate, 100% clean PR selectivity, 100% routing accuracy**.
- `npm --prefix frontend run build`: **Next.js production build succeeded cleanly**.
- Live GitHub path: **18/18 offline tests passed** (API client diff/files/comment payloads, 422 fallback, pipeline diff fetch → route → post, queue dedup and restart behavior, large-diff filtering, deterministic LLM-outage fallback).
- Gemini LLM provider: **7/7 mocked transport tests passed** plus **3/3 fallback tests** (tier routing, structured JSON, added-line grounding, safety blocks, HTTP errors, fail-fast key validation, worker wiring, deterministic fallback on 429/empty output, merge/dedupe).
- Gemini Embedding 2: **6/6 tests passed** (deterministic default, real-key guard, dimension/task-type contract, dimension mismatch, HTTP errors, query-embedding retrieval wiring).
- Live provider verification (user-supplied credentials):
  - Gemini API key: **200 OK**, 65 models listed.
  - `gemini-3.5-flash-lite`: **200 OK** in ~1.7s.
  - `gemini-3.6-flash`: **200 OK** in ~10-12s. `gemini-3.8-flash` timed out at 180s and was replaced as the high-tier default.
  - Live security specialist review: returned one grounded **critical SQL Injection** finding at `app/db.py:4` with 0.99 confidence and verbatim evidence.
  - `gemini-embedding-2`: **200 OK**, exactly **1536 dimensions** in ~0.8s; live index + hybrid retrieve succeeded with query embeddings.
  - Tiger Cloud: PostgreSQL 18.7 / TimescaleDB 2.32.1, `vector` 0.8.7 and `vectorscale` 0.9.0 installed; `001_init.sql` applied; all tables, `vector(1536)`, `agent_events` hypertable, DiskANN/GIN indexes, and both continuous aggregates verified. Live Gemini vector insert + `<=>` nearest-neighbour query returned cosine similarity 1.0, then the test row was deleted.
  - Redis: `PING=True`; `create_redis_client()` and `RedisARQWorkerQueue.claim_delivery_id()` dedup verified (`True`, `False`) with cleanup. The new `ArqWebhookQueue` connected to the live Redis (`ArqRedis` pool, `PING=True`), and the FastAPI ingress started with `QUEUE_BACKEND=redis` and reported `github_token_configured=false`.
  - GitHub: a real fine-grained PAT is configured in `.env` (value never printed). `python -m backend.cli check --repo gunfighter9901/chrome_video_scraper --pr 1` authenticated as `gunfighter9901` and fetched **253 changed files / 2,084,457 diff bytes**. A live `review --no-post` returned 3 grounded findings in ~44s. A second run hit Gemini **HTTP 429 / 503 / timeout**; the new deterministic fallback completed in ~21s with **14 findings** (8 critical security alerts routed privately, 6 HITL) and no public comments.
  - Large-diff guardrail: the 2 MB PR was reduced to **120 KB across 21 source files**; 133 vendored/generated/binary files were skipped (`diff_truncated=true`).
  - Deployment: local run (uvicorn ingress + ARQ worker + Next.js dashboard, Redis only for webhook mode). Docker/Render blueprints were removed from the repo; public webhook delivery goes through a tunnel or VPS reverse proxy in front of port 8000.

### 4. Live GitHub Integration
- **Webhook → Redis → worker:** with `QUEUE_BACKEND=redis`, the FastAPI lifespan builds an `ArqWebhookQueue`; verified jobs are deduplicated and enqueued as `process_pr_review_task`.
- **Worker → PR:** `backend/pipeline.py` fetches the PR diff/files when the webhook has none, runs the specialist engine, routes findings through `HITLRoutingGate`, persists to the local spine, and posts `auto_post` findings via `GitHubReviewTransport`.
- **Inline comment fallback:** HTTP 422 from the review-comment API falls back to a PR conversation comment instead of dropping feedback.
- **Manual path:** `python -m backend.cli check --repo owner/repo --pr N` verifies token/PR access; `review` runs the same pipeline with optional `--no-post`.
- **Persistence:** `SPINE_SQLITE_PATH` allows the worker to keep findings/HITL history in a file instead of `:memory:`.
- **Live PR evidence:** `gunfighter9901/chrome_video_scraper#1` was fetched and reviewed end-to-end. The 2 MB diff was filtered to 120 KB; the first Gemini run produced 3 grounded findings, while a later quota-exhausted run fell back to 14 deterministic findings. Findings were persisted and routed (8 private critical alerts, 6 HITL); no auto-post comments were emitted because none passed the public-posting rule. Dry runs of PRs **#2–#5** returned **0 findings each**, confirming clean-PR selectivity.
- **Posting permission:** a pending GitHub review was created and deleted on `chrome_video_scraper#1`, confirming the token can write reviews/comments without publishing test noise.
- **Hosting:** local run (uvicorn ingress + ARQ worker + Next.js dashboard). The GitHub webhook must point at a public URL tunneled to the ingress.
- **Local end-to-end:** verified with a real signed webhook while Redis, the ARQ worker, and the ingress were running. The job completed in **5.54s**: GitHub diff fetch → deterministic specialists → 14 findings persisted (6 HITL, 8 private alerts, 0 public comments). A repeated delivery returned **200 duplicate**, `closed` returned **202 ignored**, and a bad signature returned **403 unauthorized**.

### 5. Independent Verification
- An independent verification pass audited all code modules, SQL triggers, AST imports, and test suites against `project-details/done.html`.
- **Verdict:** **PASS (7/7 Invariants, 8/8 Milestones)**.

### 6. Final Review
- Prepared 3 targeted engineering questions (Design Rationale, Edge-Case Behavior, and Change-Impact Analysis) for user review.
