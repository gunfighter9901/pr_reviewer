"""Four Specialist Agents (Security, Quality, Testing, Docs) and Tiered Model Router (M5).

INV-3: Zero LangGraph imports.
INV-7: Every emitted Finding is strictly grounded with file_path, line_start, line_end,
       evidence_snippet, rationale, confidence, and versioned prompt_version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.models import AgentType, Finding
from backend.observability.events import EventSpineLogger
from backend.orchestrator.engine import WorkflowContext
from backend.prompts.registry import PromptRegistry, PromptTemplate, get_prompt_registry


@dataclass(frozen=True)
class DiffAddedLine:
    file_path: str
    line_number: int
    content: str


def parse_unified_diff(diff_text: str, default_file: str = "unknown.py") -> List[DiffAddedLine]:
    """Parse a GitHub unified diff into exact added lines with repo-relative file_path and line_number."""
    added_lines: List[DiffAddedLine] = []
    current_file = default_file
    current_line = 1

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            parts = raw_line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_file = parts[3][2:]
            continue
        if raw_line.startswith("+++ "):
            target = raw_line[4:].strip()
            if target.startswith("b/"):
                current_file = target[2:]
            elif target != "/dev/null" and target:
                current_file = target
            continue
        if raw_line.startswith("--- "):
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
            if match:
                current_line = int(match.group(1))
            continue
        if raw_line.startswith("+"):
            added_lines.append(
                DiffAddedLine(
                    file_path=current_file,
                    line_number=current_line,
                    content=raw_line[1:],
                )
            )
            current_line += 1
        elif raw_line.startswith("-"):
            # Removed line does not advance new-file line counter
            continue
        else:
            current_line += 1

    return added_lines


class ModelRouter:
    """Tiered LLM model router per specialist concern (Section 2 of AGENTS.md)."""

    DEFAULT_TIERS: Dict[str, str] = {
        "security": "claude-3-5-sonnet",
        "quality": "gpt-4o",
        "testing": "claude-3-5-haiku",
        "docs": "gpt-4o-mini",
    }

    def __init__(self, overrides: Optional[Dict[str, str]] = None) -> None:
        self._routes = dict(self.DEFAULT_TIERS)
        if overrides:
            self._routes.update(overrides)

    def get_model(self, agent_type: str) -> str:
        return self._routes.get(agent_type, "gpt-4o-mini")


LLMCompletionCallable = Callable[
    [str, PromptTemplate, str, WorkflowContext], Awaitable[List[Dict[str, Any]]]
]


class SpecialistAgent:
    """Base specialist agent with versioned prompt lookup, model routing, and grounded output validation."""

    agent_type: AgentType = "quality"

    def __init__(
        self,
        prompt_registry: Optional[PromptRegistry] = None,
        model_router: Optional[ModelRouter] = None,
        llm_callable: Optional[LLMCompletionCallable] = None,
        event_logger: Optional[EventSpineLogger] = None,
    ) -> None:
        self.prompt_registry = prompt_registry or get_prompt_registry()
        self.model_router = model_router or ModelRouter()
        self.llm_callable = llm_callable
        self.event_logger = event_logger

    @property
    def prompt_template(self) -> PromptTemplate:
        return self.prompt_registry.get(self.agent_type)

    @property
    def model_name(self) -> str:
        return self.model_router.get_model(self.agent_type)

    async def analyze(self, context: WorkflowContext) -> List[Finding]:
        tmpl = self.prompt_template
        model = self.model_name
        default_file = context.changed_files[0] if context.changed_files else "src/app.py"
        user_prompt = tmpl.render_user_prompt(
            repo=context.repo,
            pr_number=context.pr_number,
            diff=context.diff,
            semantic_context=str(context.retrieved_context.get("semantic", "")),
            episodic_context=str(context.retrieved_context.get("episodic", "")),
            procedural_context=str(context.retrieved_context.get("procedural", "")),
        )

        if self.llm_callable is not None:
            raw_items = await self.llm_callable(model, tmpl, user_prompt, context)
            findings = [
                Finding.model_validate(
                    {
                        **item,
                        "agent_type": self.agent_type,
                        "prompt_version": item.get("prompt_version") or tmpl.version_id,
                    }
                )
                for item in raw_items
            ]
        else:
            added_lines = parse_unified_diff(context.diff, default_file=default_file)
            findings = self._analyze_lines(added_lines, tmpl.version_id, context)

        if self.event_logger is not None:
            tokens_in = max(64, len(user_prompt) // 4)
            tokens_out = max(32, sum(len(f.rationale) for f in findings) // 4 + 40)
            await self.event_logger.log_event(
                trace_id=context.trace_id,
                delivery_id=context.delivery_id,
                agent_name=self.agent_type,
                event_type="specialist_llm_call",
                model=model,
                prompt_version=tmpl.version_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                status="ok",
                metadata={"findings_emitted": len(findings)},
            )

        return findings

    def _analyze_lines(
        self,
        added_lines: List[DiffAddedLine],
        prompt_version: str,
        context: WorkflowContext,
    ) -> List[Finding]:
        raise NotImplementedError


class SecurityAgent(SpecialistAgent):
    """Detects exploits, injection vectors, auth bypasses, secret leaks, and unsafe deserialization."""

    agent_type: AgentType = "security"

    def _analyze_lines(
        self,
        added_lines: List[DiffAddedLine],
        prompt_version: str,
        context: WorkflowContext,
    ) -> List[Finding]:
        findings: List[Finding] = []
        for item in added_lines:
            code = item.content.strip()
            if not code or code.startswith("#"):
                continue

            # 1. SQL Injection (f-string / format / concatenation in SQL query or execute)
            if re.search(
                r"(SELECT|INSERT|UPDATE|DELETE)\b.*(\{.*\}|%s|\+\s*[a-zA-Z_])",
                code,
                flags=re.IGNORECASE,
            ) and ('f"' in code or "f'" in code or ".format(" in code or "+" in code):
                findings.append(
                    Finding(
                        agent_type="security",
                        severity="critical",
                        category="sql_injection",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "User-controlled input is interpolated directly into a raw SQL query string, "
                            "allowing arbitrary SQL injection and unauthorized data exfiltration."
                        ),
                        suggested_fix="Use parameterized SQL queries (e.g., cursor.execute('SELECT ... WHERE id = $1', (user_id,))).",
                        confidence=0.95,
                        prompt_version=prompt_version,
                    )
                )

            # 2. Code / Command Injection (eval, exec, shell=True)
            if re.search(r"\b(eval|exec)\s*\(", code) or "shell=True" in code:
                findings.append(
                    Finding(
                        agent_type="security",
                        severity="critical",
                        category="code_injection",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Dynamic execution via eval/exec or subprocess with shell=True enables "
                            "Remote Code Execution (RCE) when untrusted input reaches this call site."
                        ),
                        suggested_fix="Replace eval/exec with safe parsing (e.g., json.loads or ast.literal_eval) and pass argument lists with shell=False.",
                        confidence=0.94,
                        prompt_version=prompt_version,
                    )
                )

            # 3. Hardcoded Secrets / API Keys
            if re.search(
                r"(API_KEY|SECRET|PASSWORD|PRIVATE_KEY|TOKEN)\s*=\s*['\"](sk-[a-zA-Z0-9_\-]{8,}|ghp_[a-zA-Z0-9]{8,}|[^'\"]{10,})['\"]",
                code,
                flags=re.IGNORECASE,
            ):
                findings.append(
                    Finding(
                        agent_type="security",
                        severity="critical",
                        category="hardcoded_secret",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Hardcoded credential or API secret committed directly in source code "
                            "exposes credentials in git history and build artifacts."
                        ),
                        suggested_fix="Load the secret from environment variables via validated Settings (os.environ / Pydantic BaseSettings).",
                        confidence=0.92,
                        prompt_version=prompt_version,
                    )
                )

            # 4. Unsafe Deserialization
            if "pickle.loads(" in code or (
                "yaml.load(" in code and "SafeLoader" not in code
            ):
                findings.append(
                    Finding(
                        agent_type="security",
                        severity="critical",
                        category="unsafe_deserialization",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Unsafe deserialization of untrusted payloads allows arbitrary object "
                            "instantiation and remote command execution."
                        ),
                        suggested_fix="Use yaml.safe_load() or strict Pydantic JSON validation instead of pickle/yaml.load.",
                        confidence=0.91,
                        prompt_version=prompt_version,
                    )
                )

        return findings


class QualityAgent(SpecialistAgent):
    """Detects logic bugs, null-safety hazards, race conditions, and anti-patterns."""

    agent_type: AgentType = "quality"

    def _analyze_lines(
        self,
        added_lines: List[DiffAddedLine],
        prompt_version: str,
        context: WorkflowContext,
    ) -> List[Finding]:
        findings: List[Finding] = []
        for item in added_lines:
            code = item.content.strip()
            if not code or code.startswith("#"):
                continue

            # Overlap check on dangerous eval/SQLi from a code quality / reliability lens
            if re.search(r"\b(eval|exec)\s*\(", code):
                findings.append(
                    Finding(
                        agent_type="quality",
                        severity="high",
                        category="unsafe_runtime_eval",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Using dynamic eval/exec bypasses static type checking, causes unpredictable "
                            "runtime exceptions on malformed inputs, and violates repository safety conventions."
                        ),
                        suggested_fix="Use structured parsing with explicit schema validation.",
                        confidence=0.86,
                        prompt_version=prompt_version,
                    )
                )

            # 1. Bare except swallowing errors
            if re.match(r"^except\s*:\s*$", code) or re.search(
                r"except\s+Exception\s*:\s*pass\b", code
            ):
                findings.append(
                    Finding(
                        agent_type="quality",
                        severity="high",
                        category="swallowed_exception",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Bare or silent exception handler swallows SystemExit, KeyboardInterrupt, "
                            "and unexpected runtime failures, hiding root causes in production."
                        ),
                        suggested_fix="Catch specific exception types, log structured context, and re-raise or return an explicit error result.",
                        confidence=0.88,
                        prompt_version=prompt_version,
                    )
                )

            # 2. Mutable default arguments
            if re.search(r"^def\s+[a-zA-Z0-9_]+\s*\([^)]*=\s*(\[\]|\{\})", code):
                findings.append(
                    Finding(
                        agent_type="quality",
                        severity="medium",
                        category="mutable_default_argument",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Mutable default argument ([] or {}) is allocated once at function definition time "
                            "and shared across all subsequent invocations, causing cross-request state leakage."
                        ),
                        suggested_fix="Default the parameter to None and initialize a fresh list/dict inside the function body.",
                        confidence=0.90,
                        prompt_version=prompt_version,
                    )
                )

            # 3. Blocking sleep inside async code
            if "time.sleep(" in code:
                findings.append(
                    Finding(
                        agent_type="quality",
                        severity="high",
                        category="blocking_event_loop",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Synchronous time.sleep() blocks the entire Python asyncio event loop thread, "
                            "stalling concurrent webhook and worker tasks."
                        ),
                        suggested_fix="Replace time.sleep(...) with await asyncio.sleep(...).",
                        confidence=0.85,
                        prompt_version=prompt_version,
                    )
                )

            # 4. Unchecked Null / Optional chain hazard
            if ".get(" in code and re.search(r"\.get\([^)]+\)\.[a-zA-Z_]+", code):
                findings.append(
                    Finding(
                        agent_type="quality",
                        severity="medium",
                        category="null_dereference",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Chaining attribute/method access directly onto dict.get() without a default value "
                            "will raise AttributeError when the key is absent and returns None."
                        ),
                        suggested_fix="Provide a safe default to .get(key, default) or guard for None before dereferencing.",
                        confidence=0.68,
                        prompt_version=prompt_version,
                    )
                )

        return findings


class TestingAgent(SpecialistAgent):
    """Identifies missing edge-case coverage, untested branches, and brittle assertions."""

    agent_type: AgentType = "testing"

    def _analyze_lines(
        self,
        added_lines: List[DiffAddedLine],
        prompt_version: str,
        context: WorkflowContext,
    ) -> List[Finding]:
        findings: List[Finding] = []
        has_test_file = any(
            "test" in l.file_path.lower() for l in added_lines
        ) or any("test" in f.lower() for f in context.changed_files)

        for item in added_lines:
            code = item.content.strip()
            if not code or code.startswith("#"):
                continue

            # 1. Tautological or brittle test assertion
            if re.match(r"^assert\s+True\b", code):
                findings.append(
                    Finding(
                        agent_type="testing",
                        severity="medium",
                        category="tautological_assertion",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Tautological `assert True` always passes and provides zero regression protection "
                            "for the code under test."
                        ),
                        suggested_fix="Assert concrete output values, state transitions, or raised exceptions.",
                        confidence=0.89,
                        prompt_version=prompt_version,
                    )
                )

            # 2. New function added in non-test file without any test changes in the PR
            if (
                not has_test_file
                and "test" not in item.file_path.lower()
                and re.match(r"^(?:async\s+)?def\s+([a-zA-Z0-9_]+)\s*\(", code)
            ):
                fn_match = re.match(r"^(?:async\s+)?def\s+([a-zA-Z0-9_]+)\s*\(", code)
                fn_name = fn_match.group(1) if fn_match else "function"
                if not fn_name.startswith("_"):
                    findings.append(
                        Finding(
                            agent_type="testing",
                            severity="medium",
                            category="missing_edge_case_tests",
                            file_path=item.file_path,
                            line_start=item.line_number,
                            line_end=item.line_number,
                            evidence_snippet=code,
                            rationale=(
                                f"Public function `{fn_name}` was introduced or modified without accompanying "
                                "unit or boundary edge-case tests in this PR diff."
                            ),
                            suggested_fix=f"Add unit tests covering happy path and error/boundary inputs for `{fn_name}`.",
                            confidence=0.80,
                            prompt_version=prompt_version,
                        )
                    )

        return findings


class DocsAgent(SpecialistAgent):
    """Checks docstring accuracy, API contract drift, and inline comment hygiene."""

    agent_type: AgentType = "docs"

    def _analyze_lines(
        self,
        added_lines: List[DiffAddedLine],
        prompt_version: str,
        context: WorkflowContext,
    ) -> List[Finding]:
        findings: List[Finding] = []
        for idx, item in enumerate(added_lines):
            code = item.content.strip()
            if not code:
                continue

            # Check public function definitions for missing docstring on next added line
            fn_match = re.match(r"^(?:async\s+)?def\s+([a-zA-Z0-9_]+)\s*\(", code)
            if fn_match and "test" not in item.file_path.lower():
                fn_name = fn_match.group(1)
                if not fn_name.startswith("_"):
                    next_line = (
                        added_lines[idx + 1].content.strip()
                        if idx + 1 < len(added_lines)
                        and added_lines[idx + 1].file_path == item.file_path
                        else ""
                    )
                    if not (next_line.startswith('"""') or next_line.startswith("'''")):
                        findings.append(
                            Finding(
                                agent_type="docs",
                                severity="low",
                                category="missing_docstring",
                                file_path=item.file_path,
                                line_start=item.line_number,
                                line_end=item.line_number,
                                evidence_snippet=code,
                                rationale=(
                                    f"Public function `{fn_name}` lacks a docstring documenting its contract, "
                                    "parameters, return type, and failure modes."
                                ),
                                suggested_fix=f'Add a concise PEP-257 docstring immediately below `{code}`.',
                                confidence=0.78,
                                prompt_version=prompt_version,
                            )
                        )

            # Check FIXME / TODO markers without issue reference
            if re.search(r"#\s*(TODO|FIXME)\b(?![^#\n]*#\d+)", code, flags=re.IGNORECASE):
                findings.append(
                    Finding(
                        agent_type="docs",
                        severity="info",
                        category="untracked_todo_comment",
                        file_path=item.file_path,
                        line_start=item.line_number,
                        line_end=item.line_number,
                        evidence_snippet=code,
                        rationale=(
                            "Inline TODO/FIXME comment lacks an owner or tracking issue reference, "
                            "creating untracked technical debt."
                        ),
                        suggested_fix="Link the TODO/FIXME comment to a tracked issue ID or resolve before merge.",
                        confidence=0.65,
                        prompt_version=prompt_version,
                    )
                )

        return findings


SPECIALIST_CLASSES: Dict[str, type[SpecialistAgent]] = {
    "security": SecurityAgent,
    "quality": QualityAgent,
    "testing": TestingAgent,
    "docs": DocsAgent,
}


async def run_specialist_agent(
    agent_type: str,
    context: WorkflowContext,
    prompt_registry: Optional[PromptRegistry] = None,
    model_router: Optional[ModelRouter] = None,
    llm_callable: Optional[LLMCompletionCallable] = None,
    event_logger: Optional[EventSpineLogger] = None,
) -> List[Finding]:
    """Instantiate and run the requested specialist agent on WorkflowContext."""
    cls = SPECIALIST_CLASSES.get(agent_type)
    if cls is None:
        raise ValueError(f"Unsupported specialist agent_type: {agent_type}")
    agent = cls(
        prompt_registry=prompt_registry,
        model_router=model_router,
        llm_callable=llm_callable,
        event_logger=event_logger,
    )
    return await agent.analyze(context)
