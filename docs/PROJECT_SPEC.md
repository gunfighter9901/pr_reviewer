# PROJECT_SPEC.md — Production Multi-Agent PR Reviewer & AI-Native Engineering Harness

> Archived full specification. Active agent instructions are in [`../AGENTS.md`](../AGENTS.md).

## 1. Project Identity & Core Philosophy (L0 First Principles)
**Project Type:** Flagship Full-Stack & Agentic Systems Portfolio Project
**Domain:** Autonomous Code Review, Distributed Multi-Agent Orchestration, Context Engineering, & Observability

### The L0 Seed: Selectivity Over Coverage
This system does **not** exist to dump generic LLM comments onto a code diff. It exists to **reclaim senior engineer attention** by automating the mechanical parts of code review while routing ambiguous, high-consequence, or low-confidence edge cases to humans.
* **Anti-Goal:** Maximal output, ungrounded prose blobs, or "vibe-coded" single-prompt wrappers.
* **Core Goal:** High-precision **selectivity**. Every finding must be grounded in repository context, carry explicit evidence (file, line, code snippet), explain its reasoning (`rationale`), and output a calibrated `confidence` score.

---

## 2. System Architecture & Full-Stack Tech Stack
We use a **Modular Monolith** pattern separating stateless ingestion from asynchronous background worker pools, backed by a **Unified Data Spine** (avoiding multi-database synchronization overhead).

### Technology Choices & Architectural Decision Records (ADRs)
* **Ingress Service:** **FastAPI (Python)** — Stateless HTTP receiver. Validates webhooks and enqueues jobs in `< 200ms` to satisfy GitHub's strict 10-second webhook timeout.
* **Queue & Checkpointing:** **Redis + ARQ** (Upstash Redis compatible) — Handles FIFO job queuing, webhook delivery deduplication, and workflow state checkpointing.
* **Orchestration Engine:** **LangGraph** (wrapped behind an abstract `WorkflowEngine` interface) — Executes parallel fan-out/fan-in specialist graphs in-process. *Note: Must be abstracted so swapping to Temporal in the future requires zero domain logic changes.*
* **Unified Data Spine (Memory + Truth + Time):** **PostgreSQL via Tiger Cloud (TimescaleDB + pgvector + pgvectorscale)** — Single durable database handling all three data shapes:
  1. **Semantic Memory (Vector + FTS):** `pgvector` + `pgvectorscale` (StreamingDiskANN index on SSD) for hybrid semantic + lexical search over codebase chunks.
  2. **Relational Truth (Transactional):** Standard PostgreSQL tables for PR reviews, structured findings, architectural conventions (procedural memory), and HITL feedback queues.
  3. **Time-Series Observability (Event Spine):** `TimescaleDB` Hypertables (`agent_events`) and Continuous Aggregates for immutable audit trails, latency P95s, and real-time LLM token budget circuit breaking.
* **LLM & Embedding Strategy:**
  * **Model Routing:** Tiered routing per agent concern (e.g., high-reasoning models like Claude Opus/Sonnet or GPT-4o for Security/Quality; fast/cost-effective models like Haiku/GLM/Mini for Docs/Formatting).
  * **Embeddings:** OpenAI `text-embedding-3-large` or `text-embedding-3-small` (Strict invariant: `.env` `EMBEDDING_DIM` **must** match the PostgreSQL `vector(N)` column definition).
* **Frontend Dashboard:** **Next.js (App Router, TypeScript, Tailwind CSS)** — Minimal, high-signal operational UI featuring the Human-in-the-Loop (HITL) Approval Queue, End-to-End Trace Viewer, and LLM Cost/Economics Dashboard.
* **Deployment:** Docker Compose (local dev) & Railway (production).

---

## 3. Cognitive Design & Multi-Agent Topology (L1–L6)

### 3.1 The 5-Stage Pipeline
1. **Stateless Ingress (`POST /webhooks/github`):**
   * Verify `X-Hub-Signature-256` using HMAC-SHA256 **before** parsing JSON.
   * Check `X-GitHub-Delivery` idempotency key against Redis to drop duplicate retries.
   * Filter strictly for `pull_request` events (`opened`, `synchronize`, `reopened`).
   * Enqueue job to Redis/ARQ and immediately return `202 Accepted`.
