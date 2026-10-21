"""Golden Dataset Regression Evaluation Runner (M8: Operational Dashboard & Golden Evals)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import fakeredis.aioredis

from backend.config import Settings
from backend.db.spine import TigerDataSpine
from backend.gates.hitl import HITLRoutingGate
from backend.github.poster import GitHubPRPoster
from backend.observability.events import EventSpineLogger
from backend.orchestrator.engine import WorkflowContext
from backend.orchestrator.langgraph_adapter import LangGraphEngine, RedisCheckpointStore
from backend.retrieval.hybrid_search import HybridRetriever


async def run_golden_evals(dataset_path: Path | None = None) -> Dict[str, Any]:
    """Execute all golden PR scenarios through the full multi-agent pipeline and verify invariants."""
    resolved_path = dataset_path or (Path(__file__).resolve().parent / "golden_dataset.json")
    cases: List[Dict[str, Any]] = json.loads(resolved_path.read_text(encoding="utf-8"))

    settings = Settings(CONFIDENCE_THRESHOLD=0.75)
    spine = TigerDataSpine(settings=settings)
    event_logger = EventSpineLogger(spine=spine)
    retriever = HybridRetriever(spine=spine, event_logger=event_logger)
    redis_client = fakeredis.aioredis.FakeRedis()
    checkpoint_store = RedisCheckpointStore(redis_client)
    engine = LangGraphEngine(checkpoint_store=checkpoint_store, event_logger=event_logger)
    gate = HITLRoutingGate(spine=spine, settings=settings, event_logger=event_logger)
    poster = GitHubPRPoster(event_logger=event_logger)

    total_expected = 0
    total_matched = 0
    total_emitted = 0
    grounded_count = 0
    clean_pr_passed = 0
    clean_pr_total = 0
    routing_matches = 0
    routing_checks = 0
    case_results: List[Dict[str, Any]] = []

    for case in cases:
        case_id = case["id"]
        delivery_id = f"eval-{case_id}"
        trace_id = f"tr-{case_id}"

        bundle = await retriever.retrieve_context(
            repo=case["repo"],
            diff=case["diff"],
            changed_files=case["changed_files"],
            trace_id=trace_id,
            delivery_id=delivery_id,
        )

        ctx = WorkflowContext(
            delivery_id=delivery_id,
            trace_id=trace_id,
            repo=case["repo"],
            pr_number=case["pr_number"],
            commit_sha=f"sha-{case['pr_number']}",
            is_public=case.get("is_public", True),
            diff=case["diff"],
            changed_files=case["changed_files"],
            retrieved_context=bundle.to_workflow_context_dict(),
        )

        wf_state = await engine.run(ctx)
        findings = wf_state.aggregated_findings

        routing = await gate.evaluate_and_persist(
            delivery_id=delivery_id,
            trace_id=trace_id,
            repo=case["repo"],
            pr_number=case["pr_number"],
            commit_sha=ctx.commit_sha,
            findings=findings,
            is_public_repo=ctx.is_public,
        )

        await poster.post_routed_findings(
            delivery_id=delivery_id,
            trace_id=trace_id,
            repo=case["repo"],
            pr_number=case["pr_number"],
            commit_sha=ctx.commit_sha,
            routed_findings=routing.auto_post_findings,
            is_public_repo=ctx.is_public,
        )

        emitted_categories = {f.category for f in findings}
        expected_categories = set(case.get("expected_categories", []))

        for f in findings:
            total_emitted += 1
            if (
                f.file_path
                and f.line_start >= 1
                and f.line_end >= f.line_start
                and f.evidence_snippet
                and f.rationale
                and 0.0 <= f.confidence <= 1.0
            ):
                grounded_count += 1

        if case.get("expect_zero_findings"):
            clean_pr_total += 1
            if len(findings) == 0:
                clean_pr_passed += 1
        else:
            total_expected += len(expected_categories)
            total_matched += len(expected_categories & emitted_categories)

        # Check routing decisions
        decision_by_cat: Dict[str, str] = {}
        for rf in routing.auto_post_findings + routing.hitl_escalated_findings:
            decision_by_cat[rf.finding.category] = rf.decision

        for cat, exp_route in case.get("expected_routing", {}).items():
            routing_checks += 1
            if decision_by_cat.get(cat) == exp_route:
                routing_matches += 1

        case_results.append(
            {
                "id": case_id,
                "expected_categories": sorted(expected_categories),
                "emitted_categories": sorted(emitted_categories),
                "decisions": decision_by_cat,
                "passed": expected_categories.issubset(emitted_categories)
                and (not case.get("expect_zero_findings") or len(findings) == 0),
            }
        )

    recall = (total_matched / total_expected) if total_expected > 0 else 1.0
    grounded_rate = (grounded_count / total_emitted) if total_emitted > 0 else 1.0
    selectivity = (clean_pr_passed / clean_pr_total) if clean_pr_total > 0 else 1.0
    routing_accuracy = (routing_matches / routing_checks) if routing_checks > 0 else 1.0

    summary = {
        "total_cases": len(cases),
        "category_recall": round(recall, 4),
        "grounded_evidence_rate": round(grounded_rate, 4),
        "clean_pr_selectivity": round(selectivity, 4),
        "routing_accuracy": round(routing_accuracy, 4),
        "all_passed": all(c["passed"] for c in case_results)
        and recall >= 1.0
        and grounded_rate == 1.0
        and selectivity == 1.0
        and routing_accuracy == 1.0,
        "cases": case_results,
    }
    return summary


def main() -> int:
    summary = asyncio.run(run_golden_evals())
    print(json.dumps(summary, indent=2))
    if not summary["all_passed"]:
        print("Golden Evaluation Regression Gate FAILED!", file=sys.stderr)
        return 1
    print("Golden Evaluation Regression Gate PASSED (100% recall, selectivity, grounding & routing).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
