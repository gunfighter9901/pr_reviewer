"""Specialist Agents and Fan-In Aggregator package (M5)."""

from backend.agents.aggregator import FindingAggregator, aggregate_and_deduplicate_findings
from backend.agents.summarizer import SummarizerAgent
from backend.agents.specialists import (
    DocsAgent,
    ModelRouter,
    QualityAgent,
    SecurityAgent,
    SpecialistAgent,
    TestingAgent,
    run_specialist_agent,
)

__all__ = [
    "DocsAgent",
    "FindingAggregator",
    "ModelRouter",
    "QualityAgent",
    "SecurityAgent",
    "SpecialistAgent",
    "SummarizerAgent",
    "TestingAgent",
    "aggregate_and_deduplicate_findings",
    "run_specialist_agent",
]
