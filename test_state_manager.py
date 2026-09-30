"""Tests for StateManager and context-utilisation accounting (GAP tasks T9, T10)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator import config
from orchestrator.context_monitor import (
    context_window_tokens,
    payload_chars,
    utilization_for_output,
    utilization_from_chars,
    utilization_from_tokens,
)
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateCorruptedError, StateManager
from orchestrator.supervisor import SupervisorAgent


class TestContextMonitor:
    def test_utilization_math(self) -> None:
        assert utilization_from_tokens(0) == 0.0
        assert utilization_from_tokens(64000) == 50.0
        assert utilization_from_tokens(10**9) == 100.0  # capped
        assert utilization_from_chars(4000) == round(4000 // 4 / 128000 * 100, 1)

    def test_window_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCHESTRATOR_CONTEXT_WINDOW_TOKENS", "1000")
        assert context_window_tokens() == 1000
        assert utilization_from_tokens(700) == 70.0
        monkeypatch.setenv("ORCHESTRATOR_CONTEXT_WINDOW_TOKENS", "nonsense")
        assert context_window_tokens() == 128000

    def test_payload_chars_handles_nested(self) -> None:
        assert payload_chars({"a": [1, 2, 3], "b": "x"}) > 10
        assert payload_chars({}) == 2

    def test_utilization_for_output_prefers_tokens(self) -> None:
        data = {"usage": {"input_tokens": 64000, "output_tokens": 0}}
        assert utilization_for_output(data, payload_char_count=10**9) == 50.0
        # fallback: chars-based estimate
        assert utilization_for_output({}, payload_char_count=4000) == pytest.approx(
            4000 // 4 / 128000 * 100, abs=0.1
        )
        # malformed usage falls back
        assert utilization_for_output({"usage": None}, 4000) == pytest.approx(
            4000 // 4 / 128000 * 100, abs=0.1
        )


class TestUtilizationMeasuredPerDispatch:
    def test_dispatch_writes_measured_utilization(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        before = StateManager(test_project).get_context_utilization()
        orchestrator.run_task("TASK-002")
        after = StateManager(test_project).get_context_utilization()
        assert after != before, "dispatch must rewrite utilisation with a measured value"
        assert 0 <= after < 70
        project = StateManager(test_project).load_project()
        assert isinstance(project["context"]["utilization_percent"], int)

    def test_llm_usage_drives_utilization(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        from conftest import FakeLLMClient
        from orchestrator.agents.llm_agent import LLMAgent

        class Dummy(LLMAgent):
            AGENT_ID = "requirements_agent"

        client = FakeLLMClient(
            [
                '{"agent_id": "requirements_agent", "task_id": "TASK-002",'
                ' "status": "completed", "summary": "ok", "data": {}}'
            ]
        )
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        agent = Dummy(state_manager=orchestrator.state, llm_client=client)
        orchestrator.register_agent(agent)
        orchestrator.run_task("TASK-002")
        # fake client reports 100 input + 20 output tokens of a 128k window -> 0%
        project = StateManager(test_project).load_project()
        assert project["context"]["utilization_percent"] == 0


class TestEscalationPersistence:
    def _trip_loop(self, state: StateManager) -> None:
        # Strategy changed twice first, then the new strategy was hammered
        # three times without progress — trips every loop detector at once.
        state.update_task_execution("TASK-002", strategy_changed=True)
        state.update_task_execution("TASK-002", strategy_changed=True)
        state.update_task_execution("TASK-002", attempt_delta=3, no_progress_delta=5)

    def test_sync_persists_escalations_as_human_decisions(
        self, test_project: Path
    ) -> None:
        state = StateManager(test_project)
        self._trip_loop(state)
        supervisor = SupervisorAgent(state_manager=state)
        report = supervisor.sync_project_health()
        assert report.escalations

        project = state.load_project()
        decisions = project["human_decisions"]
        assert decisions, "escalations must be persisted to PROJECT.yaml"
        assert any("TASK-002" in entry for entry in decisions)
        assert project["health"]["loop_detected"] is True

    def test_repeated_sync_does_not_duplicate_entries(self, test_project: Path) -> None:
        state = StateManager(test_project)
        self._trip_loop(state)
        supervisor = SupervisorAgent(state_manager=state)
        supervisor.sync_project_health()
        first = list(state.load_project()["human_decisions"])
        supervisor.sync_project_health()
        second = state.load_project()["human_decisions"]
        assert second == first

    def test_blockers_reflect_current_blocked_tasks(self, test_project: Path) -> None:
        state = StateManager(test_project)
        supervisor = SupervisorAgent(state_manager=state)
        supervisor.sync_project_health()
        blockers = state.load_project()["blockers"]
        assert any("TASK-003" in item for item in blockers)
        assert any("TASK-004" in item for item in blockers)

        # resolving the blocked tasks clears the live view
        state.update_task_status("TASK-003", "DONE")
        state.update_task_status("TASK-004", "DONE")
        supervisor.sync_project_health()
        assert state.load_project()["blockers"] == []

    def test_deadlock_persisted_as_blocker(self, test_project: Path) -> None:
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        by_id = {task["id"]: task for task in document["tasks"]}
        by_id["TASK-002"]["dependencies"] = ["TASK-001", "TASK-004"]
        by_id["TASK-004"]["dependencies"] = ["TASK-003", "TASK-002"]
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        state = StateManager(test_project)
        SupervisorAgent(state_manager=state).sync_project_health()
        blockers = state.load_project()["blockers"]
        assert any(item.startswith("deadlock:") for item in blockers)


class TestSetBlockers:
    def test_set_blockers_replaces_list(self, test_project: Path) -> None:
        state = StateManager(test_project)
        state.set_blockers(["a", "b"])
        state.set_blockers(["c"])
        assert state.load_project()["blockers"] == ["c"]


# ---------------------------------------------------------------------------
# Workspace / state loading (moved from test_orchestrator_pipeline.py)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
LIVE_PROJECT = REPO_ROOT / "projects" / "kid-robot-face"


class TestStateLoading:
    def test_workspace_project_loads(self) -> None:
        state = StateManager(LIVE_PROJECT)
        project = state.load_project()
        assert project["project"]["name"] == "kid-robot-face"
        assert project["context"]["compaction_threshold"] == 70
        assert project["context"]["critical_threshold"] == 85
        assert project["health"]["status"] in config.HEALTH_STATES

    def test_workspace_tasks_have_ready_requirements_owner(self) -> None:
        state = StateManager(LIVE_PROJECT)
        tasks = state.load_tasks()
        assert len(tasks) >= 4
        ready = state.get_ready_tasks()
        assert ready, "workspace project must expose at least one READY task"
        owners = {str(task.get("owner", "")).lower() for task in ready}
        assert "requirements_agent" in owners

    def test_workspace_memory_and_validation(self) -> None:
        state = StateManager(LIVE_PROJECT)
        memory = state.load_memory()
        assert memory.startswith("# PROJECT_MEMORY")
        assert "REQ-001" in memory
        assert state.validate() == []
        assert state.extract_requirement_ids()[:1] == ["REQ-001"]

    def test_compaction_thresholds_bands(self) -> None:
        thresholds = config.compaction_thresholds()
        assert thresholds.compaction_percent == 70
        assert thresholds.critical_percent == 85
        assert thresholds.classify(35) == "HEALTHY"
        assert thresholds.classify(65) == "WARNING"
        assert thresholds.classify(75) == "COMPACT"
        assert thresholds.classify(90) == "CRITICAL"
        assert thresholds.compaction_required(70) is True
        assert thresholds.compaction_required(69) is False

    def test_dependency_gate_controls_ready_set(self, test_project: Path) -> None:
        state = StateManager(test_project)
        ready_ids = [task["id"] for task in state.get_ready_tasks()]
        assert ready_ids == ["TASK-002"]

        state.update_task_status("TASK-002", "DONE")
        promoted = state.refresh_ready_states()
        assert "TASK-003" in promoted
        ready_ids = [task["id"] for task in state.get_ready_tasks()]
        assert ready_ids == ["TASK-003"]

    def test_blocked_tasks_reported(self, test_project: Path) -> None:
        state = StateManager(test_project)
        blocked_ids = [task["id"] for task in state.get_blocked_tasks()]
        assert "TASK-003" in blocked_ids
        assert "TASK-004" in blocked_ids

    def test_atomic_write_leaves_no_temp_files(self, test_project: Path) -> None:
        state = StateManager(test_project)
        state.update_health(status="WARNING")
        leftovers = [p.name for p in test_project.iterdir() if p.name.startswith(".")]
        assert leftovers == []
        assert state.get_health()["status"] == "WARNING"

    def test_corrupted_yaml_raises_typed_error(self, test_project: Path) -> None:
        (test_project / "PROJECT.yaml").write_text("project: [unterminated", encoding="utf-8")
        with pytest.raises(StateCorruptedError):
            StateManager(test_project).load_project()

    def test_validate_flags_bad_status(self, test_project: Path) -> None:
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        document["tasks"][0]["status"] = "BOGUS"
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        problems = StateManager(test_project).validate()
        assert any("invalid status 'BOGUS'" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Phase B — checkpoint isolation
# ---------------------------------------------------------------------------
