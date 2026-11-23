"""Live LLM provider adapters for specialist agents.

Gemini is the supported live provider. Selection is explicit via
``LLM_PROVIDER=gemini`` and is wired only into the production worker path, so the
test suite and golden evals stay deterministic and offline by default.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from backend.agents.specialists import (
    LLMCompletionCallable,
    ModelRouter,
    parse_unified_diff,
)
from backend.config import Settings, get_settings
from backend.models import Finding
from backend.orchestrator.engine import SpecialistRunner, WorkflowContext
from backend.prompts.registry import PromptTemplate


class GeminiConfigurationError(RuntimeError):
    """Raised when Gemini is selected without a usable API key."""


class GeminiAPIError(RuntimeError):
    """Raised when the Gemini HTTP endpoint fails."""


class GeminiResponseError(RuntimeError):
    """Raised when Gemini returns an unusable response envelope."""


_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_SEVERITY_ALIASES = {
    "moderate": "medium",
    "error": "high",
    "warning": "medium",
    "informational": "info",
}

_SYSTEM_CONTRACT = (
    "You are a specialist in a selective pull-request review system. "
    "Return ONLY JSON that matches the provided response schema. "
    "Report only findings you can prove from ADDED lines in the diff. "
    "file_path must be the exact repo-relative path shown in the diff. "
    "line_start and line_end must be new-file line numbers from the hunk header. "
    "evidence_snippet must be copied verbatim from the added lines. "
    "If nothing is clearly actionable, return {\"findings\": []}. "
    "Never invent files, line numbers, evidence, tests, or exploit paths."
)

FINDINGS_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "findings": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "severity": {
                        "type": "STRING",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "category": {"type": "STRING"},
                    "file_path": {"type": "STRING"},
                    "line_start": {"type": "INTEGER"},
                    "line_end": {"type": "INTEGER"},
                    "evidence_snippet": {"type": "STRING"},
                    "rationale": {"type": "STRING"},
                    "suggested_fix": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": [
                    "severity",
                    "category",
                    "file_path",
                    "line_start",
                    "line_end",
                    "evidence_snippet",
                    "rationale",
                    "confidence",
                ],
            },
        }
    },
    "required": ["findings"],
}


def _normalize_text(value: str) -> str:
    """Collapse all whitespace so evidence matching survives diff formatting."""
    return re.sub(r"\s+", " ", value).strip()


def _normalize_path(value: str) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _clean_evidence(value: Any) -> str:
    text = str(value or "").strip().replace("\r\n", "\n")
    lines = [line[1:] if line.startswith("+") else line for line in text.splitlines()]
    return "\n".join(lines).strip()


def _is_real_api_key(value: str) -> bool:
    key = (value or "").strip()
    return len(key) >= 20 and "REPLACE_ME" not in key.upper()


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiResponseError(
            f"Gemini returned invalid JSON at position {exc.pos}: {exc.msg}"
        ) from exc


class GeminiCompletionClient:
    """Async Gemini ``generateContent`` adapter with structured output and grounding."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise GeminiConfigurationError("GEMINI_API_KEY must not be empty")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport

    async def __call__(
        self,
        model: str,
        template: PromptTemplate,
        user_prompt: str,
        context: WorkflowContext,
    ) -> List[Dict[str, Any]]:
        payload = self._build_payload(template, user_prompt)
        body = await self._request(model, payload)
        text = self._extract_text(body)
        if text is None:
            return []

        data = _extract_json(text)
        if isinstance(data, list):
            raw_items: Any = data
        elif isinstance(data, dict):
            raw_items = data.get("findings") or []
        else:
            raise GeminiResponseError("Gemini JSON root must be an object or array")
        if not isinstance(raw_items, list):
            raise GeminiResponseError("Gemini 'findings' field must be an array")

        return self._ground_findings(raw_items, template, context)

    def _build_payload(
        self, template: PromptTemplate, user_prompt: str
    ) -> Dict[str, Any]:
        return {
            "systemInstruction": {
                "parts": [{"text": f"{_SYSTEM_CONTRACT}\n\n{template.system_prompt}"}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": FINDINGS_RESPONSE_SCHEMA,
            },
        }

    async def _request(self, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/models/{model}:generateContent"
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self.transport
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise GeminiAPIError(
                f"Gemini request failed before a response: {exc.__class__.__name__}"
            ) from exc

        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:300]
            raise GeminiAPIError(
                f"Gemini API returned HTTP {response.status_code}: {detail}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise GeminiResponseError("Gemini returned a non-JSON HTTP body") from exc
        if not isinstance(body, dict):
            raise GeminiResponseError("Gemini HTTP body must be a JSON object")
        return body

    @staticmethod
    def _extract_text(body: Dict[str, Any]) -> Optional[str]:
        feedback = body.get("promptFeedback") or {}
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            return None

        candidates = body.get("candidates") or []
        if not isinstance(candidates, list) or not candidates:
            return None
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        finish_reason = candidate.get("finishReason")
        if finish_reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"}:
            return None

        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            parts = []
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and not part.get("thought")
        )
        if not text.strip():
            if finish_reason == "MAX_TOKENS":
                raise GeminiResponseError("Gemini stopped at MAX_TOKENS before emitting JSON")
            return None
        return text

    def _ground_findings(
        self,
        raw_items: List[Any],
        template: PromptTemplate,
        context: WorkflowContext,
    ) -> List[Dict[str, Any]]:
        added_by_file: Dict[str, list[Any]] = {}
        for line in parse_unified_diff(context.diff or ""):
            added_by_file.setdefault(line.file_path, []).append(line)

        grounded: List[Dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue

            file_path = _normalize_path(str(raw.get("file_path") or ""))
            lines = added_by_file.get(file_path)
            if not lines:
                continue

            try:
                line_start = int(raw.get("line_start"))
                line_end = int(raw.get("line_end", line_start))
            except (TypeError, ValueError):
                continue
            if line_start < 1 or line_end < line_start or line_end - line_start > 20:
                continue

            added_numbers = {line.line_number for line in lines}
            if not set(range(line_start, line_end + 1)).issubset(added_numbers):
                continue

            evidence = _clean_evidence(raw.get("evidence_snippet"))
            if not evidence:
                continue
            cited_text = _normalize_text(
                "\n".join(
                    line.content
                    for line in sorted(lines, key=lambda item: item.line_number)
                    if line_start <= line.line_number <= line_end
                )
            )
            if _normalize_text(evidence) not in cited_text:
                continue

            severity = str(raw.get("severity") or "").strip().lower()
            severity = _SEVERITY_ALIASES.get(severity, severity)
            if severity not in _SEVERITIES:
                severity = "medium"

            category = str(raw.get("category") or "").strip()
            rationale = str(raw.get("rationale") or "").strip()
            if not category or not rationale:
                continue

            try:
                confidence = float(raw.get("confidence"))
            except (TypeError, ValueError):
                continue
            if confidence != confidence:  # NaN
                continue
            confidence = min(1.0, max(0.0, confidence))

            suggested_fix_raw = raw.get("suggested_fix")
            suggested_fix = (
                str(suggested_fix_raw).strip() if suggested_fix_raw is not None else None
            )
            if not suggested_fix:
                suggested_fix = None

            item = {
                "severity": severity,
                "category": category,
                "file_path": file_path,
                "line_start": line_start,
                "line_end": line_end,
                "evidence_snippet": evidence,
                "rationale": rationale,
                "suggested_fix": suggested_fix,
                "confidence": confidence,
            }
            try:
                Finding.model_validate(
                    {
                        **item,
                        "agent_type": template.agent_type,
                        "prompt_version": template.version_id,
                    }
                )
            except ValueError:
                continue
            grounded.append(item)

        return grounded


@dataclass(frozen=True)
class SpecialistRuntime:
    """Optional live provider components injected into specialist execution."""

    model_router: Optional[ModelRouter] = None
    llm_callable: Optional[LLMCompletionCallable] = None


def build_specialist_runtime(settings: Optional[Settings] = None) -> SpecialistRuntime:
    """Build the configured provider runtime; deterministic mode stays fully offline."""
    cfg = settings or get_settings()
    if cfg.LLM_PROVIDER == "deterministic":
        return SpecialistRuntime()
    if cfg.LLM_PROVIDER != "gemini":
        raise GeminiConfigurationError(
            f"Unsupported LLM_PROVIDER={cfg.LLM_PROVIDER!r}; use 'deterministic' or 'gemini'"
        )
    if not _is_real_api_key(cfg.GEMINI_API_KEY):
        raise GeminiConfigurationError(
            "LLM_PROVIDER=gemini requires a real GEMINI_API_KEY; "
            "replace the REPLACE_ME placeholder in .env"
        )

    router = ModelRouter.for_gemini(
        high_model=cfg.GEMINI_MODEL_HIGH,
        fast_model=cfg.GEMINI_MODEL_FAST,
    )
    client = GeminiCompletionClient(
        api_key=cfg.GEMINI_API_KEY,
        base_url=cfg.GEMINI_API_BASE_URL,
        timeout_seconds=cfg.GEMINI_TIMEOUT_SECONDS,
    )
    return SpecialistRuntime(model_router=router, llm_callable=client)


def build_specialist_runner(
    settings: Optional[Settings] = None,
) -> SpecialistRunner:
    """Return a specialist runner bound to the configured provider."""
    runtime = build_specialist_runtime(settings)

    async def _runner(
        specialist_name: str, context: WorkflowContext
    ) -> List[Finding]:
        from backend.agents.specialists import run_specialist_agent

        return await run_specialist_agent(
            specialist_name,
            context,
            model_router=runtime.model_router,
            llm_callable=runtime.llm_callable,
        )

    return _runner
