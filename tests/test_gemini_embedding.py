"""Gemini Embedding 2 tests: dimensions, task types, errors, and retrieval wiring."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from backend.config import DimensionMismatchError, Settings
from backend.db.spine import TigerDataSpine
from backend.retrieval.hybrid_search import HybridRetriever
from backend.retrieval.indexer import (
    DeterministicEmbedder,
    GeminiEmbedder,
    GeminiEmbeddingError,
    RepositoryIndexer,
    build_embedder,
)

DIM = 1536


def _client(handler: Any, dimension: int = DIM) -> GeminiEmbedder:
    return GeminiEmbedder(
        api_key="test-gemini-key-1234567890",
        model="gemini-embedding-2",
        dimension=dimension,
        base_url="https://gemini.example/v1beta",
        transport=httpx.MockTransport(handler),
    )


def test_embedding_provider_defaults_to_deterministic() -> None:
    embedder = build_embedder(Settings(_env_file=None))

    assert isinstance(embedder, DeterministicEmbedder)
    assert len(embedder.embed("def handler(): pass")) == DIM


def test_gemini_embedding_requires_a_real_key() -> None:
    settings = Settings(
        _env_file=None,
        EMBEDDING_PROVIDER="gemini",
        GEMINI_API_KEY="REPLACE_ME_gemini_key",
    )
    with pytest.raises(GeminiEmbeddingError):
        build_embedder(settings)


def test_gemini_embedding_requests_configured_dimensions_and_task_types() -> None:
    captured: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "payload": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"embedding": {"values": [0.01] * DIM}})

    embedder = _client(handler)
    document = embedder.embed("def parse_unified_diff(diff_text): return diff_text")
    query = embedder.embed_query("where is the diff parser")

    assert len(document) == DIM
    assert len(query) == DIM
    assert captured[0]["url"].endswith(
        "/models/gemini-embedding-2:embedContent"
    )
    assert captured[0]["headers"]["x-goog-api-key"] == "test-gemini-key-1234567890"
    assert captured[0]["payload"]["output_dimensionality"] == DIM
    assert captured[0]["payload"]["task_type"] == "RETRIEVAL_DOCUMENT"
    assert captured[1]["payload"]["task_type"] == "RETRIEVAL_QUERY"


def test_gemini_embedding_rejects_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": {"values": [0.01] * 768}})

    with pytest.raises(DimensionMismatchError, match="768 dimensions"):
        _client(handler).embed("dimension mismatch")


def test_gemini_embedding_surfaces_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Quota exceeded for this project")

    with pytest.raises(GeminiEmbeddingError, match="HTTP 429"):
        _client(handler).embed("rate limited")


class QueryAwareEmbedder(DeterministicEmbedder):
    def __init__(self) -> None:
        super().__init__(dimension=DIM)
        self.query_calls = 0

    def embed_query(self, text: str) -> List[float]:
        self.query_calls += 1
        return super().embed_query(text)


def test_retriever_uses_query_embedding_contract() -> None:
    settings = Settings(_env_file=None, EMBEDDING_DIM=DIM)
    spine = TigerDataSpine(settings=settings)
    embedder = QueryAwareEmbedder()
    indexer = RepositoryIndexer(spine=spine, settings=settings, embedder=embedder)
    indexer.index_repository_files(
        repo="acme/embedding-smoke",
        commit_sha="abc123",
        files={
            "app/db.py": (
                "def fetch_user(user_id):\n"
                "    return db.execute('SELECT * FROM users WHERE id = ?', user_id)\n"
            )
        },
    )

    retriever = HybridRetriever(spine=spine, embedder=embedder)
    results = retriever.search_semantic_hybrid(
        repo="acme/embedding-smoke",
        query_text="fetch_user query",
    )

    assert results
    assert embedder.query_calls == 1