2. **Context Engineering & Hybrid Retrieval:**
   * Fetch PR diff and changed files.
   * Query the Unified Data Spine across 3 memory dimensions:
     * **Semantic Memory:** Hybrid search (`DiskANN` vector similarity + PostgreSQL Full-Text Search) over `code_chunks` to find impacted callers, callees, and similar patterns.
     * **Episodic Memory:** Query past PR findings and developer disputes on the touched files.
     * **Procedural Memory:** Load repository conventions and Architecture Decision Records (ADRs).
3. **Parallel Specialist Fan-Out (LangGraph `Send` API):**
   Run four independent specialist agents concurrently, each with isolated system prompts, scoped context, and strict timeout boundaries:
   * `SecurityAgent`: Exploits, injection vectors, auth bypasses, secret leaks, unsafe deserialization.
   * `QualityAgent`: Logic correctness, null-safety, race conditions, adherence to codebase design patterns.
   * `TestingAgent`: Missing edge cases, brittle assertions, untested branches in the diff.
   * `DocsAgent`: Docstring accuracy, API contract drift, readability, inline comment hygiene.
4. **Fan-In Aggregator & Deduplicator:**
   * Collects outputs from all returning specialists (gracefully degrading if a single node times out).
   * Deduplicates overlapping findings on the same `file_path` + `line_number`.
   * Computes composite confidence and applies the **Selectivity & Autonomy Gate**.
5. **Autonomy & Human-in-the-Loop (HITL) Routing Gate:**
   * **Auto-Post to GitHub:** `confidence >= THRESHOLD` (e.g., `0.75`) AND `severity != 'critical'`.
   * **Escalate to HITL Queue & Private Alert (Slack/Dashboard):**
     * Any finding with `confidence < THRESHOLD`.
     * **Security Vulnerability Rule:** Any `critical` security finding on a public/open-source repo **must never** be posted publicly on the PR comment thread; route directly to the private HITL queue/Slack alert.

---

## 4. Core Data Contracts & Database Schemas

### 4.1 The `Finding` Contract (Pydantic / TypeScript)
Every specialist agent and the final aggregator must strictly emit findings matching this schema:
```python
class Finding(BaseModel):
    agent_type: Literal["security", "quality", "testing", "docs"]
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: str                  # e.g., "sql_injection", "null_dereference", "missing_edge_case"
    file_path: str                 # Exact repo-relative file path
    line_start: int                # Exact starting line in the diff
    line_end: int                  # Exact ending line in the diff
    evidence_snippet: str          # Exact code slice cited from the diff/context
    rationale: str                 # Skeptical, step-by-step technical proof of why this fails
    suggested_fix: Optional[str]   # Concrete code patch or remediation step
    confidence: float = Field(ge=0.0, le=1.0)
    prompt_version: str            # Version ID from the Prompt Registry
```

### 4.2 Unified Data Spine Schema (PostgreSQL + TimescaleDB + pgvectorscale)
* **`code_chunks` (Semantic Memory):** `id`, `repo`, `commit_sha`, `file_path`, `symbol_name`, `content`, `tsv` (`tsvector` for FTS), `embedding` (`vector(1536)` indexed with `diskann`).
* **`pr_reviews` & `findings` (Relational Truth):** Tracks PR metadata, status (`queued`, `processing`, `auto_posted`, `escalated_hitl`, `completed`), and individual findings linked to `github_delivery_id`.
* **`hitl_feedback` (Continuous Learning & Dispute Loop):** Stores senior reviewer overrides (`approved`, `edited`, `rejected_false_positive`) and developer disputes. Includes a `decay_weight` and `evidence_count` so the system requires a minimum evidence threshold before adapting procedural memory (preventing feedback loop poisoning).
* **`agent_events` (Immutable Time-Series Hypertable):**
  * Columns: `time TIMESTAMPTZ NOT NULL`, `trace_id`, `delivery_id`, `agent_name`, `event_type`, `model`, `prompt_version`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `status`, `metadata JSONB`.
  * **Database-Level Immutability Trigger:** Because PostgreSQL `RULE`s do not propagate across TimescaleDB hypertable chunks, attach a `BEFORE UPDATE OR DELETE` row-level trigger AND a `BEFORE TRUNCATE` statement-level trigger that raises an exception (`agent_events is append-only and immutable`).
