"""UI and application contract enforcement in the delivered prompts.

``software_agent`` produces minimal UI — a bare Tkinter progress bar, no entry
point — which satisfies the letter of "add a progress bar" and is not the
dashboard that was asked for. Nothing in the task text forbids it, and a test
that only checks the app starts cannot see the omission.

These tests deliberately assert the **delivered prompt text**, not which
constant supplies which clause. The obligation is split across
``ENTRYPOINT_CONTRACT``, ``INTERFACE_ALIGNMENT_CONTRACT`` and
``UI_CONTRACT_INSTRUCTIONS`` in ``base_agent``, and that split is an
implementation detail: what must not regress is that a ``software_agent`` prompt
requires all three. Pinning the split would make the suite fail on a harmless
reorganisation, and would reward duplicating a rule in two constants to satisfy
a stricter-looking test — the exact drift GAP-CRIT-09 existed to end.

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
    UI_CONTRACT_INSTRUCTIONS,
    UI_FIDELITY_SPEC,
    build_prompt,
    render_system_prompt,
)

PROMPT_DIR = REPO_ROOT / "framework" / "AGENT_PROMPTS"
SOFTWARE_TEMPLATE = PROMPT_DIR / "07_SOFTWARE_FIRMWARE.md"


def _rules_of(agent_class: type) -> str:
    agent = agent_class.__new__(agent_class)
    agent.extra_rules = []
    return agent.system_rules()


def _software_rules() -> str:
    from orchestrator.agents import AGENT_REGISTRY

    return _rules_of(AGENT_REGISTRY["software_agent"])


def _software_prompt() -> str:
    return build_prompt(
        {"task": {"id": "TASK-001", "owner": "software_agent"}, "agent_id": "software_agent"}
    )


def _delivered() -> str:
    """Everything a software_agent actually receives: rules + user message."""
    return _software_rules() + "\n" + _software_prompt()


# ===========================================================================
# Mandatory entry point
# ===========================================================================


class TestMandatoryEntryPoint:
    def test_main_py_must_be_delivered_at_project_root(self) -> None:
        text = _delivered()
        assert "main.py" in text
        assert "PROJECT ROOT" in text

    def test_entrypoint_must_construct_dependencies_and_run_the_loop(self) -> None:
        text = _delivered()
        assert "instantiate" in text
        assert "mainloop" in text

    def test_entrypoint_must_be_added_to_expected_outputs(self) -> None:
        """Otherwise the Definition of Done never checks it exists."""
        assert "expected_outputs" in _delivered()

    def test_entrypoint_survives_when_modules_already_exist(self) -> None:
        """The tempting case: everything looks done, so nothing runs."""
        text = _delivered()
        assert "already exists" in text or "even when" in text


# ===========================================================================
# Interface and schema consistency
# ===========================================================================


class TestInterfaceAndSchemaConsistency:
    def test_requires_typed_collector_outputs(self) -> None:
        text = _delivered()
        assert "TypedDict" in text
        assert "Pydantic" in text

    def test_requires_schema_to_match_consumer_expectations(self) -> None:
        text = _delivered()
        assert "match" in text.lower()
        assert "percent_used" in text

    def test_forbids_defensive_fallback_chains(self) -> None:
        """`.get(a, .get(b, 0.0))` converts a loud error into a silent 0.0."""
        text = _delivered()
        assert "fallback chain" in text or "defensive" in text
        assert "0.0" in text

    def test_requires_one_named_conversion_at_the_crossing(self) -> None:
        text = _delivered()
        assert "conversion function" in text or "convert" in text.lower()


class TestSignatureVerification:
    """Requirement 2: verify cross-module signatures before implementing."""

    def test_requires_reading_producer_before_calling(self) -> None:
        text = _delivered()
        assert "read" in text.lower()
        assert "before" in text.lower()

    def test_requires_verbatim_signature_match(self) -> None:
        text = _delivered()
        assert "VERBATIM" in text or "verbatim" in text

    def test_names_the_parts_of_a_signature(self) -> None:
        text = _delivered()
        assert "capitalisation" in text
        assert "keyword" in text.lower()

    def test_forbids_reconstructing_a_signature_from_vocabulary(self) -> None:
        assert "vocabulary" in _delivered()

    def test_forbids_writing_the_call_site_first(self) -> None:
        assert "call site" in _delivered()


# ===========================================================================
# UI design fidelity
# ===========================================================================


class TestUiFidelity:
    def test_dark_mode_palette_is_specified_concretely(self) -> None:
        text = _delivered()
        for colour in ("#0f172a", "#1e293b"):
            assert colour in text, f"{colour} must be named explicitly"

    def test_requires_legend_and_axes_on_the_chart(self) -> None:
        text = _delivered()
        assert "legend" in text.lower()
        assert "axes" in text.lower() or "axis" in text.lower()

    def test_requires_status_indicator(self) -> None:
        assert "status indicator" in _delivered().lower()

    def test_requires_metric_telemetry_cards(self) -> None:
        assert "telemetry card" in _delivered().lower()

    def test_requires_interactive_controls(self) -> None:
        text = _delivered().lower()
        assert "slider" in text
        assert "button" in text

    def test_forbids_decorative_controls(self) -> None:
        """A control that does nothing is worse than no control."""
        assert "decorative" in _delivered().lower()

    def test_requires_custom_canvas_chart(self) -> None:
        assert "canvas" in _delivered().lower()

    def test_forbids_the_bare_widget_outcome(self) -> None:
        text = _delivered().lower()
        assert "bare" in text or "placeholder" in text

    def test_requires_composed_layout(self) -> None:
        assert "layout" in _delivered().lower()

    def test_requires_fidelity_to_be_executed_not_asserted(self) -> None:
        """A PASS with no executed record is a fabrication."""
        text = _delivered()
        assert "data.deploy" in text
        assert "fabrication" in text.lower()


# ===========================================================================
# Reach and scoping
# ===========================================================================


class TestContractReach:
    def test_all_registered_agents_receive_the_entrypoint_obligation(self) -> None:
        from orchestrator.agents import AGENT_REGISTRY

        assert len(AGENT_REGISTRY) == 10
        for name, agent_class in AGENT_REGISTRY.items():
            rules = _rules_of(agent_class)
            assert "main.py" in rules, f"{name} misses the entrypoint obligation"

    def test_all_registered_agents_receive_interface_alignment(self) -> None:
        from orchestrator.agents import AGENT_REGISTRY

        for name, agent_class in AGENT_REGISTRY.items():
            assert "VERBATIM" in _rules_of(agent_class), f"{name} misses interface alignment"

    def test_survives_a_subclass_replacing_system_rules(self) -> None:
        """CRIT-08's lesson: SYSTEM_RULES is shadowed by all ten agents."""
        from orchestrator.agents.base_agent import BaseAgent

        class Shadowing(BaseAgent):
            AGENT_ID = "shadowing_agent"
            SYSTEM_RULES = "1. Only this one line."

        rules = _rules_of(Shadowing)
        assert "main.py" in rules
        assert "UI fidelity contract" in rules

    def test_rendered_system_prompt_carries_the_ui_obligation(self) -> None:
        assert UI_CONTRACT_INSTRUCTIONS in render_system_prompt("rules", "spec")

    def test_user_message_carries_the_ui_obligation(self) -> None:
        assert "# Application contract obligation" in _software_prompt() or (
            "UI fidelity contract" in _software_prompt()
        )


