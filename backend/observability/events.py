"""Immutable Event Spine Logger & OpenTelemetry Span Bridge (M3, INV-4)."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from opentelemetry import trace

from backend.db.spine import TigerDataSpine

# Per-1M token pricing (USD) for tiered model routing
MODEL_PRICING_PER_1M: Dict[str, tuple[float, float]] = {
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "text-embedding-3-small": (0.02, 0.00),
    "text-embedding-3-large": (0.13, 0.00),
    "system": (0.0, 0.0),
}


def estimate_llm_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Compute exact USD cost from token counts and model tier."""
    in_rate, out_rate = MODEL_PRICING_PER_1M.get(model, (1.00, 3.00))
    cost = (max(0, tokens_in) / 1_000_000.0) * in_rate + (
        max(0, tokens_out) / 1_000_000.0
    ) * out_rate
    return round(cost, 8)


class EventSpineLogger:
    """Writes every webhook receipt, rejection, LLM call, retrieval query, and gate decision to agent_events."""

    def __init__(self, spine: TigerDataSpine, service_name: str = "pr-reviewer") -> None:
        self.spine = spine
        self.tracer = trace.get_tracer(service_name)

    async def log_event(
        self,
        *,
        trace_id: str,
        delivery_id: str,
        agent_name: str,
        event_type: str,
        status: str = "ok",
        model: str = "system",
        prompt_version: str = "v1.0.0",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: Optional[float] = None,
        latency_ms: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        event_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Durably write an event span to the append-only agent_events hypertable (INV-4)."""
        resolved_cost = (
            float(cost_usd)
            if cost_usd is not None
            else estimate_llm_cost_usd(model, tokens_in, tokens_out)
        )
        with self.tracer.start_as_current_span(f"{agent_name}.{event_type}") as otel_span:
            otel_span.set_attribute("pr_reviewer.trace_id", trace_id)
            otel_span.set_attribute("pr_reviewer.delivery_id", delivery_id)
            otel_span.set_attribute("pr_reviewer.agent_name", agent_name)
            otel_span.set_attribute("pr_reviewer.event_type", event_type)
            otel_span.set_attribute("pr_reviewer.model", model)
            otel_span.set_attribute("pr_reviewer.tokens_in", tokens_in)
            otel_span.set_attribute("pr_reviewer.tokens_out", tokens_out)
            otel_span.set_attribute("pr_reviewer.cost_usd", resolved_cost)
            otel_span.set_attribute("pr_reviewer.status", status)

            return self.spine.insert_agent_event(
                trace_id=trace_id,
                delivery_id=delivery_id,
                agent_name=agent_name,
                event_type=event_type,
                status=status,
                model=model,
                prompt_version=prompt_version,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=resolved_cost,
                latency_ms=latency_ms,
                metadata=metadata or {},
                event_time=event_time or datetime.now(timezone.utc),
            )

    @asynccontextmanager
    async def span(
        self,
        *,
        trace_id: str,
        delivery_id: str,
        agent_name: str,
        event_type: str,
        model: str = "system",
        prompt_version: str = "v1.0.0",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Async context manager that measures latency and records an immutable span on exit."""
        start_ns = time.perf_counter_ns()
        span_ctx: Dict[str, Any] = {
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": None,
            "status": "ok",
            "metadata": dict(metadata or {}),
        }
        try:
            yield span_ctx
        except Exception as exc:
            span_ctx["status"] = "error"
            span_ctx["metadata"]["error"] = str(exc)
            raise
        finally:
            latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))
            await self.log_event(
                trace_id=trace_id,
                delivery_id=delivery_id,
                agent_name=agent_name,
                event_type=event_type,
                status=span_ctx["status"],
                model=model,
                prompt_version=prompt_version,
                tokens_in=int(span_ctx.get("tokens_in", 0)),
                tokens_out=int(span_ctx.get("tokens_out", 0)),
                cost_usd=span_ctx.get("cost_usd"),
                latency_ms=latency_ms,
                metadata=span_ctx["metadata"],
            )

    def get_trace_timeline(self, trace_id: str) -> List[Dict[str, Any]]:
        return self.spine.query_agent_events(trace_id=trace_id)

    def get_delivery_events(self, delivery_id: str) -> List[Dict[str, Any]]:
        return self.spine.query_agent_events(delivery_id=delivery_id)

    def get_cost_ledger(self, delivery_id: Optional[str] = None) -> Dict[str, Any]:
        hourly = self.spine.get_hourly_pr_costs(delivery_id=delivery_id)
        total_cost = sum(float(r.get("total_cost_usd", 0.0)) for r in hourly)
        total_tokens_in = sum(int(r.get("total_tokens_in", 0)) for r in hourly)
        total_tokens_out = sum(int(r.get("total_tokens_out", 0)) for r in hourly)
        return {
            "delivery_id": delivery_id,
            "total_cost_usd": round(total_cost, 8),
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "buckets": hourly,
        }
