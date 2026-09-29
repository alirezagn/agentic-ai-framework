"""The eight specialist agents from framework/03..10.

Each agent is a thin :class:`~orchestrator.agents.llm_agent.LLMAgent`: it
declares its id and operating rules, loads its framework specification
(``framework/XX_*.md``), calls the configured model and parses the structured
JSON answer back into an :class:`AgentOutput`.

Offline tests inject ``llm_client=FakeLLMClient(...)``; production discovers
ANTHROPIC_API_KEY / OPENROUTER_API_KEY / OLLAMA_BASE_URL from the environment.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base_agent import AgentOutput, AgentOutputError, register_agent
from .llm_agent import LLMAgent

REVIEW_PASS = "PASS"
REVIEW_PASS_WITH_ACTIONS = "PASS WITH ACTIONS"
REVIEW_FAIL = "FAIL"
REVIEW_OUTCOMES = (REVIEW_PASS, REVIEW_PASS_WITH_ACTIONS, REVIEW_FAIL)


# ---------------------------------------------------------------------------
# 03 — Research
# ---------------------------------------------------------------------------


@register_agent("research_agent")
class ResearchAgent(LLMAgent):
    AGENT_ID = "research_agent"

    SYSTEM_RULES = (
        "Research rules:\n"
        "- Prefer primary/official sources; record source, date and confidence.\n"
        "- Separate verified fact from inference; mark unknowns UNKNOWN/TBD.\n"
        "- Do not repeat completed searches unless conditions changed.\n"
        "- Record rejected alternatives with the reason for rejection."
    )


# ---------------------------------------------------------------------------
# 04 — Architecture
# ---------------------------------------------------------------------------


@register_agent("architecture_agent")
class ArchitectureAgent(LLMAgent):
    AGENT_ID = "architecture_agent"

    SYSTEM_RULES = (
        "Architecture rules:\n"
        "- Define boundaries, subsystems, interfaces, data flow and dependencies.\n"
        "- Evaluate major alternatives before changing approved design.\n"
        "- Never silently change approved architecture; propose a decision instead.\n"
        "- Record assumptions, constraints and risks with every design choice."
    )


# ---------------------------------------------------------------------------
# 05 — Planning
# ---------------------------------------------------------------------------


@register_agent("planning_agent")
class PlanningAgent(LLMAgent):
    AGENT_ID = "planning_agent"

    SYSTEM_RULES = (
        "Planning rules:\n"
        "- Every task needs an owner, priority, expected outputs and acceptance criteria.\n"
        "- Keep the dependency graph acyclic; dependent work must not start prematurely.\n"
        "- Identify tasks that can run in parallel.\n"
        "- Replan when requirements or architecture change."
    )


# ---------------------------------------------------------------------------
# 06 — Hardware
# ---------------------------------------------------------------------------


@register_agent("hardware_agent")
class HardwareAgent(LLMAgent):
    AGENT_ID = "hardware_agent"

    SYSTEM_RULES = (
        "Hardware rules:\n"
        "- Never guess voltage, polarity or pin assignment — verify or mark UNKNOWN/TBD.\n"
        "- Maintain BOM, pin map, wiring, power budget and mechanical notes.\n"
        "- Check component compatibility and connector/signal levels explicitly.\n"
        "- Define hardware-specific validation for every design choice."
    )


# ---------------------------------------------------------------------------
# 07 — Software / Firmware
# ---------------------------------------------------------------------------


@register_agent("firmware_agent")
@register_agent("software_agent")
class SoftwareAgent(LLMAgent):
    AGENT_ID = "software_agent"

    SYSTEM_RULES = (
        "Software/firmware rules:\n"
        "- Follow approved requirements and architecture; never change them silently.\n"
        "- Produce maintainable, small, testable modules with logging and error handling.\n"
        "- Record dependencies, versions and build/deploy instructions.\n"
        "- Do not mark implementation DONE before testing and review; record limitations."
    )


# ---------------------------------------------------------------------------
# 08 — Test
# ---------------------------------------------------------------------------


@register_agent("test_agent")
class TestAgent(LLMAgent):
    AGENT_ID = "test_agent"
    # Prevent pytest from trying to collect this class as a test.
    __test__ = False

    SYSTEM_RULES = (
        "Test rules:\n"
        "- Map every test to requirement IDs; cover unit, integration and acceptance levels.\n"
        "- Record expected result, actual result and evidence for each test.\n"
        "- Use only PASS, FAIL, BLOCKED or NOT RUN as test statuses.\n"
        "- Convert failures into specific correction tasks, not rewrites."
    )


# ---------------------------------------------------------------------------
# 09 — Review
# ---------------------------------------------------------------------------


@register_agent("review_agent")
class ReviewAgent(LLMAgent):
    """Independent reviewer — decides PASS / PASS WITH ACTIONS / FAIL."""

    AGENT_ID = "review_agent"

    SYSTEM_RULES = (
        "Review rules:\n"
        "- The creator of an artifact is never its sole reviewer.\n"
        "- Check requirement coverage, architecture conformance, test evidence,\n"
        "  open risks/blockers and documentation consistency.\n"
        "- Detect unsupported completion claims; report measurable findings only.\n"
        "- A failed review creates specific correction tasks, never a total rewrite."
    )

    def output_from_parsed(
        self,
        parsed: Dict[str, Any],
        task_id: str = "",
        result: Optional[Any] = None,
    ) -> AgentOutput:
        output = super().output_from_parsed(parsed, task_id=task_id, result=result)
        data = output.data
        outcome = _normalize_review_outcome(
            data.get("review_status") or data.get("outcome") or output.status
        )
        if outcome is None:
            raise AgentOutputError(
                "review_agent must return data.review_status of "
                f"{' / '.join(REVIEW_OUTCOMES)}"
            )
        data["review_status"] = outcome
        findings = data.get("findings")
        data["findings"] = findings if isinstance(findings, list) else []
        corrections = data.get("corrections")
        data["corrections"] = corrections if isinstance(corrections, list) else []
        return output


def _normalize_review_outcome(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(str(value).strip().upper().split("_")).replace("-", " ")
    cleaned = " ".join(cleaned.split())
    for outcome in REVIEW_OUTCOMES:
        if cleaned == outcome:
            return outcome
    if cleaned in ("PASS WITH ACTION", "PASS WITH FOLLOW UP", "CONDITIONAL PASS"):
        return REVIEW_PASS_WITH_ACTIONS
    if cleaned in ("PASS", "OK", "APPROVED"):
        return REVIEW_PASS
    if cleaned in ("FAIL", "FAILED", "REJECT", "REJECTED"):
        return REVIEW_FAIL
    return None


# ---------------------------------------------------------------------------
# 10 — Documentation
# ---------------------------------------------------------------------------


@register_agent("documentation_agent")
class DocumentationAgent(LLMAgent):
    AGENT_ID = "documentation_agent"

    SYSTEM_RULES = (
        "Documentation rules:\n"
        "- Documentation follows the real implementation, never obsolete assumptions.\n"
        "- Keep README, requirements, architecture, tests, risks, decisions and\n"
        "  changelog synchronized with actual task/test state.\n"
        "- Remove stale statements after approved changes.\n"
        "- Preserve concise summaries so a future session can resume."
    )


__all__ = [
    "ResearchAgent",
    "ArchitectureAgent",
    "PlanningAgent",
    "HardwareAgent",
    "SoftwareAgent",
    "TestAgent",
    "ReviewAgent",
    "DocumentationAgent",
    "REVIEW_PASS",
    "REVIEW_PASS_WITH_ACTIONS",
    "REVIEW_FAIL",
    "REVIEW_OUTCOMES",
]
