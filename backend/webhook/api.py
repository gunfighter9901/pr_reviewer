"""Live read/write API for the operations dashboard (minimal, structured).

Mounts under /api on the ingress app. Reads live rows from TigerDataSpine
(SQLite file in production, :memory: in tests). No demo data.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from backend.db.spine import TigerDataSpine
from backend.gates.hitl import FeedbackDecayEngine
from backend.agents.aggregator import (
    SEVERITY_RANK,
    render_pr_summary_markdown,
    summarize_pr_findings,
)
from backend.models import Finding, QueuedPRJob
from backend.webhook.queue import ArqWebhookQueue, read_index


def _spine(request: Request) -> TigerDataSpine:
    spine = getattr(request.app.state, "spine", None)
    if spine is None:
        spine = TigerDataSpine(settings=request.app.state.settings)
        request.app.state.spine = spine
    return spine


def _queue(request: Request) -> ArqWebhookQueue:
    """Queue accessor; tests inject app.state.queue_override to avoid live Redis."""
    override = getattr(request.app.state, "queue_override", None)
    if override is not None:
        return override
    return ArqWebhookQueue(request.app.state.settings)


class TriggerRequest(BaseModel):
    repo: str = Field(description="owner/name")
    pr_number: int = Field(gt=0)
    delivery_id: Optional[str] = None
    # When a Redis queue is configured, dashboard triggers default to a full
    # run: worker fetches the diff, reviews, and auto-posts eligible findings.
    # Set false for a dry run (analyze + persist only, post nothing).
    auto_post: bool = Field(default=True)


class DecisionRequest(BaseModel):
    action: str = Field(description="approved | edited | rejected_false_positive | disputed")
    reviewer_id: str = "local-reviewer"
    notes: Optional[str] = None


def register_api_routes(app: FastAPI) -> None:
    @app.get("/api/stats")
    async def api_stats(request: Request) -> Dict[str, Any]:
        spine = _spine(request)
        cfg = request.app.state.settings
        pending = spine.execute_sql(
            "SELECT COUNT(*) AS c FROM hitl_feedback WHERE reviewer_action = 'pending'"
        )
        private_pending = spine.execute_sql(
            "SELECT COUNT(*) AS c FROM hitl_feedback WHERE reviewer_action = 'pending' AND private_alert = 1"
        )
        reviews = spine.execute_sql("SELECT COUNT(*) AS c FROM pr_reviews")
        findings = spine.execute_sql("SELECT COUNT(*) AS c FROM findings")
        try:
            spend = spine.get_daily_spend_usd()
        except Exception:
            spend = 0.0
        return {
            "pending_hitl": int(pending[0]["c"]) if pending else 0,
            "private_pending": int(private_pending[0]["c"]) if private_pending else 0,
            "reviews": int(reviews[0]["c"]) if reviews else 0,
            "findings": int(findings[0]["c"]) if findings else 0,
            "spend_24h_usd": round(float(spend), 6),
            "daily_budget_usd": float(cfg.DAILY_BUDGET_USD),
            "confidence_threshold": float(cfg.CONFIDENCE_THRESHOLD),
            "spine_path": str(getattr(cfg, "SPINE_SQLITE_PATH", ":memory:")),
        }

    @app.get("/api/hitl")
    async def api_hitl(
        request: Request,
        reviewer_action: Optional[str] = None,
        repo: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        spine = _spine(request)
        limit = max(1, min(int(limit or 100), 500))
        clauses: List[str] = []
        params: List[Any] = []
        if reviewer_action:
            clauses.append("h.reviewer_action = ?")
            params.append(reviewer_action)
        if repo:
            clauses.append("h.repo = ?")
            params.append(repo)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = spine.execute_sql(
            f"""
            SELECT h.*, f.agent_type, f.routing_decision, f.evidence_snippet,
                   f.rationale, f.suggested_fix, f.confidence, f.prompt_version,
                   f.line_start, f.line_end, f.github_delivery_id AS finding_delivery_id,
                   r.pr_number, r.trace_id, r.status AS review_status
            FROM hitl_feedback h
            JOIN findings f ON f.id = h.finding_id
            LEFT JOIN pr_reviews r ON r.id = f.review_id
            {where}
            ORDER BY h.created_at DESC LIMIT ?
            """,
            params,
        )
        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "id": r.get("finding_id"),
                    "feedback_id": r.get("id"),
                    "delivery_id": r.get("github_delivery_id"),
                    "trace_id": r.get("trace_id"),
                    "repo": r.get("repo"),
                    "pr_number": r.get("pr_number"),
                    "agent_type": r.get("agent_type"),
                    "severity": r.get("severity"),
                    "category": r.get("category"),
                    "file_path": r.get("file_path"),
                    "line_start": r.get("line_start"),
                    "line_end": r.get("line_end"),
                    "evidence_snippet": r.get("evidence_snippet"),
                    "rationale": r.get("rationale"),
                    "suggested_fix": r.get("suggested_fix"),
                    "confidence": r.get("confidence"),
                    "prompt_version": r.get("prompt_version"),
                    "routing_decision": r.get("routing_decision"),
                    "private_alert": bool(r.get("private_alert")),
                    "reviewer_action": r.get("reviewer_action"),
                    "decay_weight": r.get("decay_weight"),
                    "evidence_count": r.get("evidence_count"),
                }
            )
        return {"items": items, "count": len(items)}

    @app.post("/api/hitl/{feedback_id}/decision")
    async def api_hitl_decision(
        feedback_id: str, body: DecisionRequest, request: Request
    ) -> Dict[str, Any]:
        allowed = {"approved", "edited", "rejected_false_positive", "disputed"}
        if body.action not in allowed:
            raise HTTPException(status_code=400, detail=f"action must be one of {sorted(allowed)}")
        spine = _spine(request)
        engine = FeedbackDecayEngine(spine=spine)
        try:
            result = engine.record_reviewer_decision(
                feedback_id=feedback_id,
                reviewer_action=body.action,
                reviewer_id=body.reviewer_id,
                notes=body.notes,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result

    @app.get("/api/pr-summaries")
    async def api_pr_summaries(
        request: Request,
        reviewer_action: Optional[str] = None,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Whole-PR reviews: findings grouped by PR, risk-ordered, plainly explained.

        One structured item per (repo, pr_number) — the dashboard renders each
        as a single review card instead of a line-by-line finding list.
        """
        spine = _spine(request)
        limit = max(1, min(int(limit or 500), 2000))
        clauses: List[str] = []
        params: List[Any] = []
        if reviewer_action:
            clauses.append("h.reviewer_action = ?")
            params.append(reviewer_action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = spine.execute_sql(
            f"""
            SELECT h.*, f.agent_type, f.routing_decision, f.evidence_snippet,
                   f.rationale, f.suggested_fix, f.confidence, f.prompt_version,
                   f.line_start, f.line_end, f.id AS finding_id,
                   f.github_delivery_id AS finding_delivery_id,
                   r.pr_number, r.trace_id
            FROM hitl_feedback h
            JOIN findings f ON f.id = h.finding_id
            LEFT JOIN pr_reviews r ON r.id = f.review_id
            {where}
            ORDER BY h.created_at DESC LIMIT ?
            """,
            params,
        )

        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault((r.get("repo"), r.get("pr_number") or 0), []).append(r)

        summaries: List[Dict[str, Any]] = []
        for (repo, pr_number), grows in groups.items():
            findings: List[Finding] = []
            metas: List[Dict[str, Any]] = []
            for r in grows:
                try:
                    findings.append(
                        Finding.model_validate(
                            {
                                "agent_type": r["agent_type"],
                                "severity": r["severity"],
                                "category": r["category"],
                                "file_path": r["file_path"],
                                "line_start": r["line_start"],
                                "line_end": r["line_end"],
                                "evidence_snippet": r["evidence_snippet"],
                                "rationale": r["rationale"],
                                "suggested_fix": r.get("suggested_fix"),
                                "confidence": r["confidence"],
                                "prompt_version": r["prompt_version"],
                            }
                        )
                    )
                except Exception:
                    continue
                metas.append(
                    {
                        "feedback_id": r["id"],
                        "reviewer_action": r["reviewer_action"],
                    }
                )
            if not findings:
                continue
            summary = summarize_pr_findings(
                str(repo), int(pr_number), "", findings, metas=metas
            )
            points = []
            for p in summary.points:
                points.append(
                    {
                        "file_path": p.file_path,
                        "line_start": p.line_start,
                        "line_end": p.line_end,
                        "severity": p.severity,
                        "category": p.category,
                        "agent_type": p.agent_type,
                        "confidence": p.confidence,
                        "plain_summary": p.plain_summary,
                        "evidence_snippet": p.evidence_snippet,
                        "suggested_fix": p.suggested_fix,
                        "feedback_id": p.feedback_id,
                        "reviewer_action": p.reviewer_action,
                    }
                )
            summaries.append(
                {
                    "repo": summary.repo,
                    "pr_number": summary.pr_number,
                    "risk_level": summary.risk_level,
                    "headline": summary.headline,
                    "total_points": summary.total_points,
                    "severity_counts": summary.severity_counts,
                    "points": points,
                    "markdown": render_pr_summary_markdown(summary),
                }
            )

        summaries.sort(
            key=lambda s: (
                -SEVERITY_RANK.get(s["risk_level"], 0),
                -s["total_points"],
                str(s["repo"]),
            )
        )
        return {"items": summaries[:50], "count": min(len(summaries), 50)}

    @app.get("/api/reviews")
    async def api_reviews(request: Request, limit: int = 20) -> Dict[str, Any]:
        spine = _spine(request)
        limit = max(1, min(int(limit or 20), 100))
        rows = spine.execute_sql(
            """
            SELECT r.*, COUNT(f.id) AS finding_count
            FROM pr_reviews r LEFT JOIN findings f ON f.review_id = r.id
            GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?
            """,
            (limit,),
        )
        return {"items": rows, "count": len(rows)}

    @app.get("/api/traces")
    async def api_traces(
        request: Request,
        delivery_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        spine = _spine(request)
        rows = spine.query_agent_events(
            trace_id=trace_id,
            delivery_id=delivery_id,
            limit=max(1, min(int(limit or 200), 1000)),
        )
        return {"items": rows, "count": len(rows)}

    @app.get("/api/queue")
    async def api_queue(request: Request, limit: int = 100) -> Dict[str, Any]:
        """PRs waiting in (or recently through) the Redis queue.

        Merges the dashboard index with live ARQ queue membership so webhook
        deliveries and manual triggers share one view.
        """
        limit = max(1, min(int(limit or 100), 500))
        queue = _queue(request)
        try:
            pool = await queue.connect()
            try:
                queued_ids = set(await queue.list_queued_job_ids())
                index = await read_index(pool)
            finally:
                await queue.close()
        except Exception as exc:  # noqa: BLE001 - Redis down: report, don't 500 the dashboard
            raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc.__class__.__name__}") from exc

        items: List[Dict[str, Any]] = []
        for job_id, entry in index.items():
            items.append(
                {
                    "job_id": job_id,
                    "delivery_id": entry.get("delivery_id"),
                    "trace_id": entry.get("trace_id"),
                    "repo": entry.get("repo"),
                    "pr_number": entry.get("pr_number"),
                    "status": entry.get("status", "queued"),
                    "source": entry.get("source", "webhook"),
                    "in_arq_queue": job_id in queued_ids,
                    "enqueued_at": entry.get("enqueued_at"),
                    "updated_at": entry.get("updated_at"),
                    "findings_count": entry.get("findings_count"),
                    "auto_posted": entry.get("auto_posted"),
                    "error": entry.get("error"),
                }
            )
        for job_id in sorted(queued_ids):
            if job_id not in index:
                items.append(
                    {
                        "job_id": job_id,
                        "delivery_id": None,
                        "trace_id": None,
                        "repo": None,
                        "pr_number": None,
                        "status": "queued",
                        "source": "arq",
                        "in_arq_queue": True,
                        "enqueued_at": None,
                        "updated_at": None,
                        "findings_count": None,
                        "auto_posted": None,
                        "error": None,
                    }
                )

        def _rank(item: Dict[str, Any]) -> tuple:
            live = 0 if item["in_arq_queue"] else 1
            return (live, str(item.get("updated_at") or item.get("enqueued_at") or ""))

        items.sort(key=_rank)
        counts = {"queued": 0, "processing": 0, "done": 0, "failed": 0}
        for item in items:
            # Terminal states win; otherwise the worker's live status decides.
            # (A job can briefly appear in the ARQ set while already processing.)
            st = str(item.get("status") or "queued")
            if st in ("done", "failed", "processing"):
                counts[st] += 1
            else:
                counts["queued"] += 1
        return {"items": items[:limit], "count": len(items), "counts": counts}

    @app.post("/api/queue/trigger")
    async def api_queue_trigger(body: TriggerRequest, request: Request) -> Dict[str, Any]:
        """Manually enqueue a PR review from the dashboard (same path as webhooks)."""
        repo = (body.repo or "").strip()
        if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
            raise HTTPException(status_code=400, detail="repo must be 'owner/name'")
        delivery_id = (body.delivery_id or "").strip() or f"deliv-manual-{uuid.uuid4().hex[:12]}"
        job = QueuedPRJob(
            delivery_id=delivery_id,
            trace_id=f"tr-manual-{uuid.uuid4().hex[:12]}",
            repo=repo,
            pr_number=int(body.pr_number),
            commit_sha="pending-fetch",
            action="manual-trigger",
            is_public=True,
            diff="",
            changed_files=[],
            metadata={"source": "dashboard-trigger", "post_comments": bool(body.auto_post)},
        )
        queue = _queue(request)
        try:
            pool = await queue.connect()
            try:
                enqueued = await queue.enqueue_job(job)
            finally:
                await queue.close()
        except Exception as exc:  # noqa: BLE001 - surface Redis/queue errors clearly
            from backend.webhook.queue import WebhookQueueError

            if isinstance(exc, WebhookQueueError):
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc.__class__.__name__}") from exc
        job_id = f"pr-review:{delivery_id}"
        return {
            "status": "queued" if enqueued else "duplicate",
            "job_id": job_id,
            "delivery_id": delivery_id,
            "repo": repo,
            "pr_number": int(body.pr_number),
            "auto_post": bool(body.auto_post),
        }
