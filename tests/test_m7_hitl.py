"""Milestone 7 Verification Tests: HITL Gate, GitHub Poster & Reliability (INV-6, INV-7)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from backend.config import Settings
from backend.db.spine import TigerDataSpine
from backend.gates.hitl import FeedbackDecayEngine, HITLRoutingGate
from backend.github.poster import GitHubPRPoster, PublicCriticalSecurityLeakError
from backend.models import Finding
from backend.observability.events import EventSpineLogger
from backend.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)


@pytest.fixture()
def m7_env() -> tuple[TigerDataSpine, EventSpineLogger, HITLRoutingGate, FeedbackDecayEngine]:
    settings = Settings(_env_file=None, CONFIDENCE_THRESHOLD=0.75)
    spine = TigerDataSpine(settings=settings)
    event_logger = EventSpineLogger(spine=spine)
    gate = HITLRoutingGate(spine=spine, settings=settings, event_logger=event_logger)
    decay_engine = FeedbackDecayEngine(spine=spine, min_evidence_threshold=3)
    return spine, event_logger, gate, decay_engine


@pytest.mark.asyncio
async def test_m7_routing_gate_auto_posts_high_conf_and_escalates_low_conf_and_critical_security(
    m7_env: tuple[TigerDataSpine, EventSpineLogger, HITLRoutingGate, FeedbackDecayEngine],
) -> None:
    """Verify high-confidence non-critical auto-posts, while low-confidence and critical security escalate to HITL."""
    spine, _, gate, _ = m7_env

    findings = [
        # 1. High-confidence quality finding -> AUTO-POST
        Finding(
            agent_type="quality",
            severity="high",
            category="swallowed_exception",
            file_path="src/worker.py",
            line_start=42,
            line_end=43,
            evidence_snippet="except Exception: pass",
            rationale="Silent exception handler hides failure states.",
            suggested_fix="Log and re-raise exception.",
            confidence=0.89,
            prompt_version="quality@v1.2.0",
        ),
        # 2. Low-confidence quality finding (< 0.75) -> ESCALATE TO HITL
        Finding(
            agent_type="quality",
            severity="medium",
            category="null_dereference",
            file_path="src/worker.py",
            line_start=60,
            line_end=60,
            evidence_snippet="cfg.get('host').strip()",
            rationale="Potential None dereference if key is absent.",
            confidence=0.62,
            prompt_version="quality@v1.2.0",
        ),
        # 3. Critical security finding on public repo (even with 0.97 confidence!) -> PRIVATE SECURITY ALERT (never public)
        Finding(
            agent_type="security",
            severity="critical",
            category="sql_injection",
            file_path="src/db.py",
            line_start=18,
            line_end=19,
            evidence_snippet='cursor.execute(f"SELECT * FROM accounts WHERE id = {acct_id}")',
            rationale="Direct f-string SQL injection vulnerability.",
            suggested_fix="Use parameterized SQL query.",
            confidence=0.97,
            prompt_version="security@v1.2.0",
        ),
    ]

    routing = await gate.evaluate_and_persist(
        delivery_id="deliv-m7-001",
        trace_id="tr-m7-001",
        repo="acme-open-source/public-api",
        pr_number=21,
        commit_sha="sha-m7-001",
        findings=findings,
        is_public_repo=True,
    )

    assert len(routing.auto_post_findings) == 1
    assert routing.auto_post_findings[0].finding.category == "swallowed_exception"

    assert len(routing.private_security_alerts) == 1
    assert routing.private_security_alerts[0].finding.category == "sql_injection"
    assert routing.private_security_alerts[0].private_security_alert is True

    assert len(routing.hitl_escalated_findings) == 2

    # Post via GitHubPRPoster -> only the 1 auto_post finding is posted to GitHub
    poster = GitHubPRPoster()
    posted = await poster.post_routed_findings(
        delivery_id="deliv-m7-001",
        trace_id="tr-m7-001",
        repo="acme-open-source/public-api",
        pr_number=21,
        commit_sha="sha-m7-001",
        routed_findings=routing.auto_post_findings,
        is_public_repo=True,
    )
    assert len(posted) == 1
    assert posted[0]["path"] == "src/worker.py"
    assert posted[0]["line"] == 42

    # Idempotency check: re-running post_routed_findings does not double-post
    re_posted = await poster.post_routed_findings(
        delivery_id="deliv-m7-001",
        trace_id="tr-m7-001",
        repo="acme-open-source/public-api",
        pr_number=21,
        commit_sha="sha-m7-001",
        routed_findings=routing.auto_post_findings,
        is_public_repo=True,
    )
    assert len(re_posted) == 0
    assert len(poster.posted_comments) == 1

    # Hard guard check: attempting to pass a critical security finding to public poster raises PublicCriticalSecurityLeakError
    with pytest.raises(PublicCriticalSecurityLeakError, match="Security Vulnerability Rule Violation"):
        await poster.post_routed_findings(
            delivery_id="deliv-m7-001",
            trace_id="tr-m7-001",
            repo="acme-open-source/public-api",
            pr_number=21,
            commit_sha="sha-m7-001",
            routed_findings=routing.private_security_alerts,
            is_public_repo=True,
        )


@pytest.mark.asyncio
async def test_inv6_fault_injection_trips_circuit_breaker() -> None:
    """INV-6: Inject consecutive GitHub API transport failures and verify CircuitBreaker trips OPEN."""
    cb = CircuitBreaker(
        name="github_api",
        failure_threshold=2,
        recovery_timeout_seconds=60.0,
        call_timeout_seconds=0.5,
        max_retries=0,
    )

    async def failing_github_transport(payload: Dict[str, Any]) -> Dict[str, Any]:
        raise ConnectionError("GitHub API 503 Service Unavailable")

    poster = GitHubPRPoster(circuit_breaker=cb, transport=failing_github_transport)
    sample_finding = Finding(
        agent_type="quality",
        severity="medium",
        category="mutable_default_argument",
        file_path="src/utils.py",
        line_start=5,
        line_end=5,
        evidence_snippet="def add(item, bucket=[]):",
        rationale="Mutable default argument shared across calls.",
        confidence=0.88,
        prompt_version="quality@v1.2.0",
    )

    from backend.gates.hitl import RoutedFinding

    routed = [RoutedFinding(finding=sample_finding, finding_id="fnd-1", decision="auto_post")]

    # Failure 1
    with pytest.raises(ConnectionError):
        await poster.post_routed_findings(
            delivery_id="d1",
            trace_id="t1",
            repo="acme/repo",
            pr_number=1,
            commit_sha="s1",
            routed_findings=routed,
        )
    assert cb.state == CircuitState.CLOSED

    # Failure 2 -> reaches failure_threshold=2 and trips CircuitBreaker OPEN
    with pytest.raises(ConnectionError):
        await poster.post_routed_findings(
            delivery_id="d1",
            trace_id="t1",
            repo="acme/repo",
            pr_number=1,
            commit_sha="s1",
            routed_findings=routed,
        )
    assert cb.state == CircuitState.OPEN

    # Subsequent call fails fast with CircuitBreakerOpenError without hitting network
    with pytest.raises(CircuitBreakerOpenError, match="OPEN"):
        await poster.post_routed_findings(
            delivery_id="d1",
            trace_id="t1",
            repo="acme/repo",
            pr_number=1,
            commit_sha="s1",
            routed_findings=routed,
        )


def test_m7_feedback_loop_decay_requires_minimum_evidence_threshold(
    m7_env: tuple[TigerDataSpine, EventSpineLogger, HITLRoutingGate, FeedbackDecayEngine],
) -> None:
    """Verify hitl_feedback decay loop prevents poisoning by requiring >= 3 verified outcomes before adapting."""
    spine, _, _, decay_engine = m7_env

    rev_id = spine.upsert_pr_review(
        github_delivery_id="deliv-decay-1",
        trace_id="tr-decay-1",
        repo="acme/core",
        pr_number=33,
        commit_sha="sha33",
        status="escalated_hitl",
    )

    feedback_ids = []
    for idx in range(3):
        f_id = spine.insert_finding(
            github_delivery_id="deliv-decay-1",
            review_id=rev_id,
            routing_decision="hitl_escalate",
            finding_data={
                "agent_type": "docs",
                "severity": "low",
                "category": "missing_docstring",
                "file_path": f"src/mod_{idx}.py",
                "line_start": 10,
                "line_end": 10,
                "evidence_snippet": "def helper():",
                "rationale": "Missing docstring.",
                "confidence": 0.65,
                "prompt_version": "docs@v1.1.0",
            },
        )
        fb_id = spine.insert_hitl_feedback(
            finding_id=f_id,
            github_delivery_id="deliv-decay-1",
            repo="acme/core",
            file_path=f"src/mod_{idx}.py",
            category="missing_docstring",
            severity="low",
            reviewer_action="pending",
        )
        feedback_ids.append(fb_id)

    # 1st rejection: evidence_count=1 (< 3) -> does NOT adapt procedural memory yet
    out1 = decay_engine.record_reviewer_decision(
        feedback_id=feedback_ids[0],
        reviewer_action="rejected_false_positive",
    )
    assert out1["evidence_count"] == 1
    assert out1["adapted_procedural_memory"] is False
    assert out1["decay_weight"] == 1.0

    # 2nd rejection: evidence_count=2 (< 3) -> still does NOT adapt procedural memory
    out2 = decay_engine.record_reviewer_decision(
        feedback_id=feedback_ids[1],
        reviewer_action="rejected_false_positive",
    )
    assert out2["evidence_count"] == 2
    assert out2["adapted_procedural_memory"] is False
    assert out2["decay_weight"] == 1.0

    # 3rd rejection: evidence_count=3 (>= 3) -> adapts procedural memory and decays weight to 0.85
    out3 = decay_engine.record_reviewer_decision(
        feedback_id=feedback_ids[2],
        reviewer_action="rejected_false_positive",
    )
    assert out3["evidence_count"] == 3
    assert out3["adapted_procedural_memory"] is True
    assert out3["decay_weight"] == pytest.approx(0.85)

    conventions = spine.get_procedural_conventions(repo="acme/core")
    assert any(c["rule_key"] == "AUTO-CALIBRATED-MISSING_DOCSTRING" for c in conventions)
