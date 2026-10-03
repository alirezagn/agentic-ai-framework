"""Architecture redesign verification (three requirements, one batch).

1. **Truncation recovery** — ``LLMAgent`` re-assembles a reply the output
   token cap cut mid-flight (chunked JSON parsing) and stages half-written
   file bodies in ``data.edit_buffers`` instead of silently dropping or
   applying them. The network truncation-repair stays the fallback for
   replies that carry nothing to salvage.
2. **PEP 668 deploys** — ``pip install`` into an externally managed
   environment gets ``--break-system-packages``; an install whose
   requirements are already satisfied in the running environment is skipped
   with a ``status=skipped`` record instead of re-running pip. The
   Definition of Done accepts a plan whose only deploy records are skips —
   but never lets a skip back a claim that tests passed. Since the operator
   directive of 2026-10-03 the flag append sits behind the install policy:
   every unsatisfied install is refused (``install_refusal_reason``) after
   the skip, so it is unreachable while the policy stands; the PEP 668
   helpers remain unit-tested here.
3. **Goal-driven decomposition** — a GUI or multi-module goal may not ship
   as one "implement everything" task: ``orchestrator.auto_plan`` expands it
   into the stage chain (data layer -> canvas/interfaces -> launcher) in
   both the planning contract and the deterministic starter skeleton.

Everything runs offline: fake LLM clients, fake executables inside the
project, no network, no real package installs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from conftest import FakeLLMClient
from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, create_agent
from orchestrator.agents.llm_agent import EDIT_BUFFER_KEY, LLMAgent
from orchestrator.agents.requirements_agent import RequirementsAgent
from orchestrator.auto_plan import (
    GUI_STAGES,
    MULTI_MODULE_STAGES,
    PLANNING_RULES,
    expand_implementation_stages,
    implementation_join_index,
    scope_of,
    should_decompose,
)
from orchestrator.deploy_runner import (
    STATUS_REFUSED,
    STATUS_SKIPPED,
    DeployRecord,
    DeployRunner,
    is_pep668_managed,
    is_pip_install,
    pep668_marker_candidates,
    pip_requirements_satisfied,
    pip_targets_running_environment,
)
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dummy_agent(project: Path, client: FakeLLMClient) -> LLMAgent:
    class Dummy(LLMAgent):
        AGENT_ID = "dummy_agent"

    return Dummy(project_path=project, llm_client=client)


def _delivery_task(project: Path) -> Dict[str, Any]:
    agent = RequirementsAgent(project_path=project)
    task = dict(agent.state_manager.get_task("TASK-002"))
    task["expected_outputs"] = ["target.txt"]
    return task


def _make_fake_executable(directory: Path, name: str, body: str = '#!/bin/sh\necho "$@"\n') -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _empty_state(project: Path) -> StateManager:
    state = StateManager(project)
    state.save_tasks_document(
        {"tasks": [], "parallel_groups": [], "critical_path": {"path": []}, "summary": {"total": 0}}
    )
    return state


def _seedable_project(root: Path) -> Path:
    """A fully-formed project with an empty task graph (starter seeding ground)."""
    from conftest import build_test_project

    project = build_test_project(root)
    _empty_state(project)
    return project


def _starter_spec(title: str, owner: str, outputs: List[str]) -> Dict[str, Any]:
    return {
        "title": title,
        "owner": owner,
        "dependencies": [],
        "expected_outputs": outputs,
        "acceptance_criteria": [f"{title} is complete"],
    }


# ---------------------------------------------------------------------------
# Requirement 1 — truncation recovery / chunked JSON / partial edit buffers
# ---------------------------------------------------------------------------


class TestTruncationRecovery:
    """A token-capped reply is salvaged locally, not re-requested blindly."""

    CUT_INSIDE_EDIT = (
        '{"status": "completed", "summary": "patched target.txt", '
        '"data": {"edits": {"target.txt": {"search": "BETA", "replace": "BE'
    )
    MISSING_CLOSERS = (
        '{"status": "completed", "summary": "patched target.txt", '
        '"data": {"edits": {"target.txt": {"search": "BETA", "replace": "BETA13"}}'
    )
    NO_CONTENT = '{"status": "completed", "summary": "half a reply that never closes'
    FENCED_BODY = (
        "Wrote src/app.py:\n\n```python\ndef main():\n    print('hel"
    )

    def test_cut_inside_an_edit_is_staged_as_a_buffer(
        self, test_project: Path
    ) -> None:
        """Recovery must not apply half an edit — it blocks with the partial."""
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        client = FakeLLMClient([self.CUT_INSIDE_EDIT])  # no repair reply: 1 call only
        agent = _dummy_agent(test_project, client)

        output = agent.run(_delivery_task(test_project))

        assert output.status == config.AGENT_STATUS_BLOCKED, output.errors
        assert len(client.calls) == 1, "local recovery must not spend a model call"
        buffers = output.data[EDIT_BUFFER_KEY]
        assert buffers["target.txt"]["truncated"] is True
        assert buffers["target.txt"]["field"] == "replace"
        assert buffers["target.txt"]["text"] == "BE"
        # The incomplete edit must never touch the workspace.
        assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
        assert "truncated" in output.summary

    def test_recovered_complete_delivery_is_applied(
        self, test_project: Path
    ) -> None:
        """Only the closers were lost: parse, apply, and stay completed."""
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        client = FakeLLMClient([self.MISSING_CLOSERS])
        agent = _dummy_agent(test_project, client)

        output = agent.run(_delivery_task(test_project))

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert len(client.calls) == 1
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        assert EDIT_BUFFER_KEY not in output.data
        assert any("chunked parse" in item for item in output.warnings)

    def test_reply_without_content_falls_back_to_network_repair(
        self, test_project: Path
    ) -> None:
        """No delivery to salvage -> the existing repair call still runs."""
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        repair = {
            "status": "completed",
            "summary": "patched target.txt",
            "data": {"edits": {"target.txt": {"search": "BETA", "replace": "BETA13"}}},
        }
        client = FakeLLMClient([self.NO_CONTENT, json.dumps(repair)])
        agent = _dummy_agent(test_project, client)

        output = agent.run(_delivery_task(test_project))

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert len(client.calls) == 2, "unparseable-with-no-content must repair"
        assert any("truncation-repair" in item for item in output.warnings)
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"

    def test_fenced_file_body_is_staged_as_a_buffer(
        self, test_project: Path
    ) -> None:
        """Prose + a half-written fence: the body is recovered, not lost."""
        client = FakeLLMClient([self.FENCED_BODY])
        agent = _dummy_agent(test_project, client)

        output = agent.run(_delivery_task(test_project))

        assert output.status == config.AGENT_STATUS_BLOCKED, output.errors
        assert len(client.calls) == 1
        buffers = output.data[EDIT_BUFFER_KEY]
        assert buffers, output.data
        buffer = next(iter(buffers.values()))
        assert "def main():" in buffer["text"]
        assert buffer["truncated"] is True
        assert buffer["source"] == "fence"

    @pytest.mark.parametrize(
        ("raw", "expect_content"),
        [
            # cut inside a file body -> content plus a staged buffer
            (
                '{"status":"completed","summary":"s","data":{"edits":'
                '{"a.py":{"search":"x","replace":"half of a lon',
                True,
            ),
            # a fenced JSON document survives prose braces around it
            (
                "Note the {brace} syntax.\n```json\n"
                '{"status":"completed","summary":"s","data":{"documents":'
                '{"NEW.md":{"content":"hi',
                True,
            ),
            # summary-only truncation carries no delivery
            ('{"status":"completed","summary":"half a reply', False),
            # a plain prose refusal is not JSON at all
            ("I could not complete this task.", False),
        ],
    )
    def test_recover_truncated_payload_contract(
        self, raw: str, expect_content: bool
    ) -> None:
        from orchestrator.agents.llm_agent import recover_truncated_payload

        recovery = recover_truncated_payload(raw)
        if not expect_content:
            assert recovery is None
            return
        assert recovery is not None
        assert recovery.has_delivery is True


# ---------------------------------------------------------------------------
# Requirement 2 — PEP 668 handling in the deploy channel
# ---------------------------------------------------------------------------


class TestPep668InstallHandling:
    @pytest.fixture()
    def fake_pip_project(self, tmp_path: Path) -> Path:
        project = tmp_path / "proj"
        project.mkdir()
        _make_fake_executable(project, "pip")
        return project

    @staticmethod
    def _runner(project: Path, **kwargs: Any) -> DeployRunner:
        return DeployRunner(
            project,
            enabled=True,
            allowlist=("pip",),
            **kwargs,
        )

    def test_install_is_refused_whatever_the_pep668_state(
        self, fake_pip_project: Path
    ) -> None:
        """Policy refusal precedes the flag append: no environment is touched."""
        record = self._runner(fake_pip_project, externally_managed=True).run_one(
            {"command": "./pip", "args": ["install", "some-uninstalled-pkg"]}
        )
        assert record.status == STATUS_REFUSED
        assert record.executed is False
        assert "never installs" in record.reason

    def test_unmanaged_install_is_refused_too(self, fake_pip_project: Path) -> None:
        """Not externally managed changes nothing: installing is the policy line."""
        record = self._runner(fake_pip_project, externally_managed=False).run_one(
            {"command": "./pip", "args": ["install", "some-uninstalled-pkg"]}
        )
        assert record.status == STATUS_REFUSED
        assert record.executed is False
        assert "--break-system-packages" not in record.args

    def test_flag_bearing_install_never_spawns_pip(
        self, fake_pip_project: Path
    ) -> None:
        record = self._runner(fake_pip_project, externally_managed=True).run_one(
            {
                "command": "./pip",
                "args": ["install", "--break-system-packages", "some-uninstalled-pkg"],
            }
        )
        assert record.status == STATUS_REFUSED
        assert record.executed is False

    def test_non_pip_commands_never_get_the_flag(
        self, fake_pip_project: Path
    ) -> None:
        _make_fake_executable(fake_pip_project, "ctest")
        runner = DeployRunner(
            fake_pip_project, enabled=True, allowlist=("ctest",), externally_managed=True
        )
        record = runner.run_one({"command": "./ctest", "args": ["--version"]})
        assert record.executed is True
        assert record.args == ["--version"]

    def test_marker_file_is_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "env"
        (root / "bin").mkdir(parents=True)
        pip = _make_fake_executable(root / "bin", "pip")
        assert is_pep668_managed(str(pip)) is False
        assert any(
            candidate.name == "EXTERNALLY-MANAGED"
            for candidate in pep668_marker_candidates(str(pip))
        )
        (root / "lib" / "python3.12").mkdir(parents=True)
        (root / "lib" / "python3.12" / "EXTERNALLY-MANAGED").write_text(
            "", encoding="utf-8"
        )
        assert is_pep668_managed(str(pip)) is True

    def test_detection_uses_the_target_not_the_running_interpreter(
        self, tmp_path: Path
    ) -> None:
        """A venv pip is unmanaged even when the host interpreter is managed."""
        root = tmp_path / "venv"
        (root / "bin").mkdir(parents=True)
        pip = _make_fake_executable(root / "bin", "pip")
        assert pip_targets_running_environment(str(pip)) is False
        assert pip_targets_running_environment(sys.executable) is True
        assert pip_targets_running_environment(None) is True


class TestRedundantInstallSkip:
    def test_satisfied_requirements_skip_the_process(
        self, test_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`pyyaml` is this project's own dependency: nothing to install.

        The interpreter is reached through a PATH symlink so the runner
        resolves to the *running* environment — the precondition for
        trusting ``importlib.metadata`` as proof of satisfaction.
        """
        bindir = tmp_path / "bin"
        bindir.mkdir()
        name = os.path.basename(sys.executable) or "python"
        (bindir / name).symlink_to(sys.executable)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        runner = DeployRunner(
            test_project,
            enabled=True,
            allowlist=(name,),
        )

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("pip must not be spawned for a satisfied install")

        monkeypatch.setattr("orchestrator.deploy_runner.subprocess.run", explode)
        record = runner.run_one(
            {"command": name, "args": ["-m", "pip", "install", "pyyaml"]}
        )

        assert record.status == STATUS_SKIPPED
        assert record.executed is False
        assert record.exit_code is None
        assert "already satisfied" in record.reason
        assert "pyyaml" in record.reason

    @pytest.mark.parametrize(
        "args",
        [
            ["-m", "pip", "install", "definitely-not-a-real-package-xyz"],
            ["-m", "pip", "install", "-U", "pyyaml"],
            ["-m", "pip", "install", "--force-reinstall", "pyyaml"],
            ["-m", "pip", "install", "pyyaml>=6"],
            ["-m", "pip", "install", "pyyaml==9.9.9"],
            ["-m", "pip", "install"],
        ],
    )
    def test_unprovable_installs_are_not_skipped(self, args: List[str]) -> None:
        assert pip_requirements_satisfied(sys.executable, args) is None

    def test_verifiable_install_is_reported_as_satisfied(self) -> None:
        reason = pip_requirements_satisfied(
            sys.executable, ["-m", "pip", "install", "pyyaml"]
        )
        assert reason is not None and "pyyaml" in reason

    def test_only_installs_are_examined(self) -> None:
        assert is_pip_install(sys.executable, ["-m", "pip", "install", "x"]) is True
        assert is_pip_install(sys.executable, ["-m", "pytest"]) is False
        assert is_pip_install(sys.executable, ["--version"]) is False
        assert pip_requirements_satisfied(sys.executable, ["-m", "pip", "list"]) is None

    def test_requirement_file_inside_the_project_is_read(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "requirements.txt").write_text(
            "# deps\npyyaml\n\ndefinitely-not-a-real-package-xyz\n", encoding="utf-8"
        )
        reason = pip_requirements_satisfied(
            sys.executable,
            ["-m", "pip", "install", "-r", "requirements.txt"],
            project_path=project,
        )
        assert reason is None  # one missing package -> the install must run

        (project / "requirements.txt").write_text("pyyaml\n", encoding="utf-8")
        reason = pip_requirements_satisfied(
            sys.executable,
            ["-m", "pip", "install", "-r", "requirements.txt"],
            project_path=project,
        )
        assert reason is not None and "pyyaml" in reason

    def test_requirement_file_outside_the_project_is_refused(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside-requirements.txt"
        outside.write_text("pyyaml\n", encoding="utf-8")
        reason = pip_requirements_satisfied(
            sys.executable,
            ["-m", "pip", "install", "-r", str(outside)],
            project_path=project,
        )
        assert reason is None


class TestSkippedRecordsInDefinitionOfDone:
    @staticmethod
    def _output(summary: str, declares: bool = True) -> AgentOutput:
        data: Dict[str, Any] = {}
        if declares:
            data["deploy"] = [{"command": "pip", "args": ["install", "pyyaml"]}]
        return AgentOutput(
            agent_id="software_agent",
            task_id="TASK-001",
            status=config.AGENT_STATUS_COMPLETED,
            summary=summary,
            data=data,
        )

    @staticmethod
    def _skipped() -> List[DeployRecord]:
        return [
            DeployRecord(
                command="pip",
                args=["install", "pyyaml"],
                executed=False,
                status=STATUS_SKIPPED,
                reason="requirements already satisfied: pyyaml (installed 6.0.1)",
            )
        ]

    def test_only_skips_pass_the_evidence_check(self, test_project: Path) -> None:
        orch = MasterOrchestrator(test_project)
        task = {
            "id": "TASK-001",
            "title": "Install the runtime dependencies",
            "owner": "software_agent",
            "expected_outputs": ["docs/SETUP.md"],
        }
        output = self._output("requirements were already present in the environment")
        assert orch._evidence_problems(task, output, self._skipped()) == []

    def test_a_skip_never_backs_a_passing_run_claim(
        self, test_project: Path
    ) -> None:
        orch = MasterOrchestrator(test_project)
        task = {
            "id": "TASK-001",
            "title": "Verify the build",
            "owner": "test_agent",
            "expected_outputs": ["docs/SETUP.md"],
        }
        output = self._output("pytest ran: 42/42 tests passed")
        problems = orch._evidence_problems(task, output, self._skipped())
        assert problems, "a skipped install must not certify an execution claim"

    def test_skips_do_not_excuse_test_like_outputs(self, test_project: Path) -> None:
        orch = MasterOrchestrator(test_project)
        task = {
            "id": "TASK-001",
            "title": "Run the suite",
            "owner": "test_agent",
            "expected_outputs": ["docs/test_results.log"],
        }
        output = self._output("requirements were already present in the environment")
        problems = orch._evidence_problems(task, output, self._skipped())
        assert problems, "a skip is not ground truth for a test artifact"


# ---------------------------------------------------------------------------
# Requirement 3 — GUI / multi-module decomposition into atomic stages
# ---------------------------------------------------------------------------


class TestScopeDetection:
    @pytest.mark.parametrize(
        "goal",
        [
            "Build a desktop GUI dashboard",
            "make a tkinter interface for the sensor hub",
            "Ship a graphical canvas editor",
            "Build a multi-module python package",
            "Assemble a plugin-based architecture",
        ],
    )
    def test_decomposition_triggers(self, goal: str) -> None:
        assert should_decompose(goal) is True

    @pytest.mark.parametrize(
        "goal",
        [
            "Build a demo robot face",
            "Ship the demo",
            "build a widget",
            "Build a sensor hub",
            "TBD — define the one-sentence project goal",
        ],
    )
    def test_decomposition_does_not_trigger(self, goal: str) -> None:
        assert should_decompose(goal) is False

    def test_gui_wins_over_multi_module(self) -> None:
        assert scope_of("a multi-module GUI") == "gui"
        assert scope_of("a multi-module package") == "multi-module"


class TestStageChains:
    def test_stage_titles_are_the_documented_contract(self) -> None:
        assert [stage.title for stage in GUI_STAGES] == [
            "Backend Data Layer",
            "UI Canvas Components",
            "Application Launcher",
        ]
        assert [stage.title for stage in MULTI_MODULE_STAGES] == [
            "Backend Data Layer",
            "Module Interface Layer",
            "Application Launcher",
        ]

    def test_planning_rules_name_every_stage(self) -> None:
        for title in (
            "Backend Data Layer",
            "UI Canvas Components",
            "Application Launcher",
            "Module Interface Layer",
        ):
            assert title in PLANNING_RULES
        assert "software_agent" in PLANNING_RULES

    @pytest.mark.parametrize(
        ("goal", "expected"),
        [
            ("Build a tkinter dashboard", ["Backend Data Layer", "UI Canvas Components", "Application Launcher"]),
            (
                "Build a multi-module package",
                ["Backend Data Layer", "Module Interface Layer", "Application Launcher"],
            ),
        ],
    )
    def test_expansion_splits_the_implementation_task(
        self, goal: str, expected: List[str]
    ) -> None:
        specs = [
            _starter_spec("Capture requirements", "requirements_agent", ["REQ.md"]),
            {
                "title": "Implement the planned work",
                "owner": "software_agent",
                "dependencies": ["TASK-001"],
                "expected_outputs": ["IMPLEMENTATION.md"],
                "acceptance_criteria": ["architecture followed"],
            },
            _starter_spec("Test and validate", "test_agent", ["TEST_REPORT.md"]),
        ]
        specs[0]["id"] = "TASK-001"
        specs[1]["id"] = "TASK-002"
        specs[2]["id"] = "TASK-003"
        # The test task waited on the implementation task; it must now wait
        # for the whole stage chain, i.e. the launcher at the end of it.
        specs[2]["dependencies"] = ["TASK-002"]

        expanded = expand_implementation_stages(specs, goal=goal)

        titles = [spec["title"] for spec in expanded]
        assert titles == ["Capture requirements", *expected, "Test and validate"]
        ids = [spec["id"] for spec in expanded]
        assert ids == [f"TASK-{index + 1:03d}" for index in range(len(expanded))]
        # Chain: requirements -> data layer -> middle stage -> launcher -> test
        assert expanded[1]["dependencies"] == ["TASK-001"]
        assert expanded[2]["dependencies"] == ["TASK-002"]
        assert expanded[3]["dependencies"] == ["TASK-003"]
        assert expanded[4]["dependencies"] == ["TASK-004"]
        # The original deliverable moved to the launcher, not disappeared.
        assert expanded[3]["expected_outputs"] == ["IMPLEMENTATION.md"]
        assert expanded[3]["acceptance_criteria"][0] == "architecture followed"

    def test_expansion_is_idempotent(self) -> None:
        specs = [
            {
                "title": "Implement the planned work",
                "owner": "software_agent",
                "dependencies": [],
                "expected_outputs": ["IMPLEMENTATION.md"],
                "acceptance_criteria": ["a"],
            },
        ]
        once = expand_implementation_stages(specs, goal="Build a GUI")
        assert [spec["title"] for spec in once] == [
            "Backend Data Layer",
            "UI Canvas Components",
            "Application Launcher",
        ]
        twice = expand_implementation_stages(once, goal="Build a GUI")
        assert twice == once

    def test_no_implementation_task_is_never_invented(self) -> None:
        specs = [
            _starter_spec("Capture requirements", "requirements_agent", ["REQ.md"]),
            _starter_spec("Review the design", "review_agent", ["REVIEW.md"]),
        ]
        assert expand_implementation_stages(specs, goal="Build a GUI") == specs

    def test_plain_goal_leaves_the_plan_alone(self) -> None:
        specs = [
            {
                "title": "Implement the planned work",
                "owner": "software_agent",
                "dependencies": [],
                "expected_outputs": ["IMPLEMENTATION.md"],
                "acceptance_criteria": ["a"],
            },
        ]
        assert expand_implementation_stages(specs, goal="Build a sensor hub") == specs

    def test_join_index_tracks_the_last_implementation_stage(self) -> None:
        plain = [
            {"owner": "requirements_agent"},
            {"owner": "architecture_agent"},
            {"owner": "software_agent"},
            {"owner": "test_agent"},
        ]
        assert implementation_join_index(plain) == 2
        staged = [
            {"owner": "requirements_agent"},
            {"owner": "architecture_agent"},
            {"owner": "software_agent"},
            {"owner": "software_agent"},
            {"owner": "software_agent"},
            {"owner": "test_agent"},
        ]
        assert implementation_join_index(staged) == 4
        assert implementation_join_index([{"owner": "test_agent"}]) == 2


class TestStarterSkeletonDecomposition:
    def test_gui_goal_seeds_the_stage_chain(self, tmp_path: Path) -> None:
        state = _empty_state(_seedable_project(tmp_path / "proj"))

        created = state.seed_starter_tasks("Build a desktop GUI dashboard")

        assert len(created) == 7
        tasks = state.load_tasks()
        titles = [task["title"] for task in tasks]
        assert titles[2:] == [
            "Backend Data Layer",
            "UI Canvas Components",
            "Application Launcher",
            "Test and validate the implementation",
            "Update documentation and prepare release notes",
        ]
        # Sequential chain up to the launcher; test/docs wait on the launcher.
        for index in range(1, 5):
            assert tasks[index]["dependencies"] == [created[index - 1]]
        assert tasks[5]["dependencies"] == [created[4]]
        assert tasks[6]["dependencies"] == [created[4]]
        assert all(task["owner"] == "software_agent" for task in tasks[2:5])
        assert tasks[4]["expected_outputs"] == ["IMPLEMENTATION.md"]

    def test_multi_module_goal_uses_the_interface_stage(self, tmp_path: Path) -> None:
        state = _empty_state(_seedable_project(tmp_path / "proj"))

        created = state.seed_starter_tasks("Build a python package of plugins")

        titles = [task["title"] for task in state.load_tasks()]
        assert len(created) == 7
        assert "Module Interface Layer" in titles
        assert "UI Canvas Components" not in titles

    def test_plain_goal_keeps_the_five_task_skeleton(self, tmp_path: Path) -> None:
        state = _empty_state(_seedable_project(tmp_path / "proj"))

        created = state.seed_starter_tasks("Build a sensor hub")

        assert len(created) == 5
        tasks = state.load_tasks()
        assert tasks[3]["dependencies"] == [created[2]]
        assert tasks[4]["dependencies"] == [created[2]]


class TestPlanningContractDecomposition:
    @staticmethod
    def _plan_tasks() -> List[Dict[str, Any]]:
        return [
            {
                "title": "Capture requirements",
                "owner": "requirements_agent",
                "priority": "CRITICAL",
                "dependencies": [],
                "expected_outputs": ["docs/REQUIREMENTS.md"],
                "acceptance_criteria": ["REQ-001.. measurable"],
            },
            {
                "title": "Design the architecture",
                "owner": "architecture_agent",
                "priority": "CRITICAL",
                "dependencies": [0],
                "expected_outputs": ["docs/ARCHITECTURE.md"],
                "acceptance_criteria": ["every requirement mapped"],
            },
            {
                "title": "Implement the dashboard application",
                "owner": "software_agent",
                "priority": "HIGH",
                "dependencies": [1],
                "expected_outputs": ["docs/IMPLEMENTATION.md"],
                "acceptance_criteria": ["architecture followed"],
            },
            {
                "title": "Test the system",
                "owner": "test_agent",
                "priority": "HIGH",
                "dependencies": [2],
                "expected_outputs": ["docs/TEST_REPORT.md"],
                "acceptance_criteria": ["all REQs covered"],
            },
        ]

    @staticmethod
    def _planner(project: Path, captured: Dict[str, Any]) -> Callable[..., Any]:
        client = FakeLLMClient(
            [json.dumps({"status": "completed", "summary": "plan", "data": {"tasks": TestPlanningContractDecomposition._plan_tasks()}})]
        )

        def resolve(owner: str, state: StateManager) -> Any:
            agent = create_agent(owner, state_manager=state)
            agent.use_client(client)  # type: ignore[attr-defined]
            original_run = agent.run

            def run(payload: Dict[str, Any], **kwargs: Any) -> AgentOutput:
                captured.update(payload)
                return original_run(payload, **kwargs)

            agent.run = run  # type: ignore[method-assign]
            return agent

        return resolve

    def test_planner_contract_carries_the_decomposition_rules(
        self, tmp_path: Path
    ) -> None:
        from test_auto_plan import make_project

        captured: Dict[str, Any] = {}
        project = make_project(tmp_path)
        orch = MasterOrchestrator(project, agent_resolver=self._planner(project, captured))
        orch.build_plan(goal="Build a desktop GUI dashboard")
        notes = str(captured.get("notes") or "")
        assert "Decomposition rules" in notes
        assert "Backend Data Layer" in notes
        assert "UI Canvas Components" in notes

    def test_build_plan_expands_a_single_implementation_task(
        self, tmp_path: Path
    ) -> None:
        from test_auto_plan import make_project

        project = make_project(tmp_path)
        orch = MasterOrchestrator(
            project, agent_resolver=self._planner(project, {})
        )

        created = orch.build_plan(goal="Build a desktop GUI dashboard")

        assert len(created) == 6
        tasks = orch.state.load_tasks()
        titles = [task["title"] for task in tasks]
        assert titles[2:5] == [
            "Backend Data Layer",
            "UI Canvas Components",
            "Application Launcher",
        ]
        assert tasks[5]["dependencies"] == [created[4]]
        assert all(task["owner"] == "software_agent" for task in tasks[2:5])
        # the launcher inherited the original deliverable and criteria
        assert tasks[4]["expected_outputs"] == ["docs/IMPLEMENTATION.md"]
        assert tasks[4]["acceptance_criteria"][-1]
