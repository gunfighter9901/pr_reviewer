"""Redis queue index + dashboard trigger tests (fakeredis-free fakes)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from backend.config import Settings
from backend.models import QueuedPRJob
from backend.webhook.queue import (
    ArqWebhookQueue,
    read_index,
    update_index_entry,
    write_index_entry,
)


class FakeQueuePool:
    """Minimal async Redis surface used by the queue index + ARQ listing."""

    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.zsets: Dict[str, Dict[str, float]] = {}
        self.jobs: List[Dict[str, Any]] = []
        self.closed = False

    async def set(self, key: str, value: Any, *, nx: bool = False, ex: Optional[int] = None) -> Optional[bool]:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def enqueue_job(self, function: str, payload: Dict[str, Any], *, _job_id: Optional[str] = None) -> Any:
        self.jobs.append({"function": function, "payload": payload, "job_id": _job_id})
        return {"job_id": _job_id}

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def hset(self, name: str, key: str, value: str) -> None:
        self.hashes.setdefault(name, {})[key] = value

    async def hget(self, name: str, key: str) -> Optional[str]:
        return self.hashes.get(name, {}).get(key)

    async def hgetall(self, name: str) -> Dict[str, str]:
        return dict(self.hashes.get(name, {}))

    async def zrange(self, name: str, start: int, end: int) -> List[str]:
        ordered = sorted(self.zsets.get(name, {}).items(), key=lambda kv: kv[1])
        return [job_id for job_id, _ in ordered]

    async def aclose(self) -> None:
        self.closed = True


def _job(delivery_id: str = "deliv-q-1") -> QueuedPRJob:
    return QueuedPRJob(
        delivery_id=delivery_id,
        trace_id="tr-q-1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="abc123",
        action="opened",
        is_public=True,
        diff="",
    )


@pytest.mark.asyncio
async def test_enqueue_writes_dashboard_index() -> None:
    pool = FakeQueuePool()
    queue = ArqWebhookQueue(Settings(_env_file=None), pool=pool)
    assert await queue.enqueue_job(_job()) is True
    index = await read_index(pool)
    entry = index["pr-review:deliv-q-1"]
    assert entry["repo"] == "acme/demo"
    assert entry["pr_number"] == 7
    assert entry["status"] == "queued"


@pytest.mark.asyncio
async def test_index_update_and_corrupt_entry() -> None:
    pool = FakeQueuePool()
    await write_index_entry(pool, "j1", {"status": "queued"})
    updated = await update_index_entry(pool, "j1", status="done", findings_count=3)
    assert updated is not None and updated["status"] == "done"
    assert updated["findings_count"] == 3
    assert await update_index_entry(pool, "missing", status="done") is None
    pool.hashes[ArqWebhookQueue.INDEX_KEY]["bad"] = "not-json{{{"
    assert "bad" not in await read_index(pool)


@pytest.mark.asyncio
async def test_worker_task_transitions_index_to_done() -> None:
    from backend.orchestrator.engine import WorkflowContext, WorkflowState
    from backend.pipeline import ReviewSummary
    from backend.worker import process_pr_review_task

    pool = FakeQueuePool()
    await write_index_entry(pool, "pr-review:deliv-w-1", {"status": "queued"})

    async def fake_process(job: QueuedPRJob) -> ReviewSummary:  # type: ignore[no-untyped-def]
        return ReviewSummary(
            delivery_id=job.delivery_id,
            trace_id=job.trace_id,
            repo=job.repo,
            pr_number=job.pr_number,
            commit_sha="abc",
            status="completed",
            diff_source="webhook",
            findings_count=2,
            auto_posted=1,
            hitl_escalated=1,
            private_security_alerts=0,
        )

    class StubPipeline:
        async def process(self, job: QueuedPRJob, **kwargs: Any) -> ReviewSummary:  # type: ignore[no-untyped-def]
            return await fake_process(job)

    ctx = {"redis": pool, "job_id": "pr-review:deliv-w-1", "review_pipeline": StubPipeline()}
    out = await process_pr_review_task(ctx, _job("deliv-w-1").model_dump(mode="json"))
    assert out["findings_count"] == 2
    entry = (await read_index(pool))["pr-review:deliv-w-1"]
    assert entry["status"] == "done"
    assert entry["auto_posted"] == 1


def _api_app() -> Any:
    import os

    for key in [k for k in os.environ if k.startswith(("LLM_", "EMBEDDING_", "SPINE_", "QUEUE_", "REDIS_", "GITHUB_", "GEMINI_", "CONFIDENCE_"))]:
        os.environ.pop(key, None)
    from backend.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from backend.webhook.app import create_app

    settings = Settings(_env_file=None, SPINE_SQLITE_PATH=":memory:")
    app = create_app(settings=settings)
    pool = FakeQueuePool()
    pool.zsets[ArqWebhookQueue.ARQ_QUEUE_KEY] = {"pr-review:deliv-live-9": 1.0}
    app.state.queue_override = ArqWebhookQueue(settings, pool=pool)
    return TestClient(app), pool


def test_api_queue_lists_index_and_raw_arq_jobs() -> None:
    client, pool = _api_app()
    import asyncio

    async def seed() -> None:
        await write_index_entry(
            pool, "pr-review:deliv-old-1", {"repo": "acme/demo", "pr_number": 7, "status": "done", "enqueued_at": "2026-01-01T00:00:00"}
        )

    asyncio.run(seed())
    data = client.get("/api/queue").json()
    by_id = {item["job_id"]: item for item in data["items"]}
    assert by_id["pr-review:deliv-live-9"]["in_arq_queue"] is True
    assert by_id["pr-review:deliv-live-9"]["status"] == "queued"
    assert by_id["pr-review:deliv-old-1"]["status"] == "done"
    assert data["counts"]["queued"] >= 1


def test_api_queue_trigger_enqueues_and_dedupes() -> None:
    client, pool = _api_app()
    first = client.post("/api/queue/trigger", json={"repo": "acme/demo", "pr_number": 7, "delivery_id": "deliv-man-1"}).json()
    assert first["status"] == "queued"
    assert first["job_id"] == "pr-review:deliv-man-1"
    second = client.post("/api/queue/trigger", json={"repo": "acme/demo", "pr_number": 7, "delivery_id": "deliv-man-1"}).json()
    assert second["status"] == "duplicate"
    bad = client.post("/api/queue/trigger", json={"repo": "not-a-repo", "pr_number": 7})
    assert bad.status_code == 400
    queued = client.get("/api/queue").json()
    assert "pr-review:deliv-man-1" in {item["job_id"] for item in queued["items"]}
