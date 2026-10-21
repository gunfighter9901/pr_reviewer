"""Real-Time LLM Token Budget Guard backed by Continuous Aggregates (M3, INV-6)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from backend.config import Settings, get_settings
from backend.db.spine import TigerDataSpine
from backend.observability.events import EventSpineLogger


class BudgetExceededError(RuntimeError):
    """Raised when daily LLM token spend exceeds DAILY_BUDGET_USD and trips the circuit breaker."""


class BudgetGuard:
    """Reads the hourly_pr_costs Continuous Aggregate to enforce daily LLM spend limits."""

    def __init__(
        self,
        spine: TigerDataSpine,
        settings: Optional[Settings] = None,
        event_logger: Optional[EventSpineLogger] = None,
    ) -> None:
        self.spine = spine
        self.settings = settings or get_settings()
        self.event_logger = event_logger
        self._tripped: bool = False

    @property
    def daily_limit_usd(self) -> float:
        return float(self.settings.DAILY_BUDGET_USD)

    async def check_budget(self) -> Tuple[bool, Dict[str, Any]]:
        """Query Continuous Aggregate hourly_pr_costs and return (allowed, budget_telemetry)."""
        current_spend = self.spine.get_daily_spend_usd()
        limit = self.daily_limit_usd
        utilization = (current_spend / limit) if limit > 0 else 1.0
        allowed = current_spend < limit
        self._tripped = not allowed
        status = {
            "allowed": allowed,
            "circuit_breaker_tripped": self._tripped,
            "current_daily_spend_usd": round(current_spend, 6),
            "daily_budget_usd": round(limit, 6),
            "remaining_budget_usd": round(max(0.0, limit - current_spend), 6),
            "utilization_ratio": round(utilization, 4),
        }
        return allowed, status

    async def enforce_budget(
        self,
        *,
        trace_id: str,
        delivery_id: str,
        agent_name: str = "budget_guard",
    ) -> Dict[str, Any]:
        """Raise BudgetExceededError and emit an immutable event if daily spend >= DAILY_BUDGET_USD."""
        allowed, status = await self.check_budget()
        if not allowed:
            if self.event_logger is not None:
                await self.event_logger.log_event(
                    trace_id=trace_id,
                    delivery_id=delivery_id,
                    agent_name=agent_name,
                    event_type="budget_circuit_breaker_tripped",
                    status="rejected",
                    metadata=status,
                )
            raise BudgetExceededError(
                f"Daily LLM budget exceeded: ${status['current_daily_spend_usd']:.4f} "
                f">= limit ${status['daily_budget_usd']:.2f}"
            )
        return status
