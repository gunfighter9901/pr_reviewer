"""Redis + ARQ Background Worker Pool and Queue Integration (M4, INV-2, INV-3)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import fakeredis.aioredis
from arq.connections import RedisSettings

from backend.agents.llm import build_specialist_runner
from backend.config import Settings, get_settings
from backend.db.spine import TigerDataSpine
from backend.gates.hitl import HITLRoutingGate
from backend.github.client import GitHubClient, GitHubReviewTransport, GitHubSummaryTransport
from backend.github.poster import GitHubPRPoster
from backend.models import QueuedPRJob
from backend.observability.events import EventSpineLogger
from backend.orchestrator.engine import WorkflowContext, WorkflowEngine, WorkflowState
from backend.orchestrator.langgraph_adapter import LangGraphEngine, RedisCheckpointStore
from backend.pipeline import ReviewPipeline


def create_redis_client(settings: Optional[Settings] = None) -> Any:
    """Return an async Redis client; explicit fake/memory URLs stay offline for tests."""
    cfg = settings or get_settings()
    if cfg.REDIS_URL.startswith(("fakeredis://", "memory://")) or "host:port" in cfg.REDIS_URL:
        return fakeredis.aioredis.FakeRedis(decode_responses=False)
    import redis.asyncio as aioredis

    return aioredis.from_url(cfg.REDIS_URL, decode_responses=False)


class RedisARQWorkerQueue:
    """Legacy FIFO helper used by tests; production jobs run through ARQ + ReviewPipeline."""

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


def build_review_pipeline(settings: Optional[Settings] = None) -> ReviewPipeline:
    """Build the live review pipeline used by the ARQ worker and the manual CLI."""
    cfg = settings or get_settings()
    spine = TigerDataSpine(settings=cfg)
    event_logger = EventSpineLogger(spine=spine)
    redis_client = create_redis_client(cfg)
    checkpoint_store = RedisCheckpointStore(redis_client)
    engine = LangGraphEngine(
        checkpoint_store=checkpoint_store,
        node_timeout_seconds=cfg.NODE_TIMEOUT_SECONDS,
        specialist_runner=build_specialist_runner(cfg),
        event_logger=event_logger,
    )
    github_client = GitHubClient(settings=cfg)
    gate = HITLRoutingGate(spine=spine, settings=cfg, event_logger=event_logger)
    poster = GitHubPRPoster(
        transport=GitHubReviewTransport(github_client),
        summary_transport=GitHubSummaryTransport(github_client),
        event_logger=event_logger,
    )
    return ReviewPipeline(
        settings=cfg,
        engine=engine,
        gate=gate,
        poster=poster,
        github_client=github_client,
        event_logger=event_logger,
        redis_client=redis_client,
    )


async def process_pr_review_task(
    ctx: Dict[str, Any], job_payload: Dict[str, Any]
) -> Dict[str, Any]:
    """ARQ task entrypoint: build/reuse the pipeline, fetch the diff, review, and post."""
    from backend.webhook.queue import update_index_entry

    pipeline: Optional[ReviewPipeline] = ctx.get("review_pipeline")
    if pipeline is None:
        pipeline = build_review_pipeline()
        ctx["review_pipeline"] = pipeline

    job = QueuedPRJob.model_validate(job_payload)
    job_id = str(ctx.get("job_id") or f"pr-review:{job.delivery_id}")
    pool = ctx.get("redis")
    if pool is not None:
        try:
            await update_index_entry(pool, job_id, status="processing")
        except Exception:  # noqa: BLE001 - index is observability, not the review path
            pass
    try:
        summary = await pipeline.process(job)
    except Exception as exc:  # noqa: BLE001 - record failure, then let ARQ retry
        if pool is not None:
            try:
                await update_index_entry(
                    pool, job_id, status="failed", error=f"{exc.__class__.__name__}: {str(exc)[:200]}"
                )
            except Exception:  # noqa: BLE001 - index is observability, not the review path
                pass
        raise
    if pool is not None:
        try:
            await update_index_entry(
                pool,
                job_id,
                status="done",
                findings_count=summary.findings_count,
                auto_posted=summary.auto_posted,
                hitl_escalated=summary.hitl_escalated,
                private_security_alerts=summary.private_security_alerts,
            )
        except Exception:  # noqa: BLE001 - index is observability, not the review path
            pass
    return summary.model_dump(mode="json")


class WorkerSettings:
    """ARQ WorkerSettings configuration (`arq backend.worker.WorkerSettings`)."""

    functions = [process_pr_review_task]
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    max_jobs = 4
    job_timeout = 300
    keep_result = 3600

    @staticmethod
    async def on_startup(ctx: Dict[str, Any]) -> None:
        ctx["review_pipeline"] = build_review_pipeline()

    @staticmethod
    async def on_shutdown(ctx: Dict[str, Any]) -> None:
        pipeline: Optional[ReviewPipeline] = ctx.get("review_pipeline")
        if pipeline is not None:
            await pipeline.aclose()
