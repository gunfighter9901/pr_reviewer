"""End-to-end review pipeline: webhook job -> PR diff -> specialists -> HITL -> GitHub."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from backend.config import Settings, get_settings
from backend.gates.hitl import GateRoutingResult, HITLRoutingGate
from backend.github.client import GitHubClient
from backend.github.poster import GitHubPRPoster
from backend.models import QueuedPRJob
from backend.observability.events import EventSpineLogger
from backend.orchestrator.engine import WorkflowContext, WorkflowEngine, WorkflowState


class ReviewPipelineError(RuntimeError):
    """Raised when a PR cannot be reviewed (missing diff, unusable token, etc.)."""


class ReviewSummary(BaseModel):
    """Serializable result returned by the worker and the manual review CLI."""

    delivery_id: str
    trace_id: str
    repo: str
    pr_number: int
    commit_sha: str
    status: str
    diff_source: Literal["webhook", "github_api"]
    diff_bytes: int = 0
    changed_files: int = 0
    findings_count: int = 0
    auto_posted: int = 0
    hitl_escalated: int = 0
    private_security_alerts: int = 0
    posted_comments: List[Dict[str, Any]] = Field(default_factory=list)


class ReviewPipeline:
    """Coordinates diff fetching, specialist execution, routing, persistence, and posting."""

    def __init__(
        self,
        *,
        settings: Optional[Settings],
        engine: WorkflowEngine,
        gate: HITLRoutingGate,
        poster: GitHubPRPoster,
        github_client: GitHubClient,
        event_logger: Optional[EventSpineLogger] = None,
        redis_client: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.engine = engine
        self.gate = gate
        self.poster = poster
        self.github_client = github_client
        self.event_logger = event_logger
        self._redis_client = redis_client

    async def aclose(self) -> None:
        await self.github_client.aclose()
        if self._redis_client is not None:
            await self._redis_client.aclose()

    async def process(
        self, job: QueuedPRJob, *, post_comments: bool = True
    ) -> ReviewSummary:
        diff = job.diff or ""
        changed_files = list(job.changed_files)
        commit_sha = job.commit_sha
        is_public = job.is_public

        if not diff.strip():
            bundle = await self.github_client.fetch_pull_request_bundle(
                job.repo, job.pr_number
            )
            diff = bundle.diff
            changed_files = bundle.changed_files or changed_files
            commit_sha = bundle.head_sha or commit_sha
            is_public = bundle.is_public
            diff_source: Literal["webhook", "github_api"] = "github_api"
        else:
            diff_source = "webhook"

        if not diff.strip():
            raise ReviewPipelineError(
                f"PR {job.repo}#{job.pr_number} has no diff to review"
            )

        context = WorkflowContext(
            delivery_id=job.delivery_id,
            trace_id=job.trace_id,
            repo=job.repo,
            pr_number=job.pr_number,
            commit_sha=commit_sha,
            is_public=is_public,
            diff=diff,
            changed_files=changed_files,
            metadata=job.metadata,
        )

        state: WorkflowState = await self.engine.run(context)

        routing: GateRoutingResult = await self.gate.evaluate_and_persist(
            delivery_id=job.delivery_id,
            trace_id=job.trace_id,
            repo=job.repo,
            pr_number=job.pr_number,
            commit_sha=commit_sha,
            findings=state.aggregated_findings,
            is_public_repo=is_public,
        )

        posted: List[Dict[str, Any]] = []
        if post_comments and routing.auto_post_findings:
            posted = await self.poster.post_routed_findings(
                delivery_id=job.delivery_id,
                trace_id=job.trace_id,
                repo=job.repo,
                pr_number=job.pr_number,
                commit_sha=commit_sha,
                routed_findings=routing.auto_post_findings,
                is_public_repo=is_public,
            )

        if routing.hitl_escalated_findings:
            status = "escalated_hitl"
        elif routing.auto_post_findings:
            status = "auto_posted"
        else:
            status = "completed"

        summary = ReviewSummary(
            delivery_id=job.delivery_id,
            trace_id=job.trace_id,
            repo=job.repo,
            pr_number=job.pr_number,
            commit_sha=commit_sha,
            status=status,
            diff_source=diff_source,
            diff_bytes=len(diff.encode("utf-8")),
            changed_files=len(changed_files),
            findings_count=len(state.aggregated_findings),
            auto_posted=len(routing.auto_post_findings),
            hitl_escalated=len(routing.hitl_escalated_findings),
            private_security_alerts=len(routing.private_security_alerts),
            posted_comments=posted,
        )

        if self.event_logger is not None:
            await self.event_logger.log_event(
                trace_id=job.trace_id,
                delivery_id=job.delivery_id,
                agent_name="review_pipeline",
                event_type="review_completed",
                status="ok",
                metadata={
                    "repo": job.repo,
                    "pr_number": job.pr_number,
                    "diff_source": diff_source,
                    "findings": summary.findings_count,
                    "posted": summary.auto_posted,
                    "hitl": summary.hitl_escalated,
                    "private_alerts": summary.private_security_alerts,
                },
            )
        return summary
