# AGENTS.md

Selectivity-first multi-agent PR reviewer: FastAPI webhook ingress → queue/checkpoints → LangGraph 4-specialist fan-out → deduplicating aggregator → HITL gate → GitHub poster. Python backend in `backend/`, Next.js 14 dashboard in `frontend/`, golden regression evals in `evals/`.

The full project spec (architecture rationale, milestones, invariants) is preserved at `docs/PROJECT_SPEC.md`; `project-details/done.html` is the locked invariant/milestone contract. Read them before architecture changes and do not rewrite `done.html` without user sign-off.

## Commands (use the venv)

`python3` on PATH is 3.15 and lacks the test deps; this repo is developed with `.venv` (Python 3.12).

- Full suite: `.venv/bin/python -m pytest -q`
- One milestone: `.venv/bin/python -m pytest tests/test_m5_agents.py -v`
- One test: `.venv/bin/python -m pytest tests/test_m5_agents.py::test_m5_prompt_registry_and_tiered_model_router -q`
- Golden regression gate: `.venv/bin/python evals/run_evals.py`
- Verify a real PR: `.venv/bin/python -m backend.cli check --repo owner/repo --pr 12`
- Review a real PR: `.venv/bin/python -m backend.cli review --repo owner/repo --pr 12 [--no-post]`
- Ingress server: `.venv/bin/uvicorn backend.webhook.app:app --reload --port 8000`
- Worker: `.venv/bin/arq backend.worker.WorkerSettings`
- Frontend: `npm --prefix frontend install`, then `npm --prefix frontend run dev`; the M8 build gate is `npm --prefix frontend run build`.

No lint/format gate is wired up (`npm run lint` exists, but no ESLint config/dependency is committed); pytest and the Next.js build are the real gates. `pyproject.toml` sets `asyncio_mode=auto`, `pythonpath="."`, and `testpaths=["tests"]`.

## Spec vs. implementation (read before editing)

- **The Tiger Data Spine is SQLite, not Postgres.** `TigerDataSpine` (`backend/db/spine.py`) creates SQLite tables/views/triggers. It defaults to `:memory:` for tests; `SPINE_SQLITE_PATH` selects a file for the worker. It parses `backend/db/migrations/001_init.sql` for introspection and validation, but does not open `TIGER_DATABASE_URL`. The SQL file is the introspection source of truth; runtime DDL lives in `spine.py`. Mirror schema changes in both or the M2 checks for extensions, hypertables, DiskANN/FTS indexes, and continuous aggregates fail.
- **Immutability has three layers.** `agent_events` is protected by Postgres triggers in `001_init.sql`, SQLite triggers in `spine.py`, and a `TRUNCATE` guard in `TigerDataSpine.execute_sql`. Tests require `UPDATE`, `DELETE`, and `TRUNCATE` to raise `ImmutableAuditSpineError`.
- **Specialists default to deterministic regex analyzers; Gemini is opt-in.** The four agents in `backend/agents/specialists.py` scan added diff lines with regex. `LLM_PROVIDER=gemini` wires `backend/agents/llm.py` into the ARQ worker's `ReviewPipeline`, where Gemini structured output is schema-constrained and grounded against added diff lines. Tests and golden evals never use the live provider unless a test explicitly injects a mock transport. No OpenAI/Anthropic SDK is in the dependency set.
- **Embeddings default to local approximations; Gemini Embedding 2 is opt-in.** `DeterministicEmbedder` (`backend/retrieval/indexer.py`) uses feature hashing. `EMBEDDING_PROVIDER=gemini` wires `GeminiEmbedder` (`gemini-embedding-2`, `output_dimensionality=EMBEDDING_DIM`) into `RepositoryIndexer` and `HybridRetriever`; tests and evals keep the deterministic default. `HybridRetriever` still computes cosine/lexical scores and RRF in Python over SQLite rows, so no Postgres DiskANN/`tsvector` query runs in the running app.
- **Redis is live in the webhook path when configured.** `create_redis_client()` (`backend/worker.py`) returns `fakeredis` only for explicit `fakeredis://`, `memory://`, or placeholder URLs. With `QUEUE_BACKEND=redis`, the FastAPI lifespan builds an `ArqWebhookQueue` and routes verified jobs to `process_pr_review_task`; the ARQ worker consumes them. The legacy `RedisARQWorkerQueue` FIFO helper remains for M4 tests. `backend/pipeline.py` fetches missing diffs via `backend/github/client.py`, runs the engine, persists routing, and posts with `GitHubPRPoster`.
- **The live GitHub path needs a real `GITHUB_TOKEN`.** `GitHubClient` fetches PR metadata/diff/files and posts inline review comments, falling back to a PR conversation comment on HTTP 422. `python -m backend.cli check/review` exercises the same pipeline manually. Tests use mocked transports and never call GitHub.
- **The dashboard is static.** `frontend/src/app/page.tsx` renders hard-coded HITL/trace/economics data and never calls the backend; `NEXT_PUBLIC_API_URL` is unused.
- **Only the LangGraph adapter may import langgraph.** `tests/test_m4_orchestrator.py` AST-scans `backend/` and fails on any `langgraph` import outside `backend/orchestrator/langgraph_adapter.py`. Depend on `WorkflowEngine` in `backend/orchestrator/engine.py`.
- **Docker Compose is the supported deploy path.** `Dockerfile`, `frontend/Dockerfile`, and `docker-compose.yml` run Redis, ingress, worker, and dashboard. GitHub needs a public HTTPS URL; use a VPS reverse proxy or a platform such as Render/Railway.

