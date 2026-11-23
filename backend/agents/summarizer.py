"""Balanced Summarizer Agent: fan-in synthesis over the four specialists (M5-style agent).

The summarizer is a first-class agent like Security/Quality/Testing/Docs: it
resolves a versioned prompt, resolves its model via the shared ModelRouter, emits
observability events, and degrades gracefully to the deterministic synthesis
when no live provider is configured.

Balance contract: every routed finding is represented exactly once, ordered by
risk (critical first), with per-specialist coverage reported in event metadata
so one specialist can never silently dominate or disappear.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from backend.agents.aggregator import (
    PRSummary,
    render_pr_summary_markdown,
    summarize_pr_findings,
)
from backend.agents.specialists import LLMCompletionCallable, ModelRouter
from backend.models import Finding
from backend.observability.events import EventSpineLogger
from backend.prompts.registry import PromptRegistry, PromptTemplate, get_prompt_registry


class SummarizerAgent:
    """Balanced fan-in agent turning aggregated findings into one PR verdict."""

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
        findings: Iterable[Finding],
        *,
        withheld_private: int = 0,
        withheld_hitl: int = 0,
        trace_id: str = "",
        delivery_id: str = "",
    ) -> Tuple[PRSummary, str]:
        """Build the balanced single-PR summary and its GitHub comment body."""
        tmpl = self.prompt_template
        model = self.model_name
        finding_list = list(findings)

        summary = summarize_pr_findings(repo, pr_number, commit_sha, finding_list)
        markdown = render_pr_summary_markdown(
            summary,
            withheld_private=withheld_private,
            withheld_hitl=withheld_hitl,
        )

        if self.event_logger is not None:
            per_agent: Dict[str, int] = {}
            per_severity: Dict[str, int] = {}
            for f in finding_list:
                per_agent[f.agent_type] = per_agent.get(f.agent_type, 0) + 1
                per_severity[f.severity] = per_severity.get(f.severity, 0) + 1
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
                    "withheld_private": withheld_private,
                    "withheld_hitl": withheld_hitl,
                    "llm_status": "disabled" if self.llm_callable is None else "standby",
                },
            )

        return summary, markdown
