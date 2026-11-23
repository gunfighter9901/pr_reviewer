"""Redis + ARQ Background Worker Pool and Queue Integration (M4, INV-2, INV-3)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import fakeredis.aioredis

from backend.agents.llm import build_specialist_runner
from backend.config import Settings, get_settings
from backend.models import QueuedPRJob
from backend.orchestrator.engine import WorkflowContext, WorkflowEngine, WorkflowState
from backend.orchestrator.langgraph_adapter import LangGraphEngine, RedisCheckpointStore


def create_redis_client(settings: Optional[Settings] = None) -> Any:
    """Return an async Redis client (falls back to fakeredis for deterministic local/test execution)."""
    cfg = settings or get_settings()
    if cfg.REDIS_URL.startswith(("fakeredis://", "memory://")) or "localhost" in cfg.REDIS_URL or "host:port" in cfg.REDIS_URL:
        return fakeredis.aioredis.FakeRedis(decode_responses=False)
    import redis.asyncio as aioredis

    return aioredis.from_url(cfg.REDIS_URL, decode_responses=False)


class RedisARQWorkerQueue:
    """Manages FIFO job queuing in Redis/ARQ, webhook delivery deduplication (INV-2), and worker execution."""

    QUEUE_KEY = "arq:queue:pr_reviews"
    DEDUP_PREFIX = "pr_reviewer:dedup:delivery:"

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        engine: Optional[WorkflowEngine] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client or create_redis_client(self.settings)
        self.checkpoint_store = RedisCheckpointStore(self.redis)
        self.engine: WorkflowEngine = engine or LangGraphEngine(
            checkpoint_store=self.checkpoint_store,
            node_timeout_seconds=self.settings.NODE_TIMEOUT_SECONDS,
            specialist_runner=build_specialist_runner(self.settings),
        )

    async def claim_delivery_id(self, delivery_id: str, ttl_seconds: int = 86400) -> bool:
        """Atomically deduplicate X-GitHub-Delivery using Redis SET NX (INV-2).

        Returns True if newly claimed, False if duplicate.
        """
        key = f"{self.DEDUP_PREFIX}{delivery_id}"
        claimed = await self.redis.set(key, b"1", nx=True, ex=ttl_seconds)
        return bool(claimed)

    async def enqueue_job(self, job: QueuedPRJob) -> bool:
        """Atomically deduplicate and push job onto the Redis FIFO queue."""
        if not await self.claim_delivery_id(job.delivery_id):
            return False
        await self.redis.rpush(self.QUEUE_KEY, job.model_dump_json().encode("utf-8"))
        return True

    async def dequeue_and_process_one(self) -> Optional[WorkflowState]:
        """Pop the next FIFO job from Redis and run it through the abstract WorkflowEngine."""
        raw = await self.redis.lpop(self.QUEUE_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        job = QueuedPRJob.model_validate_json(raw)
        ctx = WorkflowContext(
            delivery_id=job.delivery_id,
            trace_id=job.trace_id,
            repo=job.repo,
            pr_number=job.pr_number,
            commit_sha=job.commit_sha,
            is_public=job.is_public,
            diff=job.diff,
            changed_files=job.changed_files,
            metadata=job.metadata,
        )
        return await self.engine.run(ctx)


async def process_pr_review_task(ctx: Dict[str, Any], job_payload: Dict[str, Any]) -> Dict[str, Any]:
    """ARQ background worker entrypoint task."""
    worker_queue: RedisARQWorkerQueue = ctx.get("worker_queue") or RedisARQWorkerQueue()
    job = QueuedPRJob.model_validate(job_payload)
    wf_ctx = WorkflowContext(
        delivery_id=job.delivery_id,
        trace_id=job.trace_id,
        repo=job.repo,
        pr_number=job.pr_number,
        commit_sha=job.commit_sha,
        is_public=job.is_public,
        diff=job.diff,
        changed_files=job.changed_files,
        metadata=job.metadata,
    )
    state = await worker_queue.engine.run(wf_ctx)
    return state.model_dump()


class WorkerSettings:
    """ARQ WorkerSettings configuration (`arq backend.worker.WorkerSettings`)."""

    functions = [process_pr_review_task]
    max_jobs = 10
    job_timeout = 120
