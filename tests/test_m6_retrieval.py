"""Milestone 6 Verification Tests: Hybrid RAG & Context Engineering (INV-5, INV-6)."""

from __future__ import annotations

import pytest

from backend.config import Settings
from backend.db.spine import TigerDataSpine
from backend.observability.events import EventSpineLogger
from backend.retrieval.hybrid_search import HybridRetriever
from backend.retrieval.indexer import DeterministicEmbedder, RepositoryIndexer


SAMPLE_REPO_FILES = {
    "src/auth/tokens.py": '''
def verify_jwt_token(raw_token: str, secret_key: str) -> dict:
    """Validate JWT signature and expiration timestamp."""
    if not raw_token:
        raise ValueError("Empty token")
    return {"sub": "user_123", "role": "admin"}
''',
    "src/api/routes.py": '''
from src.auth.tokens import verify_jwt_token

async def authenticate_request(auth_header: str) -> dict:
    """Caller that depends on verify_jwt_token for request authentication."""
    claims = verify_jwt_token(auth_header, secret_key="prod-secret")
    return claims
''',
    "src/billing/invoices.py": '''
def calculate_invoice_subtotal(line_items: list[float], tax_rate: float) -> float:
    """Calculate total invoice amount with tax."""
    subtotal = sum(line_items)
    return round(subtotal * (1.0 + tax_rate), 2)
''',
}


@pytest.mark.asyncio
async def test_m6_index_repo_and_execute_hybrid_retrieval_across_3_memories() -> None:
    """Index sample repo, seed procedural ADRs & episodic disputes, and verify hybrid query on PR diff."""
    settings = Settings(_env_file=None, EMBEDDING_DIM=1536)
    spine = TigerDataSpine(settings=settings)
    event_logger = EventSpineLogger(spine=spine)
    indexer = RepositoryIndexer(spine=spine, settings=settings, event_logger=event_logger)
    retriever = HybridRetriever(spine=spine, event_logger=event_logger)

    # 1. Index sample repository into code_chunks
    chunks = indexer.index_repository_files(
        repo="acme-corp/platform",
        commit_sha="commit-abc-123",
        files=SAMPLE_REPO_FILES,
    )
    assert len(chunks) == 3
    for chk in chunks:
        assert len(chk.embedding) == 1536

    # 2. Seed Procedural Memory (ADR / repository conventions)
    spine.upsert_procedural_convention(
        repo="acme-corp/platform",
        rule_key="ADR-004-JWT-VALIDATION",
        title="Strict JWT Audience & Algorithm Enforcement",
        description="All callers of verify_jwt_token must pass explicit allowed algorithms and audience claims.",
        category="security",
        weight=1.2,
    )

    # 3. Seed Episodic Memory (past PR finding & developer dispute on src/auth/tokens.py)
    rev_id = spine.upsert_pr_review(
        github_delivery_id="deliv-past-01",
        trace_id="tr-past-01",
        repo="acme-corp/platform",
        pr_number=12,
        commit_sha="old-sha",
        status="completed",
    )
    fnd_id = spine.insert_finding(
        github_delivery_id="deliv-past-01",
        review_id=rev_id,
        finding_data={
            "agent_type": "docs",
            "severity": "low",
            "category": "missing_docstring",
            "file_path": "src/auth/tokens.py",
            "line_start": 2,
            "line_end": 4,
            "evidence_snippet": "def verify_jwt_token(...):",
            "rationale": "Docstring format dispute.",
            "confidence": 0.60,
            "prompt_version": "docs@v1.1.0",
        },
    )
    spine.insert_hitl_feedback(
        finding_id=fnd_id,
        github_delivery_id="deliv-past-01",
        repo="acme-corp/platform",
        file_path="src/auth/tokens.py",
        category="missing_docstring",
        severity="low",
        reviewer_action="rejected_false_positive",
        decay_weight=0.65,
        evidence_count=3,
        notes="Module uses concise single-line docstrings per team convention.",
    )

    # 4. Execute Hybrid Query on a PR diff modifying verify_jwt_token
    pr_diff = """diff --git a/src/auth/tokens.py b/src/auth/tokens.py
--- a/src/auth/tokens.py
+++ b/src/auth/tokens.py
@@ -1,5 +1,6 @@
-def verify_jwt_token(raw_token: str, secret_key: str) -> dict:
+def verify_jwt_token(raw_token: str, secret_key: str, audience: str) -> dict:
     if not raw_token:
         raise ValueError("Empty token")
"""

    bundle = await retriever.retrieve_context(
        repo="acme-corp/platform",
        diff=pr_diff,
        changed_files=["src/auth/tokens.py"],
        top_k=2,
        trace_id="tr-m6-query",
        delivery_id="deliv-m6-query",
    )

    # Assert top-2 semantic chunks are verify_jwt_token AND its impacted caller authenticate_request
    retrieved_symbols = {c["symbol_name"] for c in bundle.semantic_chunks}
    assert "verify_jwt_token" in retrieved_symbols
    assert "authenticate_request" in retrieved_symbols
    assert "calculate_invoice_subtotal" not in retrieved_symbols

    # Assert Procedural ADR convention was retrieved
    assert len(bundle.procedural_conventions) == 1
    assert bundle.procedural_conventions[0]["rule_key"] == "ADR-004-JWT-VALIDATION"

    # Assert Episodic dispute on src/auth/tokens.py was retrieved
    assert len(bundle.episodic_disputes) == 1
    assert bundle.episodic_disputes[0]["reviewer_action"] == "rejected_false_positive"

    # Assert context dict formats cleanly for WorkflowContext
    ctx_dict = bundle.to_workflow_context_dict()
    assert "authenticate_request" in ctx_dict["semantic"]
    assert "ADR-004-JWT-VALIDATION" in ctx_dict["procedural"]
    assert "rejected_false_positive" in ctx_dict["episodic"]
