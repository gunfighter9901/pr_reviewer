"""Balanced Summarizer Agent: fan-in synthesis over the four specialists (M5-style agent).

The summarizer is a first-class agent like Security/Quality/Testing/Docs: it
resolves a versioned prompt, resolves its model via the shared ModelRouter, emits
observability events, and degrades gracefully to the deterministic synthesis
when no live provider is configured.

Balance contract: every specialist's returned summary is represented exactly
once, ordered security → quality → testing → docs, and the agent adds its own
verdict with suggestions — so the GitHub comment reports the full analysis
behind the decision instead of a bare approval.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from backend.agents.aggregator import (
    PRSummary,
    render_pr_summary_markdown,
    summarize_pr_findings,
)
from backend.agents.specialists import (
    AGENT_FOCUS,
    LLMCompletionCallable,
    ModelRouter,
    SpecialistSummary,
)
from backend.models import Finding
from backend.observability.events import EventSpineLogger
from backend.prompts.registry import PromptRegistry, PromptTemplate, get_prompt_registry


AGENT_ORDER: Tuple[str, ...] = ("security", "quality", "testing", "docs")

VERDICT_LABELS: Dict[str, str] = {
    "auto_posted": "POST",
    "escalated_hitl": "ESCALATED TO HUMAN",
    "completed": "ALL-CLEAR",
}


class SummarizerAgent:
    """Balanced fan-in agent turning specialist summaries into one PR verdict."""

    agent_type = "summarizer"

    def __init__(
        self,
        prompt_registry: Optional[PromptRegistry] = None,
        model_router: Optional[ModelRouter] = None,
        llm_callable: Optional[LLMCompletionCallable] = None,
        event_logger: Optional[EventSpineLogger] = None,
    ) -> None:
        self.prompt_registry = prompt_registry or get_prompt_registry()
        self.model_router = model_router or ModelRouter()
        # Reserved for a future live rewrite pass; deterministic synthesis is
        # always the ground truth so the comment can never invent findings.
        self.llm_callable = llm_callable
        self.event_logger = event_logger

    @property
    def prompt_template(self) -> PromptTemplate:
        return self.prompt_registry.get(self.agent_type)

    @property
    def model_name(self) -> str:
        return self.model_router.get_model(self.agent_type)

    async def summarize(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        findings: List[Finding],
        *,
        withheld_private: int = 0,
        withheld_hitl: int = 0,
        trace_id: str = "",
        delivery_id: str = "",
        specialist_summaries: Optional[List[SpecialistSummary]] = None,
        aggregator_decision: str = "completed",
        aggregator_suggestion: str = "Safe to merge.",
    ) -> Tuple[PRSummary, str]:
        """Build the balanced single-PR summary and its GitHub comment body.

        Each specialist's returned summary is rendered first (what it analyzed
        and its decision); the agent then adds its own verdict with
        suggestions, so the comment always shows the analysis, never just an
        approval.
        """
        tmpl = self.prompt_template
        model = self.model_name
        finding_list = list(findings)

        summary = summarize_pr_findings(repo, pr_number, commit_sha, finding_list)
        markdown = render_pr_summary_markdown(
            summary,
            withheld_private=withheld_private,
            withheld_hitl=withheld_hitl,
        )
        if specialist_summaries:
            markdown = markdown.rstrip() + "\n\n" + self._render_specialist_summaries(
                specialist_summaries
            ) + "\n"
        markdown = (
            markdown.rstrip()
            + "\n\n"
            + self._render_verdict(aggregator_decision, aggregator_suggestion)
            + "\n"
        )

        if self.event_logger is not None:
            per_agent: Dict[str, int] = {}
            per_severity: Dict[str, int] = {}
            for f in finding_list:
                per_agent[f.agent_type] = per_agent.get(f.agent_type, 0) + 1
                per_severity[f.severity] = per_severity.get(f.severity, 0) + 1
            decisions = (
                {s.agent_type: s.decision for s in (specialist_summaries or [])}
                if specialist_summaries
                else {}
            )
            await self.event_logger.log_event(
                trace_id=trace_id,
                delivery_id=delivery_id,
                agent_name=self.agent_type,
                event_type="summarizer_completed",
                model=model,
                prompt_version=tmpl.version_id,
                status="ok",
                metadata={
                    "total_points": summary.total_points,
                    "risk_level": summary.risk_level,
                    "per_agent": per_agent,
                    "per_severity": per_severity,
                    "specialist_decisions": decisions,
                    "aggregator_decision": aggregator_decision,
                    "withheld_private": withheld_private,
                    "withheld_hitl": withheld_hitl,
                    "llm_status": "disabled" if self.llm_callable is None else "standby",
                },
            )

        return summary, markdown

    @staticmethod
    def _render_specialist_summaries(summaries: List[SpecialistSummary]) -> str:
        """Render each specialist's returned summary: scope analyzed + decision."""
        ordered = sorted(
            summaries,
            key=lambda s: (
                AGENT_ORDER.index(s.agent_type)
                if s.agent_type in AGENT_ORDER
                else len(AGENT_ORDER)
            ),
        )
        scope_lines = max((s.lines_scanned for s in ordered), default=0)
        scope_files = max((s.files_scanned for s in ordered), default=0)
        scope = f"{scope_lines} added line(s)"
        if scope_files:
            scope += f" across {scope_files} file(s)"
        lines = [
            "---",
            "### Sub-agent analysis",
            f"Scope: {scope}. Each specialist below analyzed the full diff; counts are grounded findings raised.",
            "",
        ]
        for s in ordered:
            focus = s.focus or AGENT_FOCUS.get(s.agent_type, "")
            lines.append(
                f"- **{s.agent_type.capitalize()}** ({focus}): {s.note}. "
                f"Decision: {s.decision}."
            )
        return "\n".join(lines)

    @classmethod
    def _render_verdict(cls, decision: str, suggestion: str) -> str:
        """Render the aggregator's own verdict with suggestions."""
        label = VERDICT_LABELS.get(decision, decision.upper())
        lines = [
            "---",
            "### Aggregator verdict",
            f"Decision: **{label}**.",
        ]
        if suggestion:
            lines.append(f"Suggestion: {suggestion}")
        return "\n".join(lines)
