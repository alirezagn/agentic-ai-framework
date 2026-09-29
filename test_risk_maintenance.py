"""Tests for RISKS.md maintenance (GAP_ANALYSIS task T14, finding 9).

Covers:
* ``StateManager.append_risk`` / ``list_risks`` / ``update_risk_status``
  (both the table fixture format and the rich section format),
* failed dispatches record a deduplicated RISK entry + link it in the
  agent output warnings,
* ``sync_project_health`` keeps the register current for FAILED tasks,
* the health report points at RISKS.md when failures exist,
* successful work never pollutes the risk register.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from conftest import _task, build_test_project
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateError, StateManager
from orchestrator.supervisor import SupervisorAgent

FIXTURE_RISK_TABLE = (
    "| RISK-ID | Risk | Probability | Impact | Mitigation |\n"
    "|---------|------|-------------|--------|------------|\n"
    "| RISK-001 | Latency above 1s | MEDIUM | Broken UX | Use tiny model |\n"
)


class PlainAgent(BaseAgent):
    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(task_id, f"finished {task_id}")


class FailingAgent(BaseAgent):
    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.failed(task_id, "model backend down", errors=["connection refused"])


def make_orchestrator(
    project: Path, checkpoints_root: Path, failing: bool = False
) -> MasterOrchestrator:
    agent_cls = FailingAgent if failing else PlainAgent

    def resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
        return agent_cls(state_manager=state_manager)

    return MasterOrchestrator(
        project,
        checkpoints_root=checkpoints_root,
        auto_checkpoint=False,
        agent_resolver=resolver,
    )


# ---------------------------------------------------------------------------
# StateManager risk API
# ---------------------------------------------------------------------------


class TestRiskRegister:
    def test_append_risk_writes_section_and_changelog(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "risk-project")
        state = StateManager(project)

        record = state.append_risk(
            "Whisper latency may exceed budget",
            probability="MEDIUM",
            impact="HIGH",
            description="STT on CPU can take 1-3s",
            mitigation="Use tiny model; pre-load",
            related_tasks=["TASK-002"],
            owner="supervisor_agent",
        )

        assert record["id"] == "RISK-002"  # fixture has RISK-001
        content = state.load_risks()
        assert "### RISK-002: Whisper latency may exceed budget" in content
        assert "**Probability:** MEDIUM" in content
        assert "**Related Tasks:** TASK-002" in content
        assert FIXTURE_RISK_TABLE.splitlines()[0] in content
        assert "Risk RISK-002 recorded (OPEN)" in state.load_changelog()

        listed = state.list_risks()
        assert [risk["id"] for risk in listed] == ["RISK-001", "RISK-002"]
        assert listed[0]["format"] == "table"
        assert listed[1]["format"] == "section"
        assert listed[1]["related_tasks"] == ["TASK-002"]
        assert listed[1]["status"] == "OPEN"

    def test_append_risk_allocates_next_ids(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "risk-project")
        state = StateManager(project)
        first = state.append_risk("First")
        second = state.append_risk("Second")
        assert first["id"] == "RISK-002"
        assert second["id"] == "RISK-003"

    def test_update_risk_status_section_format(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "risk-project")
        state = StateManager(project)
        record = state.append_risk("Section risk")

        updated = state.update_risk_status(record["id"], status="MITIGATED")

        assert updated["status"] == "MITIGATED"
        assert "Risk RISK-002 MITIGATED" in state.load_changelog()

    def test_update_risk_status_table_format_round_trips(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "risk-project")
        state = StateManager(project)

        updated = state.update_risk_status("RISK-001", status="CLOSED")

        assert updated["id"] == "RISK-001"
        assert updated["status"] == "CLOSED"

    def test_update_unknown_risk_raises(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "risk-project")
        state = StateManager(project)
        with pytest.raises(StateError, match="RISK-999"):
            state.update_risk_status("RISK-999", status="CLOSED")

    def test_list_risks_parses_rich_sections(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "risk-project")
        state = StateManager(project)
        content = state.load_risks()
        content += (
            "\n---\n\n"
            "### RISK-007: Power budget overrun\n\n"
            "**Status:** OPEN  \n"
            "**Probability:** LOW  \n"
            "**Impact:** HIGH  \n"
            "**Related Tasks:** TASK-004, TASK-003  \n"
        )
        state.save_risks(content)

        risks = {risk["id"]: risk for risk in state.list_risks()}
        assert risks["RISK-007"]["title"] == "Power budget overrun"
        assert risks["RISK-007"]["probability"] == "LOW"
        assert risks["RISK-007"]["impact"] == "HIGH"
        assert risks["RISK-007"]["related_tasks"] == ["TASK-004", "TASK-003"]


# ---------------------------------------------------------------------------
# Supervisor / orchestrator maintenance
# ---------------------------------------------------------------------------


class TestFailureRiskMaintenance:
    def test_failed_dispatch_records_risk_and_links_output(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "risk-project")
        orchestrator = make_orchestrator(project, checkpoints_root, failing=True)

        results = orchestrator.run_cycle()
        assert results and not results[0].succeeded

        state = StateManager(project)
        risks = state.list_risks()
        new_risks = [risk for risk in risks if risk["id"] != "RISK-001"]
        assert len(new_risks) == 1
        risk = new_risks[0]
        assert risk["related_tasks"] == ["TASK-002"]
        assert "keeps failing" in risk["title"]
        # Risk id is linked back into the agent output.
        assert any(risk["id"] in warn for warn in results[0].output.warnings)
        assert orchestrator.get_task("TASK-002")["status"] == "FAILED"

    def test_failure_risk_deduplicated_on_redispatch(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "risk-project")
        orchestrator = make_orchestrator(project, checkpoints_root, failing=True)

        first = orchestrator.run_cycle()
        assert first and any("RISK-" in warn for warn in first[0].output.warnings)

        second = orchestrator.dispatch("TASK-002")  # direct re-dispatch
        assert not second.succeeded
        assert not any("recorded in RISKS.md" in warn for warn in second.output.warnings)

        risks = StateManager(project).list_risks()
        assert len([risk for risk in risks if risk["id"] != "RISK-001"]) == 1

    def test_sync_project_health_maintains_register(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "risk-project")
        state = StateManager(project)
        doc = state.load_tasks_document()
        for task in doc["tasks"]:
            if task["id"] == "TASK-004":
                task["status"] = "FAILED"
                task["execution"]["last_error"] = "GPIO conflict"
        state.save_tasks_document(doc)

        supervisor = SupervisorAgent(state_manager=StateManager(project))
        report = supervisor.sync_project_health()

        assert "TASK-004" in report.failed_task_ids
        risks = StateManager(project).list_risks()
        failure_risks = [risk for risk in risks if risk["id"] != "RISK-001"]
        assert len(failure_risks) == 1
        assert failure_risks[0]["related_tasks"] == ["TASK-004"]
        assert any("RISKS.md" in rec for rec in report.recommendations)

        # Syncing again must not duplicate the entry.
        SupervisorAgent(state_manager=StateManager(project)).sync_project_health()
        risks = StateManager(project).list_risks()
        assert len([risk for risk in risks if risk["id"] != "RISK-001"]) == 1

    def test_successful_work_creates_no_risk(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "risk-project")
        orchestrator = make_orchestrator(project, checkpoints_root, failing=False)

        results = orchestrator.run_cycle()
        assert results and results[0].succeeded

        risks = StateManager(project).list_risks()
        assert [risk["id"] for risk in risks] == ["RISK-001"]

    def test_ensure_failure_risk_returns_none_when_present(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "risk-project")
        supervisor = SupervisorAgent(state_manager=StateManager(project))

        first = supervisor.ensure_failure_risk("TASK-004", "boom")
        second = supervisor.ensure_failure_risk("TASK-004", "boom again")

        assert first == "RISK-002"
        assert second is None
