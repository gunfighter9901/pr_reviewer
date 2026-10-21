"""Milestone 5 Verification Tests: Specialist Agents, Prompt Registry, Model Router & Aggregator (INV-7)."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from backend.agents.aggregator import FindingAggregator, aggregate_and_deduplicate_findings
from backend.agents.specialists import ModelRouter, run_specialist_agent
from backend.models import Finding
from backend.orchestrator.engine import SPECIALIST_NAMES, WorkflowContext
from backend.orchestrator.langgraph_adapter import LangGraphEngine, RedisCheckpointStore
from backend.prompts.registry import PromptRegistry

SAMPLE_PR_DIFF = """diff --git a/backend/payments/processor.py b/backend/payments/processor.py
index 1111111..2222222 100644
--- a/backend/payments/processor.py
+++ b/backend/payments/processor.py
@@ -10,4 +10,12 @@ class PaymentProcessor:
+def execute_payment_rule(user_query, rule_expr, items=[]):
+    query = f"SELECT * FROM transactions WHERE user_id = {user_query}"
+    result = eval(rule_expr)
+    time.sleep(1)
+    return result
"""


def test_m5_prompt_registry_and_tiered_model_router() -> None:
    """Verify PromptRegistry versions and ModelRouter high-reasoning vs fast/cost-effective tiers."""
    registry = PromptRegistry()
    router = ModelRouter()

    for agent_type in SPECIALIST_NAMES:
        tmpl = registry.get(agent_type)
        assert tmpl.version_id.startswith(f"{agent_type}@v")
        assert len(tmpl.sha256) == 16

    # High-reasoning models for Security and Quality; cost-effective models for Testing and Docs
    assert router.get_model("security") == "claude-3-5-sonnet"
    assert router.get_model("quality") == "gpt-4o"
    assert router.get_model("testing") == "claude-3-5-haiku"
    assert router.get_model("docs") == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_m5_run_sample_pr_diff_through_all_4_specialists_and_aggregator() -> None:
    """Run sample PR diff through all 4 specialists via WorkflowEngine -> verify deduplicated Finding list."""
    redis_client = fakeredis.aioredis.FakeRedis()
    store = RedisCheckpointStore(redis_client)
    engine = LangGraphEngine(checkpoint_store=store)

    ctx = WorkflowContext(
        delivery_id="deliv-m5-001",
        trace_id="tr-m5-001",
        repo="acme-corp/payments",
        pr_number=77,
        commit_sha="999888777",
        is_public=True,
        diff=SAMPLE_PR_DIFF,
        changed_files=["backend/payments/processor.py"],
    )

    state = await engine.run(ctx)
    assert state.status == "completed"

    # All 4 specialists ran and returned findings on the sample diff
    for specialist in SPECIALIST_NAMES:
        assert specialist in state.completed_nodes
        assert len(state.completed_nodes[specialist]) >= 1

    # Notice: On line 10 (`def execute_payment_rule(...)`), Quality (mutable_default_argument),
    # Testing (missing_edge_case_tests), and Docs (missing_docstring) all emit findings on line 10!
    # And on line 12 (`result = eval(rule_expr)`), both Security (code_injection) and Quality (unsafe_runtime_eval)
    # emit findings on line 12!
    raw_total = sum(len(v) for v in state.completed_nodes.values())
    deduped = state.aggregated_findings

    assert len(deduped) < raw_total, "Aggregator must deduplicate overlapping findings on same file_path + line_start"

    # Verify line 12 was deduplicated into a single critical finding with boosted confidence and merged rationale
    line_12_findings = [f for f in deduped if f.line_start == 12]
    assert len(line_12_findings) == 1
    merged_eval = line_12_findings[0]
    assert merged_eval.severity == "critical"
    assert "[security]" in merged_eval.rationale
    assert "[quality]" in merged_eval.rationale
    assert merged_eval.confidence > 0.94

    # Verify every emitted finding satisfies INV-7 (Grounded Evidence)
    for f in deduped:
        assert f.file_path == "backend/payments/processor.py"
        assert f.line_start >= 10
        assert f.line_end >= f.line_start
        assert len(f.evidence_snippet) > 0
        assert len(f.rationale) > 10
        assert 0.0 <= f.confidence <= 1.0
        assert "@v" in f.prompt_version


def test_inv7_aggregator_drops_ungrounded_findings() -> None:
    """INV-7: Verify ungrounded findings (missing file_path, invalid lines, empty rationale) are rejected."""
    aggregator = FindingAggregator()
    raw_items = [
        # Valid finding
        {
            "agent_type": "security",
            "severity": "high",
            "category": "sql_injection",
            "file_path": "app/db.py",
            "line_start": 15,
            "line_end": 15,
            "evidence_snippet": "cursor.execute(f'SELECT {x}')",
            "rationale": "Direct string interpolation into SQL statement.",
            "suggested_fix": "Use parameterized query.",
            "confidence": 0.91,
            "prompt_version": "security@v1.2.0",
        },
        # Invalid: blank file_path
        {
            "agent_type": "quality",
            "severity": "medium",
            "category": "bug",
            "file_path": "   ",
            "line_start": 10,
            "line_end": 10,
            "evidence_snippet": "x = 1",
            "rationale": "Some rationale",
            "confidence": 0.80,
            "prompt_version": "quality@v1.2.0",
        },
        # Invalid: line_end < line_start
        {
            "agent_type": "testing",
            "severity": "low",
            "category": "test",
            "file_path": "app/db.py",
            "line_start": 20,
            "line_end": 12,
            "evidence_snippet": "assert x",
            "rationale": "Invalid line range",
            "confidence": 0.70,
            "prompt_version": "testing@v1.1.0",
        },
    ]

    result = aggregator.aggregate(raw_items)
    assert len(result) == 1
    assert result[0].file_path == "app/db.py"
    assert result[0].line_start == 15