* **Continuous Aggregates:** Materialized views (`hourly_pr_costs`, `agent_health_metrics`) tracking real-time spend, P95 latency, and escalation rates. Used by the **Budget Guard** to trip a circuit breaker if daily token spend exceeds limits.

---

## 5. System Invariants (Non-Negotiable Rules)
The coding agent must never violate these invariants during implementation:
1. **INV-1 (Verify Before Parse):** Webhook HMAC-SHA256 verification must execute on raw request bytes *before* JSON parsing or database/queue calls. Return `401/403` on bad signatures; return `400 Bad Request` (never `500`) on validly signed but malformed JSON.
2. **INV-2 (Idempotent Ingress):** Every webhook delivery ID (`X-GitHub-Delivery`) is deduplicated atomically. Retries must be acknowledged (`200/202`) and dropped without re-running the LLM graph.
3. **INV-3 (Framework Agnosticism):** Domain and agent logic must not import LangGraph primitives directly outside the adapter. All orchestration goes through the abstract `WorkflowEngine` (`run()`, `resume()`, `get_state()`).
4. **INV-4 (Immutable Audit Spine):** `agent_events` is strictly append-only enforced via SQL triggers on `UPDATE`, `DELETE`, and `TRUNCATE`. Every LLM call, tool execution, retrieval query, and gate decision emits a span.
5. **INV-5 (Dimensional Consistency):** The vector dimension configured in `.env` (`EMBEDDING_DIM`) must be validated at startup against the `code_chunks.embedding` schema column.
6. **INV-6 (Fault Isolation & Timeouts):** Every external I/O call (GitHub API, LLM provider, Redis, DB) and every parallel specialist node must have an explicit timeout, exponential backoff retry, and graceful fallback so a hung agent never deadlocks the aggregator.
7. **INV-7 (Grounded Evidence):** Zero findings may be posted without a valid `file_path`, `line_start`, `rationale`, and `confidence`.

---

## 6. AI-Native Engineering Harness ("The Genesis Ritual")
You (the Coding Agent) do not "vibe code." You operate inside a strict, stateful engineering loop maintained in the `.genesis/` directory.

### 6.1 State Spine Directory (`.genesis/`)
Before writing any code in a session, inspect `.genesis/`. If it does not exist, initialize it with:
* `.genesis/done.html` — **Locked Specification & Invariants.** Defines the exact cognitive job, architecture boundaries, invariants, and milestone demo commands. **Read-Only for Builder Agents** once locked with the user.
* `.genesis/plan.md` — **Master Milestone Plan.** Lists milestones (`M1`..`M8`), touched file boundaries, success criteria, required skills, and token budgets.
* `.genesis/implementation_notes.html` — **Live Rolling State.** Tracks what is currently built vs. stubbed, active blockers, known gaps, and session logs.
* `.genesis/context_graph.md` — Maps system invariants (`INV-1`..`INV-7`) to specific code modules and tests so changes trigger targeted regression checks.
* `.genesis/current.md` — Active milestone scratchpad and gate checkpoints (`G0`–`G4`).

### 6.2 The 5-Gate Execution Loop (Run Per Milestone)
For every milestone (`M1` through `M8`), execute this exact state machine:

1. **Gate 0 (`G0` — Pre-Flight & Reality Check):**
   * Read `.genesis/implementation_notes.html`, `.genesis/plan.md`, and grep the actual codebase.
   * Verify what already exists so you never duplicate modules or overwrite working code.
   * Write the `G0` checkpoint to `.genesis/current.md` and declare verdict: `UNBUILT`, `PARTIAL`, or `DONE`.
2. **Gate 1 (`G1` — Micro-Plan & Failure Matrix):**
   * Draft a concise micro-plan for the current milestone.
   * Explicitly list **Engineering Failure Modes** (timeouts, retries, malformed inputs, deadlocks) and **LLM Failure Modes** (hallucinations, ungrounded citations, schema drift) in a 2x2 risk matrix.
