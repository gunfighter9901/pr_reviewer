"""Live GitHub client tests using mocked HTTP transports (no network)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from backend.config import Settings
from backend.github.client import (
    GitHubAPIError,
    GitHubClient,
    GitHubConfigurationError,
    build_diff_from_files,
    extract_changed_files,
    is_real_github_token,
)

DIFF = """diff --git a/app/db.py b/app/db.py
index 1111111..2222222 100644
--- a/app/db.py
+++ b/app/db.py
@@ -1,3 +1,4 @@
 import sqlite3
+def lookup(conn, user_id):
+    return conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
 
 def ping():
     return True
"""


def _settings() -> Settings:
    return Settings(_env_file=None, GITHUB_TOKEN="github_pat_test_token_1234567890")


def _client(handler: Any) -> GitHubClient:
    return GitHubClient(
        settings=_settings(),
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )


def test_token_placeholder_detection() -> None:
    assert is_real_github_token("github_pat_" + "a" * 30)
    assert not is_real_github_token("ghp_fallback_personal_access_token")
    assert not is_real_github_token("short")


@pytest.mark.asyncio
async def test_fetch_pull_request_bundle_reads_metadata_diff_and_files() -> None:
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path} {request.headers.get('accept')}")
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {"filename": "app/db.py", "status": "modified", "patch": "@@ -1 +1,2 @@"},
                    {"filename": "app/http.py", "status": "modified", "patch": "@@ -1 +1 @@"},
                ],
            )
        if request.headers.get("accept") == "application/vnd.github.v3.diff":
            return httpx.Response(200, text=DIFF)
        return httpx.Response(
            200,
            json={
                "head": {"sha": "abc123"},
                "base": {"repo": {"private": False}},
            },
        )

    client = _client(handler)
    try:
        bundle = await client.fetch_pull_request_bundle("acme/demo", 7)
    finally:
        await client.aclose()

    assert bundle.head_sha == "abc123"
    assert bundle.diff == DIFF
    assert bundle.changed_files == ["app/db.py", "app/http.py"]
    assert bundle.is_public is True
    assert any("application/vnd.github.v3.diff" in item for item in seen)


@pytest.mark.asyncio
async def test_missing_token_fails_before_network_call() -> None:
    client = GitHubClient(
        settings=Settings(_env_file=None, GITHUB_TOKEN="ghp_fallback_personal_access_token")
    )
    try:
        with pytest.raises(GitHubConfigurationError):
            await client.fetch_pull_request_bundle("acme/demo", 7)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_review_comment_payload_and_range() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 99, "html_url": "https://github.test/c/99"})

    client = _client(handler)
    try:
        result = await client.post_review_comment(
            repo="acme/demo",
            pr_number=7,
            commit_sha="abc123",
            path="app/db.py",
            line_start=4,
            line_end=5,
            body="Grounded finding",
        )
    finally:
        await client.aclose()

    assert captured["path"] == "/repos/acme/demo/pulls/7/comments"
    assert captured["payload"]["line"] == 5
    assert captured["payload"]["start_line"] == 4
    assert captured["payload"]["side"] == "RIGHT"
    assert result["id"] == 99


@pytest.mark.asyncio
async def test_review_comment_falls_back_to_issue_comment_on_422() -> None:
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/pulls/7/comments"):
            return httpx.Response(422, text="line must be part of the diff")
        return httpx.Response(200, json={"id": 100})

    client = _client(handler)
    try:
        result = await client.post_review_comment(
            repo="acme/demo",
            pr_number=7,
            commit_sha="abc123",
            path="app/db.py",
            line_start=4,
            line_end=4,
            body="Grounded finding",
        )
    finally:
        await client.aclose()

    assert calls == [
        "/repos/acme/demo/pulls/7/comments",
        "/repos/acme/demo/issues/7/comments",
    ]
    assert result["fallback"] is True
    assert result["path"] == "app/db.py"


@pytest.mark.asyncio
async def test_github_api_error_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Bad credentials")

    client = _client(handler)
    try:
        with pytest.raises(GitHubAPIError, match="HTTP 401"):
            await client.get_authenticated_user()
    finally:
        await client.aclose()


def test_diff_helpers() -> None:
    files = [
        {
            "filename": "app/db.py",
            "status": "modified",
            "patch": "@@ -1 +1,2 @@\n import x\n+print(1)",
        }
    ]
    synthesized = build_diff_from_files(files)
    assert "+++ b/app/db.py" in synthesized
    assert extract_changed_files(DIFF) == ["app/db.py"]
