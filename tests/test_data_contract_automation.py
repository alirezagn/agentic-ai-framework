"""Cross-component data contracts — the prompt contract must enforce them.

The failure this guards against is a *shape* mismatch across a task boundary,
and it is invisible until two separately-written components meet. Observed in
``projects/sys_mon``: the metrics module returns nested mappings
(``{"ram": {"percent_used": 50}}``) while a view layer wants flat scalars
(``{"ram": 50.0}``). Each agent is individually correct, both test green in
isolation, and the integration point explodes with a ``TclError`` or
``TypeError`` that names neither producer nor consumer.

Nothing in the payload prevents this, because a task carries *intent*, not a
type. The gap was never "an agent returned a dict" — it was that no artifact
obliged anyone to write down what the dict looks like. So these tests assert the
prompt requires the shape to be declared, converted at the boundary, wired into
a runnable entrypoint, and tested across the seam.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import config  # noqa: E402
from orchestrator.prompt_builder import (  # noqa: E402
    DATA_CONTRACT_INSTRUCTIONS,
    build_prompt,
    render_system_prompt,
)

PROMPT_DIR = REPO_ROOT / "framework" / "AGENT_PROMPTS"
SYS_MON = REPO_ROOT / "projects" / "sys_mon"

#: Agents that define data shapes, produce runnable components, or test them.
DATA_CONTRACT_TEMPLATES = (
    "02_REQUIREMENTS.md",
    "04_ARCHITECTURE.md",
    "07_SOFTWARE_FIRMWARE.md",
    "08_TEST.md",
)


def _rules_of(agent_class: type) -> str:
    agent = agent_class.__new__(agent_class)
    agent.extra_rules = []
    return agent.system_rules()


# ===========================================================================
# The contract itself
# ===========================================================================


class TestContractRequiresSchemaDefinition:
    def test_requires_explicit_types_in_architecture_doc(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "docs/ARCHITECTURE.md" in text
        assert "TypedDict" in text, "a prose schema is not checkable"
        assert "Pydantic" in text

    def test_names_the_agents_that_author_specs(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "requirements_agent" in text
        assert "architecture_agent" in text

    def test_demands_field_level_detail(self) -> None:
        """A contract must pin keys and types, not just name a container."""
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "key names" in text
        assert "type" in text

    def test_forbids_intent_as_a_substitute_for_a_type(self) -> None:
        assert "not a type" in DATA_CONTRACT_INSTRUCTIONS

    def test_cites_the_nested_versus_flat_divergence(self) -> None:
        """The concrete failure, so the model recognises the pattern."""
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "nested" in text and "flat" in text
        assert "percent_used" in text
        assert "TclError" in text and "TypeError" in text


class TestContractTellsTheModelHowToRunTests:
    """The invocation half of the test contract (rerun-5 TASK-007).

    ``pytest exited 2`` before collecting anything: the bare console script
    does not put the project root on ``sys.path``, so ``import src...`` died
    with ``ModuleNotFoundError`` and no test ever executed — while the
    repair round, told only the exit code, could not see why."""

    def test_runtime_contract_names_python_m_pytest(self) -> None:
        assert "python3 -m pytest" in DATA_CONTRACT_INSTRUCTIONS
        assert "conftest.py" in DATA_CONTRACT_INSTRUCTIONS

    def test_templates_name_python_m_pytest(self) -> None:
        for name in ("07_SOFTWARE_FIRMWARE.md", "08_TEST.md"):
            text = (PROMPT_DIR / name).read_text(encoding="utf-8")
            assert "python3 -m pytest" in text, f"{name} misses the invocation"


class TestContractRequiresBoundaryConversion:
    def test_requires_casting_at_the_boundary(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "software_agent" in text
        assert "BOUNDARY" in text.upper()

    def test_names_the_view_layer_and_progress_bar_cases(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "view layer" in text
        assert "progress bar" in text

    def test_requires_one_named_conversion_point(self) -> None:
        """Scattered per-call-site coercion is how the shapes drift back apart."""
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "one named function" in text

    def test_warns_against_swallowing_a_shape_error(self) -> None:
        """try/except around every call site hides the mismatch, not the bug."""
        assert "coerce across every call site" in DATA_CONTRACT_INSTRUCTIONS


class TestContractRequiresEntrypointAndIntegrationTest:
    def test_requires_main_py_at_project_root(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "main.py" in text
        assert "PROJECT ROOT" in text

    def test_requires_the_entrypoint_to_run_end_to_end(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "end to end" in text
        assert "conversion" in text.lower()

    def test_requires_testing_the_crossing_not_the_parts(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "crossing" in text
        assert "Unit tests on each component prove nothing" in text

    def test_requires_the_error_path_to_be_tested(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "error path" in text


class TestContractKeepsErrorPathsInSchema:
    """The most common way a contract breaks without anyone noticing."""

    def test_error_result_must_match_the_declared_shape(self) -> None:
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "error path" in text
        assert "Optional[str]" in text
        assert "Union" in text

    def test_bare_error_dict_is_named_as_the_antipattern(self) -> None:
        assert 'bare {"error": str}' in DATA_CONTRACT_INSTRUCTIONS

    def test_explains_why_the_failure_path_breaks_first(self) -> None:
        assert "no consumer is written against" in DATA_CONTRACT_INSTRUCTIONS
        assert "breaks first" in DATA_CONTRACT_INSTRUCTIONS


class TestContractIsActionableWhenShapeIsUnknown:
    def test_tells_the_agent_to_declare_rather_than_leave_implicit(self) -> None:
        """An invented-but-declared shape is recoverable; an undeclared one is not."""
        text = DATA_CONTRACT_INSTRUCTIONS
        assert "cannot infer" in text or "not pin a shape" in text
        assert "declare the shape yourself" in text
        assert "recoverable" in text


# ===========================================================================
# Reach: every path must carry it
# ===========================================================================


class TestContractReachesEveryAgent:
    def test_all_registered_agents_carry_it(self) -> None:
        from orchestrator.agents import AGENT_REGISTRY

        assert len(AGENT_REGISTRY) == 10
        for name, agent_class in AGENT_REGISTRY.items():
            rules = _rules_of(agent_class)
            assert "Data contract" in rules, f"{name} misses the data contract"
            assert "ARCHITECTURE.md" in rules, f"{name} is not told where to declare it"

    def test_survives_a_subclass_that_replaces_system_rules(self) -> None:
        """CRIT-08's lesson: SYSTEM_RULES is shadowed by all ten agents."""
        from orchestrator.agents.base_agent import BaseAgent

        class Shadowing(BaseAgent):
            AGENT_ID = "shadowing_agent"
            SYSTEM_RULES = "1. Only this one line."

        assert "Data contract" in _rules_of(Shadowing)

    def test_rendered_system_prompt_carries_it(self) -> None:
        assert DATA_CONTRACT_INSTRUCTIONS in render_system_prompt("rules", "spec")

    def test_user_message_carries_it(self) -> None:
        prompt = build_prompt({"task": {"id": "TASK-001", "owner": "software_agent"}})
        assert "Data contract obligation" in prompt
        assert "TypedDict" in prompt

    def test_stated_next_to_the_task_and_required_output(self) -> None:
        """Placed where "what must I deliver" is answered."""
        prompt = build_prompt({"task": {"id": "TASK-001"}})
        assert prompt.index("# Data contract obligation") > prompt.index("# Task")
        assert prompt.index("# Dependency obligation") < prompt.index(
            "# Data contract obligation"
        )

    def test_does_not_weaken_the_existing_contracts(self) -> None:
        """Adding a block must not cost the others their reach."""
        from orchestrator.prompt_builder import (
            DEPENDENCY_AUTOMATION_INSTRUCTIONS,
            OUTPUT_FORMAT_INSTRUCTIONS,
        )

        rules = _rules_of(next(iter(_registry().values())))
        assert DEPENDENCY_AUTOMATION_INSTRUCTIONS in rules
        assert OUTPUT_FORMAT_INSTRUCTIONS in render_system_prompt("", "")

    def test_is_exported_for_external_assertion(self) -> None:
        from orchestrator import prompt_builder

        assert "DATA_CONTRACT_INSTRUCTIONS" in prompt_builder.__all__


