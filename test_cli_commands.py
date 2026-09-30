"""Tests for the CLI surface: ``init``, ``tasks``, ``bin/orchestrator``, pyproject."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import build_test_project
from orchestrator import cli, config
from orchestrator.cli import main
from orchestrator.state_manager import StateManager

REPO_ROOT = Path(__file__).resolve().parent
BIN = REPO_ROOT / "bin" / "orchestrator"

STATE_FILES = [
    config.PROJECT_FILE,
    config.TASKS_FILE,
    config.MEMORY_FILE,
    config.CURRENT_STATE_FILE,
    config.DECISIONS_FILE,
    config.RISKS_FILE,
    config.CHANGELOG_FILE,
]


class TestInitCommand:
    def test_init_scaffolds_valid_project(self, tmp_path: Path) -> None:
        code = main(
            [
                "init",
                "demo",
                "--dest",
                str(tmp_path),
                "--goal",
                "Build a demo robot face",
            ]
        )
        assert code == 0

        target = tmp_path / "demo"
        for name in STATE_FILES:
            assert (target / name).is_file(), f"missing {name}"
        assert (target / "docs" / "README.md").is_file()

        assert StateManager(target).validate() == []
        memory = (target / config.MEMORY_FILE).read_text()
        assert "Build a demo robot face" in memory
        assert memory.startswith("# PROJECT_MEMORY")

    def test_init_refuses_existing_directory_without_force(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["init", "demo", "--dest", str(tmp_path)]) == 0
        assert main(["init", "demo", "--dest", str(tmp_path)]) == 2
        assert "already exists" in capsys.readouterr().err

    def test_init_force_overwrites(self, tmp_path: Path) -> None:
        assert main(["init", "demo", "--dest", str(tmp_path)]) == 0
        assert (
            main(["init", "demo", "--dest", str(tmp_path), "--force", "--goal", "new"])
            == 0
        )
        memory = (tmp_path / "demo" / config.MEMORY_FILE).read_text()
        assert "new" in memory

    def test_init_requires_name(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["init"])
        assert excinfo.value.code == 2


class TestTasksCommand:
    def test_tasks_prints_dependency_graph(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = build_test_project(tmp_path / "graph-project")
        assert main(["--project", str(project), "tasks"]) == 0
        out = capsys.readouterr().out
        assert "TASK DEPENDENCY GRAPH" in out
        for task_id in ("TASK-001", "TASK-002", "TASK-003", "TASK-004"):
            assert task_id in out
        assert "Ready now: TASK-002" in out
        assert "TOTAL 4" in out

    def test_tasks_on_empty_project(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "empty", "--dest", str(tmp_path)])
        # init seeds a starter graph (G3); clear it to exercise the empty branch
        state = StateManager(tmp_path / "empty")
        state.save_tasks_document(
            {
                "tasks": [],
                "parallel_groups": [],
                "critical_path": {"path": []},
                "summary": {"total": 0},
            }
        )
        assert main(["--project", str(tmp_path / "empty"), "tasks"]) == 0
        assert "No tasks defined." in capsys.readouterr().out

    def test_tasks_requires_project(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["tasks"])
        assert excinfo.value.code == 2
        assert "--project is required" in capsys.readouterr().err


class TestBinEntrypoint:
    def test_bin_orchestrator_is_executable_and_runs(self) -> None:
        assert BIN.is_file()
        assert os.access(BIN, os.X_OK), "bin/orchestrator must be executable"
        first_line = BIN.read_text().splitlines()[0]
        assert first_line.startswith("#!")

        completed = subprocess.run(
            [str(BIN), "--version"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert "orchestrator 2.0.0" in completed.stdout

    def test_bin_orchestrator_init_roundtrip(self, tmp_path: Path) -> None:
        completed = subprocess.run(
            [str(BIN), "init", "bin-demo", "--dest", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert (tmp_path / "bin-demo" / config.PROJECT_FILE).is_file()
        assert (tmp_path / "bin-demo" / config.TASKS_FILE).is_file()

        tasks_run = subprocess.run(
            [str(BIN), "--project", str(tmp_path / "bin-demo"), "tasks"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert tasks_run.returncode == 0, tasks_run.stderr
        # G3: init always produces a graph (starter skeleton without an LLM)
        assert "TASK-001" in tasks_run.stdout
        assert "TOTAL 5" in tasks_run.stdout


class TestPyproject:
    def test_pyproject_declares_entrypoint_and_dependencies(self) -> None:
        pyproject_path = REPO_ROOT / "pyproject.toml"
        assert pyproject_path.is_file()
        with pyproject_path.open("rb") as handle:
            data = tomllib.load(handle)

        project = data["project"]
        assert project["name"] == "agentic-ai-orchestrator"
        assert project["version"] == "2.0.0"
        assert project["scripts"]["orchestrator"] == "orchestrator.cli:main"
        assert any(dep.startswith("PyYAML") for dep in project["dependencies"])

        packages = data["tool"]["setuptools"]["packages"]["find"]["include"]
        assert "orchestrator*" in packages


# ---------------------------------------------------------------------------
# CLI integration (moved from test_orchestrator_pipeline.py)
# ---------------------------------------------------------------------------

REPO_ROOT_CLI = REPO_ROOT
LIVE_PROJECT = REPO_ROOT / "projects" / "kid-robot-face"


class TestCLI:
    @pytest.fixture(autouse=True)
    def _run_from_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

    def test_status_command(self, test_project: Path, capsys: pytest.CaptureFixture) -> None:
        exit_code = cli.main(["--project", str(test_project), "status"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "PROJECT: test-project" in captured.out
        assert "READY" in captured.out

    def test_run_command_with_task_flag(self, test_project: Path, capsys: pytest.CaptureFixture) -> None:
        exit_code = cli.main(["--project", str(test_project), "run", "--task", "TASK-002"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "[OK] TASK-002" in captured.out
        assert "SUPERVISOR HEALTH REPORT" in captured.out
        assert StateManager(test_project).get_task("TASK-002")["status"] == config.TASK_DONE

    def test_run_command_missing_task(self, test_project: Path, capsys: pytest.CaptureFixture) -> None:
        exit_code = cli.main(["--project", str(test_project), "run", "--task", "TASK-999"])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "ERROR" in captured.out

    def test_checkpoint_save_list_restore(
        self, tmp_path: Path, test_project: Path, capsys: pytest.CaptureFixture
    ) -> None:
        save_code = cli.main(
            [
                "--project",
                str(test_project),
                "checkpoint",
                "save",
                "--checkpoint",
                "cp-cli-001",
                "--notes",
                "cli saved",
            ]
        )
        assert save_code == 0
        capsys.readouterr()

        list_code = cli.main(["--project", str(test_project), "checkpoint", "list"])
        listed = capsys.readouterr().out
        assert list_code == 0
        assert "cp-cli-001" in listed

        StateManager(test_project).update_task_status("TASK-002", "CANCELLED")
        restore_code = cli.main(
            ["--project", str(test_project), "checkpoint", "restore", "--checkpoint", "cp-cli-001"]
        )
        restored = capsys.readouterr().out
        assert restore_code == 0
        assert "restored" in restored
        assert StateManager(test_project).get_task("TASK-002")["status"] == "READY"

    def test_checkpoint_restore_missing_id(self, test_project: Path, capsys: pytest.CaptureFixture) -> None:
        exit_code = cli.main(
            ["--project", str(test_project), "checkpoint", "save", "--checkpoint", "cp-x"]
        )
        assert exit_code == 0
        capsys.readouterr()
        bad = cli.main(
            ["--project", str(test_project), "checkpoint", "restore", "--checkpoint", "cp-missing"]
        )
        captured = capsys.readouterr()
        assert bad == 2
        assert "ERROR" in captured.out

    def test_agents_command(self, capsys: pytest.CaptureFixture) -> None:
        exit_code = cli.main(["--project", str(LIVE_PROJECT), "agents"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "requirements_agent" in captured.out

    def test_missing_project_flag_prints_help(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit):
            cli.main(["status"])
        captured = capsys.readouterr()
        assert "--project is required" in str(captured.out) + str(captured.err)

    def test_no_command_prints_help(self, capsys: pytest.CaptureFixture) -> None:
        exit_code = cli.main(["--project", str(LIVE_PROJECT)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "usage:" in captured.out

    def test_health_command_exit_codes(self, test_project: Path, capsys: pytest.CaptureFixture) -> None:
        exit_code = cli.main(["--project", str(test_project), "health"])
        captured = capsys.readouterr()
        assert exit_code in (0, 3, 4)
        assert "SUPERVISOR HEALTH REPORT" in captured.out


class TestRetryCommand:
    """Human recovery: `retry` clears loop counters so a refused task runs again."""

    @pytest.fixture(autouse=True)
    def _run_from_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

    def test_retry_clears_loop_limit_and_allows_dispatch(
        self, test_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = StateManager(test_project)
        state.update_task_execution(
            "TASK-002",
            set_values={
                "attempts_since_change": 3,
                "no_progress_cycles": 5,
                "last_error": "boom",
            },
        )
        # Loop-limited dispatch refuses the task and prints the recovery hint.
        assert cli.main(["--project", str(test_project), "run", "--task", "TASK-002"]) == 3
        refused = capsys.readouterr().out
        assert "LOOP LIMIT" in refused
        assert "orchestrator retry TASK-002" in refused

        # retry resets the counters, records history, and re-dispatch works.
        code = cli.main(
            ["--project", str(test_project), "retry", "TASK-002", "--reason", "inputs fixed"]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "READY" in out
        task = StateManager(test_project).get_task("TASK-002")
        assert task["execution"]["attempts_since_change"] == 0
        assert task["execution"]["no_progress_cycles"] == 0
        assert task["execution"]["last_error"] is None
        assert task["status"] == config.TASK_READY
        changelog = (test_project / config.CHANGELOG_FILE).read_text(encoding="utf-8")
        assert "retry TASK-002" in changelog

        assert cli.main(["--project", str(test_project), "run", "--task", "TASK-002"]) == 0
        assert StateManager(test_project).get_task("TASK-002")["status"] == config.TASK_DONE

    def test_retry_unknown_task_exits_2(
        self, test_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--project", str(test_project), "retry", "TASK-999"]) == 2
        assert "ERROR" in capsys.readouterr().out

    def test_retry_rejects_terminal_task(
        self, test_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # TASK-001 is DONE in the fixture.
        assert cli.main(["--project", str(test_project), "retry", "TASK-001"]) == 2
        out = capsys.readouterr().out
        assert "ERROR" in out
        assert "DONE" in out

    def test_retry_keeps_dependency_blocked_task_blocked(
        self, test_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # TASK-003 is BLOCKED with unmet deps (TASK-002 not finished): counters
        # reset, but the status must not jump to READY.
        state = StateManager(test_project)
        state.update_task_execution("TASK-003", set_values={"attempts_since_change": 3})
        assert cli.main(["--project", str(test_project), "retry", "TASK-003"]) == 0
        out = capsys.readouterr().out
        task = StateManager(test_project).get_task("TASK-003")
        assert task["execution"]["attempts_since_change"] == 0
        assert task["status"] == config.TASK_BLOCKED
        assert "BLOCKED" in out

    def test_retry_promotes_agent_blocked_task_when_deps_met(
        self, test_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An agent-refusal leaves a task BLOCKED even though dependencies are
        # met: retry must promote it back to READY via refresh.
        state = StateManager(test_project)
        state.update_task_status("TASK-002", config.TASK_DONE)
        state.update_task_status("TASK-003", config.TASK_BLOCKED)
        state.update_task_execution("TASK-003", set_values={"attempts_since_change": 3})
        assert cli.main(["--project", str(test_project), "retry", "TASK-003"]) == 0
        out = capsys.readouterr().out
        task = StateManager(test_project).get_task("TASK-003")
        assert task["status"] == config.TASK_READY
        assert "READY" in out

    def test_cycle_run_refusal_prints_retry_hint(
        self, test_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        StateManager(test_project).update_task_execution(
            "TASK-002", set_values={"attempts_since_change": 3}
        )
        code = cli.main(["--project", str(test_project), "run", "--max-tasks", "4"])
        out = capsys.readouterr().out
        assert code != 0
        assert "orchestrator retry TASK-002" in out


# ---------------------------------------------------------------------------
# Phase G — config keys
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# .env configuration (Ollama backend)
# ---------------------------------------------------------------------------

DOTENV_SAMPLE = """# comment line
export OLLAMA_BASE_URL="http://192.168.0.200:11434"
ORCHESTRATOR_LLM_MODEL='gemma4:12b'
ORCHESTRATOR_LLM_PROVIDER=ollama

