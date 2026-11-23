"""Gemini provider tests: routing, structured output, grounding, and offline defaults."""

from __future__ import annotations

import json
from typing import Any, Dict

import fakeredis.aioredis
import httpx
import pytest

from backend.agents.llm import (
    FINDINGS_RESPONSE_SCHEMA,
    GeminiAPIError,
    GeminiCompletionClient,
    GeminiConfigurationError,
    SpecialistRuntime,
    build_specialist_runtime,
)
from backend.agents.specialists import ModelRouter
from backend.config import Settings
from backend.orchestrator.engine import WorkflowContext
from backend.prompts.registry import get_prompt_registry
from backend.worker import RedisARQWorkerQueue

DIFF = """diff --git a/app/db.py b/app/db.py
index 1111111..2222222 100644
--- a/app/db.py
+++ b/app/db.py
@@ -1,3 +1,4 @@
 import db
-cursor.execute("SELECT 1")
+cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
+return cursor.fetchall()
"""

CONTEXT = WorkflowContext(
    delivery_id="deliv-gemini-001",
    trace_id="tr-gemini-001",
    repo="acme/smoke",
    pr_number=7,
    commit_sha="abc123",
    is_public=True,
    diff=DIFF,
    changed_files=["app/db.py"],
)


def _client(handler: Any) -> GeminiCompletionClient:
    return GeminiCompletionClient(
        api_key="test-gemini-key-1234567890",
        base_url="https://gemini.example/v1beta",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


def _response(findings: list[Dict[str, Any]]) -> httpx.Response:
    body = {
        "candidates": [
            {
                "content": {"parts": [{"text": json.dumps({"findings": findings})}]},
                "finishReason": "STOP",
            }
        ]
    }
    return httpx.Response(200, json=body)


def test_gemini_router_uses_high_and_fast_tiers() -> None:
    router = ModelRouter.for_gemini(high_model="high-model", fast_model="fast-model")

    assert router.get_model("security") == "high-model"
    assert router.get_model("quality") == "high-model"
    assert router.get_model("testing") == "fast-model"
    assert router.get_model("docs") == "fast-model"
    assert router.get_model("unknown") == "fast-model"

    # The offline default remains backward compatible.
    assert ModelRouter().get_model("security") == "claude-3-5-sonnet"


def test_gemini_runtime_is_offline_by_default() -> None:
    runtime = build_specialist_runtime(Settings(_env_file=None))

    assert runtime == SpecialistRuntime()
    assert runtime.llm_callable is None


def test_gemini_runtime_requires_a_real_key() -> None:
    placeholder = Settings(
        _env_file=None,
        LLM_PROVIDER="gemini",
        GEMINI_API_KEY="REPLACE_ME_gemini_key",
    )
    with pytest.raises(GeminiConfigurationError):
        build_specialist_runtime(placeholder)

    enabled = Settings(
        _env_file=None,
        LLM_PROVIDER="gemini",
        GEMINI_API_KEY="test-gemini-key-1234567890",
    )
    runtime = build_specialist_runtime(enabled)
    assert isinstance(runtime.model_router, ModelRouter)
    assert runtime.llm_callable is not None


def test_worker_builds_gemini_runtime_when_enabled() -> None:
    settings = Settings(
        _env_file=None,
        LLM_PROVIDER="gemini",
        GEMINI_API_KEY="test-gemini-key-1234567890",
    )
    queue = RedisARQWorkerQueue(
        redis_client=fakeredis.aioredis.FakeRedis(), settings=settings
    )

    assert queue.engine is not None


@pytest.mark.asyncio
async def test_gemini_client_parses_grounds_and_drops_hallucinations() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return _response(
            [
                {
                    "severity": "CRITICAL",
                    "category": "sql_injection",
                    "file_path": "b/app/db.py",
                    "line_start": 2,
                    "line_end": 2,
                    "evidence_snippet": 'cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")',
                    "rationale": "User input is interpolated directly into SQL.",
                    "confidence": 1.4,
                    "junk": "discarded",
                },
                {
                    "severity": "high",
                    "category": "debug_statement",
                    "file_path": "./app/db.py",
                    "line_start": 3,
                    "line_end": 3,
                    "evidence_snippet": "return cursor.fetchall()",
                    "rationale": "Rows are returned without any consumer boundary.",
                    "suggested_fix": "",
                    "confidence": 0.8,
                },
                {
                    "severity": "critical",
                    "category": "sql_injection",
                    "file_path": "app/other.py",
                    "line_start": 2,
                    "line_end": 2,
                    "evidence_snippet": "cursor.execute(user_input)",
                    "rationale": "Hallucinated file path.",
                    "confidence": 0.99,
                },
                {
                    "severity": "critical",
                    "category": "sql_injection",
                    "file_path": "app/db.py",
                    "line_start": 99,
                    "line_end": 99,
                    "evidence_snippet": "cursor.execute(user_input)",
                    "rationale": "Hallucinated line number.",
                    "confidence": 0.99,
                },
                {
                    "severity": "critical",
                    "category": "sql_injection",
                    "file_path": "app/db.py",
                    "line_start": 2,
                    "line_end": 2,
                    "evidence_snippet": 'os.system("rm -rf /")',
                    "rationale": "Evidence is not in the diff.",
                    "confidence": 0.99,
                },
            ]
        )

    client = _client(handler)
    findings = await client(
        "gemini-3.8-flash",
        get_prompt_registry().get("security"),
        "Review this diff",
        CONTEXT,
    )

    assert len(findings) == 2
    assert findings[0]["severity"] == "critical"
    assert findings[0]["confidence"] == 1.0
    assert findings[0]["suggested_fix"] is None
    assert findings[1]["severity"] == "high"
    assert findings[1]["suggested_fix"] is None
    assert all("agent_type" not in item for item in findings)

    assert captured["url"].endswith("/models/gemini-3.8-flash:generateContent")
    assert captured["headers"]["x-goog-api-key"] == "test-gemini-key-1234567890"
    config = captured["payload"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"] == FINDINGS_RESPONSE_SCHEMA
    incoming_system = captured["payload"]["systemInstruction"]["parts"][0]["text"]
    assert "Return ONLY JSON" in incoming_system


@pytest.mark.asyncio
async def test_gemini_client_handles_fenced_json_and_safety_blocks() -> None:
    def fenced_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "```json\n{\"findings\": []}\n```"}]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    fenced = await _client(fenced_handler)(
        "gemini-3.8-flash",
        get_prompt_registry().get("quality"),
        "Review this diff",
        CONTEXT,
    )
    assert fenced == []

    def blocked_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        )

    blocked = await _client(blocked_handler)(
        "gemini-3.8-flash",
        get_prompt_registry().get("quality"),
        "Review this diff",
        CONTEXT,
    )
    assert blocked == []


@pytest.mark.asyncio
async def test_gemini_client_surfaces_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Quota exceeded for this project")

    with pytest.raises(GeminiAPIError, match="HTTP 429"):
        await _client(handler)(
            "gemini-3.8-flash",
            get_prompt_registry().get("security"),
            "Review this diff",
            CONTEXT,
        )
