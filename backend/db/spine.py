"""Unified Tiger Data Spine (PostgreSQL + TimescaleDB + pgvectorscale & deterministic local SQL engine)."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.config import DimensionMismatchError, Settings, get_settings


class ImmutableAuditSpineError(RuntimeError):
    """Raised when UPDATE, DELETE, or TRUNCATE is attempted on agent_events (INV-4)."""


@dataclass
class MigrationIntrospection:
    """Metadata extracted and verified from 001_init.sql."""

    extensions: List[str] = field(default_factory=list)
    hypertables: List[Tuple[str, str]] = field(default_factory=list)
    diskann_indexes: List[str] = field(default_factory=list)
    fts_indexes: List[str] = field(default_factory=list)
    immutability_triggers: List[str] = field(default_factory=list)
    continuous_aggregates: List[str] = field(default_factory=list)
    embedding_dimension: int = 1536


def parse_migration_sql(sql_path: Optional[Path | str] = None) -> MigrationIntrospection:
    """Parse and verify 001_init.sql DDL constructs for TimescaleDB, pgvectorscale, and INV-4/INV-5."""
    if sql_path is None:
        sql_path = Path(__file__).resolve().parent / "migrations" / "001_init.sql"
    path = Path(sql_path)
    sql_text = path.read_text(encoding="utf-8")

    extensions = re.findall(
        r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+([a-zA-Z0-9_]+)",
        sql_text,
        flags=re.IGNORECASE,
    )
    hypertables = re.findall(
        r"create_hypertable\s*\(\s*'([^']+)'\s*,\s*'([^']+)'",
        sql_text,
        flags=re.IGNORECASE,
    )
    diskann_indexes = re.findall(
        r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)\s+ON\s+code_chunks\s+USING\s+diskann",
        sql_text,
        flags=re.IGNORECASE,
    )
    fts_indexes = re.findall(
        r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)\s+ON\s+code_chunks\s+USING\s+GIN\s*\(\s*tsv\s*\)",
        sql_text,
        flags=re.IGNORECASE,
    )
    triggers = re.findall(
        r"CREATE\s+TRIGGER\s+([a-zA-Z0-9_]+)\s+BEFORE\s+(UPDATE\s+OR\s+DELETE|TRUNCATE)\s+ON\s+agent_events",
        sql_text,
        flags=re.IGNORECASE,
    )
    caggs = re.findall(
        r"CREATE\s+MATERIALIZED\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)\s+WITH\s*\(\s*timescaledb\.continuous\s*\)",
        sql_text,
        flags=re.IGNORECASE,
    )
    dim_match = re.search(r"embedding\s+vector\s*\(\s*(\d+)\s*\)", sql_text, flags=re.IGNORECASE)
    dim = int(dim_match.group(1)) if dim_match else 0

    return MigrationIntrospection(
        extensions=[ext.lower() for ext in extensions],
        hypertables=[(tbl.lower(), col.lower()) for tbl, col in hypertables],
        diskann_indexes=diskann_indexes,
        fts_indexes=fts_indexes,
        immutability_triggers=[name for name, _ in triggers],
        continuous_aggregates=caggs,
        embedding_dimension=dim,
    )


class TigerDataSpine:
    """Unified Data Spine handling Semantic Memory, Relational Truth, and Time-Series Event Spine.

    Enforces INV-4 (Immutable Audit Spine triggers on UPDATE, DELETE, TRUNCATE)
    and INV-5 (Startup Dimensional Consistency between Settings.EMBEDDING_DIM and vector(N)).
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        sql_path: Optional[Path | str] = None,
        db_uri: Optional[str] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sql_path = (
            Path(sql_path)
            if sql_path is not None
            else Path(__file__).resolve().parent / "migrations" / "001_init.sql"
        )
        # INV-5: Validate configured EMBEDDING_DIM against migration SQL vector(N) at startup
        self.introspection = parse_migration_sql(self.sql_path)
        self.settings.validate_schema_dimension(self.introspection.embedding_dimension)

        resolved_db_uri = db_uri or getattr(self.settings, "SPINE_SQLITE_PATH", ":memory:")
        if not resolved_db_uri or resolved_db_uri == ":memory:":
            resolved_db_uri = ":memory:"
        else:
            Path(resolved_db_uri).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self.db_uri = resolved_db_uri
        self._conn = sqlite3.connect(
            resolved_db_uri, check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._apply_migrations()

    @property
    def embedding_dim(self) -> int:
        return self.introspection.embedding_dimension

    def _apply_migrations(self) -> None:
        """Apply relational tables, SQL-level immutability triggers, and continuous aggregate views."""
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS _tiger_extensions (
                name TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS _timescale_hypertables (
                table_name TEXT PRIMARY KEY,
                time_column_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS code_chunks (
                id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                file_path TEXT NOT NULL,
                symbol_name TEXT NOT NULL,
                line_start INTEGER NOT NULL DEFAULT 1,
                line_end INTEGER NOT NULL DEFAULT 1,
                content TEXT NOT NULL,
                embedding TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pr_reviews (
                id TEXT PRIMARY KEY,
                github_delivery_id TEXT NOT NULL UNIQUE,
                trace_id TEXT NOT NULL,
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                commit_sha TEXT NOT NULL,
                is_public INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'auto_posted', 'escalated_hitl', 'completed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
                confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                prompt_version TEXT NOT NULL,
                routing_decision TEXT NOT NULL DEFAULT 'auto_post' CHECK (routing_decision IN ('auto_post', 'hitl_escalate', 'private_security_alert')),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS procedural_conventions (
                id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                rule_key TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (repo, rule_key)
            );

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
                decay_weight REAL NOT NULL DEFAULT 1.0 CHECK (decay_weight >= 0.0 AND decay_weight <= 1.0),
                evidence_count INTEGER NOT NULL DEFAULT 1 CHECK (evidence_count >= 1),
                private_alert INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_events (
                time TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'system',
                prompt_version TEXT NOT NULL DEFAULT 'v1.0.0',
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            -- INV-4: Native SQL triggers enforcing immutability on UPDATE and DELETE
            CREATE TRIGGER IF NOT EXISTS trg_agent_events_no_update
            BEFORE UPDATE ON agent_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'agent_events is append-only and immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_agent_events_no_delete
            BEFORE DELETE ON agent_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'agent_events is append-only and immutable');
            END;

            -- Continuous Aggregate Views
            CREATE VIEW IF NOT EXISTS hourly_pr_costs AS
            SELECT
                substr(time, 1, 13) || ':00:00Z' AS bucket,
                delivery_id,
                agent_name,
                model,
                SUM(tokens_in) AS total_tokens_in,
                SUM(tokens_out) AS total_tokens_out,
                SUM(cost_usd) AS total_cost_usd,
                COUNT(*) AS event_count
            FROM agent_events
            GROUP BY substr(time, 1, 13), delivery_id, agent_name, model;

            CREATE VIEW IF NOT EXISTS agent_health_metrics AS
            SELECT
                substr(time, 1, 16) || ':00Z' AS bucket,
                agent_name,
                status,
                COUNT(*) AS span_count,
                AVG(latency_ms) AS avg_latency_ms,
                MAX(latency_ms) AS p95_latency_ms,
                SUM(cost_usd) AS total_cost_usd
            FROM agent_events
            GROUP BY substr(time, 1, 16), agent_name, status;
            """
        )

        for ext in self.introspection.extensions:
            cur.execute("INSERT OR IGNORE INTO _tiger_extensions (name) VALUES (?)", (ext,))
        for tbl, col in self.introspection.hypertables:
            cur.execute(
                "INSERT OR REPLACE INTO _timescale_hypertables (table_name, time_column_name) VALUES (?, ?)",
                (tbl, col),
            )
        self._conn.commit()

    def execute_sql(
        self, query: str, params: Sequence[Any] = ()
    ) -> List[Dict[str, Any]]:
        """Execute SQL statement against the Data Spine, enforcing INV-4 immutability triggers."""
        normalized = query.strip()

        # Check statement-level BEFORE TRUNCATE trigger on agent_events (INV-4)
        if re.match(
            r"^\s*TRUNCATE\s+(?:TABLE\s+)?agent_events\b",
            normalized,
            flags=re.IGNORECASE,
        ):
            raise ImmutableAuditSpineError("agent_events is append-only and immutable")

        try:
            cur = self._conn.cursor()
            cur.execute(normalized, tuple(params))
            rows = cur.fetchall() if cur.description else []
            self._conn.commit()
            return [dict(r) for r in rows]
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            if "agent_events is append-only and immutable" in str(exc):
                raise ImmutableAuditSpineError("agent_events is append-only and immutable") from exc
            raise
        except sqlite3.DatabaseError as exc:
            self._conn.rollback()
            if "agent_events is append-only and immutable" in str(exc):
                raise ImmutableAuditSpineError("agent_events is append-only and immutable") from exc
            raise

    # -------------------------------------------------------------------------
    # Event Spine Operations (M2 / M3)
    # -------------------------------------------------------------------------
    def insert_agent_event(
        self,
        *,
        trace_id: str,
        delivery_id: str,
        agent_name: str,
        event_type: str,
        status: str,
        model: str = "system",
        prompt_version: str = "v1.0.0",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        event_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Append an immutable event span to the agent_events hypertable (INV-4)."""
        ts = (event_time or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {})
        self.execute_sql(
            """
            INSERT INTO agent_events (
                time, trace_id, delivery_id, agent_name, event_type,
                model, prompt_version, tokens_in, tokens_out, cost_usd,
                latency_ms, status, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                trace_id,
                delivery_id,
                agent_name,
                event_type,
                model,
                prompt_version,
                int(tokens_in),
                int(tokens_out),
                float(cost_usd),
                int(latency_ms),
                status,
                meta_json,
            ),
        )
        return {
            "time": ts,
            "trace_id": trace_id,
            "delivery_id": delivery_id,
            "agent_name": agent_name,
            "event_type": event_type,
            "model": model,
            "prompt_version": prompt_version,
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "cost_usd": float(cost_usd),
            "latency_ms": int(latency_ms),
            "status": status,
            "metadata": metadata or {},
        }

    def query_agent_events(
        self,
        *,
        trace_id: Optional[str] = None,
        delivery_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Query immutable spans from agent_events ordered chronologically."""
        clauses: List[str] = []
        params: List[Any] = []
        if trace_id is not None:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if delivery_id is not None:
            clauses.append("delivery_id = ?")
            params.append(delivery_id)
        if agent_name is not None:
            clauses.append("agent_name = ?")
            params.append(agent_name)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.execute_sql(
            f"SELECT * FROM agent_events {where_sql} ORDER BY time ASC LIMIT ?",
            params,
        )
        for row in rows:
            if isinstance(row.get("metadata"), str):
                try:
                    row["metadata"] = json.loads(row["metadata"])
                except json.JSONDecodeError:
                    row["metadata"] = {}
        return rows

    def get_hourly_pr_costs(
        self, delivery_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Query the hourly_pr_costs Continuous Aggregate."""
        if delivery_id:
            return self.execute_sql(
                "SELECT * FROM hourly_pr_costs WHERE delivery_id = ? ORDER BY bucket DESC",
                (delivery_id,),
            )
        return self.execute_sql("SELECT * FROM hourly_pr_costs ORDER BY bucket DESC")

    def get_agent_health_metrics(self) -> List[Dict[str, Any]]:
        """Query the agent_health_metrics Continuous Aggregate."""
        return self.execute_sql("SELECT * FROM agent_health_metrics ORDER BY bucket DESC")

    def get_daily_spend_usd(self, since: Optional[datetime] = None) -> float:
        """Compute total USD spend over the current 24-hour window from hourly_pr_costs / agent_events."""
        cutoff = (
            (since or (datetime.now(timezone.utc) - timedelta(hours=24)))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:00:00Z")
        )
        rows = self.execute_sql(
            "SELECT COALESCE(SUM(total_cost_usd), 0.0) AS spend FROM hourly_pr_costs WHERE bucket >= ?",
            (cutoff,),
        )
        return float(rows[0]["spend"]) if rows else 0.0

    # -------------------------------------------------------------------------
    # Relational Truth: PR Reviews & Findings
    # -------------------------------------------------------------------------
    def upsert_pr_review(
        self,
        *,
        github_delivery_id: str,
        trace_id: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        status: str = "queued",
        is_public: bool = True,
        review_id: Optional[str] = None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.execute_sql(
            "SELECT id FROM pr_reviews WHERE github_delivery_id = ?",
            (github_delivery_id,),
        )
        if existing:
            rid = str(existing[0]["id"])
            self.execute_sql(
                "UPDATE pr_reviews SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, rid),
            )
            return rid

        rid = review_id or f"rev-{uuid.uuid4().hex[:12]}"
        self.execute_sql(
            """
            INSERT INTO pr_reviews (
                id, github_delivery_id, trace_id, repo, pr_number,
                commit_sha, is_public, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid,
                github_delivery_id,
                trace_id,
                repo,
                pr_number,
                commit_sha,
                1 if is_public else 0,
                status,
                now,
                now,
            ),
        )
        return rid

    def insert_finding(
        self,
        *,
        github_delivery_id: str,
        finding_data: Dict[str, Any],
        review_id: Optional[str] = None,
        routing_decision: str = "auto_post",
        finding_id: Optional[str] = None,
    ) -> str:
        fid = finding_id or f"fnd-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        self.execute_sql(
            """
            INSERT INTO findings (
                id, review_id, github_delivery_id, agent_type, severity,
                category, file_path, line_start, line_end, evidence_snippet,
                rationale, suggested_fix, confidence, prompt_version,
                routing_decision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fid,
                review_id,
                github_delivery_id,
                finding_data["agent_type"],
                finding_data["severity"],
                finding_data["category"],
                finding_data["file_path"],
                int(finding_data["line_start"]),
                int(finding_data["line_end"]),
                finding_data["evidence_snippet"],
                finding_data["rationale"],
                finding_data.get("suggested_fix"),
                float(finding_data["confidence"]),
                finding_data["prompt_version"],
                routing_decision,
                now,
            ),
        )
        return fid

    def get_findings(
        self, github_delivery_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if github_delivery_id:
            return self.execute_sql(
                "SELECT * FROM findings WHERE github_delivery_id = ? ORDER BY created_at ASC",
                (github_delivery_id,),
            )
        return self.execute_sql("SELECT * FROM findings ORDER BY created_at DESC")

    # -------------------------------------------------------------------------
    # Semantic Memory: code_chunks
    # -------------------------------------------------------------------------
    def upsert_code_chunk(
        self,
        *,
        chunk_id: str,
        repo: str,
        commit_sha: str,
        file_path: str,
        symbol_name: str,
        content: str,
        embedding: Sequence[float],
        line_start: int = 1,
        line_end: int = 1,
    ) -> None:
        if len(embedding) != self.settings.EMBEDDING_DIM:
            raise DimensionMismatchError(
                f"INV-5 Violation: chunk embedding length {len(embedding)} != "
                f"configured EMBEDDING_DIM {self.settings.EMBEDDING_DIM}"
            )
        now = datetime.now(timezone.utc).isoformat()
        self.execute_sql(
            """
            INSERT OR REPLACE INTO code_chunks (
                id, repo, commit_sha, file_path, symbol_name,
                line_start, line_end, content, embedding, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                repo,
                commit_sha,
                file_path,
                symbol_name,
                line_start,
                line_end,
                content,
                json.dumps(list(embedding)),
                now,
            ),
        )

    def get_code_chunks(self, repo: Optional[str] = None) -> List[Dict[str, Any]]:
        if repo:
            rows = self.execute_sql(
                "SELECT * FROM code_chunks WHERE repo = ?",
                (repo,),
            )
        else:
            rows = self.execute_sql("SELECT * FROM code_chunks")
        for r in rows:
            if isinstance(r.get("embedding"), str):
                r["embedding"] = json.loads(r["embedding"])
        return rows

    # -------------------------------------------------------------------------
    # Procedural & Episodic Memory: procedural_conventions & hitl_feedback
    # -------------------------------------------------------------------------
    def upsert_procedural_convention(
        self,
        *,
        repo: str,
        rule_key: str,
        title: str,
        description: str,
        category: str,
        weight: float = 1.0,
        active: bool = True,
        convention_id: Optional[str] = None,
    ) -> str:
        cid = convention_id or f"conv-{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        self.execute_sql(
            """
            INSERT OR REPLACE INTO procedural_conventions (
                id, repo, rule_key, title, description, category,
                weight, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, repo, rule_key, title, description, category, float(weight), 1 if active else 0, now, now),
        )
        return cid

    def get_procedural_conventions(
        self, repo: str, active_only: bool = True
    ) -> List[Dict[str, Any]]:
        if active_only:
            return self.execute_sql(
                "SELECT * FROM procedural_conventions WHERE repo = ? AND active = 1 ORDER BY rule_key ASC",
                (repo,),
            )
        return self.execute_sql(
            "SELECT * FROM procedural_conventions WHERE repo = ? ORDER BY rule_key ASC",
            (repo,),
        )

    def insert_hitl_feedback(
        self,
        *,
        finding_id: str,
        github_delivery_id: str,
        repo: str,
        file_path: str,
        category: str,
        severity: str,
        reviewer_action: str = "pending",
        reviewer_id: Optional[str] = None,
        notes: Optional[str] = None,
        decay_weight: float = 1.0,
        evidence_count: int = 1,
        private_alert: bool = False,
        feedback_id: Optional[str] = None,
    ) -> str:
        hid = feedback_id or f"hitl-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        self.execute_sql(
            """
            INSERT INTO hitl_feedback (
                id, finding_id, github_delivery_id, repo, file_path,
                category, severity, reviewer_action, reviewer_id, notes,
                decay_weight, evidence_count, private_alert, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hid,
                finding_id,
                github_delivery_id,
                repo,
                file_path,
                category,
                severity,
                reviewer_action,
                reviewer_id,
                notes,
                float(decay_weight),
                int(evidence_count),
                1 if private_alert else 0,
                now,
                now,
            ),
        )
        return hid

    def get_hitl_feedback(
        self,
        *,
        repo: Optional[str] = None,
        file_path: Optional[str] = None,
        category: Optional[str] = None,
        reviewer_action: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if repo:
            clauses.append("repo = ?")
            params.append(repo)
        if file_path:
            clauses.append("file_path = ?")
            params.append(file_path)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if reviewer_action:
            clauses.append("reviewer_action = ?")
            params.append(reviewer_action)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.execute_sql(
            f"SELECT * FROM hitl_feedback {where_sql} ORDER BY created_at DESC",
            params,
        )
