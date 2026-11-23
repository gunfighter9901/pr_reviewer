"""Idempotent webhook router and queue dispatcher (INV-2: Idempotent Ingress)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from backend.models import PullRequestWebhookPayload, QueuedPRJob

ALLOWED_PR_ACTIONS: Set[str] = {"opened", "synchronize", "reopened"}


@dataclass
class RouteResult:
    status: str  # "queued" | "duplicate" | "ignored"
    http_status: int
    delivery_id: str
    trace_id: str
    job: Optional[QueuedPRJob] = None
    reason: Optional[str] = None


class IdempotentWebhookRouter:
    """Handles atomic delivery ID deduplication (INV-2), event filtering, and job enqueueing."""

    def __init__(
        self,
        enqueue_callback: Optional[
            Callable[[QueuedPRJob], Awaitable[Optional[bool]]]
        ] = None,
    ) -> None:
        self._seen_deliveries: Set[str] = set()
        self._queued_jobs: List[QueuedPRJob] = []
        self._lock = asyncio.Lock()
        self._enqueue_callback = enqueue_callback

    @property
    def queued_jobs(self) -> List[QueuedPRJob]:
        """Return list of jobs enqueued during this router's lifecycle."""
        return list(self._queued_jobs)

    @property
    def seen_deliveries(self) -> Set[str]:
        return set(self._seen_deliveries)

    def reset(self) -> None:
        """Reset in-memory state (used in isolated tests)."""
        self._seen_deliveries.clear()
        self._queued_jobs.clear()

    async def is_duplicate_or_mark(self, delivery_id: str) -> bool:
        """Atomically check if delivery_id was already seen; if not, mark it as seen (INV-2)."""
        async with self._lock:
            if delivery_id in self._seen_deliveries:
                return True
            self._seen_deliveries.add(delivery_id)
            return False

    async def route_webhook(
        self,
        delivery_id: str,
        github_event: str,
        raw_payload: Dict[str, Any],
        trace_id: Optional[str] = None,
    ) -> RouteResult:
        """Filter, deduplicate, validate, and enqueue a verified webhook payload."""
        resolved_trace_id = trace_id or f"tr-{uuid.uuid4().hex[:16]}"

        # INV-2: Atomic deduplication on X-GitHub-Delivery before doing any downstream work
        if await self.is_duplicate_or_mark(delivery_id):
            return RouteResult(
                status="duplicate",
                http_status=200,
                delivery_id=delivery_id,
                trace_id=resolved_trace_id,
                reason="Duplicate X-GitHub-Delivery dropped idempotently",
            )

        # Filter strictly for pull_request events
        if github_event != "pull_request":
            return RouteResult(
                status="ignored",
                http_status=202,
                delivery_id=delivery_id,
                trace_id=resolved_trace_id,
                reason=f"Event '{github_event}' is not 'pull_request'",
            )

        action = raw_payload.get("action")
        if action not in ALLOWED_PR_ACTIONS:
            return RouteResult(
                status="ignored",
                http_status=202,
                delivery_id=delivery_id,
                trace_id=resolved_trace_id,
                reason=f"Action '{action}' not in {sorted(ALLOWED_PR_ACTIONS)}",
            )

        # Validate pull_request payload structure
        parsed = PullRequestWebhookPayload.model_validate(raw_payload)
        job = QueuedPRJob(
            delivery_id=delivery_id,
            trace_id=resolved_trace_id,
            repo=parsed.repository.full_name,
            pr_number=parsed.pr_number,
            commit_sha=parsed.pull_request.head.sha,
            action=parsed.action,
            is_public=not parsed.repository.private,
            diff=parsed.diff or "",
            changed_files=parsed.changed_files or [],
        )

        self._queued_jobs.append(job)
        if self._enqueue_callback is not None:
            enqueued = await self._enqueue_callback(job)
            if enqueued is False:
                self._queued_jobs.pop()
                return RouteResult(
                    status="duplicate",
                    http_status=200,
                    delivery_id=delivery_id,
                    trace_id=resolved_trace_id,
                    reason="Queue rejected duplicate X-GitHub-Delivery",
                )

        return RouteResult(
            status="queued",
            http_status=202,
            delivery_id=delivery_id,
            trace_id=resolved_trace_id,
            job=job,
        )
