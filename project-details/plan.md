# project-details/plan.md — Master Milestone Plan (M1..M8)

## Overview
This plan describes the end-to-end implementation of the **Production Multi-Agent PR Reviewer** and records verification for each milestone.

---

## Milestone Breakdown

### M1: Webhook Ingress Contract
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `backend/config.py`, `backend/models.py`, `backend/webhook/security.py`, `backend/webhook/router.py`, `backend/webhook/app.py`, `tests/test_m1_webhook.py`
- **Invariants Covered:** `INV-1`, `INV-2`, `INV-5`
- **Verification Command:** `pytest tests/test_m1_webhook.py -v` (7/7 PASSED)

---

### M2: Unified Tiger Data Spine
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `backend/db/migrations/001_init.sql`, `backend/db/spine.py`, `tests/test_m2_spine.py`
- **Invariants Covered:** `INV-4`, `INV-5`
- **Verification Command:** `pytest tests/test_m2_spine.py -v` (5/5 PASSED)

---

### M3: Event Spine & Observability
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `backend/observability/events.py`, `backend/observability/budget.py`, `tests/test_m3_events.py`
- **Invariants Covered:** `INV-4`, `INV-6`
- **Verification Command:** `pytest tests/test_m3_events.py -v` (3/3 PASSED)

---

### M4: Redis Queue & LangGraph Orchestrator
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `backend/orchestrator/engine.py`, `backend/orchestrator/langgraph_adapter.py`, `backend/worker.py`, `tests/test_m4_orchestrator.py`
- **Invariants Covered:** `INV-2`, `INV-3`, `INV-6`
- **Verification Command:** `pytest tests/test_m4_orchestrator.py -v` (5/5 PASSED)

---

### M5: Specialist Agents & Aggregator
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `backend/prompts/registry.py`, `backend/agents/specialists.py`, `backend/agents/aggregator.py`, `tests/test_m5_agents.py`
- **Invariants Covered:** `INV-3`, `INV-6`, `INV-7`
- **Verification Command:** `pytest tests/test_m5_agents.py -v` (3/3 PASSED)

---

### M6: Hybrid RAG & Context Engineering
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `backend/retrieval/indexer.py`, `backend/retrieval/hybrid_search.py`, `tests/test_m6_retrieval.py`
- **Invariants Covered:** `INV-5`, `INV-6`
- **Verification Command:** `pytest tests/test_m6_retrieval.py -v` (1/1 PASSED)

---

### M7: HITL Gate, GitHub Poster & Reliability
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `backend/gates/hitl.py`, `backend/github/poster.py`, `backend/reliability/circuit_breaker.py`, `tests/test_m7_hitl.py`
- **Invariants Covered:** `INV-6`, `INV-7`
- **Verification Command:** `pytest tests/test_m7_hitl.py -v` (3/3 PASSED)

---

### M8: Operational Dashboard & Golden Evals
- **Status:** `[DONE]`
- **Touched File Boundaries:**
  - `frontend/`, `evals/golden_dataset.json`, `evals/run_evals.py`, `tests/test_m8_evals.py`
- **Invariants Covered:** `INV-1`..`INV-7`
- **Verification Command:** `pytest && python evals/run_evals.py && npm --prefix frontend run build` (ALL PASSED)

---

## Post-M8 Extensions (all `[DONE]`)

### Single-PR Summary Review
- **Touched File Boundaries:**
  - `backend/agents/aggregator.py` (risk-ordered plain-language summarizer + markdown renderer), `backend/github/client.py` (`post_issue_comment`, `GitHubSummaryTransport`), `backend/github/poster.py` (`post_pr_summary_comment`), `backend/pipeline.py` (summary always computed, single-comment mode via `POST_PR_SUMMARY_COMMENT`), `backend/cli.py` (`--post-summary`), `backend/config.py`, `tests/test_pr_summary.py`
- **Invariants Covered:** `INV-7` (grounding preserved; critical details never posted publicly)
- **Verification Command:** `pytest tests/test_pr_summary.py -v` (9/9 PASSED)

### Live Dashboard API + PR-Grouped View
- **Touched File Boundaries:**
  - `backend/webhook/api.py` (`GET /api/stats|hitl|pr-summaries|reviews|traces`, `POST /api/hitl/{id}/decision`), `backend/webhook/app.py` (shared spine singleton), `frontend/src/app/page.tsx` (no demo data)
- **Verification Command:** `npm --prefix frontend run build` + live `curl` against `:8000` (ALL PASSED)

### Redis Queue Control
- **Touched File Boundaries:**
  - `backend/webhook/queue.py` (dashboard index, ownership-safe `close()`), `backend/worker.py` (queued → processing → done/failed transitions), `backend/webhook/api.py` (`GET /api/queue`, `POST /api/queue/trigger`), `tests/test_queue_api.py`
- **Invariants Covered:** `INV-2` (manual triggers share webhook dedup path)
- **Verification Command:** `pytest tests/test_queue_api.py -v` (5/5 PASSED)

### SummarizerAgent Verdict Flow
- **Touched File Boundaries:**
  - `backend/agents/summarizer.py` (5th balanced agent: specialist summaries → own verdict with suggestions), `backend/agents/specialists.py` (`SpecialistSummary`, 5-line prompt directives, prompt versions `security/quality@v1.3.0` `testing/docs@v1.2.0`), `backend/prompts/registry.py` (`summarizer@v1.0.0`), `backend/pipeline.py` (every comment body built through the agent on findings, HITL/private-only, and clean paths), `backend/worker.py` (wires agent with live router), `tests/test_pr_summary.py`, `tests/test_review_pipeline.py`
- **Invariants Covered:** `INV-7` (summaries carry grounded counts; the verdict adds no new claims)
- **Verification Command:** `pytest tests/test_pr_summary.py tests/test_review_pipeline.py -q` (all PASSED) + live dry-run on `chrome_video_scraper#3` showing `Sub-agent analysis` + `Aggregator verdict`

### Clean-Comment + Dashboard Auto-Post Defaults
- **Touched File Boundaries:**
  - `backend/config.py` (`POST_CLEAN_COMMENT=true` default), `backend/github/poster.py` (summary poster accepts zero findings), `backend/webhook/api.py` (`auto_post: true` trigger default, stored as `metadata.post_comments`), `frontend/src/app/page.tsx` (Post-to-GitHub checkbox, default on), `.env.example`
- **Verification Command:** live `review` on `chrome_video_scraper#3` and `#5` posted all-clear comments (`pull/3#issuecomment-7111430397`, `pull/5#issuecomment-7111152221`)
