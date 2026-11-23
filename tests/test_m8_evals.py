"""Milestone 8 Verification Tests: Golden Dataset Evaluation Suite (INV-1..INV-7)."""

from __future__ import annotations

import pytest

from evals.run_evals import run_golden_evals


@pytest.mark.asyncio
async def test_m8_golden_dataset_regression_gate() -> None:
    """Execute golden evaluation suite and assert 100% grounding, selectivity, recall, and routing accuracy."""
    summary = await run_golden_evals()
    assert summary["all_passed"] is True
    assert summary["category_recall"] == 1.0
    assert summary["grounded_evidence_rate"] == 1.0
    assert summary["clean_pr_selectivity"] == 1.0
    assert summary["routing_accuracy"] == 1.0
