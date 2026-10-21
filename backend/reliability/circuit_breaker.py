"""Fault Isolation, Exponential Backoff Retries, Timeouts & Circuit Breaker (M7, INV-6)."""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(RuntimeError):
    """Raised when an external call is rejected because the circuit breaker is OPEN (INV-6)."""


class CircuitBreaker:
    """State-machine Circuit Breaker with timeout and exponential backoff retry (INV-6)."""

    def __init__(
        self,
        name: str = "external_io",
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        call_timeout_seconds: float = 10.0,
        max_retries: int = 1,
        base_backoff_seconds: float = 0.02,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout_seconds = float(recovery_timeout_seconds)
        self.call_timeout_seconds = float(call_timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self.base_backoff_seconds = float(base_backoff_seconds)

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if (time.monotonic() - self._opened_at) >= self.recovery_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = 0.0

    def _record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute async callable with timeout, exponential backoff retry, and circuit breaker protection."""
        current_state = self.state
        if current_state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"CircuitBreaker[{self.name}] is OPEN after {self._failure_count} consecutive failures"
            )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=self.call_timeout_seconds,
                )
                self._record_success()
                return result
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self.base_backoff_seconds * (2**attempt))

        self._record_failure()
        assert last_exc is not None
        raise last_exc