3. **Gate 2 (`G2` — Iterative Build & Test Loop):**
   * Implement code strictly within the milestone's file boundaries.
   * Write unit, integration, and fault-injection tests covering both happy paths and the `G1` failure modes.
   * Run the milestone's exact **Demo Command**. If tests fail, enter the **L2 Debug Loop** (hypothesize root cause -> minimal fix -> regression test).
4. **Gate 3 (`G3` — Independent Cold Verification):**
   * Do **not** trust your own build log. Run an independent verification pass (or spawn a fresh sub-agent verifier) that receives *only* the code, `.genesis/done.html` invariants, and the demo command.
   * Verify every invariant (`INV-1`..`INV-7`) and edge case (e.g., malformed signed JSON, SQL `TRUNCATE` attempts, env dimension mismatches).
5. **Gate 4 (`G4` — Human Comprehension Quiz & State Commit):**
   * Before marking a milestone complete, present the user with **3 targeted engineering questions** (`Quiz Me` protocol):
     1. *Design Rationale Question:* Why a specific architectural mechanism was chosen (e.g., triggers vs. rules on hypertables).
     2. *Edge-Case Question:* How the code behaves under a specific adversarial or boundary input.
     3. *Change-Impact Question:* What breaks downstream if a specific config or contract changes.
   * Wait for user confirmation, then update `.genesis/implementation_notes.html`, `.genesis/plan.md`, and `.genesis/current.md`.

---

## 7. Phased Milestone Roadmap & Verification Commands

Build the system strictly in this dependency order:

| Milestone | Scope & Deliverable | Key Files | Verification / Demo Command |
| :--- | :--- | :--- | :--- |
| **M1: Webhook Ingress Contract** | FastAPI receiver, raw-byte HMAC-SHA256 verification, payload validation, and in-memory idempotent router stub. | `backend/webhook/app.py`, `backend/webhook/security.py`, `backend/webhook/router.py`, `tests/test_m1_webhook.py` | `pytest tests/test_m1_webhook.py -v` (Tests signed vs. unsigned, malformed signed JSON -> `400`, duplicate delivery ID drop). |
| **M2: Unified Tiger Data Spine** | PostgreSQL/TimescaleDB/pgvector migrations: `code_chunks`, `pr_reviews`, `findings`, `hitl_feedback`, `agent_events` hypertable, immutability triggers (`UPDATE`/`DELETE`/`TRUNCATE`), and continuous aggregates. | `backend/db/migrations/001_init.sql`, `backend/db/spine.py`, `tests/test_m2_spine.py` | `pytest tests/test_m2_spine.py -v` (Verifies extensions, hypertable creation, and hard rejection of `UPDATE`, `DELETE`, and `TRUNCATE` on `agent_events`). |
| **M3: Event Spine & Observability** | Wire OpenTelemetry/custom event logger into Ingress & DB. Every webhook receipt, rejection, and span writes durably to `agent_events` + budget check reads Continuous Aggregate. | `backend/observability/events.py`, `backend/observability/budget.py`, `tests/test_m3_events.py` | `pytest tests/test_m3_events.py -v` (Send test webhook -> query `agent_events` and verify trace spans & cost ledger). |
| **M4: Redis Queue & LangGraph Orchestrator** | Replace M1 stub queue with Redis + ARQ worker pool. Implement abstract `WorkflowEngine` and `LangGraphEngine` adapter with parallel 4-node fan-out (`Send` API), per-node timeouts, and Redis checkpointing. | `backend/orchestrator/engine.py`, `backend/orchestrator/langgraph_adapter.py`, `backend/worker.py`, `tests/test_m4_orchestrator.py` | `pytest tests/test_m4_orchestrator.py -v` (Trigger 4 parallel nodes, simulate worker kill/timeout, verify checkpoint resume and non-deadlocking fan-in). |
| **M5: Specialist Agents & Aggregator** | Implement `Security`, `Quality`, `Testing`, and `Docs` agents with versioned Prompt Registry, model router, structured `Finding` output enforcement, and deduplicating Aggregator. | `backend/agents/specialists.py`, `backend/agents/aggregator.py`, `backend/prompts/registry.py`, `tests/test_m5_agents.py` | `pytest tests/test_m5_agents.py -v` (Run sample PR diff through all 4 specialists -> verify deduplicated `Finding` list with rationale & confidence). |
| **M6: Hybrid RAG & Context Engineering** | Repository chunker/embedder plus hybrid retriever combining `pgvectorscale` DiskANN similarity, PostgreSQL FTS (`tsvector`), episodic past disputes, and procedural conventions. | `backend/retrieval/indexer.py`, `backend/retrieval/hybrid_search.py`, `tests/test_m6_retrieval.py` | `pytest tests/test_m6_retrieval.py -v` (Index sample repo, execute hybrid query on PR diff, assert relevant callers/conventions retrieved). |
| **M7: HITL Gate, GitHub Poster & Reliability** | Confidence & severity routing gate, private escalation for critical security issues, GitHub PR Review API poster, circuit breakers, and feedback loop decay logic. | `backend/gates/hitl.py`, `backend/github/poster.py`, `backend/reliability/circuit_breaker.py`, `tests/test_m7_hitl.py` | `pytest tests/test_m7_hitl.py -v` (Verify high-confidence auto-posts, low-confidence/critical routes to `hitl_feedback`, and fault injection trips circuit breaker). |
| **M8: Operational Dashboard & Golden Evals** | Next.js dashboard (HITL approval queue, end-to-end Trace Viewer, Cost/Latency telemetry) + Golden Dataset evaluation harness for regression gating prompt/model updates. | `frontend/`, `evals/golden_dataset.json`, `evals/run_evals.py` | `pytest && python evals/run_evals.py && npm --prefix frontend run build` |

