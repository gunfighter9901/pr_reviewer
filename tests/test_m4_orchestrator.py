"""Milestone 4 Verification Tests: Redis Queue & LangGraph Orchestrator (INV-2, INV-3, INV-6)."""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from typing import Dict, List

import fakeredis.aioredis
import pytest

from backend.models import Finding, QueuedPRJob
from backend.orchestrator.engine import (
    SPECIALIST_NAMES,
    WorkflowContext,
    WorkflowEngine,
    WorkflowState,
)
from backend.orchestrator.langgraph_adapter import LangGraphEngine, RedisCheckpointStore
from backend.worker import RedisARQWorkerQueue


def _make_finding(agent_type: str, line: int = 10) -> Finding:
    return Finding(
        agent_type=agent_type,  # type: ignore[arg-type]
        severity="high",
        category=f"{agent_type}_issue",
        file_path="src/service.py",
        line_start=line,
        line_end=line + 2,
        evidence_snippet="result = eval(user_input)",
        rationale=f"{agent_type} specialist verified unsafe execution path.",
        suggested_fix="result = ast.literal_eval(user_input)",
        confidence=0.88,
        prompt_version=f"{agent_type}@v1.0.0",
    )


def _sample_context(delivery_id: str = "deliv-m4-001") -> WorkflowContext:
    return WorkflowContext(
        delivery_id=delivery_id,
        trace_id=f"tr-{delivery_id}",
        repo="acme-corp/core-api",
        pr_number=55,
        commit_sha="cafebabe1234",
        is_public=True,
        diff="@@ -1,3 +1,4 @@\n+result = eval(user_input)\n",
        changed_files=["src/service.py"],
    )


@pytest.mark.asyncio
async def test_m4_parallel_4_node_fan_out_and_fan_in() -> None:
    """Verify all 4 specialist nodes execute concurrently via LangGraph Send API and fan-in aggregates."""
    redis_client = fakeredis.aioredis.FakeRedis()
    store = RedisCheckpointStore(redis_client)

    async def fast_specialist(name: str, ctx: WorkflowContext) -> List[Finding]:
        await asyncio.sleep(0.05)
        idx = SPECIALIST_NAMES.index(name)
        return [_make_finding(name, line=10 + idx * 5)]

    engine: WorkflowEngine = LangGraphEngine(
        checkpoint_store=store,
        specialist_runner=fast_specialist,
        node_timeout_seconds=2.0,
    )

    start = time.perf_counter()
    state = await engine.run(_sample_context("deliv-m4-parallel"))
    elapsed = time.perf_counter() - start

    assert state.status == "completed"
    assert set(state.completed_nodes.keys()) == {"security", "quality", "testing", "docs"}
    assert len(state.node_errors) == 0
    assert len(state.aggregated_findings) == 4
    # 4 nodes sleeping 50ms in parallel should complete well under 180ms sequential time
    assert elapsed < 0.18


@pytest.mark.asyncio
async def test_inv6_hung_specialist_timeout_does_not_deadlock_fan_in() -> None:
    """INV-6: Simulate a hung specialist node exceeding timeout; verify fan-in completes with remaining 3 nodes."""
    redis_client = fakeredis.aioredis.FakeRedis()
    store = RedisCheckpointStore(redis_client)

    async def faulty_specialist(name: str, ctx: WorkflowContext) -> List[Finding]:
        if name == "testing":
            # Simulate hung LLM call exceeding node_timeout_seconds
            await asyncio.sleep(5.0)
        idx = SPECIALIST_NAMES.index(name)
        return [_make_finding(name, line=10 + idx * 5)]

    engine = LangGraphEngine(
        checkpoint_store=store,
        specialist_runner=faulty_specialist,
        node_timeout_seconds=0.08,
        max_retries=0,
    )

    state = await engine.run(_sample_context("deliv-m4-timeout"))
    assert state.status == "completed"
    # 'testing' timed out and degraded gracefully; other 3 returned findings
    assert "testing" in state.node_errors
    assert "timeout" in state.node_errors["testing"]
    assert len(state.completed_nodes["testing"]) == 0
    assert len(state.completed_nodes["security"]) == 1
    assert len(state.completed_nodes["quality"]) == 1
    assert len(state.completed_nodes["docs"]) == 1
    assert len(state.aggregated_findings) == 3


