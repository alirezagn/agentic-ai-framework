"""Tests for milestone checkpoints (GAP_ANALYSIS task T12).

Covers:
* ``cp-milestone-<slug>`` written once all tasks of a declared milestone are terminal,
* ``cp-milestone-complete`` written once the whole task graph is terminal,
* checkpoints attributed to the dispatch / review that completed the milestone,
* dedupe (never written twice), slug formatting, manual mode opt-out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from conftest import _task, build_test_project
from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.orchestrator import MasterOrchestrator


class PlainAgent(BaseAgent):
    """Immediate-completion agent; execution content is irrelevant here."""

    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(task_id, f"finished {task_id}")


class FakeReviewer(BaseAgent):
    AGENT_ID = "review_agent"

    def __init__(self, state_manager: Any, outcome: str = "PASS") -> None:
        super().__init__(state_manager=state_manager)
        self.outcome = outcome

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(
            task_id,
            f"review {self.outcome}",
            data={"review_status": self.outcome, "findings": ["measured"]},
        )


def build_milestone_project(root: Path, tasks: List[Dict[str, Any]]) -> Path:
    build_test_project(root)
    (root / "TASKS.yaml").write_text(
        yaml.safe_dump({"tasks": tasks}, sort_keys=False), encoding="utf-8"
    )
    return root


def make_orchestrator(
    project: Path, checkpoints_root: Path, auto_checkpoint: bool = True
) -> MasterOrchestrator:
    def resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
        if owner == "review_agent":
            return FakeReviewer(state_manager=state_manager)
        return PlainAgent(state_manager=state_manager)

    return MasterOrchestrator(
        project,
        checkpoints_root=checkpoints_root,
        auto_checkpoint=auto_checkpoint,
        agent_resolver=resolver,
    )


def checkpoint_ids(orchestrator: MasterOrchestrator) -> List[str]:
    return [entry["id"] for entry in orchestrator.checkpoints.list_checkpoints()]


def test_milestone_checkpoint_when_milestone_tasks_terminal(
    tmp_path: Path, checkpoints_root: Path
) -> None:
    project = build_milestone_project(
        tmp_path / "ms-project",
        [
            _task("TASK-A", "plain_agent", "READY", [], milestone="M1 Core"),
            _task("TASK-B", "plain_agent", "DONE", [], milestone="M1 Core"),
            # FAILED is not terminal and never becomes READY: blocks
            # cp-milestone-complete while the milestone still completes.
            _task("TASK-C", "plain_agent", "FAILED", []),
        ],
    )
    orchestrator = make_orchestrator(project, checkpoints_root)

    results = orchestrator.run_cycle()
    assert results and all(result.succeeded for result in results)

    ids = checkpoint_ids(orchestrator)
    assert "cp-milestone-m1-core" in ids
    assert "cp-milestone-complete" not in ids
    # The dispatch that finished TASK-A owns the checkpoint.
    assert results[0].checkpoint_id == "cp-milestone-m1-core"
    changelog = orchestrator.state.load_changelog()
    assert "cp-milestone-m1-core" in changelog
    assert orchestrator.state.load_project()["last_checkpoint"]["id"] == (
        "cp-milestone-m1-core"
    )


def test_project_complete_checkpoint_when_all_tasks_terminal(
    tmp_path: Path, checkpoints_root: Path
) -> None:
    project = build_milestone_project(
        tmp_path / "ms-project",
        [
            _task("TASK-A", "plain_agent", "READY", []),
            _task("TASK-B", "plain_agent", "DONE", []),
        ],
    )
    orchestrator = make_orchestrator(project, checkpoints_root)

    orchestrator.run_cycle()

    ids = checkpoint_ids(orchestrator)
    assert "cp-milestone-complete" in ids
    assert not any(cid.startswith("cp-milestone-m") for cid in ids)


def test_milestone_checkpoint_never_written_twice(
    tmp_path: Path, checkpoints_root: Path
) -> None:
    project = build_milestone_project(
        tmp_path / "ms-project",
        [
            _task("TASK-A", "plain_agent", "READY", [], milestone="M1"),
            _task("TASK-B", "plain_agent", "DONE", [], milestone="M1"),
            _task("TASK-C", "plain_agent", "FAILED", []),
        ],
    )
    orchestrator = make_orchestrator(project, checkpoints_root)
    orchestrator.run_cycle()
    first_ids = checkpoint_ids(orchestrator)
    assert "cp-milestone-m1" in first_ids

    assert orchestrator.check_milestones() is None
    assert checkpoint_ids(orchestrator) == first_ids


def test_milestone_slug_formatting(tmp_path: Path, checkpoints_root: Path) -> None:
    assert MasterOrchestrator._milestone_slug("M1: Core/Scope!") == "m1-core-scope"
    assert MasterOrchestrator._milestone_slug("  ") == "milestone"

    project = build_milestone_project(
        tmp_path / "ms-project",
        [
            _task("TASK-A", "plain_agent", "READY", [], milestone="M1: Core/Scope!"),
            _task("TASK-C", "plain_agent", "FAILED", []),
        ],
    )
    orchestrator = make_orchestrator(project, checkpoints_root)
    orchestrator.run_cycle()
    assert "cp-milestone-m1-core-scope" in checkpoint_ids(orchestrator)


def test_review_completion_can_fire_milestone_checkpoint(
    tmp_path: Path, checkpoints_root: Path
) -> None:
    project = build_milestone_project(
        tmp_path / "ms-project",
        [
            _task(
                "TASK-A",
                "plain_agent",
                "READY",
                [],
                review_required=True,
                milestone="Reviewed",
            ),
            _task("TASK-C", "plain_agent", "FAILED", []),
        ],
    )
    orchestrator = make_orchestrator(project, checkpoints_root)

    first = orchestrator.run_cycle()
    assert first and first[0].new_status == config.TASK_REVIEW
    assert checkpoint_ids(orchestrator) == []

    second = orchestrator.run_cycle()
    assert second and second[0].new_status == config.TASK_DONE
    assert second[0].checkpoint_id == "cp-milestone-reviewed"
    assert "cp-milestone-reviewed" in checkpoint_ids(orchestrator)


def test_manual_mode_writes_no_milestone_checkpoints(
    tmp_path: Path, checkpoints_root: Path
) -> None:
    project = build_milestone_project(
        tmp_path / "ms-project",
        [
            _task("TASK-A", "plain_agent", "READY", [], milestone="M1"),
            _task("TASK-B", "plain_agent", "DONE", [], milestone="M1"),
        ],
    )
    orchestrator = make_orchestrator(project, checkpoints_root, auto_checkpoint=False)

    orchestrator.run_cycle()

    assert checkpoint_ids(orchestrator) == []
    assert orchestrator.check_milestones() is None
