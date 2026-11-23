"""Validated Pydantic Settings enforcing system configuration and INV-5 (Dimensional Consistency)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DimensionMismatchError(ValueError):
    """Raised when EMBEDDING_DIM does not match the database schema vector(N) column (INV-5)."""


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # GitHub Ingress & Egress
    GITHUB_WEBHOOK_SECRET: str = Field(default="dev_webhook_secret_change_me", min_length=1)
    GITHUB_APP_ID: str = Field(default="123456")
    GITHUB_PRIVATE_KEY_PATH: str = Field(default="./certs/github-private-key.pem")
    GITHUB_TOKEN: str = Field(default="ghp_fallback_personal_access_token")
    GITHUB_API_BASE_URL: str = Field(default="https://api.github.com")
    GITHUB_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)

    # Diff budget for specialists. Large/vendored chunks are filtered first.
    MAX_REVIEW_DIFF_BYTES: int = Field(default=120_000, gt=0)
    MAX_REVIEW_FILE_BYTES: int = Field(default=40_000, gt=0)

    # Unified Data Spine (Tiger Cloud / TimescaleDB + pgvectorscale)
    TIGER_DATABASE_URL: str = Field(
        default="postgresql://user:password@localhost:5432/tsdb?sslmode=disable"
    )
    # Local relational/checkpoint store. Use a file path to persist findings across worker jobs.
    SPINE_SQLITE_PATH: str = Field(default=":memory:")

    # Queue & Checkpointing (Upstash / Redis)
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    # memory: in-process only (tests / dry runs). redis: webhook -> ARQ worker.
    QUEUE_BACKEND: Literal["memory", "redis"] = Field(default="memory")

    # LLM Providers & Embeddings
    # deterministic: regex specialists (default, offline, test-safe)
    # gemini: live Gemini generateContent calls wired through the worker path
    LLM_PROVIDER: Literal["deterministic", "gemini"] = Field(default="deterministic")
    GEMINI_API_KEY: str = Field(default="")
    GEMINI_MODEL_HIGH: str = Field(default="gemini-3.6-flash")
    GEMINI_MODEL_FAST: str = Field(default="gemini-3.5-flash-lite")
    GEMINI_API_BASE_URL: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta"
    )
    GEMINI_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0.0)

    OPENAI_API_KEY: str = Field(default="sk-test-openai-key")
    ANTHROPIC_API_KEY: str = Field(default="sk-ant-test-anthropic-key")
    # deterministic: feature-hashing embedder (default, offline, test-safe)
    # gemini: live Gemini Embedding 2 calls with output_dimensionality=EMBEDDING_DIM
    EMBEDDING_PROVIDER: Literal["deterministic", "gemini"] = Field(
        default="deterministic"
    )
    EMBEDDING_MODEL: str = Field(default="text-embedding-3-small")
    EMBEDDING_DIM: int = Field(default=1536, gt=0)

    # Autonomy, Selectivity & Economics Guards
    CONFIDENCE_THRESHOLD: float = Field(default=0.75, ge=0.0, le=1.0)
    DAILY_BUDGET_USD: float = Field(default=10.00, gt=0.0)
    NODE_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0.0)

    # Single-PR summary comment: post the whole review as one structured
    # conversation comment instead of one inline comment per finding.
    POST_PR_SUMMARY_COMMENT: bool = Field(default=False)

    @field_validator("GITHUB_WEBHOOK_SECRET")
    @classmethod
    def _validate_non_blank_secret(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("GITHUB_WEBHOOK_SECRET must not be empty or whitespace")
        return value

    def validate_schema_dimension(self, schema_dim: int) -> None:
        """Enforce INV-5: EMBEDDING_DIM must match code_chunks.embedding vector(N)."""
        if self.EMBEDDING_DIM != schema_dim:
            raise DimensionMismatchError(
                f"INV-5 Violation: Configured EMBEDDING_DIM={self.EMBEDDING_DIM} "
                f"does not match code_chunks.embedding schema vector({schema_dim})."
            )

    def validate_against_migration_sql(self, sql_path: Optional[Path | str] = None) -> int:
        """Parse migration SQL and assert code_chunks.embedding vector(N) matches EMBEDDING_DIM."""
        if sql_path is None:
            sql_path = Path(__file__).resolve().parent / "db" / "migrations" / "001_init.sql"
        path = Path(sql_path)
        if not path.exists():
            raise FileNotFoundError(f"Migration SQL file not found at {path}")
        sql_text = path.read_text(encoding="utf-8")
        match = re.search(r"embedding\s+vector\s*\(\s*(\d+)\s*\)", sql_text, re.IGNORECASE)
        if not match:
            raise ValueError(f"Could not locate embedding vector(N) column in {path}")
        schema_dim = int(match.group(1))
        self.validate_schema_dimension(schema_dim)
        return schema_dim


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
