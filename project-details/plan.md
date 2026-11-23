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