def _registry() -> dict:
    from orchestrator.agents import AGENT_REGISTRY

    return AGENT_REGISTRY


# ===========================================================================
# Prompt templates
# ===========================================================================


class TestDataContractTemplates:
    @pytest.mark.parametrize("name", DATA_CONTRACT_TEMPLATES)
    def test_template_has_a_data_contract_section(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "## Data contract (shapes crossing components)" in text, name

    @pytest.mark.parametrize("name", DATA_CONTRACT_TEMPLATES)
    def test_template_requires_typed_schemas(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "TypedDict" in text, name
        assert "docs/ARCHITECTURE.md" in text, name

    @pytest.mark.parametrize("name", DATA_CONTRACT_TEMPLATES)
    def test_template_requires_boundary_conversion(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "boundary" in text.lower(), name

    @pytest.mark.parametrize("name", DATA_CONTRACT_TEMPLATES)
    def test_template_requires_error_path_in_schema(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "error path" in text, name

    def test_software_template_demands_main_py_and_an_integration_test(self) -> None:
        text = (PROMPT_DIR / "07_SOFTWARE_FIRMWARE.md").read_text(encoding="utf-8")
        assert "main.py" in text
        assert "Test the crossing" in text

    def test_architecture_template_demands_typed_specs(self) -> None:
        text = (PROMPT_DIR / "04_ARCHITECTURE.md").read_text(encoding="utf-8")
        assert "Pydantic" in text

    def test_test_template_demands_the_crossing_be_tested(self) -> None:
        text = (PROMPT_DIR / "08_TEST.md").read_text(encoding="utf-8")
        assert "not just the parts" in text or "crossing itself" in text


# ===========================================================================
# The regression project
# ===========================================================================


class TestSysMonShapeContract:
    """``sys_mon`` is the project the failure was observed in.

    Its success path returns nested mappings while a view layer wants scalars,
    and its error path returns a bare ``{"error": str}`` — a third shape that
    matches neither. Both are exactly what the contract now forbids, so they are
    asserted here against the shipped code.
    """

    def test_error_path_is_a_different_shape_from_the_success_path(self) -> None:
        """The documented antipattern, still present: worth keeping visible."""
        source = (SYS_MON / "sys_mon.py").read_text(encoding="utf-8")
        assert '"error"' in source
        assert '"ram": ram_usage' in source or '"ram":' in source

    def test_no_executable_schema_is_declared_yet(self) -> None:
        """Documents the gap this contract closes.

        The project ships no ``TypedDict``/Pydantic declaration and no
        ``docs/ARCHITECTURE.md``, which is precisely why the two shapes could
        drift apart unnoticed. If this assertion starts failing, the project has
        been fixed and this test should be replaced by one asserting the
        declaration exists.
        """
        assert not (SYS_MON / "docs" / "ARCHITECTURE.md").exists()
        source = (SYS_MON / "sys_mon.py").read_text(encoding="utf-8")
        assert "class Metrics(TypedDict)" not in source


class TestDeployChannelUnaffected:
    def test_contract_does_not_change_execution_policy(self) -> None:
        """A prompt-only change must not touch the deployment gates."""
        assert config.DEPLOY_ENABLED is False
        assert config.DEPLOY_ALLOWLIST == ()