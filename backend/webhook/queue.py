"""Redis/ARQ queue adapter for the stateless webhook ingress."""

from __future__ import annotations

from typing import Any, Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.config import Settings, get_settings
from backend.models import QueuedPRJob


class WebhookQueueError(RuntimeError):
    """Raised when the webhook cannot hand a job to the ARQ queue."""


class ArqWebhookQueue:
    """Enqueue validated PR jobs onto the same ARQ queue consumed by the worker."""

    DEDUP_PREFIX = "pr_reviewer:dedup:delivery:"
    FUNCTION_NAME = "process_pr_review_task"

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        pool: Optional[ArqRedis] = None,
        dedup_ttl_seconds: int = 86400,
    ) -> None:
        self.settings = settings or get_settings()
        self._pool = pool
        self.dedup_ttl_seconds = int(dedup_ttl_seconds)

    @property
    def pool(self) -> Optional[ArqRedis]:
        return self._pool

    async def connect(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(
                RedisSettings.from_dsn(self.settings.REDIS_URL)
            )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None

    async def enqueue_job(self, job: QueuedPRJob) -> bool:
        """Deduplicate by delivery ID, then enqueue through ARQ.

        Returns False when GitHub redelivers an event that was already accepted.
        """
        pool = await self.connect()
        dedup_key = f"{self.DEDUP_PREFIX}{job.delivery_id}"
        claimed = await pool.set(
            dedup_key, b"1", nx=True, ex=self.dedup_ttl_seconds
        )
        if not claimed:
            return False

        try:
            queued = await pool.enqueue_job(
                self.FUNCTION_NAME,
                job.model_dump(mode="json"),
                _job_id=f"pr-review:{job.delivery_id}",
            )
        except Exception as exc:  # noqa: BLE001 - surface a clear ingress error
            await pool.delete(dedup_key)
            raise WebhookQueueError(
                f"Failed to enqueue delivery {job.delivery_id}: {exc.__class__.__name__}"
            ) from exc

        if queued is None:
            # ARQ already has a job with this ID; keep the dedup marker.
            return False
        return True
