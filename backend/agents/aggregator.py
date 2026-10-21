"""Fan-In Aggregator & Deduplicator enforcing Grounded Evidence (M5, INV-7)."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import ValidationError

from backend.models import Finding

SEVERITY_RANK: Dict[str, int] = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "info": 1,
}


class FindingAggregator:
    """Collects specialist outputs, enforces INV-7 grounded evidence, deduplicates, and calibrates confidence."""

    def __init__(
        self,
        corroboration_boost: float = 0.05,
        category_weights: Optional[Dict[str, float]] = None,
    ) -> None:
        self.corroboration_boost = corroboration_boost
        self.category_weights = category_weights or {}

    def validate_and_filter_grounded(
        self, raw_findings: Iterable[Finding | Dict[str, Any]]
    ) -> List[Finding]:
        """Enforce INV-7: Drop any ungrounded finding lacking file_path, valid lines, snippet, rationale, or confidence."""
        grounded: List[Finding] = []
        for item in raw_findings:
            try:
                finding = (
                    item
                    if isinstance(item, Finding)
                    else Finding.model_validate(item)
                )
            except ValidationError:
                continue

            if (
                not finding.file_path.strip()
                or finding.line_start < 1
                or finding.line_end < finding.line_start
                or not finding.evidence_snippet.strip()
                or not finding.rationale.strip()
            ):
                continue
            grounded.append(finding)
        return grounded

    def aggregate(
        self, raw_findings: Iterable[Finding | Dict[str, Any]]
    ) -> List[Finding]:
        """Deduplicate overlapping findings on the same file_path + line_start and compute composite confidence."""
        valid_findings = self.validate_and_filter_grounded(raw_findings)

        # Group by (file_path, line_start) as specified in Section 3.1 Stage 4
        groups: Dict[Tuple[str, int], List[Finding]] = {}
        for f in valid_findings:
            key = (f.file_path.strip(), f.line_start)
            groups.setdefault(key, []).append(f)

        deduplicated: List[Finding] = []
        for (_, _), group in groups.items():
            # Sort group so highest severity, then highest confidence is primary
            sorted_group = sorted(
                group,
                key=lambda x: (SEVERITY_RANK.get(x.severity, 0), x.confidence),
                reverse=True,
            )
            primary = sorted_group[0]

            if len(sorted_group) == 1:
                weight = self.category_weights.get(primary.category, 1.0)
                calibrated_conf = round(min(1.0, max(0.0, primary.confidence * weight)), 4)
                deduplicated.append(
                    primary.model_copy(update={"confidence": calibrated_conf})
                )
                continue

            # Multiple specialists flagged the exact same file + line_start
            distinct_agents = {f.agent_type for f in sorted_group}
            max_conf = max(f.confidence for f in sorted_group)
            boost = self.corroboration_boost * max(0, len(distinct_agents) - 1)
            weight = self.category_weights.get(primary.category, 1.0)
            composite_conf = round(min(1.0, max(0.0, (max_conf + boost) * weight)), 4)

            combined_rationales: List[str] = []
            for f in sorted_group:
                tag = f"[{f.agent_type}] {f.rationale}"
                if tag not in combined_rationales:
                    combined_rationales.append(tag)

            merged = primary.model_copy(
                update={
                    "line_end": max(f.line_end for f in sorted_group),
                    "rationale": " | ".join(combined_rationales),
                    "confidence": composite_conf,
                }
            )
            deduplicated.append(merged)

        # Return sorted by severity (desc), confidence (desc), file_path, line_start
        deduplicated.sort(
            key=lambda f: (
                -SEVERITY_RANK.get(f.severity, 0),
                -f.confidence,
                f.file_path,
                f.line_start,
            )
        )
        return deduplicated


def aggregate_and_deduplicate_findings(
    raw_findings: Iterable[Finding | Dict[str, Any]],
    category_weights: Optional[Dict[str, float]] = None,
) -> List[Finding]:
    """Convenience function wrapping FindingAggregator.aggregate()."""
    aggregator = FindingAggregator(category_weights=category_weights)
    return aggregator.aggregate(raw_findings)
