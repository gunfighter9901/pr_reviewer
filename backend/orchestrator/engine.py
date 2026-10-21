"""Abstract WorkflowEngine interface (M4, INV-3: Framework Agnosticism).

Domain and agent modules must depend exclusively on this interface and never import LangGraph directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from backend.models import Finding

SPECIALIST_NAMES: tuple[str, ...] = ("security", "quality", "testing", "docs")


class WorkflowContext(BaseModel):
    """Input context passed into WorkflowEngine.run()."""

    delivery_id: str
    trace_id: str
    repo: str
    pr_number: int
    commit_sha: str
    is_public: bool = True
    diff: str = ""
    changed_files: List[str] = Field(default_factory=list)
    retrieved_context: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowState(BaseModel):
    """Serializable workflow execution state persisted across checkpoints."""

    delivery_id: str
    trace_id: str
    status: Literal["queued", "running", "interrupted", "completed", "failed"] = "queued"
    completed_nodes: Dict[str, List[Finding]] = Field(default_factory=dict)
    node_errors: Dict[str, str] = Field(default_factory=dict)
    node_latencies_ms: Dict[str, int] = Field(default_factory=dict)
    aggregated_findings: List[Finding] = Field(default_factory=list)
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


SpecialistRunner = Callable[[str, WorkflowContext], Awaitable[List[Finding]]]
AggregatorRunner = Callable[
    [WorkflowContext, Dict[str, List[Finding]], Dict[str, str]],
    Awaitable[List[Finding]],
]


class WorkflowEngine(ABC):
    """Abstract orchestration interface isolating domain logic from LangGraph/Temporal (INV-3)."""

    @abstractmethod
    async def run(self, context: WorkflowContext) -> WorkflowState:
        """Execute the 4-specialist fan-out and fan-in aggregation graph."""

    @abstractmethod
    async def resume(self, delivery_id: str) -> WorkflowState:
        """Resume an interrupted or partially checkpointed workflow from durable state."""

    @abstractmethod
    async def get_state(self, delivery_id: str) -> Optional[WorkflowState]:
        """Retrieve current checkpointed WorkflowState for delivery_id."""
