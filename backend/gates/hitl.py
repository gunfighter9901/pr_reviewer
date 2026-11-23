"""Autonomy & Human-in-the-Loop (HITL) Routing Gate and Feedback Loop Decay (M7, INV-7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from backend.config import Settings, get_settings
from backend.db.spine import TigerDataSpine
from backend.models import Finding, RoutingDecision
from backend.observability.events import EventSpineLogger


@dataclass
class RoutedFinding:
    finding: Finding
    finding_id: str
    decision: RoutingDecision
    hitl_feedback_id: Optional[str] = None
    private_security_alert: bool = False


@dataclass
class GateRoutingResult:
    delivery_id: str
    trace_id: str
    review_id: str
    auto_post_findings: List[RoutedFinding] = field(default_factory=list)
    hitl_escalated_findings: List[RoutedFinding] = field(default_factory=list)
    private_security_alerts: List[RoutedFinding] = field(default_factory=list)


class HITLRoutingGate:
    """Enforces the Selectivity & Autonomy Gate (Section 3.1 Stage 5 of docs/PROJECT_SPEC.md).

    Rules:
    - Auto-Post to GitHub: confidence >= CONFIDENCE_THRESHOLD AND severity != 'critical'.
    - Escalate to HITL Queue & Private Alert:
      * Any finding with confidence < CONFIDENCE_THRESHOLD.
      * Security Vulnerability Rule: Any 'critical' finding (especially security on a public repo)
        must NEVER be posted publicly on the PR thread; route directly to private HITL queue / Slack alert.
    """

    def __init__(
        self,
        spine: TigerDataSpine,
        settings: Optional[Settings] = None,
        event_logger: Optional[EventSpineLogger] = None,
    ) -> None:
        self.spine = spine
        self.settings = settings or spine.settings or get_settings()
        self.event_logger = event_logger

    @property
    def confidence_threshold(self) -> float:
        return float(self.settings.CONFIDENCE_THRESHOLD)

    def classify_finding(self, finding: Finding, is_public_repo: bool = True) -> RoutingDecision:
        """Determine routing decision for a grounded Finding."""
        if finding.severity == "critical":
            return "private_security_alert"
        if finding.confidence < self.confidence_threshold:
            return "hitl_escalate"
        return "auto_post"

    async def evaluate_and_persist(
        self,
        *,
        delivery_id: str,
        trace_id: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        findings: Sequence[Finding],
        is_public_repo: bool = True,
    ) -> GateRoutingResult:
        """Route findings, persist relational truth & HITL queue records, and emit audit spans."""
        review_id = self.spine.upsert_pr_review(
            github_delivery_id=delivery_id,
            trace_id=trace_id,
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            is_public=is_public_repo,
            status="processing",
        )

        result = GateRoutingResult(
            delivery_id=delivery_id,
            trace_id=trace_id,
            review_id=review_id,
        )

        for finding in findings:
            decision = self.classify_finding(finding, is_public_repo=is_public_repo)
            finding_id = self.spine.insert_finding(
                github_delivery_id=delivery_id,
                review_id=review_id,
                routing_decision=decision,
                finding_data=finding.model_dump(),
            )

            if decision == "auto_post":
                result.auto_post_findings.append(
                    RoutedFinding(
                        finding=finding,
                        finding_id=finding_id,
                        decision=decision,
                        private_security_alert=False,
                    )
                )
            elif decision == "private_security_alert":
                hitl_id = self.spine.insert_hitl_feedback(
                    finding_id=finding_id,
                    github_delivery_id=delivery_id,
                    repo=repo,
                    file_path=finding.file_path,
                    category=finding.category,
                    severity=finding.severity,
                    reviewer_action="pending",
                    private_alert=True,
                    notes="CRITICAL SECURITY ALERT: Routed privately; withheld from public PR thread.",
                )
                routed = RoutedFinding(
                    finding=finding,
                    finding_id=finding_id,
                    decision=decision,
                    hitl_feedback_id=hitl_id,
                    private_security_alert=True,
                )
                result.private_security_alerts.append(routed)
                result.hitl_escalated_findings.append(routed)
            else:
                hitl_id = self.spine.insert_hitl_feedback(
                    finding_id=finding_id,
                    github_delivery_id=delivery_id,
                    repo=repo,
                    file_path=finding.file_path,
                    category=finding.category,
                    severity=finding.severity,
                    reviewer_action="pending",
                    private_alert=False,
                    notes=f"Escalated to HITL: confidence {finding.confidence:.2f} < threshold {self.confidence_threshold:.2f}",
                )
                result.hitl_escalated_findings.append(
                    RoutedFinding(
                        finding=finding,
                        finding_id=finding_id,
                        decision=decision,
                        hitl_feedback_id=hitl_id,
                        private_security_alert=False,
                    )
                )

        final_status = (
            "escalated_hitl"
            if result.hitl_escalated_findings
            else ("auto_posted" if result.auto_post_findings else "completed")
        )
        self.spine.upsert_pr_review(
            github_delivery_id=delivery_id,
            trace_id=trace_id,
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            is_public=is_public_repo,
            status=final_status,
            review_id=review_id,
        )

        if self.event_logger is not None:
            await self.event_logger.log_event(
                trace_id=trace_id,
                delivery_id=delivery_id,
                agent_name="hitl_gate",
                event_type="gate_routing_completed",
                status="ok",
                metadata={
                    "auto_post_count": len(result.auto_post_findings),
                    "hitl_escalated_count": len(result.hitl_escalated_findings),
                    "private_security_alert_count": len(result.private_security_alerts),
                    "threshold": self.confidence_threshold,
                },
            )

        return result


class FeedbackDecayEngine:
    """Anti-poisoning feedback loop updating procedural memory only after minimum evidence threshold (M7)."""

    def __init__(
        self,
        spine: TigerDataSpine,
        min_evidence_threshold: int = 3,
        decay_factor_per_rejection: float = 0.85,
        boost_factor_per_approval: float = 1.05,
    ) -> None:
        self.spine = spine
        self.min_evidence_threshold = max(2, int(min_evidence_threshold))
        self.decay_factor_per_rejection = float(decay_factor_per_rejection)
        self.boost_factor_per_approval = float(boost_factor_per_approval)

    def record_reviewer_decision(
        self,
        *,
        feedback_id: str,
        reviewer_action: str,
        reviewer_id: str = "senior-eng",
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record senior reviewer action and adapt procedural weight only if evidence_count >= threshold."""
        rows = self.spine.execute_sql(
            "SELECT * FROM hitl_feedback WHERE id = ?",
            (feedback_id,),
        )
        if not rows:
            raise KeyError(f"hitl_feedback id '{feedback_id}' not found")
        row = rows[0]
        repo = str(row["repo"])
        category = str(row["category"])

        # Count historical evidence for this (repo, category)
        history = self.spine.get_hitl_feedback(repo=repo, category=category)
        prior_resolved = [
            h
            for h in history
            if h["id"] != feedback_id
            and h["reviewer_action"] in ("approved", "rejected_false_positive", "disputed")
        ]
        evidence_count = len(prior_resolved) + 1

        current_decay = float(row.get("decay_weight", 1.0))
        adapted_procedural = False

        if evidence_count >= self.min_evidence_threshold:
            if reviewer_action in ("rejected_false_positive", "disputed"):
                current_decay = round(
                    max(0.1, current_decay * self.decay_factor_per_rejection), 4
                )
                adapted_procedural = True
            elif reviewer_action == "approved":
                current_decay = round(
                    min(1.0, current_decay * self.boost_factor_per_approval), 4
                )
                adapted_procedural = True

            # Persist updated procedural convention weight when evidence threshold is met
            self.spine.upsert_procedural_convention(
                repo=repo,
                rule_key=f"AUTO-CALIBRATED-{category.upper()}",
                title=f"Calibrated sensitivity for {category}",
                description=f"Procedural weight calibrated from {evidence_count} verified HITL outcomes.",
                category=category,
                weight=current_decay,
            )

        self.spine.execute_sql(
            """
            UPDATE hitl_feedback
            SET reviewer_action = ?, reviewer_id = ?, notes = COALESCE(?, notes),
                decay_weight = ?, evidence_count = ?
            WHERE id = ?
            """,
            (
                reviewer_action,
                reviewer_id,
                notes,
                current_decay,
                evidence_count,
                feedback_id,
            ),
        )

        return {
            "feedback_id": feedback_id,
            "repo": repo,
            "category": category,
            "reviewer_action": reviewer_action,
            "evidence_count": evidence_count,
            "min_evidence_threshold": self.min_evidence_threshold,
            "decay_weight": current_decay,
            "adapted_procedural_memory": adapted_procedural,
        }
