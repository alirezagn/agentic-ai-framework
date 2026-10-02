"""Agent contract enforcement — entry points and cross-module interface alignment.

Two failure modes produced unrunnable multi-module projects during autonomous
dispatch, both visible in ``projects/sys_mon_gui``:

1. **No entry point.** Three modules, each internally correct and each green in
   isolation, and nothing that starts them. ``main.py`` existed only because it
   was written by hand afterwards. Structurally, nothing in a per-task
   deliverable list says "something has to tie these together" — so a
   multi-module project arrives as modules, and every task can still pass its own
   Definition of Done.
2. **Interface drift.** ``SchemaAdapterDataStore`` had to guess between
   ``ram["percent_used"]`` and ``ram["percent"]`` and covered the guess with
   ``.get("percent_used", .get("percent", 0.0))`` — a fallback chain that hides
   the mismatch instead of resolving it, and that silently renders ``0.0``
   forever if the key ever changes.

These tests assert the *prompt* now carries both obligations, that the strictest
form lands on the agent that writes module boundaries, and that it does not
displace the contracts already in place.

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
from orchestrator.agents.base_agent import (  # noqa: E402
    ENTRYPOINT_CONTRACT,
    INTERFACE_ALIGNMENT_CONTRACT,
)
from orchestrator.prompt_builder import (  # noqa: E402
    DATA_CONTRACT_INSTRUCTIONS,
    DATA_CONTRACT_SPEC,
    DATA_CONTRACT_SPEC_AGENTS,
    build_prompt,
    render_system_prompt,
)

PROMPT_DIR = REPO_ROOT / "framework" / "AGENT_PROMPTS"
SYS_MON_GUI = REPO_ROOT.parent / "sys_mon_gui"

TEMPLATES_WITH_ENTRYPOINT = ("04_ARCHITECTURE.md", "07_SOFTWARE_FIRMWARE.md")


def _rules_of(agent_class: type) -> str:
    agent = agent_class.__new__(agent_class)
    agent.extra_rules = []
    return agent.system_rules()


# ===========================================================================
# Requirement 1a — mandatory entry point
# ===========================================================================


class TestMandatoryEntryPoint:
    def test_entry_point_is_mandatory_not_optional(self) -> None:
        text = ENTRYPOINT_CONTRACT
        assert "MUST" in text
        assert "main.py" in text
        assert "PROJECT ROOT" in text

    def test_entry_point_must_be_unconditional_for_runnable_apps(self) -> None:
        assert "runnable components" in ENTRYPOINT_CONTRACT

    def test_requires_importing_every_module(self) -> None:
        assert "import EVERY module" in ENTRYPOINT_CONTRACT

    def test_requires_initialising_dependencies_in_order(self) -> None:
        assert "instantiate their dependencies in the correct order" in (
            ENTRYPOINT_CONTRACT
        )

    def test_requires_entering_the_run_loop(self) -> None:
        assert "run loop" in ENTRYPOINT_CONTRACT
        assert "mainloop" in ENTRYPOINT_CONTRACT

    def test_requires_the_import_guard(self) -> None:
        """Importing main.py in a test must not open a window or block."""
        assert '__main__' in ENTRYPOINT_CONTRACT

    def test_requires_adding_it_to_expected_outputs(self) -> None:
        assert "expected_outputs" in ENTRYPOINT_CONTRACT

    def test_still_required_when_modules_already_exist(self) -> None:
        """The exact state in which the project was unrunnable."""
        assert "every module already exists" in ENTRYPOINT_CONTRACT


# ===========================================================================
# Requirement 1b — strict interface alignment
# ===========================================================================


class TestStrictInterfaceAlignment:
    def test_requires_reading_the_producer_first(self) -> None:
        text = INTERFACE_ALIGNMENT_CONTRACT
        lowered = text.lower()
        assert "read the existing file" in lowered
        assert "read the producer before writing the consumer" in lowered

    def test_requires_verbatim_matching(self) -> None:
        assert "VERBATIM" in INTERFACE_ALIGNMENT_CONTRACT

    def test_names_the_three_thingsto_match(self) -> None:
        text = INTERFACE_ALIGNMENT_CONTRACT
        assert "capitalisation" in text
        assert "positional argument" in text
        assert "return" in text and "structure" in text

    def test_forbids_calling_unseen_methods(self) -> None:
        assert "have not seen defined" in INTERFACE_ALIGNMENT_CONTRACT

    def test_forbids_defensive_fallback_chains(self) -> None:
        """The exact anti-pattern that hid the sys_mon_gui mismatch."""
        text = INTERFACE_ALIGNMENT_CONTRACT
        assert "defensive fallback chain" in text
        assert ".get(a, .get(b, 0.0))" in text

    def test_explains_why_guessing_is_worse_than_failing(self) -> None:
        text = INTERFACE_ALIGNMENT_CONTRACT
        assert "loud TypeError" in text
        assert "wrong value" in text

    def test_requires_one_named_conversion_when_shapes_differ(self) -> None:
        assert "one named conversion function" in INTERFACE_ALIGNMENT_CONTRACT

    def test_requires_nested_versus_flat_to_be_resolved(self) -> None:
        assert "percent_used" in INTERFACE_ALIGNMENT_CONTRACT
        assert "nested" in INTERFACE_ALIGNMENT_CONTRACT


# ===========================================================================
# Requirement 2 — DATA_CONTRACT_SPEC for software_agent
# ===========================================================================


class TestDataContractSpec:
    def test_spec_requires_exact_type_specifications(self) -> None:
        assert "TypedDict" in DATA_CONTRACT_SPEC
        assert "Pydantic" in DATA_CONTRACT_SPEC or "Pydantic" in DATA_CONTRACT_INSTRUCTIONS

    def test_spec_offers_a_scalar_return_option(self) -> None:
        """Either a typed model or a documented scalar -- but chosen."""
        assert "SCALAR" in DATA_CONTRACT_SPEC
        assert "-> float" in DATA_CONTRACT_SPEC

    def test_spec_covers_the_nested_versus_flat_failure(self) -> None:
        assert "percent_used" in DATA_CONTRACT_SPEC

    def test_spec_requires_parameter_arity(self) -> None:
        text = DATA_CONTRACT_SPEC
        assert "positional" in text
        assert "keyword" in text
        assert "Never invent a parameter" in text

    def test_spec_requires_raises_clause(self) -> None:
        assert "RAISES" in DATA_CONTRACT_SPEC

    def test_spec_forbids_guess_chains(self) -> None:
        assert "guess" in DATA_CONTRACT_SPEC

    def test_spec_requires_error_path_in_the_declared_shape(self) -> None:
        assert "error: Optional[str]" in DATA_CONTRACT_SPEC

    def test_spec_is_exported(self) -> None:
        from orchestrator import prompt_builder

        assert "DATA_CONTRACT_SPEC" in prompt_builder.__all__
        assert "DATA_CONTRACT_SPEC_AGENTS" in prompt_builder.__all__


class TestSpecIsInjectedOnlyWhereItApplies:
    def test_software_agent_prompt_contains_the_spec(self) -> None:
        prompt = build_prompt(
            {"agent_id": "software_agent", "task": {"id": "TASK-001"}}
        )
        assert "# Module interface spec (software_agent)" in prompt
        assert DATA_CONTRACT_SPEC in prompt

    def test_requirements_agent_does_not_receive_it(self) -> None:
        """Contract an agent cannot use is prompt budget spent on noise."""
        prompt = build_prompt(
            {"agent_id": "requirements_agent", "task": {"id": "TASK-001"}}
        )
        assert "Module interface spec" not in prompt
        assert DATA_CONTRACT_SPEC not in prompt

    @pytest.mark.parametrize(
        "agent_id", ["firmware_agent", "architecture_agent", "test_agent"]
    )
    def test_other_agents_do_not_receive_it(self, agent_id: str) -> None:
        prompt = build_prompt({"agent_id": agent_id, "task": {"id": "TASK-001"}})
        assert "Module interface spec" not in prompt

    def test_missing_agent_id_does_not_inject(self) -> None:
        prompt = build_prompt({"task": {"id": "TASK-001"}})
        assert "Module interface spec" not in prompt

    def test_matching_is_case_and_space_insensitive(self) -> None:
        """Mirrors normalize_agent_name (strip().lower()) for agent ids."""
        for variant in ("software_agent", "Software_Agent", "  software_agent  "):
            prompt = build_prompt(
                {"agent_id": variant, "task": {"id": "TASK-001"}}
            )
            assert "Module interface spec" in prompt, variant

    def test_every_agent_in_the_set_is_a_real_agent(self) -> None:
        """A typo in the set would silently disable the whole contract."""
        from orchestrator.agents import AGENT_REGISTRY

        assert DATA_CONTRACT_SPEC_AGENTS <= set(AGENT_REGISTRY)

    def test_global_contract_reaches_everyone_regardless(self) -> None:
        """The general obligation is not narrowed to software_agent."""
        for agent_id in ("software_agent", "requirements_agent", "review_agent"):
            prompt = build_prompt(
                {"agent_id": agent_id, "task": {"id": "TASK-001"}}
            )
            assert DATA_CONTRACT_INSTRUCTIONS in prompt, agent_id


# ===========================================================================
# Reach: the system rules and the templates
# ===========================================================================


class TestContractsReachEveryAgent:
    def test_all_registered_agents_carry_both(self) -> None:
        from orchestrator.agents import AGENT_REGISTRY

        assert len(AGENT_REGISTRY) == 10
        for name, agent_class in AGENT_REGISTRY.items():
            rules = _rules_of(agent_class)
            assert ENTRYPOINT_CONTRACT in rules, f"{name} misses the entry-point contract"
            assert INTERFACE_ALIGNMENT_CONTRACT in rules, (
                f"{name} misses the interface-alignment contract"
            )

    def test_survives_a_subclass_that_replaces_system_rules(self) -> None:
        """CRIT-08's lesson: SYSTEM_RULES is shadowed by all ten agents."""
        from orchestrator.agents.base_agent import BaseAgent

        class Shadowing(BaseAgent):
            AGENT_ID = "shadowing_agent"
            SYSTEM_RULES = "1. Only this one line."

        rules = _rules_of(Shadowing)
        assert "main.py" in rules
        assert "VERBATIM" in rules

    def test_rendered_system_prompt_still_carries_the_output_contract(self) -> None:
        rendered = render_system_prompt("rules", "spec")
        assert DATA_CONTRACT_INSTRUCTIONS in rendered


