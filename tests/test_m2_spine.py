"""Milestone 2 Verification Tests: Unified Tiger Data Spine (INV-4, INV-5)."""

from __future__ import annotations

import pytest

from backend.config import DimensionMismatchError, Settings
from backend.db.spine import ImmutableAuditSpineError, TigerDataSpine, parse_migration_sql


@pytest.fixture()
def spine() -> TigerDataSpine:
    return TigerDataSpine(settings=Settings(_env_file=None, EMBEDDING_DIM=1536))


def test_m2_extensions_hypertable_and_indexes_registered(spine: TigerDataSpine) -> None:
    """Verify timescaledb, vector, and vectorscale extensions, DiskANN index, FTS GIN index, and hypertable."""
    intro = spine.introspection
    assert "timescaledb" in intro.extensions
    assert "vector" in intro.extensions
    assert "vectorscale" in intro.extensions

    # Verify agent_events hypertable creation in SQL migration & spine catalog
    assert ("agent_events", "time") in intro.hypertables
    hypertable_rows = spine.execute_sql(
        "SELECT table_name, time_column_name FROM _timescale_hypertables WHERE table_name = 'agent_events'"
    )
    assert len(hypertable_rows) == 1
    assert hypertable_rows[0]["time_column_name"] == "time"

    # Verify DiskANN and FTS GIN indexes on code_chunks
    assert "idx_code_chunks_embedding_diskann" in intro.diskann_indexes
    assert "idx_code_chunks_tsv" in intro.fts_indexes

    # Verify Continuous Aggregates
    assert "hourly_pr_costs" in intro.continuous_aggregates
    assert "agent_health_metrics" in intro.continuous_aggregates


def test_inv4_agent_events_rejects_update_delete_and_truncate(spine: TigerDataSpine) -> None:
    """INV-4: Verify hard rejection of UPDATE, DELETE, and TRUNCATE on agent_events hypertable."""
    spine.insert_agent_event(
        trace_id="tr-m2-001",
        delivery_id="deliv-m2-001",
        agent_name="security",
        event_type="llm_call",
        model="claude-3-5-sonnet",
        prompt_version="security@v1.0.0",
        tokens_in=800,
        tokens_out=200,
        cost_usd=0.0054,
        latency_ms=420,
        status="ok",
    )

    events = spine.query_agent_events(trace_id="tr-m2-001")
    assert len(events) == 1

    # 1. Attempt UPDATE -> must raise ImmutableAuditSpineError ('agent_events is append-only and immutable')
    with pytest.raises(ImmutableAuditSpineError, match="agent_events is append-only and immutable"):
        spine.execute_sql(
            "UPDATE agent_events SET cost_usd = 0.0 WHERE trace_id = ?",
            ("tr-m2-001",),
        )

    # 2. Attempt DELETE -> must raise ImmutableAuditSpineError ('agent_events is append-only and immutable')
    with pytest.raises(ImmutableAuditSpineError, match="agent_events is append-only and immutable"):
        spine.execute_sql(
            "DELETE FROM agent_events WHERE trace_id = ?",
            ("tr-m2-001",),
        )

    # 3. Attempt TRUNCATE -> must raise ImmutableAuditSpineError ('agent_events is append-only and immutable')
    with pytest.raises(ImmutableAuditSpineError, match="agent_events is append-only and immutable"):
        spine.execute_sql("TRUNCATE TABLE agent_events")

    # Confirm the event record remains completely intact
    after_events = spine.query_agent_events(trace_id="tr-m2-001")
    assert len(after_events) == 1
    assert after_events[0]["cost_usd"] == pytest.approx(0.0054)


def test_inv5_startup_dimension_mismatch_fails_fast() -> None:
    """INV-5: Configured EMBEDDING_DIM mismatching code_chunks.embedding vector(1536) fails at startup."""
    bad_settings = Settings(_env_file=None, EMBEDDING_DIM=768)
    with pytest.raises(DimensionMismatchError, match="INV-5 Violation"):
        TigerDataSpine(settings=bad_settings)


