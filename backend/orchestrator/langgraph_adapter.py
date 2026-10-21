"""LangGraph Adapter implementing WorkflowEngine with Send fan-out, per-node timeouts, and Redis checkpointing (M4).

INV-3: This is the ONLY module in the repository permitted to import LangGraph primitives.
INV-6: Every specialist node executes under strict timeout + graceful degradation so fan-in never deadlocks.
"""

from __future__ import annotations

import asyncio
import operator
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional, Sequence, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from backend.models import Finding
from backend.observability.events import EventSpineLogger
from backend.orchestrator.engine import (
    SPECIALIST_NAMES,
    AggregatorRunner,
    SpecialistRunner,
    WorkflowContext,
    WorkflowEngine,
    WorkflowState,
)


def _merge_dict_findings(
    left: Dict[str, List[Dict[str, Any]]],
    right: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    merged = dict(left or {})
    for k, v in (right or {}).items():
        merged[k] = v
    return merged


def _merge_dict_str(
    left: Dict[str, str],
    right: Dict[str, str],
) -> Dict[str, str]:
    merged = dict(left or {})
    for k, v in (right or {}).items():
        merged[k] = v
    return merged


def _merge_dict_int(
    left: Dict[str, int],
    right: Dict[str, int],
) -> Dict[str, int]:
    merged = dict(left or {})
    for k, v in (right or {}).items():
        merged[k] = v
    return merged


class _GraphState(TypedDict):
    context: Dict[str, Any]
    pending_specialists: List[str]
    completed_nodes: Annotated[Dict[str, List[Dict[str, Any]]], _merge_dict_findings]
    node_errors: Annotated[Dict[str, str], _merge_dict_str]
    node_latencies_ms: Annotated[Dict[str, int], _merge_dict_int]
    aggregated_findings: List[Dict[str, Any]]
    status: str


class _SpecialistBranchInput(TypedDict):
    specialist_name: str
    context: Dict[str, Any]


class RedisCheckpointStore:
    """Persists WorkflowContext and WorkflowState checkpoints in Redis (compatible with Upstash & fakeredis)."""

    def __init__(self, redis_client: Any, ttl_seconds: int = 86400) -> None:
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

    def _state_key(self, delivery_id: str) -> str:
        return f"pr_reviewer:checkpoint:state:{delivery_id}"

    def _ctx_key(self, delivery_id: str) -> str:
        return f"pr_reviewer:checkpoint:context:{delivery_id}"

    async def save_context(self, context: WorkflowContext) -> None:
        await self.redis.set(
            self._ctx_key(context.delivery_id),
            context.model_dump_json(),
            ex=self.ttl_seconds,
        )

    async def load_context(self, delivery_id: str) -> Optional[WorkflowContext]:
        raw = await self.redis.get(self._ctx_key(delivery_id))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return WorkflowContext.model_validate_json(raw)

    async def save_state(self, state: WorkflowState) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        await self.redis.set(
            self._state_key(state.delivery_id),
            state.model_dump_json(),
            ex=self.ttl_seconds,
        )

    async def record_node_checkpoint(
        self,
        delivery_id: str,
        trace_id: str,
        specialist_name: str,
        findings: List[Finding],
        error: Optional[str],
        latency_ms: int,
    ) -> WorkflowState:
        """Atomically merge a single specialist node's output into the Redis checkpoint."""
        async with self._lock:
            current = await self.load_state(delivery_id)
            if current is None:
                current = WorkflowState(
                    delivery_id=delivery_id,
                    trace_id=trace_id,
                    status="running",
                )
            current.completed_nodes[specialist_name] = findings
            current.node_latencies_ms[specialist_name] = latency_ms
            if error:
                current.node_errors[specialist_name] = error
            await self.save_state(current)
            return current

    async def load_state(self, delivery_id: str) -> Optional[WorkflowState]:
        raw = await self.redis.get(self._state_key(delivery_id))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return WorkflowState.model_validate_json(raw)


async def _default_specialist_runner(
    specialist_name: str, context: WorkflowContext
) -> List[Finding]:
    """Default specialist runner delegating to M5 specialists module."""
    from backend.agents.specialists import run_specialist_agent

    return await run_specialist_agent(specialist_name, context)


async def _default_aggregator_runner(
    context: WorkflowContext,
    completed_nodes: Dict[str, List[Finding]],
    node_errors: Dict[str, str],
) -> List[Finding]:
    """Default aggregator runner delegating to M5 aggregator module."""
    from backend.agents.aggregator import aggregate_and_deduplicate_findings

    all_findings: List[Finding] = []
    for findings in completed_nodes.values():
        all_findings.extend(findings)
    return aggregate_and_deduplicate_findings(all_findings)


class LangGraphEngine(WorkflowEngine):
    """LangGraph implementation of WorkflowEngine using parallel Send API fan-out and Redis checkpointing."""

    def __init__(
        self,
        checkpoint_store: RedisCheckpointStore,
        specialist_runner: Optional[SpecialistRunner] = None,
        aggregator_runner: Optional[AggregatorRunner] = None,
        node_timeout_seconds: float = 30.0,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.05,
        event_logger: Optional[EventSpineLogger] = None,
        specialist_names: Sequence[str] = SPECIALIST_NAMES,
    ) -> None:
        self.checkpoint_store = checkpoint_store
        self.specialist_runner = specialist_runner or _default_specialist_runner
        self.aggregator_runner = aggregator_runner or _default_aggregator_runner
        self.node_timeout_seconds = float(node_timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.event_logger = event_logger
        self.specialist_names = tuple(specialist_names)
        self._compiled_graph = self._build_graph()

    def _build_graph(self) -> Any:
        builder = StateGraph(_GraphState)

        async def dispatch_node(state: _GraphState) -> Dict[str, Any]:
            return {"status": "running"}

        def fan_out_edges(state: _GraphState) -> Sequence[Send]:
            pending = state.get("pending_specialists") or list(self.specialist_names)
            return [
                Send(
                    "execute_specialist",
                    {
                        "specialist_name": name,
                        "context": state["context"],
                    },
                )
                for name in pending
            ]

        async def execute_specialist(branch: _SpecialistBranchInput) -> Dict[str, Any]:
            name = branch["specialist_name"]
            ctx = WorkflowContext.model_validate(branch["context"])
            start_ns = time.perf_counter_ns()
            findings: List[Finding] = []
            error_msg: Optional[str] = None

            for attempt in range(self.max_retries + 1):
                try:
                    findings = await asyncio.wait_for(
                        self.specialist_runner(name, ctx),
                        timeout=self.node_timeout_seconds,
                    )
                    error_msg = None
                    break
                except asyncio.TimeoutError:
                    error_msg = (
                        f"timeout after {self.node_timeout_seconds}s "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                except Exception as exc:
                    error_msg = (
                        f"error: {exc} "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))

            latency_ms = max(1, int((time.perf_counter_ns() - start_ns) / 1_000_000))

            # Checkpoint node completion immediately in Redis
            await self.checkpoint_store.record_node_checkpoint(
                delivery_id=ctx.delivery_id,
                trace_id=ctx.trace_id,
                specialist_name=name,
                findings=findings,
                error=error_msg,
                latency_ms=latency_ms,
            )

            if self.event_logger is not None:
                await self.event_logger.log_event(
                    trace_id=ctx.trace_id,
                    delivery_id=ctx.delivery_id,
                    agent_name=name,
                    event_type="specialist_node_finished",
                    status="error" if error_msg else "ok",
                    latency_ms=latency_ms,
                    metadata={
                        "findings_count": len(findings),
                        "error": error_msg,
                    },
                )

            serialized = [f.model_dump() for f in findings]
            update: Dict[str, Any] = {
                "completed_nodes": {name: serialized},
                "node_latencies_ms": {name: latency_ms},
            }
            if error_msg:
                update["node_errors"] = {name: error_msg}
            return update

        async def fan_in_aggregator(state: _GraphState) -> Dict[str, Any]:
            ctx = WorkflowContext.model_validate(state["context"])
            raw_completed = state.get("completed_nodes") or {}
            completed_models: Dict[str, List[Finding]] = {
                k: [Finding.model_validate(item) for item in items]
                for k, items in raw_completed.items()
            }
            node_errors = dict(state.get("node_errors") or {})
            aggregated = await self.aggregator_runner(ctx, completed_models, node_errors)

            if self.event_logger is not None:
                await self.event_logger.log_event(
                    trace_id=ctx.trace_id,
                    delivery_id=ctx.delivery_id,
                    agent_name="aggregator",
                    event_type="fan_in_completed",
                    status="ok",
                    metadata={
                        "specialists_returned": list(completed_models.keys()),
                        "node_errors": node_errors,
                        "aggregated_count": len(aggregated),
                    },
                )

            return {
                "aggregated_findings": [f.model_dump() for f in aggregated],
                "status": "completed",
            }

        builder.add_node("dispatch", dispatch_node)
        builder.add_node("execute_specialist", execute_specialist)
        builder.add_node("aggregator", fan_in_aggregator)

        builder.add_edge(START, "dispatch")
        builder.add_conditional_edges("dispatch", fan_out_edges, ["execute_specialist"])
        builder.add_edge("execute_specialist", "aggregator")
        builder.add_edge("aggregator", END)

        return builder.compile()

    async def run(self, context: WorkflowContext) -> WorkflowState:
        """Execute all 4 specialist nodes in parallel via LangGraph Send API and checkpoint state."""
        await self.checkpoint_store.save_context(context)
        initial_state = WorkflowState(
            delivery_id=context.delivery_id,
            trace_id=context.trace_id,
            status="running",
        )
        await self.checkpoint_store.save_state(initial_state)

        graph_input: _GraphState = {
            "context": context.model_dump(),
            "pending_specialists": list(self.specialist_names),
            "completed_nodes": {},
            "node_errors": {},
            "node_latencies_ms": {},
            "aggregated_findings": [],
            "status": "running",
        }

        result: _GraphState = await self._compiled_graph.ainvoke(graph_input)
        return await self._finalize_state(context, result)

    async def resume(self, delivery_id: str) -> WorkflowState:
        """Resume an interrupted workflow from its Redis checkpoint, running only unfinished specialists."""
        context = await self.checkpoint_store.load_context(delivery_id)
        if context is None:
            raise KeyError(f"No checkpointed WorkflowContext found for delivery_id={delivery_id}")

        saved_state = await self.checkpoint_store.load_state(delivery_id)
        if saved_state is None:
            return await self.run(context)

        if saved_state.status == "completed" and len(saved_state.completed_nodes) == len(
            self.specialist_names
        ):
            return saved_state

        already_done = {
            name: [f.model_dump() for f in findings]
            for name, findings in saved_state.completed_nodes.items()
            if name not in saved_state.node_errors
        }
        pending = [name for name in self.specialist_names if name not in already_done]

        if not pending:
            # All specialist nodes finished; only run fan-in aggregator
            completed_models = {
                k: [Finding.model_validate(item) for item in items]
                for k, items in already_done.items()
            }
            aggregated = await self.aggregator_runner(
                context, completed_models, saved_state.node_errors
            )
            saved_state.aggregated_findings = aggregated
            saved_state.status = "completed"
            await self.checkpoint_store.save_state(saved_state)
            return saved_state

        graph_input: _GraphState = {
            "context": context.model_dump(),
            "pending_specialists": pending,
            "completed_nodes": already_done,
            "node_errors": {
                k: v for k, v in saved_state.node_errors.items() if k in already_done
            },
            "node_latencies_ms": dict(saved_state.node_latencies_ms),
            "aggregated_findings": [],
            "status": "running",
        }
        result: _GraphState = await self._compiled_graph.ainvoke(graph_input)
        return await self._finalize_state(context, result)

    async def get_state(self, delivery_id: str) -> Optional[WorkflowState]:
        return await self.checkpoint_store.load_state(delivery_id)

    async def _finalize_state(
        self, context: WorkflowContext, result: _GraphState
    ) -> WorkflowState:
        completed_models: Dict[str, List[Finding]] = {
            k: [Finding.model_validate(item) for item in items]
            for k, items in (result.get("completed_nodes") or {}).items()
        }
        aggregated_models = [
            Finding.model_validate(item)
            for item in (result.get("aggregated_findings") or [])
        ]
        final_state = WorkflowState(
            delivery_id=context.delivery_id,
            trace_id=context.trace_id,
            status="completed",
            completed_nodes=completed_models,
            node_errors=dict(result.get("node_errors") or {}),
            node_latencies_ms=dict(result.get("node_latencies_ms") or {}),
            aggregated_findings=aggregated_models,
        )
        await self.checkpoint_store.save_state(final_state)
        return final_state
