"""Core data contracts and domain schemas (Section 4 of AGENTS.md)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


AgentType = Literal["security", "quality", "testing", "docs"]
SeverityLevel = Literal["critical", "high", "medium", "low", "info"]
RoutingDecision = Literal["auto_post", "hitl_escalate", "private_security_alert"]
ReviewStatus = Literal["queued", "processing", "auto_posted", "escalated_hitl", "completed"]


class Finding(BaseModel):
    """The strict Finding contract emitted by every specialist agent and aggregator (INV-7)."""

    model_config = ConfigDict(extra="forbid")

    agent_type: AgentType
    severity: SeverityLevel
    category: str = Field(min_length=1, description="e.g., sql_injection, null_dereference")
    file_path: str = Field(min_length=1, description="Exact repo-relative file path")
    line_start: int = Field(ge=1, description="Exact starting line in the diff")
    line_end: int = Field(ge=1, description="Exact ending line in the diff")
    evidence_snippet: str = Field(min_length=1, description="Exact code slice cited from the diff")
    rationale: str = Field(
        min_length=1,
        description="Skeptical, step-by-step technical proof of why this fails",
    )
    suggested_fix: Optional[str] = Field(
        default=None, description="Concrete code patch or remediation step"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    prompt_version: str = Field(min_length=1, description="Version ID from the Prompt Registry")

    @model_validator(mode="after")
    def _validate_grounded_evidence(self) -> "Finding":
        """Enforce INV-7: Grounded Evidence."""
        if not self.file_path.strip():
            raise ValueError("INV-7 Violation: file_path must not be blank")
        if not self.category.strip():
            raise ValueError("INV-7 Violation: category must not be blank")
        if not self.evidence_snippet.strip():
            raise ValueError("INV-7 Violation: evidence_snippet must not be blank")
        if not self.rationale.strip():
            raise ValueError("INV-7 Violation: rationale must not be blank")
        if not self.prompt_version.strip():
            raise ValueError("INV-7 Violation: prompt_version must not be blank")
        if self.line_end < self.line_start:
            raise ValueError(
                f"INV-7 Violation: line_end ({self.line_end}) must be >= line_start ({self.line_start})"
            )
        return self


class PullRequestHead(BaseModel):
    sha: str = Field(min_length=1)
    ref: str = Field(default="feature-branch")


class PullRequestBase(BaseModel):
    sha: str = Field(default="base-sha")
    ref: str = Field(default="main")


class PullRequestInfo(BaseModel):
    number: int = Field(ge=1)
    title: str = Field(default="")
    body: Optional[str] = Field(default="")
    head: PullRequestHead
    base: PullRequestBase = Field(default_factory=PullRequestBase)
    diff_url: Optional[str] = None


class RepositoryInfo(BaseModel):
    full_name: str = Field(min_length=1)
    private: bool = Field(default=False)


class PullRequestWebhookPayload(BaseModel):
    """Validated GitHub pull_request webhook payload."""

    action: str = Field(min_length=1)
    number: Optional[int] = None
    pull_request: PullRequestInfo
    repository: RepositoryInfo
    diff: Optional[str] = None
    changed_files: Optional[List[str]] = None

    @property
    def pr_number(self) -> int:
        return self.number or self.pull_request.number


class QueuedPRJob(BaseModel):
    """Normalized job payload enqueued from Stateless Ingress into ARQ/Redis."""

    delivery_id: str
    trace_id: str
    repo: str
    pr_number: int
    commit_sha: str
    action: str
    is_public: bool
    diff: str = ""
    changed_files: List[str] = Field(default_factory=list)
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = Field(default_factory=dict)
