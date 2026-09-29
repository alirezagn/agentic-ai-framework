"""Tests for extended loop detection (GAP_ANALYSIS task T15, finding 11).

Covers the three missing detectors from framework/00_MASTER_ORCHESTRATOR
lines 92-108:

* state oscillation (``LOOP_KIND_STATE_OSCILLATION`` is now produced),
* ``data.strategy_changed`` from agents advancing the alternative-strategy
  counter (``alternatives_exhausted`` becomes reachable in production),
* repeated substantially identical outputs (new ``repeated_output`` kind).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from conftest import build_test_project
from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateManager
from orchestrator.supervisor import SupervisorAgent


class FailingAgent(BaseAgent):
    """Fails identically on every attempt (repeated-output scenario)."""

    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.failed(task_id, "model backend down", errors=["connection refused"])


class StrategyChangerAgent(BaseAgent):
    """Succeeds while reporting a materially different strategy each time."""

    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task = payload["task"]
        task_id = str(task["id"])
        attempt = ((task.get("execution") or {}).get("attempt_count") or 0) + 1
        return self.completed(
            task_id,
            f"attempt {attempt} used strategy variant {attempt}",
            data={"strategy_changed": True, "variant": attempt},
        )


class BlockedAgent(BaseAgent):
    """Always reports BLOCKED, so refresh/dispatch flip the state every cycle."""

    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.blocked(task_id, "waiting on hardware", errors=["no sensor"])


def make_orchestrator(project: Path, checkpoints_root: Path, agent_cls: type) -> MasterOrchestrator:
    def resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
        return agent_cls(state_manager=state_manager)

    return MasterOrchestrator(
        project,
        checkpoints_root=checkpoints_root,
        auto_checkpoint=False,
        agent_resolver=resolver,
    )


def detection_kinds(orchestrator: MasterOrchestrator) -> List[str]:
    return [
        loop.kind
        for loop in orchestrator.supervisor.detect_loops()
        if loop.exceeded
    ]


class TestRepeatedOutputDetection:
    def test_identical_failures_count_and_detect(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, FailingAgent)
        # Neutralise the same-strategy counter so the new kind is observable.
        orchestrator.supervisor.thresholds = config.loop_thresholds(
            {"same_strategy_max_attempts": 99, "no_progress_max_cycles": 99}
        )

        orchestrator.dispatch("TASK-002")
        execution = orchestrator.get_task("TASK-002")["execution"]
        assert execution["repeated_output_count"] == 1
        assert execution["last_output_hash"]
        assert detection_kinds(orchestrator) == []

        orchestrator.dispatch("TASK-002")
        assert orchestrator.get_task("TASK-002")["execution"]["repeated_output_count"] == 2
        assert detection_kinds(orchestrator) == []

        orchestrator.dispatch("TASK-002")
        assert orchestrator.get_task("TASK-002")["execution"]["repeated_output_count"] == 3
        assert config.LOOP_KIND_REPEATED_OUTPUT in detection_kinds(orchestrator)

    def test_repeated_output_blocks_next_dispatch(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, FailingAgent)
        orchestrator.supervisor.thresholds = config.loop_thresholds(
            {"same_strategy_max_attempts": 99, "no_progress_max_cycles": 99}
        )

        for _ in range(3):
            orchestrator.dispatch("TASK-002")

        from orchestrator.orchestrator import LoopLimitExceededError
        import pytest

        with pytest.raises(LoopLimitExceededError) as excinfo:
            orchestrator.dispatch("TASK-002")
        assert excinfo.value.loop is not None
        assert excinfo.value.loop.kind == config.LOOP_KIND_REPEATED_OUTPUT

    def test_changing_outputs_never_count_as_repeats(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, StrategyChangerAgent)

        for _ in range(3):
            orchestrator.dispatch("TASK-002")

        execution = orchestrator.get_task("TASK-002")["execution"]
        assert execution["repeated_output_count"] == 1
        assert config.LOOP_KIND_REPEATED_OUTPUT not in detection_kinds(orchestrator)


class TestStrategyChangedWiring:
    def test_agent_flag_increments_strategy_counter(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, StrategyChangerAgent)

        orchestrator.dispatch("TASK-002")
        execution = orchestrator.get_task("TASK-002")["execution"]
        assert execution["strategy_changes"] == 1
        assert execution["repeated_output_count"] == 1  # outputs differed

    def test_alternatives_exhausted_reachable_in_production(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, StrategyChangerAgent)

        for _ in range(3):
            orchestrator.dispatch("TASK-002")

        execution = orchestrator.get_task("TASK-002")["execution"]
        assert execution["strategy_changes"] >= 2
        assert execution["attempt_count"] >= 3

        # detect_loops skips terminal tasks: reopen the finished task the way
        # a rework cycle would, and the counters must produce the detection.
        state = StateManager(project)
        doc = state.load_tasks_document()
        for task in doc["tasks"]:
            if task["id"] == "TASK-002":
                task["status"] = config.TASK_READY
        state.save_tasks_document(doc)

        kinds = detection_kinds(orchestrator)
        assert config.LOOP_KIND_ALTERNATIVES_EXHAUSTED in kinds
        assert orchestrator.supervisor.should_block_dispatch("TASK-002") is not None


class TestStateOscillationDetection:
    def _run_cycles(self, orchestrator: MasterOrchestrator, count: int):
        return [orchestrator.run_cycle() for _ in range(count)]

    def test_blocked_flip_flop_is_detected_and_gates_dispatch(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, BlockedAgent)

        self._run_cycles(orchestrator, 2)
        # Two cycles recorded four state changes (READY <-> BLOCKED).
        detected = orchestrator._detect_oscillation()
        assert detected is not None
        assert detected.kind == config.LOOP_KIND_STATE_OSCILLATION

        third = orchestrator.run_cycle()
        assert third and not third[0].succeeded
        assert orchestrator._detect_oscillation() is not None
        loop = third[0].loop
        assert loop is not None
        assert loop.kind == config.LOOP_KIND_STATE_OSCILLATION
        assert "oscillating" in third[0].output.summary
        # The gate refuses before execution: the task is not marked FAILED.
        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_READY

        # The gate persists while the two-state pattern is the last thing seen.
        fourth = orchestrator.run_cycle()
        assert fourth and not fourth[0].succeeded
        assert fourth[0].loop is not None
        assert fourth[0].loop.kind == config.LOOP_KIND_STATE_OSCILLATION

    def test_normal_progress_never_flags_oscillation(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, StrategyChangerAgent)

        for _ in range(4):
            cycles = orchestrator.run_cycle()
            assert orchestrator._detect_oscillation() is None
            for result in cycles:
                assert result.succeeded or not result.output.errors

        assert config.LOOP_KIND_STATE_OSCILLATION not in detection_kinds(orchestrator)

    def test_history_cleared_on_checkpoint_restore(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        orchestrator = make_orchestrator(project, checkpoints_root, BlockedAgent)
        self._run_cycles(orchestrator, 3)
        assert orchestrator._detect_oscillation() is not None

        orchestrator.create_checkpoint("cp-osc-reset", notes="clear the loop")
        orchestrator.resume_from_checkpoint("cp-osc-reset")

        assert orchestrator._fingerprint_history == []
        assert orchestrator._detect_oscillation() is None


class TestLoopThresholds:
    def test_identical_output_threshold_round_trips(self) -> None:
        thresholds = config.loop_thresholds({"identical_output_max_repeats": 5})
        assert thresholds.identical_output_max_repeats == 5
        assert thresholds.as_dict()["identical_output_max_repeats"] == 5
        restored = config.LoopThresholds.from_dict(thresholds.as_dict())
        assert restored.identical_output_max_repeats == 5

    def test_repeated_output_kind_registered(self) -> None:
        assert config.LOOP_KIND_REPEATED_OUTPUT in config.LOOP_KINDS

    def test_supervisor_detects_repeated_output_from_state(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "loop-project")
        state = StateManager(project)
        doc = state.load_tasks_document()
        for task in doc["tasks"]:
            if task["id"] == "TASK-002":
                task["status"] = config.TASK_READY
                task["execution"]["repeated_output_count"] = 3
        state.save_tasks_document(doc)

        supervisor = SupervisorAgent(state_manager=StateManager(project))
        kinds = [loop.kind for loop in supervisor.detect_loops() if loop.exceeded]
        assert config.LOOP_KIND_REPEATED_OUTPUT in kinds
