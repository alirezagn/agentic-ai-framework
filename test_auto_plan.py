"""Tests for goal-driven task-graph generation (G1-G7).

Covers the starter-task skeleton, the planning-agent graph build, and the
CLI wiring in ``init`` / ``plan`` / ``run``. Everything stays offline:
LLM responses come from ``FakeLLMClient`` and ``LLMClient.is_available``
is monkeypatched wherever the CLI would otherwise discover a backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from conftest import FakeLLMClient
from orchestrator.agents.base_agent import create_agent
from orchestrator.cli import main
from orchestrator.llm_client import LLMClient
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateError, StateManager

REPO_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_project(tmp_path: Path, name: str = "plan-proj") -> Path:
    """Scaffold an empty project (offline starter skeleton) and clear it."""
    assert main(["init", name, "--dest", str(tmp_path), "--no-plan"]) == 0
    project = tmp_path / name
    StateManager(project).save_tasks_document(
        {
            "tasks": [],
            "parallel_groups": [],
            "critical_path": {"path": []},
            "summary": {"total": 0},
        }
    )
    return project


def plan_response(tasks: List[Dict[str, Any]]) -> str:
    return json.dumps(
        {
            "status": "completed",
            "summary": "initial task graph generated",
            "data": {"tasks": tasks},
        }
    )


SAMPLE_TASKS = [
    {
        "title": "Capture requirements",
        "owner": "requirements_agent",
        "priority": "CRITICAL",
        "dependencies": [],
        "expected_outputs": ["docs/REQUIREMENTS.md"],
        "acceptance_criteria": ["REQ-001..005 measurable"],
    },
    {
        "title": "Design the architecture",
        "owner": "architecture_agent",
        "priority": "CRITICAL",
        "dependencies": [0],
        "expected_outputs": ["docs/ARCHITECTURE.md"],
        "acceptance_criteria": ["Every requirement mapped"],
    },
    {
        "title": "Implement the core",
        "owner": "software_agent",
        "priority": "HIGH",
        "dependencies": [1],
        "expected_outputs": ["docs/IMPLEMENTATION.md"],
        "acceptance_criteria": ["Architecture followed"],
    },
    {
        "title": "Test the system",
        "owner": "test_agent",
        "priority": "HIGH",
        "dependencies": [1],
        "expected_outputs": ["docs/TEST_REPORT.md"],
        "acceptance_criteria": ["All REQs covered"],
    },
]


def fake_resolver(responses: List[Any]) -> Callable[[str, Any], Any]:
    """Agent resolver whose planning agent answers from ``responses``."""
    client = FakeLLMClient(responses)

    def resolve(owner: str, state: StateManager) -> Any:
        agent = create_agent(owner, state_manager=state)
        agent.use_client(client)  # type: ignore[attr-defined]
        return agent

    return resolve


def force_backend(
    monkeypatch: pytest.MonkeyPatch, available: bool
) -> None:
    monkeypatch.setattr(
        LLMClient, "is_available", staticmethod(lambda: available)
    )


# ---------------------------------------------------------------------------
# G1 — deterministic starter skeleton
# ---------------------------------------------------------------------------


class TestStarterSkeleton:
    def test_seed_starter_tasks_creates_valid_graph(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        state = StateManager(project)
        created = state.seed_starter_tasks("Build a sensor hub")
        assert created == [
            "TASK-001",
            "TASK-002",
            "TASK-003",
            "TASK-004",
            "TASK-005",
        ]
        assert state.validate() == []
        tasks = state.load_tasks()
        assert len(tasks) == 5
        assert "sensor hub" in tasks[0]["title"]
        assert tasks[0]["status"] == "READY"
        assert all(task["status"] == "BLOCKED" for task in tasks[1:])
        assert tasks[4]["dependencies"] == ["TASK-003"]
        assert all(task["owner"] for task in tasks)

    def test_seed_starter_tasks_is_idempotent(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        state = StateManager(project)
        first = state.seed_starter_tasks("goal")
        second = state.seed_starter_tasks("other goal")
        assert len(first) == 5
        assert second == []
        assert len(state.load_tasks()) == 5

    def test_seed_starter_tasks_tolerates_missing_goal(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        state = StateManager(project)
        created = state.seed_starter_tasks("")
        assert len(created) == 5
        assert "project goal" in state.load_tasks()[0]["title"]


# ---------------------------------------------------------------------------
# G2 — MasterOrchestrator.build_plan
# ---------------------------------------------------------------------------


class TestBuildPlan:
    def test_build_plan_creates_goal_derived_graph(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(SAMPLE_TASKS)])
        )
        created = orch.build_plan(goal="Build a robot face")
        assert created == [f"TASK-00{i}" for i in range(1, 5)]
        state = orch.state
        assert state.validate() == []
        tasks = state.load_tasks()
        assert len(tasks) == 4
        assert tasks[0]["status"] == "READY"
        assert tasks[1]["dependencies"] == ["TASK-001"]
        assert tasks[3]["dependencies"] == ["TASK-002"]
        assert "Build a robot face" not in tasks[0]["id"]

    def test_build_plan_refuses_existing_graph_without_force(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        StateManager(project).seed_starter_tasks("goal")
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(SAMPLE_TASKS)])
        )
        with pytest.raises(StateError, match="already has"):
            orch.build_plan()

    def test_build_plan_force_replaces_graph(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        StateManager(project).seed_starter_tasks("goal")
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(SAMPLE_TASKS)])
        )
        created = orch.build_plan(force=True)
        assert created == [f"TASK-00{i}" for i in range(1, 5)]
        assert len(orch.state.load_tasks()) == 4
        assert orch.state.validate() == []

    def test_build_plan_force_keeps_graph_when_llm_fails(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        StateManager(project).seed_starter_tasks("goal")
        orch = MasterOrchestrator(
            project,
            agent_resolver=fake_resolver([RuntimeError("backend down")]),
        )
        with pytest.raises(StateError, match="planning agent failed"):
            orch.build_plan(force=True)
        assert len(orch.state.load_tasks()) == 5

    def test_build_plan_resolves_index_and_title_dependencies(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        tasks = [
            {
                "title": "First step",
                "owner": "requirements_agent",
                "dependencies": [],
            },
            {
                "title": "Second step",
                "owner": "architecture_agent",
                "dependencies": [0],
            },
            {
                "title": "Third step",
                "owner": "test_agent",
                "dependencies": ["First step"],
            },
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        created = orch.build_plan()
        assert created == ["TASK-001", "TASK-002", "TASK-003"]
        loaded = {task["id"]: task for task in orch.state.load_tasks()}
        assert loaded["TASK-002"]["dependencies"] == ["TASK-001"]
        assert loaded["TASK-003"]["dependencies"] == ["TASK-001"]

    def test_build_plan_maps_unknown_owner_to_software_agent(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        tasks = [
            {"title": "Good task", "owner": "requirements_agent", "dependencies": []},
            {
                "title": "Invented owner",
                "owner": "manager_agent",
                "dependencies": [0],
            },
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        created = orch.build_plan()
        assert created == ["TASK-001", "TASK-002"]
        loaded = {task["id"]: task for task in orch.state.load_tasks()}
        assert loaded["TASK-002"]["owner"] == "software_agent"
        assert orch.state.validate() == []

    def test_build_plan_drops_forward_dependencies(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        tasks = [
            {
                "title": "Premature",
                "owner": "requirements_agent",
                "dependencies": [2],
            },
            {"title": "Independent", "owner": "research_agent", "dependencies": []},
            {
                "title": "Third",
                "owner": "test_agent",
                "dependencies": [1],
            },
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        created = orch.build_plan()
        assert created == ["TASK-001", "TASK-002", "TASK-003"]
        loaded = {task["id"]: task for task in orch.state.load_tasks()}
        # forward ref (index 2 -> TASK-003, not yet appended) dropped
        assert loaded["TASK-001"]["dependencies"] == []
        assert loaded["TASK-003"]["dependencies"] == ["TASK-002"]

    def test_build_plan_skips_ownerless_task(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        tasks = [
            {"title": "No owner", "owner": "", "dependencies": []},
            {"title": "Kept", "owner": "test_agent", "dependencies": []},
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        created = orch.build_plan()
        assert created == ["TASK-002"]
        assert orch.state.validate() == []

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("requirements_agent", "requirements_agent"),
            ("QA_agent", "test_agent"),
            ("kernel_dev_agent", "software_agent"),
            ("hardware_audit_agent", "hardware_agent"),
            ("fs_agent", "software_agent"),
            ("documentation_specialist", "documentation_agent"),
            ("architecture_guru", "architecture_agent"),
            ("", None),
        ],
    )
    def test_resolve_plan_owner(self, given: str, expected: str) -> None:
        assert MasterOrchestrator._resolve_plan_owner(given) == expected

    def test_build_plan_rejects_missing_tasks_payload(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        response = json.dumps({"status": "completed", "summary": "no graph"})
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([response])
        )
        with pytest.raises(StateError, match="no tasks"):
            orch.build_plan()

    def test_build_plan_rejects_unparseable_output(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver(["this is not JSON"])
        )
        with pytest.raises(StateError, match="planning agent failed"):
            orch.build_plan()

    def test_build_plan_strips_injected_status_fields(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        tasks = [
            {
                "title": "Sneaky",
                "owner": "requirements_agent",
                "dependencies": [],
                "status": "DONE",
                "execution": {"attempt_count": 9},
                "id": "TASK-999",
            },
            {
                "title": "Dependent",
                "owner": "test_agent",
                "dependencies": ["TASK-999"],
            },
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        created = orch.build_plan()
        assert created == ["TASK-001", "TASK-002"]
        loaded = {task["id"]: task for task in orch.state.load_tasks()}
        assert loaded["TASK-001"]["status"] in ("TODO", "READY")
        # injected execution payload replaced by runtime defaults
        assert loaded["TASK-001"]["execution"]["attempt_count"] == 0

    def test_build_plan_respects_max_tasks(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(SAMPLE_TASKS)])
        )
        created = orch.build_plan(max_tasks=2)
        assert created == ["TASK-001", "TASK-002"]

    def test_plan_context_files_exclude_state_files(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        (project / "docs" / "ARCHITECTURE.md").write_text("# arch\n")
        files = MasterOrchestrator(project)._plan_context_files()
        assert "docs/ARCHITECTURE.md" in files
        assert "PROJECT_MEMORY.md" not in files
        assert "CHANGELOG.md" not in files


# ---------------------------------------------------------------------------
# G9 — planned tasks carry execution context (input_files / notes)
# ---------------------------------------------------------------------------


class TestPlanInputFiles:
    def test_model_input_files_validated_and_kept(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "PRD.md").write_text("# prd\n", encoding="utf-8")
        tasks = [
            dict(
                SAMPLE_TASKS[0],
                input_files=["docs/PRD.md", "missing.md", "/etc/passwd", "../x.md", 7],
                notes="read the PRD first",
            )
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        orch.build_plan(goal="g")
        loaded = orch.state.load_tasks()
        assert loaded[0]["input_files"] == ["docs/PRD.md"]
        assert loaded[0]["notes"] == "read the PRD first"

    def test_fallback_context_when_model_omits_input_files(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "PRD.md").write_text("# prd\n", encoding="utf-8")
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(SAMPLE_TASKS)])
        )
        orch.build_plan(goal="g")
        for task in orch.state.load_tasks():
            assert task.get("input_files"), task["id"]
            assert "docs/PRD.md" in task["input_files"]
            for rel in task["input_files"]:
                assert (project / rel).is_file()

    def test_fallback_when_all_model_paths_are_invalid(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "PRD.md").write_text("# prd\n", encoding="utf-8")
        tasks = [dict(SAMPLE_TASKS[0], input_files=["/etc/passwd", "../x.md"])]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        orch.build_plan(goal="g")
        loaded = orch.state.load_tasks()
        assert "docs/PRD.md" in loaded[0]["input_files"]
        for rel in loaded[0]["input_files"]:
            assert (project / rel).is_file()

    def test_plan_source_index_lists_real_sources(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        (project / "kernel").mkdir(exist_ok=True)
        (project / "kernel" / "kernel.c").write_text("int main(void) {}\n")
        index = MasterOrchestrator(project)._plan_source_index()
        assert "kernel/kernel.c" in index


# ---------------------------------------------------------------------------
# G11 — producer wiring (input_files another task will produce)
# ---------------------------------------------------------------------------


class TestPlanProducerWiring:
    def test_review_without_deps_waits_for_report_producer(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "PRD.md").write_text("# prd\n", encoding="utf-8")
        tasks = [
            {
                "title": "Write the gap report",
                "owner": "planning_agent",
                "priority": "CRITICAL",
                "dependencies": [],
                "expected_outputs": ["docs/GAP_ANALYSIS.md"],
                "input_files": ["docs/PRD.md"],
            },
            {
                "title": "Review the gap report",
                "owner": "review_agent",
                "priority": "LOW",
                "dependencies": [],  # model forgot the edge
                "input_files": ["docs/GAP_ANALYSIS.md"],
            },
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        orch.build_plan(goal="g")
        loaded = orch.state.load_tasks()
        producer = next(t for t in loaded if t["owner"] == "planning_agent")
        review = next(t for t in loaded if t["owner"] == "review_agent")
        assert producer["id"] in review["dependencies"]
        # the report is dropped by plan-time existence filtering — wiring
        # restores it so the reviewer actually reads it at execution time
        assert "docs/GAP_ANALYSIS.md" in review["input_files"]

    def test_forward_producer_edge_reorders_append_order(
        self, tmp_path: Path
    ) -> None:
        project = make_project(tmp_path)
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "PRD.md").write_text("# prd\n", encoding="utf-8")
        tasks = [
            {  # review appears BEFORE its producer in the model array
                "title": "Review the report",
                "owner": "review_agent",
                "priority": "LOW",
                "dependencies": [],
                "input_files": ["docs/GAP_ANALYSIS.md"],
            },
            {
                "title": "Write the report",
                "owner": "planning_agent",
                "priority": "CRITICAL",
                "dependencies": [],
                "expected_outputs": ["docs/GAP_ANALYSIS.md"],
                "input_files": ["docs/PRD.md"],
            },
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        orch.build_plan(goal="g")
        loaded = orch.state.load_tasks()
        producer = next(t for t in loaded if t["owner"] == "planning_agent")
        review = next(t for t in loaded if t["owner"] == "review_agent")
        assert producer["id"] in review["dependencies"]
        ids = [t["id"] for t in loaded]
        # append_task rejects forward references: producer must be first
        assert ids.index(producer["id"]) < ids.index(review["id"])

    def test_mutual_artifacts_do_not_create_a_cycle(self, tmp_path: Path) -> None:
        project = make_project(tmp_path)
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "PRD.md").write_text("# prd\n", encoding="utf-8")
        tasks = [
            {
                "title": "Task A",
                "owner": "software_agent",
                "priority": "HIGH",
                "dependencies": [],
                "expected_outputs": ["docs/A.md"],
                "input_files": ["docs/B.md"],
            },
            {
                "title": "Task B",
                "owner": "software_agent",
                "priority": "HIGH",
                "dependencies": [],
                "expected_outputs": ["docs/B.md"],
                "input_files": ["docs/A.md"],
            },
        ]
        orch = MasterOrchestrator(
            project, agent_resolver=fake_resolver([plan_response(tasks)])
        )
        orch.build_plan(goal="g")
        loaded = orch.state.load_tasks()
        a = next(t for t in loaded if t["title"] == "Task A")
        b = next(t for t in loaded if t["title"] == "Task B")
        # one direction only — no A↔B 2-cycle
        assert not (b["id"] in a["dependencies"] and a["id"] in b["dependencies"])


# ---------------------------------------------------------------------------
# G3 — init wiring
# ---------------------------------------------------------------------------


class TestInitWiring:
    def test_init_no_plan_flag_writes_starter_graph(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "init",
                "demo",
                "--dest",
                str(tmp_path),
                "--no-plan",
                "--goal",
                "Build a demo",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "starter task graph: 5 tasks" in out
        state = StateManager(tmp_path / "demo")
        assert len(state.load_tasks()) == 5
        assert state.validate() == []
        memory = state.load_memory()
        assert "Task graph generated: 5 tasks" in memory

    def test_init_uses_starter_graph_without_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, False)
        assert main(["init", "demo", "--dest", str(tmp_path)]) == 0
        assert "starter task graph: 5 tasks" in capsys.readouterr().out
        assert len(StateManager(tmp_path / "demo").load_tasks()) == 5

    def test_init_invokes_llm_plan_when_backend_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, True)
        calls: List[Dict[str, Any]] = []

        def fake_build_plan(self, goal=None, max_tasks=8, force=False):
            calls.append({"goal": goal, "force": force})
            return self.state.seed_starter_tasks(goal or "")

        monkeypatch.setattr(MasterOrchestrator, "build_plan", fake_build_plan)
        code = main(["init", "demo", "--dest", str(tmp_path), "--goal", "G"])
        assert code == 0
        assert calls == [{"goal": "G", "force": False}]
        out = capsys.readouterr().out
        assert "generated 5 tasks" in out
        assert len(StateManager(tmp_path / "demo").load_tasks()) == 5

    def test_init_falls_back_to_starter_when_plan_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, True)

        def broken_plan(self, goal=None, max_tasks=8, force=False):
            raise StateError("planning exploded")

        monkeypatch.setattr(MasterOrchestrator, "build_plan", broken_plan)
        code = main(["init", "demo", "--dest", str(tmp_path)])
        assert code == 0
        captured = capsys.readouterr()
        assert "writing starter tasks" in captured.err
        assert len(StateManager(tmp_path / "demo").load_tasks()) == 5


# ---------------------------------------------------------------------------
# G4 — plan subcommand
# ---------------------------------------------------------------------------


class TestPlanCommand:
    def test_plan_seeds_starter_without_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, False)
        project = make_project(tmp_path)
        code = main(["--project", str(project), "plan", "--goal", "Ship it"])
        assert code == 0
        out = capsys.readouterr().out
        assert "Starter task graph written (5 tasks)" in out
        assert len(StateManager(project).load_tasks()) == 5

    def test_plan_without_backend_errors_on_existing_graph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, False)
        project = make_project(tmp_path)
        StateManager(project).seed_starter_tasks("goal")
        code = main(["--project", str(project), "plan"])
        assert code == 2
        assert "already has tasks" in capsys.readouterr().err
        assert len(StateManager(project).load_tasks()) == 5

    def test_plan_passes_flags_to_build_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, True)
        captured: Dict[str, Any] = {}

        def fake_build_plan(self, goal=None, max_tasks=8, force=False):
            captured.update(goal=goal, max_tasks=max_tasks, force=force)
            return self.state.seed_starter_tasks(goal or "")

        monkeypatch.setattr(MasterOrchestrator, "build_plan", fake_build_plan)
        project = make_project(tmp_path)
        code = main(
            [
                "--project",
                str(project),
                "plan",
                "--goal",
                "Fresh goal",
                "--force",
                "--max-tasks",
                "5",
            ]
        )
        assert code == 0
        assert captured == {"goal": "Fresh goal", "max_tasks": 5, "force": True}
        assert "Generated 5 tasks" in capsys.readouterr().out

    def test_plan_reports_build_plan_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, True)

        def broken_plan(self, goal=None, max_tasks=8, force=False):
            raise StateError("graph already has 3 task(s)")

        monkeypatch.setattr(MasterOrchestrator, "build_plan", broken_plan)
        project = make_project(tmp_path)
        StateManager(project).seed_starter_tasks("g")
        code = main(["--project", str(project), "plan"])
        assert code == 2
        assert "ERROR: graph already has" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# G5 — run on an empty graph
# ---------------------------------------------------------------------------


class TestRunEmptyGraph:
    def test_run_empty_graph_without_backend_prints_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, False)
        project = make_project(tmp_path)
        code = main(["--project", str(project), "run"])
        assert code == 0
        out = capsys.readouterr().out
        assert "Task graph is empty" in out
        assert "orchestrator plan" in out
        assert "No READY tasks available." in out

    def test_run_empty_graph_reports_autoplan_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, True)

        def broken_plan(self, goal=None, max_tasks=8, force=False):
            raise StateError("model unavailable")

        monkeypatch.setattr(MasterOrchestrator, "build_plan", broken_plan)
        project = make_project(tmp_path)
        code = main(["--project", str(project), "run"])
        assert code == 0
        out = capsys.readouterr().out
        assert "auto-plan failed: model unavailable" in out

    def test_run_empty_graph_generates_then_dispatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        force_backend(monkeypatch, True)
        monkeypatch.setattr(
            MasterOrchestrator,
            "build_plan",
            lambda self, goal=None, max_tasks=8, force=False: (
                self.state.seed_starter_tasks(goal or "")
            ),
        )
        project = make_project(tmp_path)
        code = main(["--project", str(project), "run", "--max-tasks", "1"])
        assert code == 0
        out = capsys.readouterr().out
        assert "generated 5 tasks: TASK-001" in out
        # run consumed the generated graph: the READY root dispatched and
        # completed offline (requirements agent falls back without an LLM).
        assert "[OK] TASK-001 (requirements_agent)" in out
        tasks = StateManager(project).load_tasks()
        assert tasks[0]["status"] == "DONE"
        assert tasks[1]["status"] == "READY"


# ---------------------------------------------------------------------------
# G6 — doc guards for the plan command
# ---------------------------------------------------------------------------


class TestPlanDocs:
    @pytest.fixture()
    def guide(self) -> str:
        return (REPO_ROOT / "ORCHESTRATOR_GUIDE.md").read_text(encoding="utf-8")

    def test_guide_has_plan_row(self, guide: str) -> None:
        assert "| `plan [" in guide

    def test_guide_documents_init_no_plan_flag(self, guide: str) -> None:
        assert "--no-plan" in guide

    def test_how_to_use_documents_plan_command(self) -> None:
        text = (REPO_ROOT / "HOW_TO_USE.md").read_text(encoding="utf-8")
        assert "plan --force" in text
        assert "--no-plan" in text

    def test_framework_05_documents_data_tasks_contract(self) -> None:
        text = (REPO_ROOT / "framework" / "05_PLANNING_AGENT.md").read_text(
            encoding="utf-8"
        )
        assert "data.tasks" in text
        assert "0-based indices" in text
