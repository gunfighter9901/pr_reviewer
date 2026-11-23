"""Diff selection guardrails for large or vendored pull requests.

GitHub can return multi-megabyte diffs (bundled dependencies, lockfiles, source
maps, generated assets). Sending those to every specialist is slow, expensive,
and usually irrelevant. This module keeps reviewable source changes within a
bounded budget while preserving unified-diff structure so finding grounding
still maps to exact added lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Directory segments that almost never contain hand-written reviewable code.
_EXCLUDED_DIR_SEGMENTS = frozenset(
    {
        "node_modules",
        "bower_components",
        "vendor",
        "vendors",
        "third_party",
        "third-party",
        "external",
        "dist",
        "build",
        "out",
        "coverage",
        ".next",
        ".nuxt",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".cache",
        "site-packages",
    }
)

_EXCLUDED_FILENAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pipfile.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "bun.lockb",
        "uv.lock",
    }
)

_EXCLUDED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".map",
    ".tsbuildinfo",
    ".pyc",
    ".pyo",
    ".class",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".mov",
    ".webm",
    ".sqlite",
    ".sqlite3",
    ".db",
)

_SOURCE_SUFFIXES = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".cs",
    ".swift",
    ".m",
    ".mm",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".sql",
    ".graphql",
    ".proto",
    ".vue",
    ".svelte",
)

_CONFIG_SUFFIXES = (
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".xml",
    ".svg",
)

_CONFIG_FILENAMES = frozenset(
    {
        "dockerfile",
        "makefile",
        "procfile",
        "gemfile",
        "rakefile",
        "jenkinsfile",
    }
)


@dataclass(frozen=True)
class DiffSelection:
    """A bounded diff plus a summary of what was included or skipped."""

    diff: str
    included_files: List[str] = field(default_factory=list)
    excluded_files: List[str] = field(default_factory=list)
    truncated: bool = False
    original_bytes: int = 0

    @property
    def selected_bytes(self) -> int:
        return len(self.diff.encode("utf-8"))


def _path_from_chunk(chunk: str) -> Optional[str]:
    """Return the new-side path of a unified-diff file chunk."""
    new_path: Optional[str] = None
    old_path: Optional[str] = None
    for line in chunk.splitlines():
        if line.startswith("+++ "):
            new_path = line[4:].strip()
        elif line.startswith("--- "):
            old_path = line[4:].strip()

    for candidate in (new_path, old_path):
        if not candidate or candidate == "/dev/null":
            continue
        if candidate.startswith("b/") or candidate.startswith("a/"):
            candidate = candidate[2:]
        return candidate
    return None


def split_unified_diff(diff_text: str) -> List[Tuple[str, str]]:
    """Split a unified diff into ``(path, chunk)`` pairs, preserving chunk text."""
    chunks: List[Tuple[str, str]] = []
    current: List[str] = []

    def flush() -> None:
        if not current:
            return
        chunk = "".join(current)
        chunks.append((_path_from_chunk(chunk) or "unknown", chunk))

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            current = [line]
        elif current:
            current.append(line)
    flush()
    return chunks


def _has_added_lines(chunk: str) -> bool:
    for line in chunk.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            return True
    return False


def _classify(path: str) -> Optional[int]:
    """Return 0 for source, 1 for docs/config, 2 for other; None means excluded."""
    lowered = path.lower()
    segments = lowered.split("/")
    filename = segments[-1]

    if any(segment in _EXCLUDED_DIR_SEGMENTS for segment in segments[:-1]):
        return None
    if filename in _EXCLUDED_FILENAMES:
        return None
    if any(filename.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES):
        return None

    if any(filename.endswith(suffix) for suffix in _SOURCE_SUFFIXES):
        return 0
    if filename in _CONFIG_FILENAMES:
        return 1
    if any(filename.endswith(suffix) for suffix in _CONFIG_SUFFIXES):
        return 1
    return 2


def _truncate_bytes(text: str, limit: int) -> Tuple[str, bool]:
    """Truncate ``text`` to at most ``limit`` bytes on a line boundary."""
    if limit <= 0:
        return "", True
    if len(text.encode("utf-8")) <= limit:
        return text, False

    kept: List[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        line_size = len(line.encode("utf-8"))
        if size + line_size > limit:
            break
        kept.append(line)
        size += line_size

    truncated = "".join(kept)
    if truncated and not truncated.endswith("\n"):
        truncated += "\n"
    return truncated, True


def select_reviewable_diff(
    diff_text: str,
    *,
    max_bytes: int = 250_000,
    max_file_bytes: int = 50_000,
) -> DiffSelection:
    """Keep the most reviewable file chunks within byte budgets.

    Source files are preferred over docs/config, which are preferred over
    unknown file types. Vendored, generated, lockfile, binary, and minified
    chunks are excluded. If nothing survives the filters, the leading
    ``max_bytes`` of the original diff is returned so the PR still gets a
    best-effort review.
    """
    if not diff_text:
        return DiffSelection(diff="", original_bytes=0)

    original_bytes = len(diff_text.encode("utf-8"))
    chunks = split_unified_diff(diff_text)

    candidates: List[Tuple[int, int, str, str]] = []
    excluded: List[str] = []
    excluded_set = set()

    for index, (path, chunk) in enumerate(chunks):
        priority = _classify(path)
        if priority is None or not _has_added_lines(chunk):
            if priority is None:
                excluded.append(path)
                excluded_set.add(path)
            continue
        candidates.append((priority, index, path, chunk))

    candidates.sort(key=lambda item: (item[0], item[1]))

    included: List[str] = []
    selected_chunks: List[str] = []
    total_bytes = 0
    truncated = False

    for _priority, _index, path, chunk in candidates:
        bounded, file_truncated = _truncate_bytes(chunk, max_file_bytes)
        if not bounded:
            truncated = True
            continue
        bounded_bytes = len(bounded.encode("utf-8"))
        if total_bytes + bounded_bytes > max_bytes:
            remaining = max_bytes - total_bytes
            if remaining <= 0:
                truncated = True
                continue
            bounded, _ = _truncate_bytes(bounded, remaining)
            bounded_bytes = len(bounded.encode("utf-8"))
            if bounded_bytes <= 0:
                truncated = True
                continue
            file_truncated = True

        selected_chunks.append(bounded)
        included.append(path)
        total_bytes += bounded_bytes
        truncated = truncated or file_truncated

    if not selected_chunks:
        fallback, fallback_truncated = _truncate_bytes(diff_text, max_bytes)
        return DiffSelection(
            diff=fallback,
            included_files=[],
            excluded_files=excluded,
            truncated=fallback_truncated,
            original_bytes=original_bytes,
        )

    selected_names = set(included)
    return DiffSelection(
        diff="".join(selected_chunks),
        included_files=included,
        excluded_files=[
            path
            for path in dict.fromkeys(excluded)
            if path not in selected_names
        ],
        truncated=truncated,
        original_bytes=original_bytes,
    )
