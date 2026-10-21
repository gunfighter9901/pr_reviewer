# Multi-Agent PR Reviewer

An autonomous code review system built to save senior engineers time. Instead of leaving noisy, generic comments on every line of a pull request, it focuses on **selectivity over coverage**—only posting high-confidence findings backed by exact code snippets, and routing ambiguous or sensitive issues to a human review queue.

## How It Works

1. **Webhook Ingress (`backend/webhook/`)** — Receives GitHub `pull_request` events via FastAPI, verifies the HMAC-SHA256 signature on raw bytes before parsing JSON, drops duplicate deliveries, and queues the job in under 200ms.
2. **Context Retrieval (`backend/retrieval/`)** — Pulls relevant context from the repository before reviewing:
   - **Semantic & Lexical Search:** Hybrid vector similarity (`pgvectorscale` DiskANN) and full-text search (`tsvector`) to find impacted callers and related functions.
   - **Episodic Memory:** Past reviewer decisions and disputed findings on the touched files.
   - **Procedural Memory:** Team conventions and Architecture Decision Records (ADRs).
3. **Parallel Specialist Agents (`backend/orchestrator/`, `backend/agents/`)** — Fans out via LangGraph (`WorkflowEngine`) to four isolated specialists running concurrently with strict timeouts:
   - **Security:** SQL injection, command/code execution, leaked secrets, unsafe deserialization.
   - **Quality:** Swallowed exceptions, mutable defaults, event-loop blocking, null-safety hazards.
   - **Testing:** Untested public functions and tautological assertions.
   - **Docs:** Missing docstrings and untracked `TODO`/`FIXME` debt.
4. **Deduplication & HITL Gate (`backend/gates/`, `backend/github/`)** — Merges overlapping findings on the same file and line, checks confidence against `CONFIDENCE_THRESHOLD` (`0.75`), and routes each finding:
   - High-confidence, non-critical findings are posted directly to the PR.
   - Low-confidence findings go to the Human-in-the-Loop (HITL) approval queue.
   - **Critical security vulnerabilities** on public repos are never posted publicly on the PR thread; they are routed privately to the security/HITL queue.
5. **Immutable Audit & Cost Guard (`backend/db/`, `backend/observability/`)** — Every webhook, agent run, and routing decision is logged to an append-only `agent_events` hypertable (protected against `UPDATE`, `DELETE`, and `TRUNCATE` by SQL triggers) with a daily token budget circuit breaker.

## Project Layout

```text
├── backend/
│   ├── webhook/         # FastAPI webhook receiver, HMAC security, idempotent router
│   ├── db/              # PostgreSQL/TimescaleDB/pgvector migrations & data spine
│   ├── observability/   # Append-only event logger & daily budget circuit breaker
│   ├── orchestrator/    # Abstract WorkflowEngine & LangGraph adapter
│   ├── agents/          # Security, Quality, Testing, Docs agents & aggregator
│   ├── prompts/         # Versioned prompt registry
│   ├── retrieval/       # AST code chunker & hybrid vector + FTS retriever
│   ├── gates/           # Confidence/severity HITL routing & feedback decay
│   ├── github/          # Idempotent GitHub PR comment poster
│   ├── reliability/     # Circuit breaker, retries, and timeouts
│   └── worker.py        # Redis / ARQ background worker
├── frontend/            # Next.js dashboard (HITL queue, trace viewer, cost metrics)
├── evals/               # Golden PR dataset & regression evaluation runner
└── tests/               # Unit, integration, and fault-injection test suites (M1–M8)
```

## Quick Start

### 1. Backend & Tests

Install Python dependencies and configure your environment:

```bash
cp .env.example .env
pip install -r requirements.txt
```

Start the FastAPI webhook server locally:

```bash
uvicorn backend.webhook.app:app --reload --port 8000
```

Run the test suite and golden dataset evaluations:

```bash
pytest -v
python evals/run_evals.py
```

### 2. Frontend Dashboard

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000` to view the HITL Approval Queue, End-to-End Trace Viewer, and LLM Cost Dashboard.

### 3. Docker Compose (Full Stack)

```bash
docker compose up --build
```
