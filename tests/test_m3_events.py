"""Milestone 3 Verification Tests: Event Spine & Observability (INV-4, INV-6)."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.db.spine import TigerDataSpine
from backend.observability.budget import BudgetExceededError, BudgetGuard
from backend.observability.events import EventSpineLogger
from backend.webhook.app import create_app
from backend.webhook.router import IdempotentWebhookRouter
from backend.webhook.security import compute_github_signature

TEST_SECRET = "m3_webhook_hmac_secret"


def _pr_payload() -> Dict[str, Any]:
    return {
        "action": "opened",
        "number": 108,
        "pull_request": {
            "number": 108,
            "title": "Refactor auth token validator",
            "head": {"sha": "99887766554433", "ref": "feat/auth-refactor"},
            "base": {"sha": "11223344556677", "ref": "main"},
        },
        "repository": {
            "full_name": "acme-corp/auth-service",
            "private": False,
        },
    }


@pytest.fixture()
def observability_env() -> tuple[TestClient, TigerDataSpine, EventSpineLogger, BudgetGuard]:
    settings = Settings(
        _env_file=None,
        GITHUB_WEBHOOK_SECRET=TEST_SECRET,
        DAILY_BUDGET_USD=1.00,
    )
    spine = TigerDataSpine(settings=settings)
    event_logger = EventSpineLogger(spine=spine)
    budget_guard = BudgetGuard(spine=spine, settings=settings, event_logger=event_logger)
    router = IdempotentWebhookRouter()
    app = create_app(
        settings=settings,
        router=router,
        event_logger=event_logger,
        budget_guard=budget_guard,
    )
    return TestClient(app), spine, event_logger, budget_guard


def test_m3_webhook_receipt_and_rejection_write_durable_events(
    observability_env: tuple[TestClient, TigerDataSpine, EventSpineLogger, BudgetGuard],
) -> None:
    """Send valid, duplicate, and unsigned webhooks -> verify all spans durably recorded in agent_events."""
    client, spine, event_logger, _ = observability_env

    # 1. Valid signed webhook
    raw_bytes = json.dumps(_pr_payload()).encode("utf-8")
    sig = compute_github_signature(raw_bytes, TEST_SECRET)
    resp1 = client.post(
        "/webhooks/github",
        content=raw_bytes,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": "deliv-m3-valid",
            "X-GitHub-Event": "pull_request",
            "X-Trace-Id": "tr-m3-trace-1",
        },
    )
    assert resp1.status_code == 202

    # 2. Duplicate delivery
    resp2 = client.post(
        "/webhooks/github",
        content=raw_bytes,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": "deliv-m3-valid",
            "X-GitHub-Event": "pull_request",
            "X-Trace-Id": "tr-m3-trace-1",
        },
    )
    assert resp2.status_code == 200

    # 3. Bad signature rejection
    resp3 = client.post(
        "/webhooks/github",
        content=raw_bytes,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Delivery": "deliv-m3-bad-sig",
            "X-GitHub-Event": "pull_request",
            "X-Trace-Id": "tr-m3-trace-bad",
        },
    )
    assert resp3.status_code == 403

    # Query agent_events for trace_id="tr-m3-trace-1"
    spans = event_logger.get_trace_timeline("tr-m3-trace-1")
    assert len(spans) == 2
    assert spans[0]["event_type"] == "webhook_queued"
    assert spans[0]["status"] == "ok"
    assert spans[1]["event_type"] == "webhook_duplicate"

    # Query rejected signature span
    rejected_spans = event_logger.get_trace_timeline("tr-m3-trace-bad")
    assert len(rejected_spans) == 1
    assert rejected_spans[0]["event_type"] == "webhook_rejected_signature"
    assert rejected_spans[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_m3_llm_spans_and_cost_ledger_continuous_aggregate(
    observability_env: tuple[TestClient, TigerDataSpine, EventSpineLogger, BudgetGuard],
) -> None:
    """Verify span context manager records token usage & cost ledger in hourly_pr_costs."""
    _, _, event_logger, _ = observability_env

    async with event_logger.span(
        trace_id="tr-m3-cost",
        delivery_id="deliv-m3-cost",
        agent_name="security",
        event_type="specialist_llm_call",
        model="claude-3-5-sonnet",
        prompt_version="security@v1.0.0",
    ) as sp:
        sp["tokens_in"] = 10_000
        sp["tokens_out"] = 2_000

    async with event_logger.span(
        trace_id="tr-m3-cost",
        delivery_id="deliv-m3-cost",
        agent_name="docs",
        event_type="specialist_llm_call",
        model="gpt-4o-mini",
        prompt_version="docs@v1.0.0",
    ) as sp:
        sp["tokens_in"] = 5_000
        sp["tokens_out"] = 1_000

    ledger = event_logger.get_cost_ledger("deliv-m3-cost")
    assert ledger["total_tokens_in"] == 15_000
    assert ledger["total_tokens_out"] == 3_000
    # claude-3-5-sonnet: 10k * $3/1M ($0.03) + 2k * $15/1M ($0.03) = $0.06
    # gpt-4o-mini: 5k * $0.15/1M ($0.00075) + 1k * $0.60/1M ($0.0006) = $0.00135
    assert ledger["total_cost_usd"] == pytest.approx(0.06135)


@pytest.mark.asyncio
async def test_m3_budget_guard_trips_circuit_breaker_when_daily_budget_exceeded(
    observability_env: tuple[TestClient, TigerDataSpine, EventSpineLogger, BudgetGuard],
) -> None:
    """Verify BudgetGuard trips circuit breaker and blocks ingress when Continuous Aggregate spend >= DAILY_BUDGET_USD."""
    client, _, event_logger, budget_guard = observability_env

    # Initially within $1.00 daily budget
    allowed, status = await budget_guard.check_budget()
    assert allowed is True
    assert status["circuit_breaker_tripped"] is False

    # Log a span costing $1.25 (exceeding the $1.00 daily limit)
    await event_logger.log_event(
        trace_id="tr-m3-expensive",
        delivery_id="deliv-m3-expensive",
        agent_name="security",
        event_type="specialist_llm_call",
        model="claude-3-5-sonnet",
        tokens_in=200_000,
        tokens_out=50_000,
        cost_usd=1.25,
        status="ok",
    )

    # Direct enforce_budget raises BudgetExceededError
    with pytest.raises(BudgetExceededError, match="Daily LLM budget exceeded"):
        await budget_guard.enforce_budget(
            trace_id="tr-m3-trip",
            delivery_id="deliv-m3-trip",
        )

    # Ingress endpoint now returns 429 budget_exceeded when new webhook arrives
    raw_bytes = json.dumps(_pr_payload()).encode("utf-8")
    sig = compute_github_signature(raw_bytes, TEST_SECRET)
    resp = client.post(
        "/webhooks/github",
        content=raw_bytes,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": "deliv-m3-blocked-by-budget",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.status_code == 429
    assert resp.json()["status"] == "budget_exceeded"
