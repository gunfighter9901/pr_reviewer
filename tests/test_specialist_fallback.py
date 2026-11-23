"""Specialist resilience: Gemini failures must not drop deterministic findings."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from backend.agents.specialists import SecurityAgent
from backend.orchestrator.engine import WorkflowContext

DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,2 +1,3 @@
 import sqlite3
+def lookup(conn, user_id):
+    return conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
"""


def _context() -> WorkflowContext:
    return WorkflowContext(
        delivery_id="deliv-fallback-1",
        trace_id="tr-fallback-1",
        repo="acme/demo",
        pr_number=7,
        commit_sha="abc123",
        is_public=True,
        diff=DIFF,
        changed_files=["app/db.py"],
    )


@pytest.mark.asyncio
async def test_llm_outage_falls_back_to_deterministic_findings() -> None:
    async def failing_llm(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        raise RuntimeError("Gemini API returned HTTP 429")

    agent = SecurityAgent(llm_callable=failing_llm)
    findings = await agent.analyze(_context())

    assert [finding.category for finding in findings] == ["sql_injection"]
    assert findings[0].confidence == 0.95


@pytest.mark.asyncio
async def test_empty_llm_result_keeps_deterministic_findings() -> None:
    async def empty_llm(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    agent = SecurityAgent(llm_callable=empty_llm)
    findings = await agent.analyze(_context())

    assert [finding.category for finding in findings] == ["sql_injection"]


@pytest.mark.asyncio
async def test_llm_findings_merge_and_dedupe_by_location_and_category() -> None:
    async def llm(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return [
            {
                "severity": "critical",
                "category": "sql_injection",
                "file_path": "app/db.py",
                "line_start": 3,
                "line_end": 3,
                "evidence_snippet": 'return conn.execute(f"SELECT * FROM users WHERE id = {user_id}")',
                "rationale": "Model agrees this is SQL injection.",
                "suggested_fix": "Use parameters.",
                "confidence": 0.99,
            },
            {
                "severity": "medium",
                "category": "unbounded_query",
                "file_path": "app/db.py",
                "line_start": 3,
                "line_end": 3,
                "evidence_snippet": 'return conn.execute(f"SELECT * FROM users WHERE id = {user_id}")',
                "rationale": "The query has no row limit.",
                "suggested_fix": "Add a LIMIT clause.",
                "confidence": 0.82,
            },
        ]

    agent = SecurityAgent(llm_callable=llm)
    findings = await agent.analyze(_context())

    by_category = {finding.category: finding for finding in findings}
    assert set(by_category) == {"sql_injection", "unbounded_query"}
    # The same category at the same location keeps the higher-confidence LLM finding.
    assert by_category["sql_injection"].confidence == 0.99
