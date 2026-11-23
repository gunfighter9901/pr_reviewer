# project-details/current.md — Milestone Status & Verification Checkpoints

## Active Status: M1–M8 Completed | Live GitHub integration completed | Final live PR run pending token

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
- `pytest -q`: **56 passed** (`test_m1_webhook.py` .. `test_m8_evals.py`, plus `test_gemini_provider.py`, `test_gemini_embedding.py`, `test_github_client.py`, `test_review_pipeline.py`, and `test_webhook_queue.py`).
- `python evals/run_evals.py`: **100% category recall, 100% grounded evidence rate, 100% clean PR selectivity, 100% routing accuracy**.
- `npm --prefix frontend run build`: **Next.js production build succeeded cleanly**.
- Live GitHub path: **15/15 offline tests passed** (API client diff/files/comment payloads, 422 fallback, pipeline diff fetch → route → post, queue dedup and restart behavior).
- Gemini LLM provider: **7/7 mocked transport tests passed** (tier routing, structured JSON, added-line grounding, safety blocks, HTTP errors, fail-fast key validation, worker wiring).
- Gemini Embedding 2: **6/6 tests passed** (deterministic default, real-key guard, dimension/task-type contract, dimension mismatch, HTTP errors, query-embedding retrieval wiring).
- Live provider verification (user-supplied credentials):
  - Gemini API key: **200 OK**, 65 models listed.
  - `gemini-3.5-flash-lite`: **200 OK** in ~1.7s.
  - `gemini-3.6-flash`: **200 OK** in ~10-12s. `gemini-3.8-flash` timed out at 180s and was replaced as the high-tier default.
  - Live security specialist review: returned one grounded **critical SQL Injection** finding at `app/db.py:4` with 0.99 confidence and verbatim evidence.
  - `gemini-embedding-2`: **200 OK**, exactly **1536 dimensions** in ~0.8s; live index + hybrid retrieve succeeded with query embeddings.
  - Tiger Cloud: PostgreSQL 18.7 / TimescaleDB 2.32.1, `vector` 0.8.7 and `vectorscale` 0.9.0 installed; `001_init.sql` applied; all tables, `vector(1536)`, `agent_events` hypertable, DiskANN/GIN indexes, and both continuous aggregates verified. Live Gemini vector insert + `<=>` nearest-neighbour query returned cosine similarity 1.0, then the test row was deleted.
  - Redis: `PING=True`; `create_redis_client()` and `RedisARQWorkerQueue.claim_delivery_id()` dedup verified (`True`, `False`) with cleanup. The new `ArqWebhookQueue` connected to the live Redis (`ArqRedis` pool, `PING=True`), and the FastAPI ingress started with `QUEUE_BACKEND=redis` and reported `github_token_configured=false`.
  - GitHub: token in `.env` is still a placeholder, so live API diff fetching and comment posting were not exercised. The same pipeline is covered by mocked client/pipeline tests and is available through `python -m backend.cli check/review` once a real token is added.
  - Deployment: `Dockerfile`, `frontend/Dockerfile`, and `docker-compose.yml` added for Redis + ingress + worker + dashboard. `docker compose config` could not be validated because Docker is not installed on this machine.

### 4. Live GitHub Integration
- **Webhook → Redis → worker:** with `QUEUE_BACKEND=redis`, the FastAPI lifespan builds an `ArqWebhookQueue`; verified jobs are deduplicated and enqueued as `process_pr_review_task`.
- **Worker → PR:** `backend/pipeline.py` fetches the PR diff/files when the webhook has none, runs the specialist engine, routes findings through `HITLRoutingGate`, persists to the local spine, and posts `auto_post` findings via `GitHubReviewTransport`.
- **Inline comment fallback:** HTTP 422 from the review-comment API falls back to a PR conversation comment instead of dropping feedback.
- **Manual path:** `python -m backend.cli check --repo owner/repo --pr N` verifies token/PR access; `review` runs the same pipeline with optional `--no-post`.
- **Persistence:** `SPINE_SQLITE_PATH` allows the worker to keep findings/HITL history in a file instead of `:memory:`.

### 5. Independent Verification
- An independent verification pass audited all code modules, SQL triggers, AST imports, and test suites against `project-details/done.html`.
- **Verdict:** **PASS (7/7 Invariants, 8/8 Milestones)**.

### 6. Final Review
- Prepared 3 targeted engineering questions (Design Rationale, Edge-Case Behavior, and Change-Impact Analysis) for user review.
