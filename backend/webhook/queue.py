"""Redis/ARQ queue adapter for the stateless webhook ingress."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.config import Settings, get_settings
from backend.models import QueuedPRJob


class WebhookQueueError(RuntimeError):
    """Raised when the webhook cannot hand a job to the ARQ queue."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArqWebhookQueue:
    """Enqueue validated PR jobs onto the same ARQ queue consumed by the worker."""

    DEDUP_PREFIX = "pr_reviewer:dedup:delivery:"
    FUNCTION_NAME = "process_pr_review_task"
    # Dashboard-visible index: job_id -> JSON {repo, pr_number, status, ...}.
    INDEX_KEY = "pr_reviewer:queue:index"
    # ARQ's default queue sorted set (job_id members, run-at score).
    ARQ_QUEUE_KEY = "arq:queue"

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        pool: Optional[ArqRedis] = None,
        dedup_ttl_seconds: int = 86400,
    ) -> None:
        self.settings = settings or get_settings()
        self._pool = pool
        self._owns_pool = pool is None
        self.dedup_ttl_seconds = int(dedup_ttl_seconds)

    @property
    def pool(self) -> Optional[ArqRedis]:
        return self._pool

    async def connect(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(
                RedisSettings.from_dsn(self.settings.REDIS_URL)
            )
            self._owns_pool = True
        return self._pool

    async def close(self) -> None:
        # Only tear down pools this instance created; injected (test or
        # lifespan-shared) pools stay open for their owner.
        if self._pool is not None and self._owns_pool:
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

        # Best-effort dashboard index: a missing entry never breaks the review,
        # and /api/queue also surfaces raw ARQ job IDs as a fallback.
        try:
            await write_index_entry(
                pool,
                f"pr-review:{job.delivery_id}",
                {
                    "job_id": f"pr-review:{job.delivery_id}",
                    "delivery_id": job.delivery_id,
                    "trace_id": job.trace_id,
                    "repo": job.repo,
                    "pr_number": job.pr_number,
                    "status": "queued",
                    "source": (job.metadata or {}).get("source", "webhook"),
                    "enqueued_at": _now_iso(),
                    "updated_at": _now_iso(),
                },
            )
        except Exception:  # noqa: BLE001 - index is observability, not the review path
            pass
        return True

    async def list_queued_job_ids(self) -> List[str]:
        """Return job IDs currently waiting in the ARQ queue (not yet picked up)."""
        pool = await self.connect()
        members = await pool.zrange(self.ARQ_QUEUE_KEY, 0, -1)
        return [
            m.decode("utf-8") if isinstance(m, (bytes, bytearray)) else str(m)
            for m in (members or [])
        ]


async def write_index_entry(pool: Any, job_id: str, entry: Dict[str, Any]) -> None:
    """Upsert one dashboard index entry (job_id -> JSON)."""
    await pool.hset(ArqWebhookQueue.INDEX_KEY, job_id, json.dumps(entry))


async def update_index_entry(pool: Any, job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Merge fields into an index entry; returns the updated entry or None."""
    raw = await pool.hget(ArqWebhookQueue.INDEX_KEY, job_id)
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    try:
        entry = json.loads(raw)
    except (ValueError, TypeError):
        entry = {}
    if not isinstance(entry, dict):
        entry = {}
    entry.update(fields)
    entry["updated_at"] = _now_iso()
    await pool.hset(ArqWebhookQueue.INDEX_KEY, job_id, json.dumps(entry))
    return entry


async def read_index(pool: Any) -> Dict[str, Dict[str, Any]]:
    """Read the whole dashboard index, skipping corrupt entries."""
    try:
        raw_all = await pool.hgetall(ArqWebhookQueue.INDEX_KEY)
    except Exception:  # noqa: BLE001 - Redis without hash support (test fakes)
        return {}
    entries: Dict[str, Dict[str, Any]] = {}
    for key, raw in (raw_all or {}).items():
        job_id = key.decode("utf-8") if isinstance(key, (bytes, bytearray)) else str(key)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            entry.setdefault("job_id", job_id)
            entries[job_id] = entry
    return entries
