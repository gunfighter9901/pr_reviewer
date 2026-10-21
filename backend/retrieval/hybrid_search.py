"""Hybrid Retriever combining DiskANN Vector Search, PostgreSQL FTS (tsvector), Episodic & Procedural Memory (M6)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from backend.db.spine import TigerDataSpine
from backend.observability.events import EventSpineLogger
from backend.retrieval.indexer import DeterministicEmbedder, _tokenize_code


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _fts_tsvector_score(query_tokens: Set[str], symbol_name: str, content: str) -> float:
    """Compute PostgreSQL FTS (tsvector/ts_rank) lexical score with symbol boosting."""
    if not query_tokens:
        return 0.0
    doc_tokens = set(_tokenize_code(f"{symbol_name} {content}"))
    sym_tokens = set(_tokenize_code(symbol_name))
    if not doc_tokens:
        return 0.0

    overlap = len(query_tokens & doc_tokens)
    sym_overlap = len(query_tokens & sym_tokens)
    # Direct substring match for exact function call references in content (impacted callers/callees)
    caller_boost = sum(
        1.5 for tok in query_tokens if len(tok) > 3 and f"{tok}(" in content.lower()
    )
    return (overlap / len(query_tokens)) + (0.75 * sym_overlap) + caller_boost


@dataclass
class RetrievedContextBundle:
    """3-Dimensional Memory Context retrieved from the Unified Tiger Data Spine."""

    semantic_chunks: List[Dict[str, Any]] = field(default_factory=list)
    episodic_disputes: List[Dict[str, Any]] = field(default_factory=list)
    procedural_conventions: List[Dict[str, Any]] = field(default_factory=list)

    def to_workflow_context_dict(self) -> Dict[str, Any]:
        semantic_text = "\n---\n".join(
            f"[{c['file_path']}:{c['symbol_name']} (score={c['hybrid_score']:.3f})]\n{c['content']}"
            for c in self.semantic_chunks
        )
        episodic_text = "\n".join(
            f"- [{d['reviewer_action']}] {d['file_path']} ({d['category']}): "
            f"decay_weight={d['decay_weight']}, evidence_count={d['evidence_count']}"
            for d in self.episodic_disputes
        )
        procedural_text = "\n".join(
            f"- [{p['rule_key']}] {p['title']}: {p['description']} (weight={p['weight']})"
            for p in self.procedural_conventions
        )
        return {
            "semantic": semantic_text,
            "episodic": episodic_text,
            "procedural": procedural_text,
            "semantic_chunks": self.semantic_chunks,
            "episodic_disputes": self.episodic_disputes,
            "procedural_conventions": self.procedural_conventions,
        }


class HybridRetriever:
    """Executes 3-dimensional retrieval (Semantic DiskANN+FTS RRF, Episodic Disputes, Procedural ADRs)."""

    def __init__(
        self,
        spine: TigerDataSpine,
        embedder: Optional[DeterministicEmbedder] = None,
        event_logger: Optional[EventSpineLogger] = None,
        rrf_k: int = 20,
    ) -> None:
        self.spine = spine
        self.embedder = embedder or DeterministicEmbedder(dimension=spine.embedding_dim)
        self.event_logger = event_logger
        self.rrf_k = rrf_k

    def search_semantic_hybrid(
        self,
        *,
        repo: str,
        query_text: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Combine DiskANN vector similarity and PostgreSQL FTS tsvector ranking via Reciprocal Rank Fusion."""
        chunks = self.spine.get_code_chunks(repo=repo)
        if not chunks:
            return []

        query_vec = self.embedder.embed(query_text)
        query_tokens = set(_tokenize_code(query_text))

        scored: List[Dict[str, Any]] = []
        for chk in chunks:
            vec_sim = _cosine_similarity(query_vec, chk["embedding"])
            fts_score = _fts_tsvector_score(
                query_tokens, chk["symbol_name"], chk["content"]
            )
            scored.append(
                {
                    **{k: v for k, v in chk.items() if k != "embedding"},
                    "vector_score": round(vec_sim, 6),
                    "fts_score": round(fts_score, 6),
                }
            )

        # Rank by vector similarity (DiskANN)
        by_vec = sorted(scored, key=lambda x: x["vector_score"], reverse=True)
        vec_ranks = {item["id"]: rank + 1 for rank, item in enumerate(by_vec)}

        # Rank by FTS lexical match (tsvector GIN)
        by_fts = sorted(scored, key=lambda x: x["fts_score"], reverse=True)
        fts_ranks = {item["id"]: rank + 1 for rank, item in enumerate(by_fts)}

        for item in scored:
            cid = item["id"]
            rrf = (1.0 / (self.rrf_k + vec_ranks[cid])) + (
                1.0 / (self.rrf_k + fts_ranks[cid])
            )
            # Combine RRF with normalized raw signal
            item["hybrid_score"] = round(
                rrf + 0.35 * max(0.0, item["vector_score"]) + 0.25 * item["fts_score"],
                6,
            )

        scored.sort(key=lambda x: x["hybrid_score"], reverse=True)
        return scored[:top_k]

    async def retrieve_context(
        self,
        *,
        repo: str,
        diff: str,
        changed_files: Optional[Sequence[str]] = None,
        top_k: int = 5,
        trace_id: str = "tr-retrieval",
        delivery_id: str = "deliv-retrieval",
    ) -> RetrievedContextBundle:
        """Query Semantic (DiskANN+FTS), Episodic (past PR disputes), and Procedural (ADRs) memory."""
        semantic_chunks = self.search_semantic_hybrid(
            repo=repo,
            query_text=diff,
            top_k=top_k,
        )

        all_feedback = self.spine.get_hitl_feedback(repo=repo)
        if changed_files:
            changed_set = set(changed_files)
            episodic_disputes = [
                fb
                for fb in all_feedback
                if fb["file_path"] in changed_set
                or fb["reviewer_action"] in ("rejected_false_positive", "disputed")
            ]
        else:
            episodic_disputes = all_feedback

        procedural_conventions = self.spine.get_procedural_conventions(
            repo=repo, active_only=True
        )

        if self.event_logger is not None:
            await self.event_logger.log_event(
                trace_id=trace_id,
                delivery_id=delivery_id,
                agent_name="retriever",
                event_type="hybrid_context_retrieved",
                model=self.spine.settings.EMBEDDING_MODEL,
                status="ok",
                tokens_in=max(16, len(diff) // 4),
                tokens_out=0,
                metadata={
                    "semantic_hits": len(semantic_chunks),
                    "episodic_hits": len(episodic_disputes),
                    "procedural_hits": len(procedural_conventions),
                },
            )

        return RetrievedContextBundle(
            semantic_chunks=semantic_chunks,
            episodic_disputes=episodic_disputes,
            procedural_conventions=procedural_conventions,
        )