def test_inv5_code_chunk_insert_dimension_check(spine: TigerDataSpine) -> None:
    """INV-5: Inserting a code_chunk vector with wrong dimension raises DimensionMismatchError."""
    with pytest.raises(DimensionMismatchError, match="INV-5 Violation"):
        spine.upsert_code_chunk(
            chunk_id="chk-bad-dim",
            repo="acme/api",
            commit_sha="sha123",
            file_path="app/main.py",
            symbol_name="handler",
            content="def handler(): pass",
            embedding=[0.1] * 384,
        )

    # Valid 1536-dim vector succeeds
    spine.upsert_code_chunk(
        chunk_id="chk-good-dim",
        repo="acme/api",
        commit_sha="sha123",
        file_path="app/main.py",
        symbol_name="handler",
        content="def handler(): pass",
        embedding=[0.01] * 1536,
    )
    chunks = spine.get_code_chunks(repo="acme/api")
    assert len(chunks) == 1
    assert len(chunks[0]["embedding"]) == 1536


def test_continuous_aggregates_and_relational_tables(spine: TigerDataSpine) -> None:
    """Verify hourly_pr_costs, agent_health_metrics, pr_reviews, findings, and hitl_feedback."""
    spine.insert_agent_event(
        trace_id="tr-cagg-1",
        delivery_id="deliv-cagg-1",
        agent_name="security",
        event_type="specialist_completed",
        model="claude-3-5-sonnet",
        tokens_in=1000,
        tokens_out=300,
        cost_usd=0.012,
        latency_ms=610,
        status="ok",
    )
    spine.insert_agent_event(
        trace_id="tr-cagg-1",
        delivery_id="deliv-cagg-1",
        agent_name="quality",
        event_type="specialist_completed",
        model="gpt-4o",
        tokens_in=900,
        tokens_out=250,
        cost_usd=0.008,
        latency_ms=480,
        status="ok",
    )

    costs = spine.get_hourly_pr_costs(delivery_id="deliv-cagg-1")
    assert len(costs) == 2
    assert spine.get_daily_spend_usd() == pytest.approx(0.020)

    health = spine.get_agent_health_metrics()
    assert len(health) >= 2

    # Relational truth: pr_reviews -> findings -> hitl_feedback
    rev_id = spine.upsert_pr_review(
        github_delivery_id="deliv-cagg-1",
        trace_id="tr-cagg-1",
        repo="acme/api",
        pr_number=17,
        commit_sha="deadbeef",
        status="processing",
    )
    fnd_id = spine.insert_finding(
        github_delivery_id="deliv-cagg-1",
        review_id=rev_id,
        routing_decision="hitl_escalate",
        finding_data={
            "agent_type": "security",
            "severity": "critical",
            "category": "sql_injection",
            "file_path": "db/query.py",
            "line_start": 12,
            "line_end": 14,
            "evidence_snippet": 'cursor.execute(f"SELECT * FROM users WHERE id = {uid}")',
            "rationale": "Unsanitized f-string interpolation directly into SQL query string.",
            "suggested_fix": 'cursor.execute("SELECT * FROM users WHERE id = %s", (uid,))',
            "confidence": 0.96,
            "prompt_version": "security@v1.0.0",
        },
    )
    hitl_id = spine.insert_hitl_feedback(
        finding_id=fnd_id,
        github_delivery_id="deliv-cagg-1",
        repo="acme/api",
        file_path="db/query.py",
        category="sql_injection",
        severity="critical",
        reviewer_action="pending",
        private_alert=True,
    )
    assert hitl_id.startswith("hitl-")
    feedback_rows = spine.get_hitl_feedback(repo="acme/api")
    assert len(feedback_rows) == 1
    assert feedback_rows[0]["private_alert"] == 1