class TestFidelityDetailIsScoped:
    """The palette and widget list only mean something to the view layer."""

    def test_software_agent_receives_the_concrete_checklist(self) -> None:
        prompt = _software_prompt()
        assert UI_FIDELITY_SPEC in prompt
        assert "#0f172a" in prompt

    def test_other_agents_do_not_receive_the_palette(self) -> None:
        prompt = build_prompt(
            {
                "task": {"id": "TASK-001", "owner": "documentation_agent"},
                "agent_id": "documentation_agent",
            }
        )
        assert UI_FIDELITY_SPEC not in prompt
        assert "#0f172a" not in prompt

    def test_the_obligation_still_reaches_every_agent(self) -> None:
        """Scoping the detail must not scope the duty."""
        prompt = build_prompt(
            {
                "task": {"id": "TASK-001", "owner": "documentation_agent"},
                "agent_id": "documentation_agent",
            }
        )
        assert UI_CONTRACT_INSTRUCTIONS in prompt

    def test_constants_are_exported(self) -> None:
        from orchestrator import prompt_builder

        assert "UI_CONTRACT_INSTRUCTIONS" in prompt_builder.__all__
        assert "UI_FIDELITY_SPEC" in prompt_builder.__all__


class TestNoDuplicationOfRules:
    """Duplicating a rule across constants is how the prompts drift apart."""

    def test_entrypoint_rule_is_stated_once_in_source(self) -> None:
        """One authoritative statement of the main.py obligation.

        ``ENTRYPOINT_CONTRACT`` and ``UI_CONTRACT_INSTRUCTIONS`` both mention
        the entrypoint; the detailed obligation must live in exactly one of
        them, or a future edit will update one and not the other.
        """
        source = (REPO_ROOT / "orchestrator" / "prompt_builder.py").read_text(
            encoding="utf-8"
        ) + (REPO_ROOT / "orchestrator" / "agents" / "base_agent.py").read_text(
            encoding="utf-8"
        )
        # "main.py at the PROJECT ROOT" is the mandatory-delivery sentence.
        assert source.count("MUST also deliver a main.py") == 1
        assert source.count("MANDATORY") <= 2, "entrypoint rule re-stated"

    def test_ui_obligation_does_not_restate_the_entrypoint_rule(self) -> None:
        """Keeps the narrowing honest: my block owns fidelity only."""
        assert "main.py" not in UI_CONTRACT_INSTRUCTIONS


class TestSoftwareTemplate:
    @pytest.fixture(autouse=True)
    def _text(self) -> None:
        self.template = SOFTWARE_TEMPLATE.read_text(encoding="utf-8")

    def test_template_has_a_ui_fidelity_section(self) -> None:
        assert "## UI fidelity (a dashboard, not a widget)" in self.template

    def test_template_names_the_dark_palette(self) -> None:
        for colour in ("#0f172a", "#1e293b", "#334155"):
            assert colour in self.template, colour

    def test_template_requires_legend_axes_and_controls(self) -> None:
        for term in ("legend", "axes", "slider", "button"):
            assert term in self.template.lower(), term

    def test_template_requires_status_indicator_and_cards(self) -> None:
        lowered = self.template.lower()
        assert "status indicator" in lowered
        assert "telemetry card" in lowered

    def test_template_warns_about_invalid_widget_options(self) -> None:
        """`px=` raises TclError before anything is visible."""
        assert "padx" in self.template
        assert "TclError" in self.template

    def test_template_still_embeds_the_canonical_output_contract(self) -> None:
        from orchestrator.prompt_builder import OUTPUT_FORMAT_INSTRUCTIONS

        assert OUTPUT_FORMAT_INSTRUCTIONS in self.template


class TestPromptOnlyChangeIsInert:
    def test_deployment_policy_untouched(self) -> None:
        """Prompt text must not move the execution gates."""
        assert config.DEPLOY_ENABLED is False
        assert config.DEPLOY_ALLOWLIST == ()