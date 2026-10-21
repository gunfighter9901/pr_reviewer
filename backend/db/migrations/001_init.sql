-- =============================================================================
-- 001_init.sql — Unified Tiger Data Spine Migration
-- PostgreSQL + TimescaleDB + pgvector + pgvectorscale (DiskANN)
-- =============================================================================

-- 1. Core Extensions
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS vector CASCADE;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;

-- 2. Semantic Memory: code_chunks (pgvector + pgvectorscale StreamingDiskANN + FTS)
CREATE TABLE IF NOT EXISTS code_chunks (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    line_start INTEGER NOT NULL DEFAULT 1,
    line_end INTEGER NOT NULL DEFAULT 1,
    content TEXT NOT NULL,
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(symbol_name, '') || ' ' || coalesce(content, ''))) STORED,
    embedding vector(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding_diskann
    ON code_chunks USING diskann (embedding);

CREATE INDEX IF NOT EXISTS idx_code_chunks_tsv
    ON code_chunks USING GIN (tsv);

CREATE INDEX IF NOT EXISTS idx_code_chunks_repo_file
    ON code_chunks (repo, file_path);

-- 3. Relational Truth: pr_reviews & findings
CREATE TABLE IF NOT EXISTS pr_reviews (
    id TEXT PRIMARY KEY,
    github_delivery_id TEXT NOT NULL UNIQUE,
    trace_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    is_public BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'auto_posted', 'escalated_hitl', 'completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    review_id TEXT REFERENCES pr_reviews(id) ON DELETE CASCADE,
    github_delivery_id TEXT NOT NULL,
    agent_type TEXT NOT NULL CHECK (agent_type IN ('security', 'quality', 'testing', 'docs')),
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    category TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_start INTEGER NOT NULL CHECK (line_start >= 1),
    line_end INTEGER NOT NULL CHECK (line_end >= line_start),
    evidence_snippet TEXT NOT NULL,
    rationale TEXT NOT NULL,
    suggested_fix TEXT,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    prompt_version TEXT NOT NULL,
    routing_decision TEXT NOT NULL DEFAULT 'auto_post' CHECK (routing_decision IN ('auto_post', 'hitl_escalate', 'private_security_alert')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_findings_delivery_id ON findings (github_delivery_id);
CREATE INDEX IF NOT EXISTS idx_findings_file_path ON findings (file_path);

-- 4. Procedural Memory: Repository Conventions & ADRs
CREATE TABLE IF NOT EXISTS procedural_conventions (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo, rule_key)
);

-- 5. Episodic Memory & Continuous Learning: hitl_feedback
CREATE TABLE IF NOT EXISTS hitl_feedback (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    github_delivery_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    file_path TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    reviewer_action TEXT NOT NULL CHECK (reviewer_action IN ('pending', 'approved', 'edited', 'rejected_false_positive', 'disputed')),
    reviewer_id TEXT,
    notes TEXT,
    decay_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (decay_weight >= 0.0 AND decay_weight <= 1.0),
    evidence_count INTEGER NOT NULL DEFAULT 1 CHECK (evidence_count >= 1),
    private_alert BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hitl_feedback_repo_file ON hitl_feedback (repo, file_path);
CREATE INDEX IF NOT EXISTS idx_hitl_feedback_category ON hitl_feedback (repo, category);

-- 6. Time-Series Observability (Event Spine): agent_events Hypertable
CREATE TABLE IF NOT EXISTS agent_events (
    time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trace_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'system',
    prompt_version TEXT NOT NULL DEFAULT 'v1.0.0',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Convert agent_events into a TimescaleDB hypertable partitioned on time
SELECT create_hypertable('agent_events', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_agent_events_trace_id ON agent_events (trace_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_delivery_id ON agent_events (delivery_id, time DESC);

-- 7. INV-4: Database-Level Immutability Triggers on agent_events
-- PostgreSQL RULEs do not propagate across TimescaleDB hypertable chunks;
-- we enforce strict append-only semantics via BEFORE UPDATE OR DELETE (row-level)
-- AND BEFORE TRUNCATE (statement-level) triggers.
CREATE OR REPLACE FUNCTION prevent_agent_events_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'agent_events is append-only and immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_events_no_update_delete ON agent_events;
CREATE TRIGGER trg_agent_events_no_update_delete
    BEFORE UPDATE OR DELETE ON agent_events
    FOR EACH ROW
    EXECUTE FUNCTION prevent_agent_events_mutation();

DROP TRIGGER IF EXISTS trg_agent_events_no_truncate ON agent_events;
CREATE TRIGGER trg_agent_events_no_truncate
    BEFORE TRUNCATE ON agent_events
    FOR EACH STATEMENT
    EXECUTE FUNCTION prevent_agent_events_mutation();

-- 8. Continuous Aggregates for Real-Time Economics & Health Telemetry
CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_pr_costs
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    delivery_id,
    agent_name,
    model,
    SUM(tokens_in) AS total_tokens_in,
    SUM(tokens_out) AS total_tokens_out,
    SUM(cost_usd) AS total_cost_usd,
    COUNT(*) AS event_count
FROM agent_events
GROUP BY bucket, delivery_id, agent_name, model
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_metrics
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', time) AS bucket,
    agent_name,
    status,
    COUNT(*) AS span_count,
    AVG(latency_ms) AS avg_latency_ms,
    MAX(latency_ms) AS p95_latency_ms,
    SUM(cost_usd) AS total_cost_usd
FROM agent_events
GROUP BY bucket, agent_name, status
WITH NO DATA;
