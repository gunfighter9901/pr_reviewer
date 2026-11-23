"""End-to-end review pipeline tests with a stubbed engine and GitHub transport."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from backend.config import Settings
from backend.db.spine import TigerDataSpine
from backend.gates.hitl import HITLRoutingGate
from backend.github.client import PullRequestBundle
from backend.github.poster import GitHubPRPoster
from backend.models import Finding, QueuedPRJob
from backend.orchestrator.engine import WorkflowContext, WorkflowState
from backend.pipeline import ReviewPipeline, ReviewPipelineError

DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,2 +1,3 @@
 import sqlite3
+def lookup(conn, user_id):
+    return conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
"""


def _finding() -> Finding:
    return Finding(
        agent_type="security",
        severity="high",
        category="sql_injection",
        file_path="app/db.py",
        line_start=4,
        line_end=4,
        evidence_snippet='return conn.execute(f"SELECT * FROM users WHERE id = {user_id}")',
        rationale="User input is interpolated into SQL.",
        suggested_fix="Use a parameterized query.",
        confidence=0.93,
        prompt_version="security@v1.2.0",
    )


class StubEngine:
    def __init__(self, findings: Optional[List[Finding]] = None) -> None:
        self.findings = findings or []
        self.contexts: List[WorkflowContext] = []

    async def run(self, context: WorkflowContext) -> WorkflowState:
        self.contexts.append(context)
        return WorkflowState(
            delivery_id=context.delivery_id,
            trace_id=context.trace_id,
            status="completed",
            aggregated_findings=self.findings,
        )


class StubGitHubClient:
    def __init__(self, bundle: PullRequestBundle) -> None:
        self.bundle = bundle
        self.fetch_calls = 0
        self.closed = False

    async def fetch_pull_request_bundle(self, repo: str, pr_number: int) -> PullRequestBundle:
        self.fetch_calls += 1
        return self.bundle

    async def aclose(self) -> None:
        self.closed = True


class ExplodingGitHubClient(StubGitHubClient):
    async def fetch_pull_request_bundle(self, repo: str, pr_number: int) -> PullRequestBundle:
        raise AssertionError("GitHub fetch must not run when the webhook supplied a diff")


def _pipeline(
    engine: StubEngine, client: StubGitHubClient, *, spine: TigerDataSpine
) -> tuple[ReviewPipeline, List[Dict[str, Any]]]:
    settings = Settings(_env_file=None, SPINE_SQLITE_PATH=":memory:")
    gate = HITLRoutingGate(spine=spine, settings=settings)
    posted: List[Dict[str, Any]] = []

    async def transport(payload: Dict[str, Any]) -> Dict[str, Any]:
        posted.append(payload)
        return {"id": f"comment-{len(posted)}", **payload}

    poster = GitHubPRPoster(transport=transport)
    return (
        ReviewPipeline(
            settings=settings,
            engine=engine,  # type: ignore[arg-type]
            gate=gate,
            poster=poster,
            github_client=client,  # type: ignore[arg-type]
        ),
        posted,
    )


def _job(*, diff: str = "") -> QueuedPRJob:
    return QueuedPRJob(
        delivery_id="deliv-pipeline-1",
        trace_id="tr-pipeline-1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="pending",
        action="opened",
        is_public=True,
        diff=diff,
    )


@pytest.mark.asyncio
async def test_pipeline_fetches_diff_routes_and_posts() -> None:
    spine = TigerDataSpine(settings=Settings(_env_file=None, SPINE_SQLITE_PATH=":memory:"))
    engine = StubEngine([_finding()])
    client = StubGitHubClient(
        PullRequestBundle(
            repo="acme/demo",
            pr_number=7,
            head_sha="abc123",
            diff=DIFF,
            changed_files=["app/db.py"],
            is_public=True,
        )
    )
    pipeline, posted = _pipeline(engine, client, spine=spine)

    summary = await pipeline.process(_job())

    assert client.fetch_calls == 1
    assert engine.contexts[0].diff == DIFF
    assert engine.contexts[0].commit_sha == "abc123"
    assert summary.diff_source == "github_api"
    assert summary.status == "auto_posted"
    assert summary.auto_posted == 1
    assert len(posted) == 1
    assert posted[0]["path"] == "app/db.py"
    assert len(spine.get_findings(github_delivery_id="deliv-pipeline-1")) == 1


@pytest.mark.asyncio
async def test_pipeline_uses_webhook_diff_without_github_fetch() -> None:
    spine = TigerDataSpine(settings=Settings(_env_file=None, SPINE_SQLITE_PATH=":memory:"))
    engine = StubEngine([])
    client = ExplodingGitHubClient(
        PullRequestBundle("acme/demo", 7, "abc123", DIFF, ["app/db.py"])
    )
    pipeline, posted = _pipeline(engine, client, spine=spine)

    summary = await pipeline.process(_job(diff=DIFF))

    assert summary.diff_source == "webhook"
    assert summary.status == "completed"
    assert posted == []
    assert engine.contexts[0].diff == DIFF


@pytest.mark.asyncio
async def test_pipeline_dry_run_does_not_post() -> None:
    spine = TigerDataSpine(settings=Settings(_env_file=None, SPINE_SQLITE_PATH=":memory:"))
    engine = StubEngine([_finding()])
    client = StubGitHubClient(
        PullRequestBundle("acme/demo", 7, "abc123", DIFF, ["app/db.py"])
    )
    pipeline, posted = _pipeline(engine, client, spine=spine)

    summary = await pipeline.process(_job(), post_comments=False)

    assert summary.auto_posted == 1
    assert posted == []
    assert len(spine.get_findings(github_delivery_id="deliv-pipeline-1")) == 1


@pytest.mark.asyncio
async def test_pipeline_rejects_empty_diff() -> None:
    spine = TigerDataSpine(settings=Settings(_env_file=None, SPINE_SQLITE_PATH=":memory:"))
    engine = StubEngine([])
    client = StubGitHubClient(PullRequestBundle("acme/demo", 7, "abc123", "", []))
    pipeline, _ = _pipeline(engine, client, spine=spine)

    with pytest.raises(ReviewPipelineError):
        await pipeline.process(_job())