## Coupled contracts and gates

- `Finding` (`backend/models.py`) is `extra="forbid"` and validates grounded evidence. Changing it also touches the `findings` DDL (`001_init.sql` + `spine.py`), `FindingContract` in `frontend/src/app/page.tsx`, and the M5/M7 tests.
- `tests/test_m1_webhook.py` asserts a signed webhook enqueues in under 200 ms; don't add blocking I/O or LLM work to the FastAPI handler.
- The aggregator dedupes strictly by `(file_path, line_start)` and boosts confidence when multiple agent types collide; tests assert merged rationales and boosted confidence. Don't change grouping keys without updating M5 tests and golden expectations.
- Golden evals are a hard regression gate: `tests/test_m8_evals.py` requires 100% category recall, grounded evidence, clean-PR selectivity, and routing accuracy. Heuristic or confidence changes usually require updating `evals/golden_dataset.json` and/or expected routing.
- Routing: `critical` → `private_security_alert` even at high confidence; `confidence < CONFIDENCE_THRESHOLD` (default 0.75) → `hitl_escalate`; otherwise `auto_post`. `GitHubPRPoster` raises `PublicCriticalSecurityLeakError` if a critical finding is passed for a public repo.
- HITL feedback decay only adapts procedural memory after `min_evidence_threshold` (3) verified outcomes; preserve this anti-poisoning behavior.
- `EMBEDDING_DIM` must equal `vector(1536)` in `001_init.sql`; `TigerDataSpine`, `RepositoryIndexer`, and `upsert_code_chunk` fail fast on mismatch. Change the migration and env contract together.

## Environment and state

- `.env` is gitignored; `.env.example` is the committed contract. `Settings` (`backend/config.py`) supplies defaults, so tests run without secrets. Don't hardcode keys or dimensions.
- Tests need no external services (in-memory SQLite + fakeredis); webhook tests use FastAPI `TestClient`.
- Milestone status and verification checkpoints live in `project-details/` (`plan.md`, `current.md`, `implementation_notes.html`, `context_graph.md`). Update them after meaningful changes; `context_graph.md` maps each invariant to the modules and tests that must be re-run.
- Record milestone status and verification results in `project-details/current.md`.
