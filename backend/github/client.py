"""GitHub REST client for pulling PR diffs and posting review comments (live integration)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from backend.config import Settings, get_settings

_PLACEHOLDER_TOKENS = (
    "replace_me",
    "ghp_fallback",
    "your_",
    "change_me",
    "github_pat_xxx",
)


class GitHubConfigurationError(RuntimeError):
    """Raised when a GitHub API call is attempted without a usable token."""


class GitHubAPIError(RuntimeError):
    """Raised when GitHub rejects a request or the transport fails."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitHub API HTTP {status_code}: {message}")


def is_real_github_token(token: str) -> bool:
    """Return True when the configured value looks like a real token, not a placeholder."""
    value = (token or "").strip()
    if len(value) < 20:
        return False
    lowered = value.lower()
    return not any(marker in lowered for marker in _PLACEHOLDER_TOKENS)


@dataclass(frozen=True)
class PullRequestBundle:
    """Everything the review pipeline needs to analyze a pull request."""

    repo: str
    pr_number: int
    head_sha: str
    diff: str
    changed_files: List[str] = field(default_factory=list)
    is_public: bool = True


def extract_changed_files(diff_text: str) -> List[str]:
    """Extract repo-relative paths from the ``+++ b/...`` headers of a unified diff."""
    paths: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            if target and target != "/dev/null" and target not in paths:
                paths.append(target)
    return paths


def build_diff_from_files(files: List[Dict[str, Any]]) -> str:
    """Synthesize a unified diff from the per-file ``patch`` fields GitHub returns."""
    chunks: List[str] = []
    for item in files:
        patch = item.get("patch")
        filename = item.get("filename")
        if not patch or not filename:
            continue
        status = str(item.get("status") or "modified")
        previous = str(item.get("previous_filename") or filename)
        old_path = "/dev/null" if status == "added" else f"a/{previous}"
        new_path = "/dev/null" if status == "removed" else f"b/{filename}"
        chunks.append(
            f"diff --git a/{previous} b/{filename}\n"
            f"--- {old_path}\n"
            f"+++ {new_path}\n"
            f"{patch.rstrip()}\n"
        )
    return "".join(chunks)


class GitHubClient:
    """Small async GitHub API client scoped to the PR review workflow."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        settings: Optional[Settings] = None,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        cfg = settings or get_settings()
        self.settings = cfg
        self.token = (token if token is not None else cfg.GITHUB_TOKEN).strip()
        self.base_url = (base_url or cfg.GITHUB_API_BASE_URL).rstrip("/")
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None else cfg.GITHUB_TIMEOUT_SECONDS
        )
        self._client = httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def _require_token(self) -> str:
        if not is_real_github_token(self.token):
            raise GitHubConfigurationError(
                "A real GITHUB_TOKEN is required to fetch PR diffs and post reviews. "
                "Set it in .env (fine-grained PAT: Pull requests read/write, Contents read)."
            )
        return self.token

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_token()}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-reviewer",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        json: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(accept=accept),
                json=json,
            )
        except httpx.HTTPError as exc:
            raise GitHubAPIError(
                0, f"transport error: {exc.__class__.__name__}"
            ) from exc

        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:500]
            raise GitHubAPIError(response.status_code, detail)
        return response

    async def get_authenticated_user(self) -> Dict[str, Any]:
        response = await self._request("GET", "/user")
        return response.json()

    async def fetch_pull_request_bundle(
        self, repo: str, pr_number: int
    ) -> PullRequestBundle:
        """Fetch PR metadata, the unified diff, and the changed-file list."""
        meta_response = await self._request("GET", f"/repos/{repo}/pulls/{pr_number}")
        meta = meta_response.json()
        head_sha = str((meta.get("head") or {}).get("sha") or "")

        files: List[Dict[str, Any]] = []
        try:
            files = await self._fetch_files(repo, pr_number)
        except GitHubAPIError:
            files = []

        diff = ""
        try:
            diff_response = await self._request(
                "GET",
                f"/repos/{repo}/pulls/{pr_number}",
                accept="application/vnd.github.v3.diff",
            )
            diff = diff_response.text
        except GitHubAPIError as exc:
            if exc.status_code not in (406, 415, 422):
                raise
            diff = build_diff_from_files(files)

        if not diff.strip() and files:
            diff = build_diff_from_files(files)

        changed_files = [
            str(item["filename"]) for item in files if item.get("filename")
        ]
        if not changed_files:
            changed_files = extract_changed_files(diff)

        base_repo = (meta.get("base") or {}).get("repo") or {}
        is_public = not bool(base_repo.get("private", False))

        return PullRequestBundle(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            diff=diff,
            changed_files=changed_files,
            is_public=is_public,
        )

    async def _fetch_files(
        self, repo: str, pr_number: int, max_pages: int = 10
    ) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            response = await self._request(
                "GET",
                f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}",
            )
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            files.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        return files

    async def post_review_comment(
        self,
        *,
        repo: str,
        pr_number: int,
        commit_sha: str,
        path: str,
        line_start: int,
        line_end: Optional[int],
        body: str,
    ) -> Dict[str, Any]:
        """Post an inline review comment, falling back to a PR issue comment on 422."""
        resolved_end = int(line_end or line_start)
        payload: Dict[str, Any] = {
            "body": body,
            "commit_id": commit_sha,
            "path": path,
            "line": resolved_end,
            "side": "RIGHT",
        }
        if resolved_end > int(line_start):
            payload["start_line"] = int(line_start)
            payload["start_side"] = "RIGHT"

        try:
            response = await self._request(
                "POST",
                f"/repos/{repo}/pulls/{pr_number}/comments",
                json=payload,
            )
            data = response.json()
            data.setdefault("path", path)
            data.setdefault("line", int(line_start))
            return data
        except GitHubAPIError as exc:
            if exc.status_code != 422:
                raise

        # GitHub rejects lines that are not part of the diff. Keep the feedback
        # visible by falling back to a regular PR conversation comment.
        fallback_body = f"**`{path}:{line_start}`**\n\n{body}"
        response = await self._request(
            "POST",
            f"/repos/{repo}/issues/{pr_number}/comments",
            json={"body": fallback_body},
        )
        data = response.json()
        data["fallback"] = True
        data["path"] = path
        data["line"] = int(line_start)
        return data


class GitHubReviewTransport:
    """Adapter matching ``GitHubPRPoster``'s transport callable contract."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    async def __call__(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.client.post_review_comment(
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]),
            commit_sha=str(payload["commit_sha"]),
            path=str(payload["path"]),
            line_start=int(payload["line"]),
            line_end=int(payload.get("line_end") or payload["line"]),
            body=str(payload["body"]),
        )
