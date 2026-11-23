"""Single-PR summary tests: aggregator explainer, one-comment poster, API grouping."""

from __future__ import annotations

import pytest

from backend.agents.aggregator import (
    render_pr_summary_markdown,
    summarize_pr_findings,
)
from backend.config import Settings
from backend.db.spine import TigerDataSpine
from backend.gates.hitl import HITLRoutingGate
from backend.github.client import GitHubClient
from backend.github.poster import GitHubPRPoster, PublicCriticalSecurityLeakError
from backend.models import Finding
from backend.pipeline import ReviewPipeline


def _finding(**over: object) -> Finding:
    base: dict = {
        "agent_type": "quality",
        "severity": "medium",
        "category": "null_dereference",
        "file_path": "a.py",
        "line_start": 10,
        "line_end": 10,
        "evidence_snippet": "x = d.get('k').strip()",
        "rationale": "May crash.",
        "confidence": 0.68,
        "prompt_version": "quality@v1.2.0",
    }
    base.update(over)
    if "line_start" in over and "line_end" not in over:
        base["line_end"] = base["line_start"]
    return Finding(**base)


def test_summary_orders_by_risk_and_explains_plainly() -> None:
    findings = [
        _finding(severity="medium", category="null_dereference", line_start=30),
        _finding(
            agent_type="security",
            severity="critical",
            category="sql_injection",
            file_path="db.py",
            line_start=4,
            line_end=5,
            evidence_snippet="f'SELECT {u}'",
            confidence=0.95,
            prompt_version="security@v1.2.0",
        ),
    ]
    summary = summarize_pr_findings("acme/demo", 7, "abc", findings)
    assert summary.risk_level == "critical"
    assert summary.total_points == 2
    assert summary.severity_counts == {"critical": 1, "medium": 1}
    assert summary.points[0].category == "sql_injection"
    assert "db.py:4-5" in summary.points[0].plain_summary
    assert "attacker" in summary.points[0].plain_summary
    assert "Highest risk: sql_injection" in summary.headline


def test_summary_empty_pr_is_safe() -> None:
    summary = summarize_pr_findings("acme/demo", 7, "abc", [])
    assert summary.risk_level == "info"
    assert "safe to merge" in summary.headline
    assert "Safe to merge" in render_pr_summary_markdown(summary)


def test_summary_metas_follow_findings_through_sort() -> None:
    findings = [
        _finding(severity="low", category="missing_docstring", line_start=50),
        _finding(severity="high", category="swallowed_exception", line_start=5),
    ]
    summary = summarize_pr_findings(
        "acme/demo",
        7,
        "abc",
        findings,
        metas=[{"feedback_id": "hitl-low"}, {"feedback_id": "hitl-high"}],
    )
    assert summary.points[0].feedback_id == "hitl-high"
    assert summary.points[1].feedback_id == "hitl-low"


def test_summary_markdown_is_single_structured_comment() -> None:
    summary = summarize_pr_findings(
        "acme/demo", 7, "abc", [_finding(), _finding(line_start=20)]
    )
    body = render_pr_summary_markdown(
        summary, withheld_private=1, withheld_hitl=2
    )
    assert body.startswith("## PR Review — acme/demo#7")
    assert "### 1." in body and "### 2." in body
    assert "withheld from this thread" in body
    assert "held for human review" in body


@pytest.mark.asyncio
async def test_poster_summary_is_single_idempotent_comment() -> None:
    calls: list = []

    async def transport(payload: dict) -> dict:
        calls.append(payload)
        return {"id": 1, **payload}

    poster = GitHubPRPoster(transport=transport, summary_transport=transport)
    first = await poster.post_pr_summary_comment(
        delivery_id="d1",
        trace_id="t1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="abc",
        summary_markdown="## PR Review",
        findings_count=2,
        is_public_repo=True,
    )
    assert first is not None and len(calls) == 1
    assert calls[0]["body"] == "## PR Review"
    assert "path" not in calls[0]
    second = await poster.post_pr_summary_comment(
        delivery_id="d1",
        trace_id="t1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="abc",
        summary_markdown="## PR Review",
        findings_count=2,
        is_public_repo=True,
    )
    assert second is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_poster_summary_refuses_critical_details_on_public() -> None:
    poster = GitHubPRPoster()
    with pytest.raises(PublicCriticalSecurityLeakError):
        await poster.post_pr_summary_comment(
            delivery_id="d1",
            trace_id="t1",
            repo="acme/demo",
            pr_number=7,
            commit_sha="abc",
            summary_markdown="## PR Review",
            findings_count=1,
            is_public_repo=True,
            contains_critical_details=True,
        )


