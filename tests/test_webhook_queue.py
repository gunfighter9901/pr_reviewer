"""Webhook -> ARQ queue handoff tests without a live Redis server."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from backend.config import Settings
from backend.models import QueuedPRJob
from backend.webhook.queue import ArqWebhookQueue, WebhookQueueError
from backend.webhook.router import IdempotentWebhookRouter


class FakePool:
    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}
        self.jobs: List[Dict[str, Any]] = []
        self.fail_next = False
        self.closed = False

    async def set(
        self, key: str, value: Any, *, nx: bool = False, ex: Optional[int] = None
    ) -> Optional[bool]:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def enqueue_job(
        self, function: str, payload: Dict[str, Any], *, _job_id: Optional[str] = None
    ) -> Any:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("redis write failed")
        self.jobs.append({"function": function, "payload": payload, "job_id": _job_id})
        return {"job_id": _job_id}

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def aclose(self) -> None:
        self.closed = True


def _job(delivery_id: str = "deliv-queue-1") -> QueuedPRJob:
    return QueuedPRJob(
        delivery_id=delivery_id,
        trace_id="tr-queue-1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="abc123",
        action="opened",
        is_public=True,
        diff="",
    )


def _payload(action: str = "opened") -> Dict[str, Any]:
    return {
        "action": action,
        "number": 7,
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
        "repository": {"full_name": "acme/demo", "private": False},
    }


@pytest.mark.asyncio
async def test_arq_queue_deduplicates_and_enqueues_worker_task() -> None:
    pool = FakePool()
    queue = ArqWebhookQueue(Settings(_env_file=None), pool=pool)

    assert await queue.enqueue_job(_job()) is True
    assert await queue.enqueue_job(_job()) is False

    assert len(pool.jobs) == 1
    assert pool.jobs[0]["function"] == "process_pr_review_task"
    assert pool.jobs[0]["payload"]["delivery_id"] == "deliv-queue-1"
    assert pool.jobs[0]["job_id"] == "pr-review:deliv-queue-1"
    assert "pr_reviewer:dedup:delivery:deliv-queue-1" in pool.store


@pytest.mark.asyncio
async def test_arq_queue_clears_dedup_marker_when_enqueue_fails() -> None:
    pool = FakePool()
    queue = ArqWebhookQueue(Settings(_env_file=None), pool=pool)
    pool.fail_next = True

    with pytest.raises(WebhookQueueError):
        await queue.enqueue_job(_job())

    assert "pr_reviewer:dedup:delivery:deliv-queue-1" not in pool.store
    assert await queue.enqueue_job(_job()) is True


@pytest.mark.asyncio
async def test_router_reports_duplicate_when_queue_rejects_delivery() -> None:
    pool = FakePool()
    queue = ArqWebhookQueue(Settings(_env_file=None), pool=pool)
    router = IdempotentWebhookRouter(enqueue_callback=queue.enqueue_job)

    first = await router.route_webhook(
        delivery_id="deliv-router-1",
        github_event="pull_request",
        raw_payload=_payload(),
    )
    second = await router.route_webhook(
        delivery_id="deliv-router-1",
        github_event="pull_request",
        raw_payload=_payload(),
    )

    assert first.status == "queued"
    assert second.status == "duplicate"
    assert len(router.queued_jobs) == 1
    assert len(pool.jobs) == 1


@pytest.mark.asyncio
async def test_router_handles_queue_level_duplicate_after_restart() -> None:
    pool = FakePool()
    queue = ArqWebhookQueue(Settings(_env_file=None), pool=pool)
    router = IdempotentWebhookRouter(enqueue_callback=queue.enqueue_job)

    # Simulate a delivery already claimed by a previous ingress process.
    pool.store["pr_reviewer:dedup:delivery:deliv-router-2"] = b"1"
    result = await router.route_webhook(
        delivery_id="deliv-router-2",
        github_event="pull_request",
        raw_payload=_payload(),
    )

    assert result.status == "duplicate"
    assert router.queued_jobs == []
    assert pool.jobs == []
