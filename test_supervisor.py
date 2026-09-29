"""Supervisor thresholds, loop faults and system-key config.

Split from the former ``test_orchestrator_pipeline.py`` (GAP_ANALYSIS task T20,
finding 20 — roadmap expects ``test_supervisor.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.orchestrator import LoopLimitExceededError, MasterOrchestrator
from orchestrator.state_manager import StateManager
from orchestrator.supervisor import SupervisorAgent

REPO_ROOT = Path(__file__).resolve().parent
LIVE_PROJECT = REPO_ROOT / "projects" / "kid-robot-face"


class FailingAgent(BaseAgent):
    """Agent that always reports failure — used to hit retry limits."""

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str((payload.get("task") or {}).get("id"))
        return self.failed(task_id, "simulated strategy failure", errors=["strategy did not converge"])


class TestLoopFaultThresholds:
    def test_same_strategy_limit_is_three(self, test_project: Path) -> None:
        state = StateManager(test_project)
        state.update_task_execution("TASK-002", attempt_delta=2)
        supervisor = SupervisorAgent(state_manager=state)
        assert supervisor.detect_loops() == []

        state.update_task_execution("TASK-002", attempt_delta=1)
        loops = supervisor.detect_loops()
        assert len(loops) == 1
        assert loops[0].kind == config.LOOP_KIND_SAME_STRATEGY
        assert loops[0].count == 3
        assert loops[0].threshold == 3
        assert loops[0].exceeded is True

    def test_no_progress_limit_is_five(self, test_project: Path) -> None:
        state = StateManager(test_project)
        state.update_task_execution("TASK-002", no_progress_delta=4)
        supervisor = SupervisorAgent(state_manager=state)
        assert all(loop.kind != config.LOOP_KIND_NO_PROGRESS for loop in supervisor.detect_loops())

        state.update_task_execution("TASK-002", no_progress_delta=1)
        loops = [loop for loop in supervisor.detect_loops() if loop.kind == config.LOOP_KIND_NO_PROGRESS]
        assert len(loops) == 1
        assert loops[0].count == 5
        assert loops[0].threshold == 5

    def test_orchestrator_allows_three_retries_then_blocks(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        orchestrator.register_agent(FailingAgent(state_manager=orchestrator.state))

        for attempt in range(1, 4):
            result = orchestrator.run_task("TASK-002")
            assert result.output.status == config.AGENT_STATUS_FAILED
            task = orchestrator.get_task("TASK-002")
            assert task["execution"]["attempt_count"] == attempt
            assert task["execution"]["no_progress_cycles"] == attempt

        blocking = orchestrator.supervisor.should_block_dispatch("TASK-002")
        assert blocking is not None
        assert blocking.kind == config.LOOP_KIND_SAME_STRATEGY

        with pytest.raises(LoopLimitExceededError):
            orchestrator.run_task("TASK-002")

        report = orchestrator.sync_health()
        assert report.loop_detected is True
        assert str(report.state) in (config.HEALTH_STALLED, config.HEALTH_HUMAN_DECISION_REQUIRED)
        loop_task = orchestrator.get_task("TASK-002")
        assert loop_task["execution"]["attempt_count"] == 3

    def test_escalation_created_when_strategy_exhausted(self, test_project: Path) -> None:
        state = StateManager(test_project)
        state.update_task_execution(
            "TASK-002", attempt_delta=3, no_progress_delta=5, strategy_changed=True
        )
        state.update_task_execution("TASK-002", attempt_delta=0, strategy_changed=True)
        supervisor = SupervisorAgent(state_manager=state)
        report = supervisor.check_health()
        kinds = {loop.kind for loop in report.loops}
        assert config.LOOP_KIND_SAME_STRATEGY in kinds
        assert config.LOOP_KIND_NO_PROGRESS in kinds
        assert config.LOOP_KIND_ALTERNATIVES_EXHAUSTED in kinds
        assert report.escalations, "exceeded loops must escalate to a human decision"
        assert str(report.state) == config.HEALTH_HUMAN_DECISION_REQUIRED

    def test_circular_dependency_detection(self, test_project: Path) -> None:
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        by_id = {task["id"]: task for task in document["tasks"]}
        by_id["TASK-002"]["dependencies"] = ["TASK-001", "TASK-004"]
        by_id["TASK-004"]["dependencies"] = ["TASK-003", "TASK-002"]
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

        supervisor = SupervisorAgent(project_path=test_project)
        cycles = supervisor.detect_circular_dependencies()
        assert cycles, "a dependency cycle must be detected"
        flat = {frozenset(cycle) for cycle in cycles}
        assert any({"TASK-002", "TASK-003", "TASK-004"} <= cycle for cycle in flat)

        report = supervisor.check_health()
        assert report.deadlock_detected is True
        assert report.deadlocks
        assert str(report.state) in (config.HEALTH_BLOCKED, config.HEALTH_HUMAN_DECISION_REQUIRED)

    def test_self_loop_detected(self, test_project: Path) -> None:
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        for task in document["tasks"]:
            if task["id"] == "TASK-002":
                task["dependencies"] = ["TASK-002"]
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        supervisor = SupervisorAgent(project_path=test_project)
        cycles = supervisor.detect_circular_dependencies()
        assert any("TASK-002" in cycle for cycle in cycles)

    def test_workspace_project_has_no_deadlock(self) -> None:
        supervisor = SupervisorAgent(project_path=LIVE_PROJECT)
        assert supervisor.detect_circular_dependencies() == []
        report = supervisor.check_health()
        assert str(report.state) != config.HEALTH_BLOCKED

    def test_health_report_render_contains_thresholds(self, test_project: Path) -> None:
        supervisor = SupervisorAgent(project_path=test_project)
        rendered = supervisor.print_health_report()
        assert "SUPERVISOR HEALTH REPORT" in rendered
        assert "compaction at 70%" in rendered
        assert "critical at 85%" in rendered

    def test_supervisor_never_completes_tasks(self, test_project: Path) -> None:
        supervisor = SupervisorAgent(project_path=test_project)
        supervisor.record_attempt("TASK-002", progressed=False, error="boom")
        task = StateManager(test_project).get_task("TASK-002")
        assert task["status"] == "READY", "supervisor must not change task status"
        assert not hasattr(supervisor, "mark_done")


# ---------------------------------------------------------------------------
# Phase D — requirements agent
# ---------------------------------------------------------------------------


class TestConfigSystemKeys:
    def test_system_keys_resolve_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        assert config.SYSTEM_KEYS.resolve("anthropic_api_key") == "sk-test-123"
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        assert config.SYSTEM_KEYS.resolve("anthropic_api_key") is None

    def test_required_key_raises_when_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHECKPOINT_SIGNING_KEY", raising=False)
        required = config.SystemKey(
            key="sample_required", env_var="SAMPLE_REQUIRED", description="d", required=True
        )
        with pytest.raises(config.MissingSystemKeyError):
            required.resolve()
        assert config.SYSTEM_KEYS.names() == sorted(config.SYSTEM_KEYS.names())

    def test_loop_threshold_overrides(self) -> None:
        thresholds = config.loop_thresholds({"same_strategy_max_attempts": 7})
        assert thresholds.same_strategy_max_attempts == 7
        assert thresholds.no_progress_max_cycles == 5
