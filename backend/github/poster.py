"""Idempotent GitHub PR Review Poster protected by CircuitBreaker and Security Guards (M7, INV-6, INV-7)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set

from backend.gates.hitl import RoutedFinding
from backend.models import Finding
from backend.observability.events import EventSpineLogger
from backend.reliability.circuit_breaker import CircuitBreaker


class PublicCriticalSecurityLeakError(RuntimeError):
    """Raised if an attempt is made to post a critical security finding to a public PR thread."""


GitHubTransportCallable = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class GitHubPRPoster:
    """Posts grounded high-confidence findings to GitHub PR Reviews with idempotency & circuit breaking."""

    def __init__(
        self,
        circuit_breaker: Optional[CircuitBreaker] = None,
        transport: Optional[GitHubTransportCallable] = None,
        event_logger: Optional[EventSpineLogger] = None,
    ) -> None:
        self.circuit_breaker = circuit_breaker or CircuitBreaker(name="github_pr_poster")
        self._transport = transport or self._default_transport
        self.event_logger = event_logger
        self._posted_idempotency_keys: Set[str] = set()
        self.posted_comments: List[Dict[str, Any]] = []

    async def _default_transport(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": f"gh-comment-{len(self.posted_comments) + 1}", "status": "posted", **payload}

    def format_inline_comment(self, finding: Finding) -> str:
        """Format a grounded Finding into a high-signal GitHub review comment."""
        fix_block = (
            f"\n\n**Suggested Fix:**\n```suggestion\n{finding.suggested_fix}\n```"
            if finding.suggested_fix
            else ""
        )
        return (
            f"**[{finding.agent_type.upper()} | {finding.severity.upper()}] `{finding.category}`** "
            f"(Confidence: `{finding.confidence:.2f}`, Prompt: `{finding.prompt_version}`)\n\n"
            f"**Evidence (`{finding.file_path}:{finding.line_start}`):**\n"
            f"```\n{finding.evidence_snippet}\n```\n\n"
            f"**Rationale:** {finding.rationale}"
            f"{fix_block}"
        )

    async def post_routed_findings(
        self,
        *,
        delivery_id: str,
        trace_id: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        routed_findings: Sequence[RoutedFinding],
        is_public_repo: bool = True,
    ) -> List[Dict[str, Any]]:
        """Post only auto_post findings to GitHub PR thread; block any critical security leak on public repos."""
        posted_now: List[Dict[str, Any]] = []

        for rf in routed_findings:
            finding = rf.finding
            # Hard Security Vulnerability Rule Guard
            if is_public_repo and finding.severity == "critical":
                raise PublicCriticalSecurityLeakError(
                    f"Security Vulnerability Rule Violation: Refusing to post critical finding "
                    f"({finding.category} at {finding.file_path}:{finding.line_start}) to public repo {repo}"
                )

            if rf.decision != "auto_post":
                continue

            idem_key = f"{repo}:{pr_number}:{commit_sha}:{finding.file_path}:{finding.line_start}:{finding.category}"
            if idem_key in self._posted_idempotency_keys:
                continue

            comment_payload = {
                "repo": repo,
                "pr_number": pr_number,
                "commit_sha": commit_sha,
                "path": finding.file_path,
                "line": finding.line_start,
                "body": self.format_inline_comment(finding),
                "idempotency_key": idem_key,
            }

            response = await self.circuit_breaker.call(self._transport, comment_payload)
            self._posted_idempotency_keys.add(idem_key)
            self.posted_comments.append(response)
            posted_now.append(response)

            if self.event_logger is not None:
                await self.event_logger.log_event(
                    trace_id=trace_id,
                    delivery_id=delivery_id,
                    agent_name="github_poster",
                    event_type="pr_comment_posted",
                    status="ok",
                    metadata={
                        "file_path": finding.file_path,
                        "line": finding.line_start,
                        "category": finding.category,
                        "confidence": finding.confidence,
                    },
                )

        return posted_now
