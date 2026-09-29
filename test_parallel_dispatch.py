"""Tests for parallel dispatch (GAP_ANALYSIS task T11).

Covers:
* ``run_cycle(max_concurrent>1)`` actually overlaps agent executions,
* results are returned in READY order regardless of finish order,
* every task-state update survives concurrency (no lost TASKS.yaml writes),
* the default (``max_concurrent=1``) path stays strictly serialized.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict

import yaml

from conftest import PROJECT_TEMPLATE, _task, build_test_project
from orchestrator.agents.base_agent import BaseAgent
from orchestrator.orchestrator import MasterOrchestrator


class ConcurrencyTracker:
    """Shared, thread-safe overlap monitor for parallel dispatch tests."""

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def exit(self) -> None:
        with self.lock:
            self.active -= 1


class SlowAgent(BaseAgent):
    """Test agent that sleeps inside execute() to expose overlap."""

    AGENT_ID = "slow_agent"

    def __init__(self, state_manager: Any, tracker: ConcurrencyTracker) -> None:
        super().__init__(state_manager=state_manager)
        self.tracker = tracker

    def execute(self, payload: Dict[str, Any]) -> Any:
        task_id = str(payload["task"]["id"])
        self.tracker.enter()
        try:
            time.sleep(self.tracker.delay)
        finally:
            self.tracker.exit()
        return self.completed(task_id, f"completed {task_id} in parallel")


def build_parallel_project(root: Path, task_count: int = 4) -> Path:
    """Project with ``task_count`` independent READY tasks (no deps)."""
    build_test_project(root)
    project = dict(PROJECT_TEMPLATE)
    (root / "PROJECT.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    tasks = {
        "tasks": [
            _task(
                f"TASK-P{index:03d}",
                "slow_agent",
                "READY",
                [],
                priority="HIGH",
                notes=f"parallel task {index}",
            )
            for index in range(1, task_count + 1)
        ]
    }
    (root / "TASKS.yaml").write_text(
        yaml.safe_dump(tasks, sort_keys=False), encoding="utf-8"
    )
    return root


def _make_orchestrator(project: Path, tracker: ConcurrencyTracker) -> MasterOrchestrator:
    def resolver(owner: str, state_manager: Any) -> SlowAgent:
        return SlowAgent(state_manager=state_manager, tracker=tracker)

    return MasterOrchestrator(project, agent_resolver=resolver)


READY_ORDER = [f"TASK-P{index:03d}" for index in range(1, 5)]


def test_parallel_dispatch_overlaps_executions(tmp_path: Path) -> None:
    project = build_parallel_project(tmp_path / "parallel-project")
    tracker = ConcurrencyTracker()
    orchestrator = _make_orchestrator(project, tracker)

    results = orchestrator.run_cycle(max_concurrent=3, max_tasks=4)

    assert tracker.max_active >= 2, (
        f"expected overlapping executions, observed max_active={tracker.max_active}"
    )
    assert [result.task_id for result in results] == READY_ORDER
    assert all(result.succeeded for result in results), [
        result.output.errors for result in results if not result.succeeded
    ]

    tasks = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))["tasks"]
    statuses = {task["id"]: task["status"] for task in tasks}
    assert statuses == {task_id: "DONE" for task_id in READY_ORDER}
    for task_id in READY_ORDER:
        assert (project / "docs" / f"{task_id}.md").exists()


def test_parallel_results_keep_ready_order(tmp_path: Path) -> None:
    project = build_parallel_project(tmp_path / "parallel-project")
    # Staggered delays: later tasks finish first, order must not follow finish time.
    tracker = ConcurrencyTracker(delay=0.2)

    orchestrator = _make_orchestrator(project, tracker)
    original_run = SlowAgent.execute
    delays = {"TASK-P001": 0.30, "TASK-P002": 0.20, "TASK-P003": 0.10, "TASK-P004": 0.05}

    def staggered_execute(self: SlowAgent, payload: Dict[str, Any]) -> Any:
        task_id = str(payload["task"]["id"])
        tracker.enter()
        try:
            time.sleep(delays.get(task_id, tracker.delay))
        finally:
            tracker.exit()
        return self.completed(task_id, f"completed {task_id}")

    SlowAgent.execute = staggered_execute  # type: ignore[method-assign]
    try:
        results = orchestrator.run_cycle(max_concurrent=4, max_tasks=4)
    finally:
        SlowAgent.execute = original_run  # type: ignore[method-assign]

    assert [result.task_id for result in results] == READY_ORDER
    assert tracker.max_active >= 2


def test_default_run_cycle_stays_serialized(tmp_path: Path) -> None:
    project = build_parallel_project(tmp_path / "parallel-project")
    tracker = ConcurrencyTracker(delay=0.05)
    orchestrator = _make_orchestrator(project, tracker)

    results = orchestrator.run_cycle()

    assert tracker.max_active == 1, (
        f"default run must not overlap, observed max_active={tracker.max_active}"
    )
    assert [result.task_id for result in results] == READY_ORDER
    assert all(result.succeeded for result in results)


def test_parallel_respects_max_tasks_batch(tmp_path: Path) -> None:
    project = build_parallel_project(tmp_path / "parallel-project", task_count=4)
    tracker = ConcurrencyTracker(delay=0.05)
    orchestrator = _make_orchestrator(project, tracker)

    results = orchestrator.run_cycle(max_concurrent=4, max_tasks=2)

    assert len(results) == 2
    tasks = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))["tasks"]
    statuses = {task["status"] for task in tasks}
    assert statuses == {"DONE", "READY"}
