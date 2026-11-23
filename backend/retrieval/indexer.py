"""AST-Aware Repository Chunker and Embedder enforcing INV-5 Dimensional Consistency (M6)."""

from __future__ import annotations

import ast
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Sequence

import httpx

from backend.config import DimensionMismatchError, Settings, get_settings
from backend.db.spine import TigerDataSpine
from backend.observability.events import EventSpineLogger


class GeminiEmbeddingError(RuntimeError):
    """Raised when the Gemini embedding endpoint cannot produce a usable vector."""


@dataclass
class CodeChunkData:
    chunk_id: str
    repo: str
    commit_sha: str
    file_path: str
    symbol_name: str
    line_start: int
    line_end: int
    content: str
    embedding: List[float]


def _tokenize_code(text: str) -> List[str]:
    """Split camelCase, snake_case, and identifiers into lowercase tokens."""
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    raw_tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expanded.lower())
    tokens: List[str] = []
    for tok in raw_tokens:
        tokens.append(tok)
        if "_" in tok:
            tokens.extend(part for part in tok.split("_") if len(part) > 1)
    return tokens


class DeterministicEmbedder:
    """Feature-hashing code embedder producing L2-normalized vectors of exact dimension EMBEDDING_DIM (INV-5)."""

    def __init__(self, dimension: int = 1536) -> None:
        if dimension <= 0:
            raise ValueError("Embedding dimension must be positive")
        self.dimension = int(dimension)

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dimension
        tokens = _tokenize_code(text)
        if not tokens:
            vec[0] = 1.0
            return vec

        # Unigrams + bigrams hashed into `self.dimension` signed projections
        features = list(tokens) + [
            f"{tokens[i]}::{tokens[i + 1]}" for i in range(len(tokens) - 1)
        ]
        for feat in features:
            digest = hashlib.sha256(feat.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[idx] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [round(v / norm, 6) for v in vec]
        else:
            vec[0] = 1.0
        return vec

    def embed_query(self, text: str) -> List[float]:
        """Query embeddings use the same deterministic projection as documents."""
        return self.embed(text)


class GeminiEmbedder:
    """Gemini Embedding 2 adapter producing exactly EMBEDDING_DIM dimensions (INV-5)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-embedding-2",
        dimension: int = 1536,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 60.0,
        task_type: str = "RETRIEVAL_DOCUMENT",
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise GeminiEmbeddingError("GEMINI_API_KEY must not be empty")
        if dimension <= 0:
            raise ValueError("Embedding dimension must be positive")
        self.api_key = api_key.strip()
        self.model = model
        self.dimension = int(dimension)
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.task_type = task_type
        self.transport = transport

    def embed(self, text: str) -> List[float]:
        """Embed repository content for indexing."""
        return self._embed(text, self.task_type)

    def embed_query(self, text: str) -> List[float]:
        """Embed a retrieval query with the asymmetric query task type."""
        return self._embed(text, "RETRIEVAL_QUERY")

    def _embed(self, text: str, task_type: str) -> List[float]:
        payload = {
            "content": {"parts": [{"text": text}]},
            "task_type": task_type,
            "output_dimensionality": self.dimension,
        }
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={"x-goog-api-key": self.api_key},
            ) as client:
                response = client.post(
                    f"{self.base_url}/models/{self.model}:embedContent", json=payload
                )
        except httpx.HTTPError as exc:
            raise GeminiEmbeddingError(
                f"Gemini embedding request failed: {exc.__class__.__name__}"
            ) from exc

        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:300]
            raise GeminiEmbeddingError(
                f"Gemini embedding API returned HTTP {response.status_code}: {detail}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise GeminiEmbeddingError("Gemini embedding returned non-JSON") from exc

        values = (
            body.get("embedding", {}).get("values", [])
            if isinstance(body, dict)
            else []
        )
        if len(values) != self.dimension:
            raise DimensionMismatchError(
                f"INV-5 Violation: Gemini embedding returned {len(values)} dimensions, "
                f"expected {self.dimension}"
            )
        return [float(value) for value in values]


class Embedder(Protocol):
    """Minimal embedder contract shared by deterministic and Gemini providers."""

    dimension: int

    def embed(self, text: str) -> List[float]:
        ...

    def embed_query(self, text: str) -> List[float]:
        ...


def build_embedder(settings: Optional[Settings] = None) -> Embedder:
    """Build the configured embedder. Deterministic mode stays fully offline."""
    cfg = settings or get_settings()
    if cfg.EMBEDDING_PROVIDER == "deterministic":
        return DeterministicEmbedder(dimension=cfg.EMBEDDING_DIM)
    if cfg.EMBEDDING_PROVIDER != "gemini":
        raise GeminiEmbeddingError(
            f"Unsupported EMBEDDING_PROVIDER={cfg.EMBEDDING_PROVIDER!r}"
        )

    key = cfg.GEMINI_API_KEY.strip()
    if len(key) < 20 or "REPLACE_ME" in key.upper():
        raise GeminiEmbeddingError(
            "EMBEDDING_PROVIDER=gemini requires a real GEMINI_API_KEY"
        )
    model = cfg.EMBEDDING_MODEL
    if not model.startswith("gemini-embedding"):
        model = "gemini-embedding-2"
    return GeminiEmbedder(
        api_key=key,
        model=model,
        dimension=cfg.EMBEDDING_DIM,
        base_url=cfg.GEMINI_API_BASE_URL,
        timeout_seconds=cfg.GEMINI_TIMEOUT_SECONDS,
    )


class RepositoryIndexer:
    """Chunks source files by AST symbols and indexes them into TigerDataSpine.code_chunks."""

    def __init__(
        self,
        spine: TigerDataSpine,
        settings: Optional[Settings] = None,
        embedder: Optional[Embedder] = None,
        event_logger: Optional[EventSpineLogger] = None,
    ) -> None:
        self.spine = spine
        self.settings = settings or spine.settings or get_settings()
        self.embedder = embedder or build_embedder(self.settings)
        self.event_logger = event_logger

        # INV-5 check on embedder dimension vs spine dimension
        if self.embedder.dimension != self.spine.embedding_dim:
            raise DimensionMismatchError(
                f"INV-5 Violation: Embedder dimension ({self.embedder.dimension}) != "
                f"Spine code_chunks.embedding dimension ({self.spine.embedding_dim})"
            )

    def chunk_file(
        self,
        *,
        repo: str,
        commit_sha: str,
        file_path: str,
        source_code: str,
    ) -> List[CodeChunkData]:
        """Extract symbol-level chunks from Python AST or fallback line blocks."""
        lines = source_code.splitlines()
        raw_symbols: List[tuple[str, int, int, str]] = []

        if file_path.endswith(".py"):
            try:
                tree = ast.parse(source_code, filename=file_path)
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        start = getattr(node, "lineno", 1)
                        end = getattr(node, "end_lineno", start)
                        snippet = "\n".join(lines[start - 1 : end])
                        raw_symbols.append((node.name, start, end, snippet))
            except SyntaxError:
                raw_symbols = []

        if not raw_symbols:
            # Generic regex or whole-file chunking for non-Python or top-level scripts
            pattern = re.compile(
                r"^(?:export\s+)?(?:async\s+)?(?:function|class|def)\s+([a-zA-Z0-9_]+)",
                flags=re.MULTILINE,
            )
            matches = list(pattern.finditer(source_code))
            if matches:
                for i, m in enumerate(matches):
                    sym = m.group(1)
                    start_line = source_code[: m.start()].count("\n") + 1
                    end_line = (
                        source_code[: matches[i + 1].start()].count("\n")
                        if i + 1 < len(matches)
                        else max(1, len(lines))
                    )
                    snippet = "\n".join(lines[start_line - 1 : end_line])
                    raw_symbols.append((sym, start_line, end_line, snippet))
            else:
                raw_symbols.append(
                    (
                        Path_stem(file_path),
                        1,
                        max(1, len(lines)),
                        source_code,
                    )
                )

        chunks: List[CodeChunkData] = []
        for symbol_name, l_start, l_end, snippet in raw_symbols:
            cid_hash = hashlib.sha256(
                f"{repo}:{commit_sha}:{file_path}:{symbol_name}:{l_start}".encode("utf-8")
            ).hexdigest()[:14]
            chunk_id = f"chk-{cid_hash}"
            embedding = self.embedder.embed(f"{symbol_name}\n{snippet}")
            chunks.append(
                CodeChunkData(
                    chunk_id=chunk_id,
                    repo=repo,
                    commit_sha=commit_sha,
                    file_path=file_path,
                    symbol_name=symbol_name,
                    line_start=l_start,
                    line_end=l_end,
                    content=snippet,
                    embedding=embedding,
                )
            )
        return chunks

    def index_repository_files(
        self,
        *,
        repo: str,
        commit_sha: str,
        files: Dict[str, str],
    ) -> List[CodeChunkData]:
        """Chunk and index a dictionary of {file_path: source_code} into TigerDataSpine."""
        indexed: List[CodeChunkData] = []
        for file_path, content in files.items():
            file_chunks = self.chunk_file(
                repo=repo,
                commit_sha=commit_sha,
                file_path=file_path,
                source_code=content,
            )
            for chk in file_chunks:
                self.spine.upsert_code_chunk(
                    chunk_id=chk.chunk_id,
                    repo=chk.repo,
                    commit_sha=chk.commit_sha,
                    file_path=chk.file_path,
                    symbol_name=chk.symbol_name,
                    line_start=chk.line_start,
                    line_end=chk.line_end,
                    content=chk.content,
                    embedding=chk.embedding,
                )
                indexed.append(chk)
        return indexed


def Path_stem(file_path: str) -> str:
    name = file_path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name
