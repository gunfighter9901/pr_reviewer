"""FastAPI Stateless Webhook Receiver (M1: Webhook Ingress Contract)."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.github.client import is_real_github_token
from backend.webhook.queue import ArqWebhookQueue
from backend.webhook.api import register_api_routes
from backend.webhook.router import IdempotentWebhookRouter
from backend.webhook.security import WebhookSignatureError, verify_github_signature


def create_app(
    settings: Optional[Settings] = None,
    router: Optional[IdempotentWebhookRouter] = None,
    event_logger: Optional[Any] = None,
    budget_guard: Optional[Any] = None,
) -> FastAPI:
    """Create and configure the FastAPI webhook ingress application."""
    resolved_settings = settings or get_settings()
    resolved_router = router

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        queue: Optional[ArqWebhookQueue] = None
        if resolved_router is None and resolved_settings.QUEUE_BACKEND == "redis":
            queue = ArqWebhookQueue(resolved_settings)
            await queue.connect()
            application.state.webhook_router = IdempotentWebhookRouter(
                enqueue_callback=queue.enqueue_job
            )
        try:
            yield
        finally:
            if queue is not None:
                await queue.close()

    application = FastAPI(
        title="Production Multi-Agent PR Reviewer — Stateless Ingress",
        version="1.0.0",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.state.settings = resolved_settings
    application.state.webhook_router = resolved_router or IdempotentWebhookRouter()
    application.state.event_logger = event_logger
    application.state.budget_guard = budget_guard
    from backend.db.spine import TigerDataSpine as _Spine

    application.state.spine = _Spine(settings=resolved_settings)
    register_api_routes(application)

    @application.get("/health")
    async def health_check() -> dict[str, Any]:
        return {
            "status": "ok",
            "queue_backend": resolved_settings.QUEUE_BACKEND,
            "github_token_configured": is_real_github_token(
                resolved_settings.GITHUB_TOKEN
            ),
        }

    @application.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        start_ns = time.perf_counter_ns()
        cfg: Settings = request.app.state.settings
        wh_router: IdempotentWebhookRouter = request.app.state.webhook_router
        ev_logger = request.app.state.event_logger
        b_guard = request.app.state.budget_guard

        signature_header = request.headers.get("X-Hub-Signature-256")
        delivery_id = (request.headers.get("X-GitHub-Delivery") or "").strip()
        github_event = (request.headers.get("X-GitHub-Event") or "pull_request").strip()
        trace_id = (request.headers.get("X-Trace-Id") or f"tr-{uuid.uuid4().hex[:16]}").strip()

        # INV-1: Read raw request bytes and verify HMAC-SHA256 BEFORE parsing JSON
        raw_body: bytes = await request.body()
        try:
            verify_github_signature(
                raw_body=raw_body,
                signature_header=signature_header,
                secret=cfg.GITHUB_WEBHOOK_SECRET,
            )
        except WebhookSignatureError as exc:
            latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
            if ev_logger is not None:
                await ev_logger.log_event(
                    trace_id=trace_id,
                    delivery_id=delivery_id or "missing-delivery-id",
                    agent_name="ingress",
                    event_type="webhook_rejected_signature",
                    status="rejected",
                    latency_ms=latency_ms,
                    metadata={"reason": exc.message, "http_status": exc.status_code},
                )
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.message, "status": "unauthorized"},
            )

        if not delivery_id:
            latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
            if ev_logger is not None:
                await ev_logger.log_event(
                    trace_id=trace_id,
                    delivery_id="missing-delivery-id",
                    agent_name="ingress",
                    event_type="webhook_rejected_missing_delivery",
                    status="error",
                    latency_ms=latency_ms,
                    metadata={"reason": "Missing X-GitHub-Delivery header"},
                )
            return JSONResponse(
                status_code=400,
                content={"error": "Missing X-GitHub-Delivery header", "status": "bad_request"},
            )

        # INV-1: Malformed signed JSON must return 400 Bad Request (never 500)
        try:
            decoded_text = raw_body.decode("utf-8")
            payload = json.loads(decoded_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
            if ev_logger is not None:
                await ev_logger.log_event(
                    trace_id=trace_id,
                    delivery_id=delivery_id,
                    agent_name="ingress",
                    event_type="webhook_rejected_malformed_json",
                    status="error",
                    latency_ms=latency_ms,
                    metadata={"reason": str(exc)},
                )
            return JSONResponse(
                status_code=400,
                content={"error": "Malformed JSON payload", "status": "bad_request"},
            )

        if not isinstance(payload, dict):
            latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
            if ev_logger is not None:
                await ev_logger.log_event(
                    trace_id=trace_id,
                    delivery_id=delivery_id,
                    agent_name="ingress",
                    event_type="webhook_rejected_non_object_json",
                    status="error",
                    latency_ms=latency_ms,
                    metadata={"reason": "JSON root must be an object"},
                )
            return JSONResponse(
                status_code=400,
                content={"error": "JSON payload must be an object", "status": "bad_request"},
            )

        # Optional M3 Budget Guard check before enqueueing LLM work
        if b_guard is not None:
            allowed, budget_status = await b_guard.check_budget()
            if not allowed:
                latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
                if ev_logger is not None:
                    await ev_logger.log_event(
                        trace_id=trace_id,
                        delivery_id=delivery_id,
                        agent_name="ingress",
                        event_type="budget_circuit_breaker_tripped",
                        status="rejected",
                        latency_ms=latency_ms,
                        metadata=budget_status,
                    )
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "Daily LLM budget exceeded; circuit breaker open",
                        "status": "budget_exceeded",
                        "budget": budget_status,
                    },
                )

        # Route & deduplicate (INV-2)
        try:
            result = await wh_router.route_webhook(
                delivery_id=delivery_id,
                github_event=github_event,
                raw_payload=payload,
                trace_id=trace_id,
            )
        except ValidationError as exc:
            latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
            if ev_logger is not None:
                await ev_logger.log_event(
                    trace_id=trace_id,
                    delivery_id=delivery_id,
                    agent_name="ingress",
                    event_type="webhook_rejected_schema",
                    status="error",
                    latency_ms=latency_ms,
                    metadata={"errors": exc.errors()},
                )
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Invalid pull_request webhook schema",
                    "details": exc.errors(),
                    "status": "bad_request",
                },
            )

        latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
        if ev_logger is not None:
            await ev_logger.log_event(
                trace_id=result.trace_id,
                delivery_id=delivery_id,
                agent_name="ingress",
                event_type=f"webhook_{result.status}",
                status="ok",
                latency_ms=latency_ms,
                metadata={
                    "github_event": github_event,
                    "action": payload.get("action"),
                    "reason": result.reason,
                },
            )

        return JSONResponse(
            status_code=result.http_status,
            content={
                "status": result.status,
                "delivery_id": result.delivery_id,
                "trace_id": result.trace_id,
                "reason": result.reason,
            },
        )

    return application


app = create_app()
