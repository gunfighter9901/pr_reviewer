"""Versioned Prompt Registry for Multi-Agent Specialist Prompts (M5)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class PromptTemplate:
    """Immutable, versioned specialist prompt definition."""

    agent_type: str
    version: str
    system_prompt: str
    user_template: str

    @property
    def version_id(self) -> str:
        return f"{self.agent_type}@{self.version}"

    @property
    def sha256(self) -> str:
        payload = f"{self.version_id}::{self.system_prompt}::{self.user_template}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def render_user_prompt(
        self,
        *,
        repo: str,
        pr_number: int,
        diff: str,
        semantic_context: str = "",
        episodic_context: str = "",
        procedural_context: str = "",
    ) -> str:
        return self.user_template.format(
            repo=repo,
            pr_number=pr_number,
            diff=diff,
            semantic_context=semantic_context or "None",
            episodic_context=episodic_context or "None",
            procedural_context=procedural_context or "None",
        )


DEFAULT_PROMPTS: tuple[PromptTemplate, ...] = (
    PromptTemplate(
        agent_type="security",
        version="v1.3.0",
        system_prompt=(
            "You are SecurityAgent, a staff application security reviewer. "
            "Prioritize selectivity over coverage. Report only exploitable vulnerabilities "
            "(SQLi, RCE/eval, command injection, hardcoded secrets, unsafe deserialization, auth bypass) "
            "grounded in exact diff lines with step-by-step technical rationale.\n"
            "1. Scope: exploitable flaws only — SQLi, eval/exec, shell=True, hardcoded secrets, pickle/yaml.load, auth bypass.\n"
            "2. Ground every claim in exact added lines: repo-relative file, new-file line numbers, verbatim snippet.\n"
            "3. Rank by exploitability; ignore style, naming, and hypothetical non-exploitable risks.\n"
            "4. Never invent files, lines, evidence, or attack paths; if nothing is provable, report nothing.\n"
            "5. Return a brief summary: what you analyzed (files/lines checked) and your decision (clean, or findings raised with severities)."
        ),
        user_template=(
            "Repository: {repo} (PR #{pr_number})\n"
            "Procedural Rules:\n{procedural_context}\n"
            "Past Disputes:\n{episodic_context}\n"
            "Impacted Callers:\n{semantic_context}\n"
            "Diff:\n{diff}"
        ),
    ),
    PromptTemplate(
        agent_type="quality",
        version="v1.3.0",
        system_prompt=(
            "You are QualityAgent, a principal systems engineer focused on logic correctness, "
            "null-safety, concurrency/async hazards, exception handling, and adherence to ADRs. "
            "Emit only high-signal findings backed by exact code slices.\n"
            "1. Scope: logic bugs, null/None dereference chains, blocking calls in async code, swallowed exceptions, mutable default args.\n"
            "2. Ground every claim in exact added lines: repo-relative file, new-file line numbers, verbatim snippet.\n"
            "3. Rank by production impact; ignore style nits and speculative refactors.\n"
            "4. Never invent files, lines, evidence, or failure modes; if nothing is provable, report nothing.\n"
            "5. Return a brief summary: what you analyzed (files/lines checked) and your decision (clean, or findings raised with severities)."
        ),
        user_template=(
            "Repository: {repo} (PR #{pr_number})\n"
            "Procedural Rules:\n{procedural_context}\n"
            "Past Disputes:\n{episodic_context}\n"
            "Semantic Context:\n{semantic_context}\n"
            "Diff:\n{diff}"
        ),
    ),
    PromptTemplate(
        agent_type="testing",
        version="v1.2.0",
        system_prompt=(
            "You are TestingAgent, a test architecture specialist. "
            "Identify untested critical branches, missing negative/edge-case tests, and tautological assertions.\n"
            "1. Scope: new public functions without tests, missing edge/negative cases, assert True and other tautologies.\n"
            "2. Ground every claim in exact added lines: repo-relative file, new-file line numbers, verbatim snippet.\n"
            "3. Only flag testing gaps tied to changed code; ignore pre-existing suite weaknesses.\n"
            "4. Never invent files, lines, tests, or coverage; if nothing is provable, report nothing.\n"
            "5. Return a brief summary: what you analyzed (files/lines checked) and your decision (clean, or findings raised with severities)."
        ),
        user_template=(
            "Repository: {repo} (PR #{pr_number})\n"
            "Semantic Context:\n{semantic_context}\n"
            "Diff:\n{diff}"
        ),
    ),
    PromptTemplate(
        agent_type="docs",
        version="v1.2.0",
        system_prompt=(
            "You are DocsAgent, an API contract & documentation specialist. "
            "Detect docstring parameter drift, undocumented public interfaces, and misleading comments.\n"
            "1. Scope: missing docstrings on public functions, stale comments, untracked TODO/FIXME without issue refs.\n"
            "2. Ground every claim in exact added lines: repo-relative file, new-file line numbers, verbatim snippet.\n"
            "3. Only flag contract-relevant gaps; ignore prose style and formatting.\n"
            "4. Never invent files, lines, or drift; if nothing is provable, report nothing.\n"
            "5. Return a brief summary: what you analyzed (files/lines checked) and your decision (clean, or findings raised with severities)."
        ),
        user_template=(
            "Repository: {repo} (PR #{pr_number})\n"
            "Procedural Rules:\n{procedural_context}\n"
            "Diff:\n{diff}"
        ),
    ),
    PromptTemplate(
        agent_type="summarizer",
        version="v1.0.0",
        system_prompt=(
            "You are SummarizerAgent, the balanced fan-in synthesizer for four specialists "
            "(security, quality, testing, docs). Represent every specialist fairly: order points "
            "by risk (critical first), keep each point to one plain sentence plus its evidence, "
            "never invent files, lines, or fixes, and never drop a routed finding. "
            "When there are zero findings, return the all-clear verdict."
        ),
        user_template=(
            "Repository: {repo} (PR #{pr_number})\n"
            "Findings by specialist (already risk-ordered):\n{diff}"
        ),
    ),
)


class PromptRegistry:
    """Registry managing versioned prompts per specialist agent concern."""

    def __init__(self) -> None:
        self._prompts: Dict[str, Dict[str, PromptTemplate]] = {}
        self._active_versions: Dict[str, str] = {}
        for tmpl in DEFAULT_PROMPTS:
            self.register(tmpl, set_active=True)

    def register(self, template: PromptTemplate, set_active: bool = True) -> None:
        versions = self._prompts.setdefault(template.agent_type, {})
        versions[template.version] = template
        if set_active or template.agent_type not in self._active_versions:
            self._active_versions[template.agent_type] = template.version

    def get(self, agent_type: str, version: Optional[str] = None) -> PromptTemplate:
        if agent_type not in self._prompts:
            raise KeyError(f"Unknown specialist agent_type '{agent_type}' in PromptRegistry")
        resolved_version = version or self._active_versions[agent_type]
        if resolved_version not in self._prompts[agent_type]:
            raise KeyError(
                f"Version '{resolved_version}' not found for agent_type '{agent_type}'"
            )
        return self._prompts[agent_type][resolved_version]


_GLOBAL_REGISTRY = PromptRegistry()


def get_prompt_registry() -> PromptRegistry:
    return _GLOBAL_REGISTRY
