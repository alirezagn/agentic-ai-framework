"""Tests for derived-state recompute (GAP_ANALYSIS task T17, finding 17).

``StateManager.recompute_derived_state`` keeps TASKS.yaml
``summary``/``parallel_groups``/``critical_path`` and PROJECT.yaml
``progress``/``agents``/``next_tasks`` in sync with the actual task graph,
per framework/10_DOCUMENTATION_AGENT.md ("status files reflect actual task
state").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from conftest import build_test_project
from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateManager, derive_agent_status


class DoneAgent(BaseAgent):
    """Completes whatever task it is given."""

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(task_id, "done")


def _write_tasks(project: Path, document: Dict[str, Any]) -> None:
    import yaml

    (project / config.TASKS_FILE).write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


class TestRecomputeDerivedState:
    def test_summary_matches_actual_status_counts(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "derived")
        state = StateManager(project)

        document = state.load_tasks_document()
        document["summary"] = {"total_tasks": 99, "status_breakdown": {"DONE": 99}}
        _write_tasks(project, document)

        state.recompute_derived_state()

        summary = state.load_tasks_document()["summary"]
        assert summary["total_tasks"] == 4
        assert summary["status_breakdown"] == {
            config.TASK_DONE: 1,
            config.TASK_READY: 1,
            config.TASK_BLOCKED: 1,
            config.TASK_TODO: 1,
        }

    def test_critical_path_is_longest_dependency_chain(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "derived")
        state = StateManager(project)
        state.recompute_derived_state()

        critical = state.load_tasks_document()["critical_path"]
        assert critical["path"] == (
            "TASK-001 -> TASK-002 -> TASK-003 -> TASK-004"
        )

    def test_progress_agents_and_next_tasks(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "derived")
        state = StateManager(project)
        result = state.recompute_derived_state()

        assert result["progress"]["requirements"] == 50  # 1 of 2 tasks DONE
        assert result["progress"]["architecture"] == 0
        assert result["progress"]["implementation"] == 0

        agents = result["agents"]
        assert agents["requirements_agent"] == "READY"  # TASK-002 READY
        assert agents["architecture_agent"] == "BLOCKED"
        assert agents["hardware_agent"] == "READY"
        # non-owner keys preserved
        assert agents["orchestrator"] == "ACTIVE"
        assert agents["supervisor"] == "ACTIVE"

        assert result["next_tasks"] == ["TASK-002"]
        project_doc = state.load_project()
        assert project_doc["next_tasks"] == ["TASK-002"]

    def test_parallel_groups_pruned_to_existing_tasks(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "derived")
        state = StateManager(project)

        document = state.load_tasks_document()
        document["parallel_groups"] = [
            {"id": "GROUP-1", "name": "valid", "tasks": ["TASK-002", "TASK-999"]},
            {"id": "GROUP-2", "name": "stale", "tasks": ["TASK-999"]},
        ]
        _write_tasks(project, document)

        state.recompute_derived_state()

        groups = state.load_tasks_document()["parallel_groups"]
        assert groups == [{"id": "GROUP-1", "name": "valid", "tasks": ["TASK-002"]}]

    def test_recompute_is_idempotent(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "derived")
        state = StateManager(project)
        state.recompute_derived_state()

        tasks_path = project / config.TASKS_FILE
        project_path = project / config.PROJECT_FILE
        tasks_mtime = tasks_path.stat().st_mtime_ns
        project_mtime = project_path.stat().st_mtime_ns

        state.recompute_derived_state()

        assert tasks_path.stat().st_mtime_ns == tasks_mtime
        assert project_path.stat().st_mtime_ns == project_mtime

    def test_refresh_ready_states_triggers_recompute(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "derived")
        state = StateManager(project)

        project_doc = state.load_project()
        project_doc["next_tasks"] = ["TASK-999"]
        state.save_project(project_doc)

        state.refresh_ready_states()

        assert state.load_project()["next_tasks"] == ["TASK-002"]

    def test_dispatch_writes_derived_state(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "derived")

        def resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
            return DoneAgent(state_manager=state_manager)

        orchestrator = MasterOrchestrator(
            project, auto_checkpoint=False, agent_resolver=resolver
        )
        orchestrator.dispatch("TASK-002")

        state = StateManager(project)
        summary = state.load_tasks_document()["summary"]
        assert summary["status_breakdown"][config.TASK_DONE] == 2
        # dispatch-phase recompute: requirements workstream fully DONE
        assert state.load_project()["progress"]["requirements"] == 100

        # refresh wiring: TASK-003 unlocks, recompute publishes it
        state.refresh_ready_states()
        assert state.load_project()["next_tasks"] == ["TASK-003"]


class TestDeriveAgentStatus:
    def test_in_progress_wins(self) -> None:
        assert (
            derive_agent_status(
                [{"status": "IN_PROGRESS"}, {"status": "TODO"}, {"status": "DONE"}]
            )
            == "ACTIVE"
        )

    def test_ready_before_blocked(self) -> None:
        assert derive_agent_status([{"status": "READY"}, {"status": "BLOCKED"}]) == "READY"

    def test_blocked(self) -> None:
        assert derive_agent_status([{"status": "BLOCKED"}, {"status": "TODO"}]) == "BLOCKED"

    def test_all_terminal_is_completed(self) -> None:
        assert (
            derive_agent_status(
                [{"status": "DONE"}, {"status": "DONE WITH ACCEPTED LIMITATION"}]
            )
            == "COMPLETED"
        )

    def test_empty(self) -> None:
        assert derive_agent_status([]) == "IDLE"
