"""Docs-consistency tests (GAP_ANALYSIS task T18, findings 13 + 15).

Guards against documentation drift: the guide must describe the v2.0 runtime,
every path referenced by the top-level docs must exist, and the copy-paste
state-file templates must produce a project that passes ``validate()``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orchestrator import config
from orchestrator.cli import build_parser
from orchestrator.state_manager import StateManager

REPO_ROOT = Path(__file__).resolve().parent

TEMPLATE_STATE_FILES = [
    config.PROJECT_FILE,
    config.TASKS_FILE,
    config.MEMORY_FILE,
    config.CURRENT_STATE_FILE,
    config.DECISIONS_FILE,
    config.RISKS_FILE,
    config.CHANGELOG_FILE,
]

AGENT_PROMPT_FILES = [
    "README.md",
    "00_ORCHESTRATOR.md",
    "01_SUPERVISOR.md",
    "02_REQUIREMENTS.md",
    "03_RESEARCH.md",
    "04_ARCHITECTURE.md",
    "05_PLANNING.md",
    "06_HARDWARE.md",
    "07_SOFTWARE_FIRMWARE.md",
    "08_TEST.md",
    "09_REVIEW.md",
    "10_DOCUMENTATION.md",
]

TEMPLATE_DOC_FILES = [
    "README.md",
    "REQUIREMENTS.md",
    "RESEARCH.md",
    "ARCHITECTURE.md",
    "PLAN.md",
    "TEST_PLAN.md",
    "REVIEW.md",
]


class TestGuideV2:
    @pytest.fixture()
    def guide(self) -> str:
        return (REPO_ROOT / "ORCHESTRATOR_GUIDE.md").read_text(encoding="utf-8")

    def test_guide_declares_current_version(self, guide: str) -> None:
        assert "**Version:** 3.0.0" in guide

    def test_guide_has_no_v11_api(self, guide: str) -> None:
        # v1.1 symbols/examples must be gone (mentioning them as "removed" is ok
        # only outside of usage examples)
        assert "ProjectManager(" not in guide
        assert "TaskExecutor(" not in guide
        assert "/home/claude" not in guide
        assert "checkpoint.py`)" not in guide

    def test_guide_documents_every_cli_subcommand(self, guide: str) -> None:
        parser = build_parser()
        # argparse keeps subparsers in a _SubParsersAction
        for action in parser._actions:
            if isinstance(action, type(parser._subparsers._group_actions[0])) and hasattr(
                action, "choices"
            ):
                for name in action.choices:
                    assert name in guide, f"subcommand '{name}' missing from guide"
                break
        else:  # pragma: no cover - argparse internals changed
            pytest.skip("subparser action not found")

    def test_guide_documents_exit_codes_and_loop_kinds(self, guide: str) -> None:
        from orchestrator import config as cfg

        for kind in cfg.LOOP_KINDS:
            assert kind in guide, f"loop kind {kind} missing from guide"
        assert "Exit codes" in guide


class TestReferencedPathsExist:
    @pytest.mark.parametrize(
        "relative",
        [
            "framework/20_DEFAULT_PROJECT_START_PROMPT.md",
            "framework/AGENT_PROMPTS",
            "framework/TEMPLATES",
            "project-templates/NEW_PROJECT_CHECKLIST.md",
            "project-templates/PROJECT.yaml",
            "project-templates/PROJECT_MEMORY.md",
            "project-templates/CURRENT_STATE.md",
            "project-templates/TASKS.yaml",
            "project-templates/DECISIONS.md",
            "project-templates/RISKS.md",
            "project-templates/CHANGELOG.md",
            "meta/GETTING_STARTED.md",
            "meta/WORKFLOW.md",
            "meta/TROUBLESHOOTING.md",
            "agents/orchestrator.md",
            "agents/supervisor.md",
            "agents/specialists/README.md",
            "references/README.md",
            "bin/orchestrator",
            "pyproject.toml",
        ],
    )
    def test_path(self, relative: str) -> None:
        assert (REPO_ROOT / relative).exists(), f"missing {relative}"

    def test_agent_prompt_files(self) -> None:
        for name in AGENT_PROMPT_FILES:
            assert (REPO_ROOT / "framework" / "AGENT_PROMPTS" / name).is_file()

    def test_template_files(self) -> None:
        for name in TEMPLATE_DOC_FILES:
            assert (REPO_ROOT / "framework" / "TEMPLATES" / name).is_file()

    def test_readme_no_stale_paths(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "new-project-starter" not in readme
        assert "checklists/" not in readme
        assert "20_DEFAULT_PROJECT_START_PROMPT" in readme


class TestStateFileTemplates:
    def test_templates_copy_to_valid_project(self, tmp_path: Path) -> None:
        target = tmp_path / "from-template"
        target.mkdir()
        for name in TEMPLATE_STATE_FILES:
            shutil.copy(REPO_ROOT / "project-templates" / name, target / name)

        problems = StateManager(target).validate()
        assert problems == [], problems

    def test_memory_template_has_required_heading(self) -> None:
        memory = (REPO_ROOT / "project-templates" / config.MEMORY_FILE).read_text(
            encoding="utf-8"
        )
        assert memory.startswith("# PROJECT_MEMORY")
        assert "## Goal" in memory
