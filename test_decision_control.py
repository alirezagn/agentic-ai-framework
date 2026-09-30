"""Tests for decision control: append_decision + PROPOSED_CHANGE gate.

Covers GAP_ANALYSIS task T13 (finding 8 — framework/00_MASTER_ORCHESTRATOR
Decision Control, GETTING_STARTED Pattern 3):

* ``StateManager.append_decision`` writes a parseable DECISIONS.md section
  and a CHANGELOG entry,
* ``list_decisions`` understands both the table fixture format and the rich
  section format used by real projects,
* ``resolve_decision`` flips PROPOSED_CHANGE -> APPROVED / REJECTED,
* agent ``data.proposed_change`` output is recorded and gates dispatch of
  affected tasks (task stays READY, never FAILED),
* the gate opens after human approval and does not block unaffected tasks,
* reviews are gated too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from conftest import _task, build_test_project
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateError, StateManager

FIXTURE_DECISION_TABLE = (
    "| DEC-ID | Decision | Status | Reason |\n"
    "|--------|----------|--------|--------|\n"
    "| DEC-001 | Use YAML for state | APPROVED | Human readable |\n"
)


class PlainAgent(BaseAgent):
    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(task_id, f"finished {task_id}")


class ChangeProposerAgent(BaseAgent):
    """Completes the task while proposing a gated architecture change."""

    AGENT_ID = "change_agent"

    def __init__(self, state_manager: Any, affected: List[str]) -> None:
        super().__init__(state_manager=state_manager)
        self.affected = list(affected)

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(
            task_id,
            f"finished {task_id} with a proposed change",
            data={
                "proposed_change": {
                    "title": "Switch OLED driver to SSD1306 alt library",
                    "reason": "New datasheet revision changed init sequence",
                    "alternatives": "Keep old driver (rejected: init bug)",
                    "impact": "Firmware GPIO init and test harness",
                    "affected_tasks": self.affected,
                    "risks": "Timing differences on 400kHz bus",
                    "recommendation": "Approve and update references",
                }
            },
        )


class ReviewerAgent(BaseAgent):
    AGENT_ID = "review_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(
            task_id,
            "review passed",
            data={"review_status": "PASS", "findings": ["measured"]},
        )


def make_orchestrator(
    project: Path,
    checkpoints_root: Path,
    proposer_affected: Optional[List[str]] = None,
) -> MasterOrchestrator:
    affected = proposer_affected if proposer_affected is not None else ["TASK-002", "TASK-003"]

    def resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
        if owner == "review_agent":
            return ReviewerAgent(state_manager=state_manager)
        if owner in ("requirements_agent", "change_agent"):
            return ChangeProposerAgent(state_manager=state_manager, affected=affected)
        return PlainAgent(state_manager=state_manager)

    return MasterOrchestrator(
        project,
        checkpoints_root=checkpoints_root,
        auto_checkpoint=False,
        agent_resolver=resolver,
    )


# ---------------------------------------------------------------------------
# StateManager API
# ---------------------------------------------------------------------------


class TestAppendDecision:
    def test_append_decision_writes_section_and_changelog(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)

        record = state.append_decision(
            "Switch OLED driver",
            status="PROPOSED_CHANGE",
            reason="Datasheet init sequence changed",
            alternatives="Keep old driver (rejected: bug)",
            impact="Firmware GPIO init",
            affected_tasks=["TASK-002", "TASK-003"],
            risks="Bus timing differences",
            recommendation="Approve + update references",
            proposed_by="architecture_agent",
        )

        assert record["id"] == "DEC-002"  # fixture already has DEC-001
        content = state.load_decisions()
        assert "## DEC-002: Switch OLED driver" in content
        assert "**Status:** PROPOSED_CHANGE" in content
        assert "**Affected Tasks:** TASK-002, TASK-003" in content
        assert "**Proposed by:** architecture_agent" in content
        assert FIXTURE_DECISION_TABLE.splitlines()[0] in content  # table kept intact

        changelog = state.load_changelog()
        assert "DEC-002 recorded (PROPOSED_CHANGE)" in changelog

        pending = state.pending_proposed_changes()
        assert [change["id"] for change in pending] == ["DEC-002"]
        assert pending[0]["affected_tasks"] == ["TASK-002", "TASK-003"]
        assert pending[0]["reason"] == "Datasheet init sequence changed"

    def test_append_decision_allocates_next_ids(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)

        first = state.append_decision("First change")
        second = state.append_decision("Second change")

        assert first["id"] == "DEC-002"
        assert second["id"] == "DEC-003"

    def test_list_decisions_reads_table_and_section_formats(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)

        table_entries = state.list_decisions()
        assert table_entries[0]["id"] == "DEC-001"
        assert table_entries[0]["format"] == "table"
        assert table_entries[0]["status"] == "APPROVED"

        state.append_decision("Rich entry", status="REJECTED")
        entries = state.list_decisions()
        assert [entry["format"] for entry in entries] == ["table", "section"]
        assert entries[1]["status"] == "REJECTED"

    def test_append_decision_flattens_multiline_values(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)
        state.append_decision("Multiline", reason="line one\nline two")
        pending = state.pending_proposed_changes()
        assert pending[0]["reason"] == "line one line two"


class TestResolveDecision:
    def test_approve_flips_status_and_changelogs(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)
        record = state.append_decision("Gate me", affected_tasks=["TASK-002"])

        resolved = state.resolve_decision(record["id"], approved=True)

        assert resolved["status"] == "APPROVED"
        assert state.pending_proposed_changes() == []
        assert "DEC-002 APPROVED" in state.load_changelog()
        # Section format keeps every other field intact.
        assert "## DEC-002: Gate me" in state.load_decisions()

    def test_reject_flips_status(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)
        record = state.append_decision("Reject me")

        resolved = state.resolve_decision(record["id"], approved=False)

        assert resolved["status"] == "REJECTED"
        assert "DEC-002 REJECTED" in state.load_changelog()

    def test_resolve_table_format_row(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)

        resolved = state.resolve_decision("DEC-001", approved=False)

        assert resolved["status"] == "REJECTED"
        row = next(
            line for line in state.load_decisions().splitlines()
            if line.startswith("| DEC-001 ")
        )
        assert "| REJECTED |" in row

    def test_resolve_unknown_decision_raises(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "dec-project")
        state = StateManager(project)
        with pytest.raises(StateError, match="DEC-999"):
            state.resolve_decision("DEC-999", approved=True)


# ---------------------------------------------------------------------------
# Orchestrator gate
# ---------------------------------------------------------------------------


class TestProposedChangeGate:
    def test_change_recorded_and_gates_affected_task(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "gate-project")
        orchestrator = make_orchestrator(project, checkpoints_root)

        first = orchestrator.run_cycle()
        assert first and first[0].succeeded
        # The proposed change is persisted and warned about.
        assert first[0].output.warnings
        assert any("PROPOSED_CHANGE" in warn for warn in first[0].output.warnings)
        state = StateManager(project)
        pending = state.pending_proposed_changes()
        assert len(pending) == 1 and pending[0]["id"] == "DEC-002"
        assert pending[0]["affected_tasks"] == ["TASK-002", "TASK-003"]
        project_data = orchestrator.state.load_project()
        assert any("DEC-002 pending approval" in str(item)
                   for item in project_data.get("human_decisions", []))

        # TASK-003 parks in WAITING but the gate still refuses dispatch.
        second = orchestrator.run_cycle()
        assert second and not second[0].succeeded
        assert "PROPOSED_CHANGE" in "; ".join(second[0].output.errors)
        assert "DEC-002" in "; ".join(second[0].output.errors)
        task_003 = orchestrator.get_task("TASK-003")
        assert task_003["status"] == "WAITING"  # gated, never marked FAILED
        project_data = orchestrator.state.load_project()
        assert any("must be approved" in str(item)
                   for item in project_data.get("human_decisions", []))

        # Human approval opens the gate.
        orchestrator.approve_decision("DEC-002", approved=True)
        assert StateManager(project).pending_proposed_changes() == []

        third = orchestrator.run_cycle()
        assert third and third[0].succeeded
        assert orchestrator.get_task("TASK-003")["status"] == "DONE"

    def test_gate_ignores_unaffected_tasks(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "gate-project")
        # Independent task that unlocks only after TASK-002 completes, so it
        # runs in the same cycle the gate blocks TASK-003.
        tasks_doc = StateManager(project).load_tasks_document()
        tasks_doc["tasks"].append(
            _task("TASK-009", "plain_agent", "TODO", ["TASK-002"], notes="independent work")
        )
        StateManager(project).save_tasks_document(tasks_doc)

        orchestrator = make_orchestrator(
            project, checkpoints_root, proposer_affected=["TASK-003"]
        )

        first = orchestrator.run_cycle()
        assert first and all(result.succeeded for result in first)

        second = orchestrator.run_cycle()
        # TASK-009 (unaffected) runs; TASK-003 (affected) is gated.
        by_id = {result.task_id: result for result in second}
        assert by_id["TASK-009"].succeeded
        assert orchestrator.get_task("TASK-009")["status"] == "DONE"
        assert not by_id["TASK-003"].succeeded
        assert "PROPOSED_CHANGE" in "; ".join(by_id["TASK-003"].output.errors)

    def test_review_dispatch_gated_until_approval(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "gate-project")
        tasks_doc = StateManager(project).load_tasks_document()
        for task in tasks_doc["tasks"]:
            if task["id"] == "TASK-002":
                task["review"] = {"required": True, "status": "NOT_STARTED"}
        StateManager(project).save_tasks_document(tasks_doc)

        orchestrator = make_orchestrator(
            project, checkpoints_root, proposer_affected=["TASK-002"]
        )

        first = orchestrator.run_cycle()
        assert first and first[0].new_status == "REVIEW"
        assert StateManager(project).pending_proposed_changes()

        # Review of a task touched by the pending change is refused.
        second = orchestrator.run_cycle()
        assert second and not second[0].succeeded
        assert "PROPOSED_CHANGE" in "; ".join(second[0].output.errors)
        task = orchestrator.get_task("TASK-002")
        assert task["status"] == "REVIEW"
        assert task["review"]["status"] == "READY"

        orchestrator.approve_decision("DEC-002", approved=True)
        third = orchestrator.run_cycle()
        assert third and third[0].new_status == "DONE"
        assert orchestrator.get_task("TASK-002")["review"]["status"] == "PASS"

    def test_no_proposed_change_leaves_decisions_untouched(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "gate-project")

        def plain_resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
            return PlainAgent(state_manager=state_manager)

        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=plain_resolver,
        )
        orchestrator.run_cycle()

        state = StateManager(project)
        assert state.pending_proposed_changes() == []
        assert "DEC-002" not in state.load_decisions()