class TestPromptTemplatesStateBothRules:
    @pytest.mark.parametrize("name", TEMPLATES_WITH_ENTRYPOINT)
    def test_template_has_the_entry_point_section(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "## Entry point and interface alignment (non-optional)" in text, name

    @pytest.mark.parametrize("name", TEMPLATES_WITH_ENTRYPOINT)
    def test_template_requires_main_py_at_project_root(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "main.py" in text, name
        assert "expected_outputs" in text, name

    @pytest.mark.parametrize("name", TEMPLATES_WITH_ENTRYPOINT)
    def test_template_requires_verbatim_alignment(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "verbatim" in text.lower(), name

    @pytest.mark.parametrize("name", TEMPLATES_WITH_ENTRYPOINT)
    def test_template_forbids_guess_chains(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "guess" in text.lower(), name

    def test_software_template_requires_typed_interface(self) -> None:
        text = (PROMPT_DIR / "07_SOFTWARE_FIRMWARE.md").read_text(encoding="utf-8")
        assert "TypedDict" in text
        assert "scalar" in text.lower()

    def test_architecture_template_requires_declared_signatures(self) -> None:
        text = (PROMPT_DIR / "04_ARCHITECTURE.md").read_text(encoding="utf-8")
        assert "docs/ARCHITECTURE.md" in text
        assert "signature" in text.lower()

    def test_import_guard_is_documented_in_the_template(self) -> None:
        text = (PROMPT_DIR / "07_SOFTWARE_FIRMWARE.md").read_text(encoding="utf-8")
        assert '__main__' in text


# ===========================================================================
# The regression project
# ===========================================================================


class TestSysMonGuiRegression:
    """``sys_mon_gui`` is where both failures were observed."""

    def _source(self, relative: str) -> str:
        path = SYS_MON_GUI / relative
        if not path.is_file():
            pytest.skip(f"{path} not present")
        return path.read_text(encoding="utf-8")

    def test_entry_point_exists_at_project_root(self) -> None:
        """The end state both contracts exist to produce."""
        if not SYS_MON_GUI.is_dir():
            pytest.skip("sys_mon_gui project not present")
        assert (SYS_MON_GUI / "main.py").is_file()

    def test_entry_point_imports_the_modules(self) -> None:
        source = self._source("main.py")
        assert "UIController" in source
        assert "MetricCollector" in source

    def test_entry_point_is_import_safe(self) -> None:
        """Importing main.py must not start a window or block the process."""
        source = self._source("main.py")
        assert '__main__' in source

    def test_the_documented_guess_chain_was_present(self) -> None:
        """Records the anti-pattern the contract now forbids."""
        source = self._source("main.py")
        assert 'raw.get("ram", 0)' in source or "ram_val" in source

    def test_invalid_tkinter_option_is_absent(self) -> None:
        """`px=` is not a pack option; it raised TclError at construction."""
        source = self._source("src/gui_controller.py")
        assert "px=" not in source, "px is not a valid geometry-manager option"
        assert 'padx=20' in source


class TestExecutionPolicyUnaffected:
    def test_prompt_only_change_leaves_deploy_gates_closed(self) -> None:
        assert config.DEPLOY_ENABLED is False
        assert config.DEPLOY_ALLOWLIST == ()