no_equals_line
"""


class TestEnvFileLoading:
    def test_load_env_file_parses_quotes_export_and_comments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text(DOTENV_SAMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        loaded = config.load_env_file()
        assert loaded == 3
        assert os.environ["OLLAMA_BASE_URL"] == "http://192.168.0.200:11434"
        assert os.environ["ORCHESTRATOR_LLM_MODEL"] == "gemma4:12b"
        assert os.environ["ORCHESTRATOR_LLM_PROVIDER"] == "ollama"
        for var in ("OLLAMA_BASE_URL", "ORCHESTRATOR_LLM_MODEL", "ORCHESTRATOR_LLM_PROVIDER"):
            monkeypatch.delenv(var, raising=False)

    def test_existing_environment_always_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text(
            "OLLAMA_BASE_URL=http://from-file\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://from-shell")
        assert config.load_env_file() == 0
        assert os.environ["OLLAMA_BASE_URL"] == "http://from-shell"

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert config.load_env_file(tmp_path / "nope.env") == 0

    def test_maybe_load_skipped_while_pytest_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text("DOTENV_MARKER=1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert config.maybe_load_env_file() == 0
        assert "DOTENV_MARKER" not in os.environ

    def test_maybe_load_runs_outside_tests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text("DOTENV_MARKER=1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert config.maybe_load_env_file() == 1
        assert os.environ.get("DOTENV_MARKER") == "1"
        monkeypatch.delenv("DOTENV_MARKER", raising=False)

    def test_cli_main_auto_loads_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text("OLLAMA_BASE_URL=http://192.168.0.200:11434\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert main([]) == 1  # no subcommand -> help, exit 1
        assert os.environ.get("OLLAMA_BASE_URL") == "http://192.168.0.200:11434"
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    def test_env_example_documents_project_backend(self) -> None:
        example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "OLLAMA_BASE_URL=http://192.168.0.200:11434" in example
        assert "ORCHESTRATOR_LLM_MODEL=gemma4:12b" in example
        assert "ORCHESTRATOR_LLM_PROVIDER=ollama" in example
