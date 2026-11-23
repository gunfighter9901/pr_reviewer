"""Fan-In Aggregator & Deduplicator enforcing Grounded Evidence (M5, INV-7)."""

from __future__ import annotations

from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Single-PR summary: one structured, plain-language review per pull request.
# The aggregator takes every specialist result, orders points by risk, and
# explains each in simple terms so a PR reads as one review, not N fragments.
# ---------------------------------------------------------------------------

RISK_LABELS: Dict[str, str] = {
    "critical": "Critical risk — fix before merge",
    "high": "High risk — should fix before merge",
    "medium": "Medium risk — worth fixing",
    "low": "Low risk — polish if time allows",
    "info": "Note — no action required",
}

CATEGORY_EXPLAINERS: Dict[str, str] = {
    "sql_injection": "User input is built directly into a database query, so an attacker could read or change data they should not access.",
    "code_injection": "The code can execute untrusted input as program commands, which allows an attacker to run their own code.",
    "hardcoded_secret": "A password, token, or API key is written directly in the source, so it leaks to everyone with read access.",
    "unsafe_deserialization": "Untrusted data is converted back into live objects, which can trigger hidden code execution.",
    "unsafe_runtime_eval": "Dynamic code execution bypasses type checks and can crash or misbehave on unexpected input.",
    "swallowed_exception": "Errors are silently ignored, so failures become invisible and hard to debug.",
    "mutable_default_argument": "A list or dict default is shared between all calls, so data leaks from one request into the next.",
    "blocking_event_loop": "A blocking sleep freezes the whole async server, stalling every other request.",
    "null_dereference": "The code assumes a value exists where it can be missing, which crashes with an attribute error.",
    "missing_edge_case_tests": "New behavior was added without tests, so regressions will go unnoticed.",
    "tautological_assertion": "The test always passes without checking anything, so it protects nothing.",
    "missing_docstring": "A public function has no documented contract, so callers must guess how to use it.",
    "untracked_todo_comment": "A TODO has no owner or issue link, so the debt is invisible to planning.",
}


@dataclass(frozen=True)
class PRSummaryPoint:
    """One risk-ordered review point in plain language."""

    file_path: str
    line_start: int
    line_end: int
    severity: str
    category: str
    agent_type: str
    confidence: float
    plain_summary: str
    evidence_snippet: str
    suggested_fix: Optional[str] = None
    feedback_id: Optional[str] = None
    reviewer_action: str = "pending"


@dataclass(frozen=True)
class PRSummary:
    """Whole-PR review: risk verdict, headline, and ordered points."""

    repo: str
    pr_number: int
    commit_sha: str
    risk_level: str
    headline: str
    total_points: int
    severity_counts: Dict[str, int]
    points: Tuple[PRSummaryPoint, ...]


def _explain(category: str) -> str:
    return CATEGORY_EXPLAINERS.get(
        category,
        "This pattern is flagged as risky and deserves a closer look before merging.",
    )


def summarize_pr_findings(
    repo: str,
    pr_number: int,
    commit_sha: str,
    findings: Iterable[Finding],
    *,
    metas: Optional[List[Dict[str, Any]]] = None,
) -> PRSummary:
    """Order findings by risk and explain each in one plain sentence.

    Pure and deterministic. `metas` is an optional parallel list carrying
    caller-owned context (e.g. HITL feedback ids); entries follow their
    finding through the risk sort.
    """
    indexed = list(enumerate(list(findings)))
    indexed.sort(
        key=lambda iv: (
            -SEVERITY_RANK.get(iv[1].severity, 0),
            -iv[1].confidence,
            iv[1].file_path,
            iv[1].line_start,
        )
    )
    counts: Dict[str, int] = {}
    points: List[PRSummaryPoint] = []
    for orig_idx, f in indexed:
        counts[f.severity] = counts.get(f.severity, 0) + 1
        meta = (metas[orig_idx] if metas and orig_idx < len(metas) else {}) or {}
        where = (
            f"{f.file_path}:{f.line_start}"
            if f.line_end <= f.line_start
            else f"{f.file_path}:{f.line_start}-{f.line_end}"
        )
        points.append(
            PRSummaryPoint(
                file_path=f.file_path,
                line_start=f.line_start,
                line_end=f.line_end,
                severity=f.severity,
                category=f.category,
                agent_type=f.agent_type,
                confidence=round(float(f.confidence), 4),
                plain_summary=(
                    f"{RISK_LABELS.get(f.severity, 'Needs review')} at {where}: "
                    f"{_explain(f.category)} (confidence {f.confidence:.0%})."
                ),
                evidence_snippet=f.evidence_snippet,
                suggested_fix=f.suggested_fix,
                feedback_id=meta.get("feedback_id"),
                reviewer_action=str(meta.get("reviewer_action") or "pending"),
            )
        )

    risk_level = indexed[0][1].severity if indexed else "info"
    if not indexed:
        headline = "No issues found. The changed lines look safe to merge."
    else:
        parts = ", ".join(
            f"{n} {sev}" for sev, n in sorted(counts.items(), key=lambda kv: -SEVERITY_RANK.get(kv[0], 0))
        )
        top = indexed[0][1]
        headline = (
            f"This PR has {len(indexed)} issue(s) ({parts}). "
            f"Highest risk: {top.category} in {top.file_path} — "
            f"{_explain(top.category)}"
        )
    return PRSummary(
        repo=repo,
        pr_number=pr_number,
        commit_sha=commit_sha,
        risk_level=risk_level,
        headline=headline,
        total_points=len(indexed),
        severity_counts=counts,
        points=tuple(points),
    )


def render_pr_summary_markdown(
    summary: PRSummary,
    *,
    withheld_private: int = 0,
    withheld_hitl: int = 0,
) -> str:
    """Render the single-comment body: verdict, counts, then risk-ordered points."""
    lines: List[str] = [
        f"## PR Review — {summary.repo}#{summary.pr_number}",
        "",
        f"**Risk: {summary.risk_level.upper()}** — {summary.headline}",
        "",
    ]
    if summary.total_points == 0:
        lines.append("No actionable findings. Safe to merge.")
    else:
        counts = ", ".join(
            f"{n} {sev}"
            for sev, n in sorted(
                summary.severity_counts.items(),
                key=lambda kv: -SEVERITY_RANK.get(kv[0], 0),
            )
        )
        lines.append(f"**{summary.total_points} point(s)** ({counts}), ordered by risk:")
        lines.append("")
        for idx, p in enumerate(summary.points, start=1):
            where = (
                f"{p.file_path}:{p.line_start}"
                if p.line_end <= p.line_start
                else f"{p.file_path}:{p.line_start}-{p.line_end}"
            )
            lines.append(f"### {idx}. `{p.category}` — {where} [{p.severity}]")
            lines.append("")
            lines.append(p.plain_summary)
            lines.append("")
            lines.append(f"```\n{p.evidence_snippet}\n```")
            if p.suggested_fix:
                lines.append("")
                lines.append(f"**Suggested fix:** {p.suggested_fix}")
            lines.append("")
    notes: List[str] = []
    if withheld_private:
        notes.append(
            f"{withheld_private} critical finding(s) withheld from this thread "
            "and routed to the private security queue."
        )
    if withheld_hitl:
        notes.append(
            f"{withheld_hitl} finding(s) held for human review in the HITL queue."
        )
    if notes:
        lines.append("---")
        lines.append(" ".join(notes))
    return "\n".join(lines).rstrip() + "\n"
