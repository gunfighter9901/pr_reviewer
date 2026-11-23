"""Hybrid RAG & Context Engineering package (M6)."""

from backend.retrieval.hybrid_search import HybridRetriever, RetrievedContextBundle
from backend.retrieval.indexer import (
    DeterministicEmbedder,
    Embedder,
    GeminiEmbedder,
    RepositoryIndexer,
    build_embedder,
)

__all__ = [
    "DeterministicEmbedder",
    "Embedder",
    "GeminiEmbedder",
    "HybridRetriever",
    "RepositoryIndexer",
    "RetrievedContextBundle",
    "build_embedder",
]
