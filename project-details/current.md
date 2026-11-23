# project-details/current.md — Milestone Status & Verification Checkpoints

## Active Status: M1–M8 Completed | Final review pending

### 1. Pre-Flight Check
- **Verdict:** Transitioned from `UNBUILT` -> `DONE` across M1..M8.

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
- `pytest -q`: **41 passed** (`test_m1_webhook.py` .. `test_m8_evals.py`, plus `test_gemini_provider.py` and `test_gemini_embedding.py`).
- `python evals/run_evals.py`: **100% category recall, 100% grounded evidence rate, 100% clean PR selectivity, 100% routing accuracy**.
- `npm --prefix frontend run build`: **Next.js production build succeeded cleanly**.
- Gemini LLM provider: **7/7 mocked transport tests passed** (tier routing, structured JSON, added-line grounding, safety blocks, HTTP errors, fail-fast key validation, worker wiring).
- Gemini Embedding 2: **6/6 tests passed** (deterministic default, real-key guard, dimension/task-type contract, dimension mismatch, HTTP errors, query-embedding retrieval wiring).
- Live provider verification (user-supplied credentials):
  - Gemini API key: **200 OK**, 65 models listed.
  - `gemini-3.5-flash-lite`: **200 OK** in ~1.7s.
  - `gemini-3.6-flash`: **200 OK** in ~10-12s. `gemini-3.8-flash` timed out at 180s and was replaced as the high-tier default.
  - Live security specialist review: returned one grounded **critical SQL Injection** finding at `app/db.py:4` with 0.99 confidence and verbatim evidence.
  - `gemini-embedding-2`: **200 OK**, exactly **1536 dimensions** in ~0.8s; live index + hybrid retrieve succeeded with query embeddings.
  - Tiger Cloud: PostgreSQL 18.7 / TimescaleDB 2.32.1, `vector` 0.8.7 and `vectorscale` 0.9.0 installed; `001_init.sql` applied; all tables, `vector(1536)`, `agent_events` hypertable, DiskANN/GIN indexes, and both continuous aggregates verified. Live Gemini vector insert + `<=>` nearest-neighbour query returned cosine similarity 1.0, then the test row was deleted.
  - Redis: `PING=True`; `create_redis_client()` and `RedisARQWorkerQueue.claim_delivery_id()` dedup verified (`True`, `False`) with cleanup.
  - GitHub: token in `.env` is still a placeholder, so live API PR creation was not exercised. Branch push and the GitHub "Create pull request" URL remain available.

### 4. Independent Verification
- An independent verification pass audited all code modules, SQL triggers, AST imports, and test suites against `project-details/done.html`.
- **Verdict:** **PASS (7/7 Invariants, 8/8 Milestones)**.

### 5. Final Review
- Prepared 3 targeted engineering questions (Design Rationale, Edge-Case Behavior, and Change-Impact Analysis) for user review.