@pytest.mark.asyncio
async def test_pipeline_summary_mode_posts_single_comment() -> None:
    from backend.orchestrator.engine import WorkflowContext, WorkflowState

    settings = Settings(
        _env_file=None,
        SPINE_SQLITE_PATH=":memory:",
        POST_PR_SUMMARY_COMMENT=True,
    )
    spine = TigerDataSpine(settings=settings)

    finding = _finding(
        agent_type="quality",
        severity="high",
        category="swallowed_exception",
        file_path="app/db.py",
        line_start=42,
        line_end=42,
        evidence_snippet="except: pass",
        rationale="Silent handler.",
        suggested_fix="Log it.",
        confidence=0.89,
    )

    class StubEngine:
        async def run(self, context: WorkflowContext) -> WorkflowState:
            return WorkflowState(
                delivery_id=context.delivery_id,
                trace_id=context.trace_id,
                status="completed",
                aggregated_findings=[finding],
            )

    posted: list = []

    async def transport(payload: dict) -> dict:
        posted.append(payload)
        return {"id": f"c{len(posted)}", **payload}

    from backend.models import QueuedPRJob

    pipeline = ReviewPipeline(
        settings=settings,
        engine=StubEngine(),  # type: ignore[arg-type]
        gate=HITLRoutingGate(spine=spine, settings=settings),
        poster=GitHubPRPoster(transport=transport, summary_transport=transport),
        github_client=None,  # type: ignore[arg-type]
    )
    job = QueuedPRJob(
        delivery_id="deliv-sum-1",
        trace_id="tr-sum-1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="abc123",
        action="opened",
        is_public=True,
        diff="diff --git a/app/db.py b/app/db.py\n",
    )
    summary = await pipeline.process(job)
    assert summary.auto_posted == 1
    assert len(posted) == 1  # single comment, not one per finding
    assert "PR Review" in posted[0]["body"]
    assert "swallowed_exception" in posted[0]["body"]
    assert "PR Review" in summary.pr_summary_markdown


@pytest.mark.asyncio
async def test_client_posts_issue_comment() -> None:
    import httpx

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": 7})

    client = GitHubClient(
        settings=Settings(_env_file=None, GITHUB_TOKEN="github_pat_test_token_1234567890"),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.post_issue_comment("acme/demo", 7, "## PR Review")
    finally:
        await client.aclose()
    assert seen["path"] == "/repos/acme/demo/issues/7/comments"
    assert result["id"] == 7


def test_api_pr_summaries_groups_by_pr() -> None:
    import os

    for key in [k for k in os.environ if k.startswith(("LLM_", "EMBEDDING_", "SPINE_", "QUEUE_", "REDIS_", "GITHUB_", "GEMINI_", "CONFIDENCE_"))]:
        os.environ.pop(key, None)
    from backend.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from backend.webhook.app import create_app

    settings = Settings(_env_file=None, SPINE_SQLITE_PATH=":memory:")
    app = create_app(settings=settings)
    client = TestClient(app)
    spine = app.state.spine
    rev = spine.upsert_pr_review(
        github_delivery_id="deliv-g1",
        trace_id="tr-g1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="abc",
        status="processing",
    )
    for idx, sev in enumerate(["medium", "critical"]):
        fid = spine.insert_finding(
            github_delivery_id="deliv-g1",
            review_id=rev,
            routing_decision="hitl_escalate",
            finding_data={
                "agent_type": "quality",
                "severity": sev,
                "category": "null_dereference",
                "file_path": "a.py",
                "line_start": 10 + idx,
                "line_end": 10 + idx,
                "evidence_snippet": "x",
                "rationale": "r",
                "confidence": 0.7,
                "prompt_version": "quality@v1.2.0",
            },
        )
        spine.insert_hitl_feedback(
            finding_id=fid,
            github_delivery_id="deliv-g1",
            repo="acme/demo",
            file_path="a.py",
            category="null_dereference",
            severity=sev,
        )

    data = client.get("/api/pr-summaries?reviewer_action=pending").json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["repo"] == "acme/demo" and item["pr_number"] == 7
    assert item["risk_level"] == "critical"
    assert item["total_points"] == 2
    assert item["points"][0]["severity"] == "critical"
    assert item["points"][0]["feedback_id"].startswith("hitl-")
    assert "PR Review" in item["markdown"]
