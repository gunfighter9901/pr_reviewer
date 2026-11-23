# Multi-Agent PR Reviewer

**Clear code reviews, without the noise.**

Multi-Agent PR Reviewer helps teams spend less time and effort on code reviews. It takes on the repetitive cognitive work — reading changes, gathering context, checking patterns, and spotting likely issues — so reviewers can focus on the judgment calls that need human experience. It shares only clear, code-backed feedback, and hands uncertain or sensitive findings to a person.

The result is faster reviews, less reviewer fatigue, and responsible automation that keeps people in control.

**Less noise. More signal. Human judgment where it matters.**

## How It Works

1. **It notices a new code change.** When a developer opens or updates a pull request on GitHub, the system receives a secure notification and starts reviewing right away. Duplicate notifications are ignored.
2. **It gathers the surrounding context.** Before commenting, it looks at related code, past review decisions, and the team's own conventions. This helps it understand whether a change fits the project.
3. **It takes on the repetitive checks.** Security, code quality, testing, and documentation specialists examine the change at the same time — like a small review team working together.
4. **It keeps only the strongest feedback.** Overlapping comments are combined, feedback that cannot be tied to the code is filtered out, and every remaining comment points to the exact file and line it refers to.
5. **It sends feedback to the right place.** Clear, well-supported comments are posted on the pull request. Uncertain findings go to a human review queue. Critical security issues are never posted publicly and are sent privately to the team.
6. **It improves while staying in control.** Human decisions help improve future reviews. Every action is recorded, and a spending limit keeps costs predictable.

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

#### Optional: Live Gemini reviews

The default path is deterministic and offline. To run specialist reviews through
Gemini, put a Google AI Studio key in `.env` and switch the provider:

```ini
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_google_ai_studio_key
GEMINI_MODEL_HIGH=gemini-3.6-flash
GEMINI_MODEL_FAST=gemini-3.5-flash-lite
```

The worker wires the live provider via `backend/agents/llm.py`. Tests and golden
evals keep using the deterministic specialists so results stay reproducible.
Gemini responses are schema-constrained and every cited file, line, and evidence
snippet is checked against the added diff lines before a finding is accepted.

#### Optional: Gemini Embedding 2

Embeddings are deterministic by default. To use Gemini Embedding 2 for semantic
memory, add:

```ini
EMBEDDING_PROVIDER=gemini
EMBEDDING_MODEL=gemini-embedding-2
EMBEDDING_DIM=1536
```

The adapter requests `output_dimensionality=1536` to match the committed
`vector(1536)` schema, uses `RETRIEVAL_DOCUMENT` for indexed chunks and
`RETRIEVAL_QUERY` for search queries, and raises on any dimension mismatch.

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
