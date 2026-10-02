"""Automated dependency management — the prompt contract must enforce it.

The failure this guards against was real and observed in ``projects/sys_mon``:
the agent delivered correct code importing ``psutil``, and reported "you may
need to install psutil" **in prose**. No ``requirements.txt`` was created and no
``data.deploy`` step was requested, so the next turn — the test run — died at
import time and the framework's own suite could not be collected at all.

A warning in ``summary`` installs nothing. The contract therefore has to reach
the model on every code-producing path, say three specific things, and — just as
important — stay *honest*: the execution channel is closed by default, so a
contract that merely said "always install" would invite a fabricated
execution record, trading a missing file for a worse defect.

Layout note: the suite lives at the repository root
(``conftest.py`` supplies ``build_test_project``).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import config  # noqa: E402
from orchestrator.prompt_builder import (  # noqa: E402
    DEPENDENCY_AUTOMATION_INSTRUCTIONS,
    build_prompt,
    render_system_prompt,
)

PROMPT_DIR = REPO_ROOT / "framework" / "AGENT_PROMPTS"
SYS_MON = REPO_ROOT / "projects" / "sys_mon"

#: Templates for agents that can introduce or depend on an import. The hardware,
#: research and documentation agents do not write importing code, and 00/01/05/09
#: are not registry agents, so they are deliberately excluded.
CODE_AGENT_TEMPLATES = (
    "02_REQUIREMENTS.md",
    "07_SOFTWARE_FIRMWARE.md",
    "08_TEST.md",
)


def _load_sys_mon() -> object:
    """Import the sys_mon deliverable by explicit path.

    Not ``sys.path`` + ``import``: the repository suite also collects
    ``projects/sys_mon/test_sys_mon.py``, which imports the same module name,
    so a plain import here would depend on collection order and a later
    ``importlib.reload`` would fail once the path entry was gone.
    """
    import importlib.util

    location = SYS_MON / "sys_mon.py"
    spec = importlib.util.spec_from_file_location("_sysmon_under_test", location)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rules_of(agent_class: type) -> str:
    """``system_rules()`` without constructing an agent (no project needed)."""
    agent = agent_class.__new__(agent_class)
    agent.extra_rules = []
    return agent.system_rules()


# ===========================================================================
# The contract itself
# ===========================================================================


class TestDependencyContractContent:
    """Three obligations, and the ordering between them is the whole point."""

    def test_requires_a_declaration_file(self) -> None:
        assert "requirements.txt" in DEPENDENCY_AUTOMATION_INSTRUCTIONS

    def test_requires_the_declaration_to_be_delivered_not_mentioned(self) -> None:
        text = DEPENDENCY_AUTOMATION_INSTRUCTIONS
        assert "data.documents" in text, "a missing requirements.txt must be delivered"
        assert "data.edits" in text, "an existing requirements.txt must be patched"
        assert "expected_outputs" in text, (
            "the declaration must be verifiable by the Definition of Done"
        )

    def test_requires_an_explicit_install_command(self) -> None:
        text = DEPENDENCY_AUTOMATION_INSTRUCTIONS
        assert '"pip"' in text and '"install"' in text
        assert "-r" in text
        assert "data.deploy" in text, "the install must be a real execution request"

    def test_states_the_install_precedes_the_verification(self) -> None:
        text = DEPENDENCY_AUTOMATION_INSTRUCTIONS.lower()
        assert "before" in text, "ordering must be stated, not implied"
        assert "order" in text

    def test_requires_checking_dependencies_before_concluding(self) -> None:
        text = DEPENDENCY_AUTOMATION_INSTRUCTIONS.lower()
        assert "verify" in text
        assert "modulenotfounderror" in text, (
            "the contract must name the failure it prevents, so the model "
            "recognises it as its own omission"
        )

    def test_names_prose_as_insufficient(self) -> None:
        assert "installs nothing" in DEPENDENCY_AUTOMATION_INSTRUCTIONS

    def test_uses_a_concrete_example(self) -> None:
        """A concrete package makes the obligation legible to the model."""
        assert "psutil" in DEPENDENCY_AUTOMATION_INSTRUCTIONS


class TestContractStaysHonest:
    """The channel is closed by default; the contract must not over-promise."""

    def test_offers_the_honest_not_run_path(self) -> None:
        text = DEPENDENCY_AUTOMATION_INSTRUCTIONS
        assert "NOT RUN" in text
        assert "executed: false" in text, "must name the real refusal signal"

    def test_forbids_claiming_an_install_without_evidence(self) -> None:
        assert "never report a dependency as installed" in (
            DEPENDENCY_AUTOMATION_INSTRUCTIONS.lower()
        )

    def test_does_not_weaken_the_existing_deploy_refusal_rule(self) -> None:
        """The new contract must not contradict DATA_DEPLOY_CONTRACT."""
        from orchestrator.agents.base_agent import DATA_DEPLOY_CONTRACT

        assert "NOT RUN" in DATA_DEPLOY_CONTRACT
        assert "allowlist" in DEPENDENCY_AUTOMATION_INSTRUCTIONS

    def test_does_not_open_the_execution_channel(self) -> None:
        """Telling agents to run pip must not quietly allowlist pip."""
        assert config.DEPLOY_ENABLED is False
        assert config.DEPLOY_ALLOWLIST == ()
        assert "pip" not in config.DEPLOY_ALLOWLIST


# ===========================================================================
# Reach: every code-producing path must carry it
# ===========================================================================


class TestContractReachesEveryAgent:
    def test_all_registered_agents_carry_the_contract(self) -> None:
        from orchestrator.agents import AGENT_REGISTRY

        assert len(AGENT_REGISTRY) == 10
        for name, agent_class in AGENT_REGISTRY.items():
            rules = _rules_of(agent_class)
            assert "requirements.txt" in rules, f"{name} misses the dependency contract"
            assert "pip" in rules, f"{name} is not told how to install"

    def test_contract_survives_a_subclass_that_replaces_system_rules(self) -> None:
        """The original CRIT-08 lesson: SYSTEM_RULES is shadowed by subclasses."""
        from orchestrator.agents.base_agent import BaseAgent

        class Shadowing(BaseAgent):
            AGENT_ID = "shadowing_agent"
            SYSTEM_RULES = "1. Only this one line."

        assert "requirements.txt" in _rules_of(Shadowing)

    def test_rendered_system_prompt_carries_it(self) -> None:
        rendered = render_system_prompt("rules here", "spec here")
        assert DEPENDENCY_AUTOMATION_INSTRUCTIONS in rendered

    def test_user_message_carries_it(self) -> None:
        prompt = build_prompt({"task": {"id": "TASK-001", "owner": "software_agent"}})
        assert "requirements.txt" in prompt
        assert '"pip"' in prompt

    def test_contract_is_stated_near_the_required_output(self) -> None:
        """Placed after the task, where "what must I deliver" is answered."""
        prompt = build_prompt({"task": {"id": "TASK-001"}})
        assert prompt.index("# Dependency obligation") > prompt.index("# Task")


class TestCodeAgentTemplatesDocumentIt:
    @pytest.mark.parametrize("name", CODE_AGENT_TEMPLATES)
    def test_template_has_a_dependencies_section(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "## Dependencies (automated, never announced)" in text, name

    @pytest.mark.parametrize("name", CODE_AGENT_TEMPLATES)
    def test_template_states_declaration_and_installation(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "requirements.txt" in text, name
        assert '"pip"' in text and '"install"' in text, name
        assert "data.deploy" in text, name

    @pytest.mark.parametrize("name", CODE_AGENT_TEMPLATES)
    def test_template_states_ordering_and_honesty(self, name: str) -> None:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert "before" in text.lower(), name
        assert "NOT RUN" in text, name

    def test_software_template_covers_the_reported_failure(self) -> None:
        text = (PROMPT_DIR / "07_SOFTWARE_FIRMWARE.md").read_text(encoding="utf-8")
        assert "ModuleNotFoundError" in text

    def test_requirements_template_names_the_libraries(self) -> None:
        text = (PROMPT_DIR / "02_REQUIREMENTS.md").read_text(encoding="utf-8")
        assert "third-party" in text.lower()


# ===========================================================================
# The regression project itself
# ===========================================================================


class TestSysMonDeclaresItsDependency:
    def test_requirements_file_exists(self) -> None:
        assert (SYS_MON / "requirements.txt").is_file()

    def test_requirements_declares_psutil(self) -> None:
        lines = [
            line.strip()
            for line in (SYS_MON / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert any(line.lower().startswith("psutil") for line in lines), lines

    def test_requirements_are_pinned(self) -> None:
        for line in (SYS_MON / "requirements.txt").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert any(op in stripped for op in "<>=!~"), f"unpinned: {stripped}"


class TestSysMonAutomatesTheInstall:
    def test_module_is_importable_without_the_package(self) -> None:
        """The original defect killed *collection*, not a single assertion.

        ``psutil`` must not be imported at module scope, so the module can be
        read before the dependency exists.
        """
        tree = ast.parse((SYS_MON / "sys_mon.py").read_text(encoding="utf-8"))
        top_level: List[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.append(node.module)
        assert "psutil" not in top_level, (
            "psutil is imported at module scope; collection dies when it is absent"
        )

    def test_install_command_targets_the_requirements_file(self) -> None:
        module = _load_sys_mon()

        command = module.install_command()  # type: ignore[attr-defined]
        assert command[:3] == [sys.executable, "-m", "pip"], command
        assert command[3] == "install"
        assert command[4] == "-r"
        assert command[5].endswith("requirements.txt")

    def test_dependency_is_actually_available_now(self) -> None:
        module = _load_sys_mon()

        assert module.dependency_available(), (  # type: ignore[attr-defined]
            "the declared dependency is not installed; run "
            + " ".join(module.install_command())  # type: ignore[attr-defined]
        )

    def test_install_path_runs_when_the_package_is_missing(self) -> None:
        """The install itself must execute, not merely be declared."""
        module = _load_sys_mon()
        calls: List[List[str]] = []

        class _Completed:
            returncode = 0
            stdout = "Successfully installed psutil"
            stderr = ""

        def fake_runner(command: List[str], **kwargs: object) -> _Completed:
            calls.append(list(command))
            return _Completed()

        # Simulate "absent before, present after" without uninstalling anything.
        states = iter([False, True])
        module.dependency_available = lambda: next(states)  # type: ignore[attr-defined]
        result = module.ensure_dependencies(runner=fake_runner)  # type: ignore[attr-defined]

        assert result["action"] == "installed", result
        assert result["installed"] is True
        assert calls == [module.install_command()]  # type: ignore[attr-defined]
        assert calls[0][-1].endswith("requirements.txt")

    def test_failure_is_reported_not_raised(self) -> None:
        """A refused install is a finding, not an exception or a fake pass."""
        module = _load_sys_mon()

        def exploding_runner(command: List[str], **kwargs: object) -> None:
            raise RuntimeError("PEP 668: externally-managed-environment")

        module.dependency_available = lambda: False  # type: ignore[attr-defined]
        result = module.ensure_dependencies(runner=exploding_runner)  # type: ignore[attr-defined]

        assert result["action"] == "failed"
        assert result["installed"] is False
        assert "PEP 668" in result["detail"]

    def test_no_work_when_dependency_is_satisfied(self) -> None:
        """Never re-installs on every test run."""
        module = _load_sys_mon()

        def should_not_run(command: List[str], **kwargs: object) -> None:
            raise AssertionError("installer ran although the dependency is present")

        result = module.ensure_dependencies(runner=should_not_run)  # type: ignore[attr-defined]
        assert result["action"] == "none"

    def test_missing_requirements_file_is_reported(self) -> None:
        """No declaration means no automated install is possible."""
        module = _load_sys_mon()
        module.REQUIREMENTS_FILE = SYS_MON / "does_not_exist.txt"  # type: ignore[attr-defined]
        module.dependency_available = lambda: False  # type: ignore[attr-defined]

        result = module.ensure_dependencies()  # type: ignore[attr-defined]

        assert result["action"] == "failed"
        assert "requirements.txt is missing" in result["detail"]


class TestSysMonTestSuiteHonestAboutSetup:
    def test_setup_module_installs_before_tests(self) -> None:
        text = (SYS_MON / "test_sys_mon.py").read_text(encoding="utf-8")
        assert "def setUpModule" in text
        assert "ensure_dependencies()" in text

    def test_failed_setup_skips_rather_than_fakes_a_pass(self) -> None:
        text = (SYS_MON / "test_sys_mon.py").read_text(encoding="utf-8")
        assert "unittest.SkipTest" in text, (
            "a failed install must NOT RUN the tests, and must not pass them either"
        )