---

## 8. Repository Structure
Maintain this clean modular directory structure:
```text
├── AGENTS.md
├── .env.example
├── docker-compose.yml
├── pyproject.toml / requirements.txt
├── .genesis/
│   ├── done.html
│   ├── plan.md
│   ├── implementation_notes.html
│   ├── context_graph.md
│   └── current.md
├── backend/
│   ├── config.py                  # Validated Pydantic Settings (enforces INV-5)
│   ├── webhook/                   # M1: FastAPI stateless ingress & HMAC security
│   ├── db/                        # M2: SQL migrations, connection pool, Tiger Spine
│   ├── observability/             # M3: Append-only event logger & budget guard
│   ├── orchestrator/              # M4: Abstract WorkflowEngine & LangGraph adapter
│   ├── agents/                    # M5: 4 Specialists, Aggregator, Model Router
│   ├── prompts/                   # M5: Versioned Prompt Registry
│   ├── retrieval/                 # M6: AST/Code chunker & Hybrid DiskANN + FTS retriever
│   ├── gates/                     # M7: Selectivity, HITL escalation, feedback decay
│   ├── github/                    # M7: GitHub App / API client & idempotent PR poster
│   ├── reliability/               # M7: Retries, timeouts, & circuit breakers
│   └── worker.py                  # ARQ background worker entrypoint
├── frontend/                      # M8: Next.js HITL Queue, Trace Viewer, & Cost UI
├── evals/                         # M8: Golden PR dataset & regression evaluation runner
└── tests/                         # Milestone-aligned unit, integration, & fault tests
```

---

## 9. Environment Variables Contract (`.env.example`)
Never hardcode secrets or dimensions. Validate all variables at startup via `backend/config.py`:
```ini
# GitHub Ingress & Egress
GITHUB_WEBHOOK_SECRET=your_hmac_webhook_secret
GITHUB_APP_ID=your_app_id
GITHUB_PRIVATE_KEY_PATH=./certs/github-private-key.pem
GITHUB_TOKEN=ghp_fallback_personal_access_token

# Unified Data Spine (Tiger Cloud / TimescaleDB + pgvectorscale)
TIGER_DATABASE_URL=postgresql://user:password@host:port/tsdb?sslmode=require

# Queue & Checkpointing (Upstash / Redis)
REDIS_URL=rediss://default:password@host:port

# LLM Providers & Embeddings
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536

# Autonomy, Selectivity & Economics Guards
CONFIDENCE_THRESHOLD=0.75
DAILY_BUDGET_USD=10.00
NODE_TIMEOUT_SECONDS=30
```