@pytest.mark.asyncio
async def test_m4_worker_kill_checkpoint_and_stateful_resume() -> None:
    """Simulate a worker killed mid-flight after 2 specialists checkpointed; verify resume() runs only remaining 2."""
    redis_client = fakeredis.aioredis.FakeRedis()
    store = RedisCheckpointStore(redis_client)
    ctx = _sample_context("deliv-m4-resume")

    # Save context and simulate 2 nodes ('security' and 'quality') already checkpointed before worker kill
    await store.save_context(ctx)
    await store.record_node_checkpoint(
        delivery_id=ctx.delivery_id,
        trace_id=ctx.trace_id,
        specialist_name="security",
        findings=[_make_finding("security", line=10)],
        error=None,
        latency_ms=45,
    )
    await store.record_node_checkpoint(
        delivery_id=ctx.delivery_id,
        trace_id=ctx.trace_id,
        specialist_name="quality",
        findings=[_make_finding("quality", line=30)],
        error=None,
        latency_ms=50,
    )

    executed_on_resume: List[str] = []

    async def resume_tracking_specialist(name: str, wf_ctx: WorkflowContext) -> List[Finding]:
        executed_on_resume.append(name)
        return [_make_finding(name, line=50 if name == "testing" else 70)]

    engine = LangGraphEngine(
        checkpoint_store=store,
        specialist_runner=resume_tracking_specialist,
        node_timeout_seconds=1.0,
    )

    partial_state = await engine.get_state(ctx.delivery_id)
    assert partial_state is not None
    assert set(partial_state.completed_nodes.keys()) == {"security", "quality"}

    resumed_state = await engine.resume(ctx.delivery_id)
    assert resumed_state.status == "completed"
    # Only the 2 unfinished nodes ('testing' and 'docs') were executed on resume!
    assert set(executed_on_resume) == {"testing", "docs"}
    assert set(resumed_state.completed_nodes.keys()) == {"security", "quality", "testing", "docs"}
    assert len(resumed_state.aggregated_findings) == 4


@pytest.mark.asyncio
async def test_m4_redis_arq_worker_queue_and_deduplication() -> None:
    """Verify RedisARQWorkerQueue deduplicates deliveries atomically and processes queued jobs."""
    redis_client = fakeredis.aioredis.FakeRedis()
    store = RedisCheckpointStore(redis_client)

    async def stub_specialist(name: str, ctx: WorkflowContext) -> List[Finding]:
        return [_make_finding(name, line=12)]

    engine = LangGraphEngine(checkpoint_store=store, specialist_runner=stub_specialist)
    worker_queue = RedisARQWorkerQueue(redis_client=redis_client, engine=engine)

    job = QueuedPRJob(
        delivery_id="deliv-m4-queue-1",
        trace_id="tr-m4-queue-1",
        repo="acme/repo",
        pr_number=9,
        commit_sha="1234567890",
        action="opened",
        is_public=True,
        diff="@@ -1 +1 @@\n+eval(x)",
        changed_files=["src/service.py"],
    )

    first_enqueued = await worker_queue.enqueue_job(job)
    duplicate_enqueued = await worker_queue.enqueue_job(job)

    assert first_enqueued is True
    assert duplicate_enqueued is False

    state = await worker_queue.dequeue_and_process_one()
    assert state is not None
    assert state.status == "completed"
    assert await worker_queue.dequeue_and_process_one() is None


def test_inv3_framework_agnosticism_no_langgraph_imports_outside_adapter() -> None:
    """INV-3: Verify zero modules outside backend/orchestrator/langgraph_adapter.py import langgraph."""
    backend_root = Path(__file__).resolve().parent.parent / "backend"
    allowed_file = (backend_root / "orchestrator" / "langgraph_adapter.py").resolve()

    violations: List[str] = []
    for py_file in backend_root.rglob("*.py"):
        if py_file.resolve() == allowed_file:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "langgraph" or alias.name.startswith("langgraph."):
                        violations.append(f"{py_file}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "langgraph" or mod.startswith("langgraph."):
                    violations.append(f"{py_file}: from {mod} import ...")

    assert violations == [], f"INV-3 violated by LangGraph imports outside adapter: {violations}"
