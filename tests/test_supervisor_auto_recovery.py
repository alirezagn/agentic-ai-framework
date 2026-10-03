"""Auto-recovery: DoD fallback ingestion, validation circuit-breaker, harvest.

Requirement verification for the auto-recovery batch:

* a task whose planning omitted ``acceptance_criteria`` passes the DoD via
  fallback ingestion — owner-scoped defaults are persisted to TASKS.yaml
  instead of rejecting the task with "no acceptance criteria defined";
* two consecutive DoD/schema rejections auto-waive the task (WAIVED:
  terminal, satisfies dependents) with a WARNING in CURRENT_STATE.md, so
  the project never stalls in ``HUMAN_DECISION_REQUIRED`` — code/runtime
  failures keep the normal escalation path;
* analysis/findings from a rejected turn are harvested to disk
  (``docs/findings/<task>.md`` + RISKS.md), de-duplicated across retries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.orchestrator import (
    MasterOrchestrator,
    _fallback_acceptance_criteria,
)
from orchestrator.state_manager import StateManager
from orchestrator.supervisor import is_validation_failure

# ---------------------------------------------------------------------------
# Helpers and deterministic agents
# ---------------------------------------------------------------------------


def _set_acceptance_criteria(project: Path, task_id: str, criteria: list) -> None:
    document = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))
    for task in document["tasks"]:
        if task["id"] == task_id:
            task["acceptance_criteria"] = criteria
    (project / "TASKS.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


class DeliveringAgent(BaseAgent):
    """Completes and really materializes the expected output."""

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task = payload.get("task") or {}
        task_id = str(task.get("id"))
        expected = str((task.get("expected_outputs") or ["TASK.md"])[0])
        return self.completed(
            task_id,
            "analysis delivered as markdown",
            data={"documents": {expected: f"# {expected}\n\nmeasured results\n"}},
        )


class DoDRejectingAgent(BaseAgent):
    """Completes but the DoD rejects the turn (failing acceptance check).

    Carries real analysis/findings/risks so the harvesting path is exercised
    on every rejected turn.
    """

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str((payload.get("task") or {}).get("id"))
        return self.completed(
            task_id,
            "structured analysis produced",
            data={
                "acceptance_results": [
                    {"name": "latency budget", "status": "FAIL", "detail": "measured 2.1s"}
                ],
                "analysis": "The subsystem needs a bigger buffer.",
                "findings": ["buffer overruns under load"],
                "risks": [
                    {
                        "title": "latency spike",
                        "description": "p99 exceeds 2s",
                        "probability": "HIGH",
                        "impact": "MEDIUM",
                    }
                ],
            },
        )


class SchemaRejectingAgent(BaseAgent):
    """Returns a structurally invalid output (empty summary) — schema failure."""

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str((payload.get("task") or {}).get("id"))
        return AgentOutput(
            agent_id=self.AGENT_ID,
            task_id=task_id,
            status=config.AGENT_STATUS_COMPLETED,
            summary="",
            data={"result": "ok"},
        )


class RuntimeFailingAgent(BaseAgent):
    """Crashes like bad code — must never be auto-waived."""

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        raise RuntimeError("boom")


def _orch(project: Path, checkpoints_root: Path, agent_cls: type) -> MasterOrchestrator:
    orch = MasterOrchestrator(
        project, checkpoints_root=checkpoints_root, auto_checkpoint=False
    )
    agent = agent_cls(state_manager=orch.state)
    orch.resolve_agent = lambda owner, fresh=False: agent  # type: ignore[method-assign]
    return orch


# ---------------------------------------------------------------------------
# Requirement 1 — DoD fallback ingestion
# ---------------------------------------------------------------------------


class TestDoDFallbackIngestion:
    def test_missing_criteria_pass_dod_and_are_persisted(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _set_acceptance_criteria(test_project, "TASK-002", [])
        orch = _orch(test_project, checkpoints_root, DeliveringAgent)

        result = orch.run_task("TASK-002")

        assert result.new_status == config.TASK_DONE, result.output.summary
        task = orch.get_task("TASK-002")
        assert task["acceptance_criteria"] == _fallback_acceptance_criteria(task)
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        stored = [t for t in document["tasks"] if t["id"] == "TASK-002"][0]
        assert stored["acceptance_criteria"] == task["acceptance_criteria"]

    def test_dod_never_reports_missing_criteria(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _set_acceptance_criteria(test_project, "TASK-002", [])
        orch = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        problems = orch.definition_of_done(orch.get_task("TASK-002"))
        assert not any("no acceptance criteria" in problem for problem in problems)

    def test_owner_scoped_and_default_fallbacks(self) -> None:
        assert _fallback_acceptance_criteria({"owner": "test_agent"}) == [
            "Tests map to requirements and pass"
        ]
        assert _fallback_acceptance_criteria({"owner": "documentation_agent"}) == [
            "Docs match shipped behavior"
        ]
        assert _fallback_acceptance_criteria({"owner": "mystery_agent"}) == [
            "Complete task analysis and produce structured markdown outputs"
        ]
        assert _fallback_acceptance_criteria({}) == [
            "Complete task analysis and produce structured markdown outputs"
        ]


# ---------------------------------------------------------------------------
# Requirement 2 — supervisor validation circuit-breaker
# ---------------------------------------------------------------------------


class TestValidationCircuitBreaker:
    def test_two_dod_rejections_auto_waive(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orch = _orch(test_project, checkpoints_root, DoDRejectingAgent)

        first = orch.run_task("TASK-002")
        assert first.new_status == config.TASK_FAILED, first.output.summary
        task = orch.get_task("TASK-002")
        assert task["status"] == config.TASK_FAILED
        assert task["execution"]["validation_failure_streak"] == 1

        second = orch.run_task("TASK-002")
        task = orch.get_task("TASK-002")
        assert second.new_status == config.TASK_WAIVED
        assert task["status"] == config.TASK_WAIVED
        assert task["execution"]["validation_failure_streak"] == 2

        state_text = (test_project / "CURRENT_STATE.md").read_text(encoding="utf-8")
        assert "WARNING" in state_text
        assert "auto-waived" in state_text

        # Dependents proceed: the waived task satisfies the graph, so the
        # BLOCKED dependent is promoted without a human decision.
        assert orch.get_task("TASK-003")["status"] == config.TASK_READY

        report = orch.sync_health()
        assert str(report.state) != config.HEALTH_HUMAN_DECISION_REQUIRED
        assert all(not escalation.blocking for escalation in report.escalations)

    def test_two_schema_rejections_auto_waive(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orch = _orch(test_project, checkpoints_root, SchemaRejectingAgent)

        orch.run_task("TASK-002")
        assert orch.get_task("TASK-002")["status"] == config.TASK_FAILED
        orch.run_task("TASK-002")
        task = orch.get_task("TASK-002")
        assert task["status"] == config.TASK_WAIVED
        assert task["execution"]["validation_failure_streak"] == 2

    def test_runtime_failures_never_auto_waive(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orch = _orch(test_project, checkpoints_root, RuntimeFailingAgent)

        orch.run_task("TASK-002")
        orch.run_task("TASK-002")
        task = orch.get_task("TASK-002")
        assert task["status"] == config.TASK_FAILED
        assert task["execution"]["validation_failure_streak"] == 0
        assert orch.supervisor.should_block_dispatch("TASK-002") is None

        state_text = (test_project / "CURRENT_STATE.md").read_text(encoding="utf-8")
        assert "auto-waived" not in state_text

    def test_health_sync_sweeps_struck_out_tasks(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orch = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        state = StateManager(test_project)
        state.update_task_status(
            "TASK-002", config.TASK_FAILED, error="DoD unmet: schema constraint"
        )
        state.update_task_execution(
            "TASK-002", set_values={"validation_failure_streak": 2}
        )

        orch.sync_health()

        assert orch.get_task("TASK-002")["status"] == config.TASK_WAIVED
        state_text = (test_project / "CURRENT_STATE.md").read_text(encoding="utf-8")
        assert "auto-waived" in state_text

    def test_waived_is_terminal_and_satisfies_dependencies(self) -> None:
        assert config.TASK_WAIVED in config.TASK_STATUSES
        assert config.TASK_WAIVED in config.TERMINAL_TASK_STATUSES
        assert config.TASK_WAIVED in config.SATISFIED_DEPENDENCY_STATUSES
        assert config.loop_thresholds().validation_waive_after == 2

    def test_is_validation_failure_classification(self) -> None:
        assert is_validation_failure("DoD unmet: expected output missing")
        assert is_validation_failure("Output summary must not be empty")
        assert is_validation_failure("Model output was not parseable JSON")
        assert not is_validation_failure(
            "Traceback (most recent call last): RuntimeError: boom"
        )
        assert not is_validation_failure("connection refused")
        assert not is_validation_failure("strategy did not converge")
        assert not is_validation_failure("")


# ---------------------------------------------------------------------------
# Requirement 3 — findings harvesting on non-compliant turns
# ---------------------------------------------------------------------------


class TestFindingsHarvest:
    def test_harvest_writes_findings_and_risks(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orch = _orch(test_project, checkpoints_root, DoDRejectingAgent)

        first = orch.run_task("TASK-002")
        assert any(
            "findings harvested" in warning for warning in first.output.warnings
        ), first.output.warnings

        findings_file = test_project / "docs" / "findings" / "TASK-002.md"
        assert findings_file.exists()
        text = findings_file.read_text(encoding="utf-8")
        assert "buffer overruns under load" in text
        assert "The subsystem needs a bigger buffer" in text
        risks = (test_project / "RISKS.md").read_text(encoding="utf-8")
        assert "TASK-002: latency spike" in risks

        # A second (identical) rejected turn must not duplicate the harvest,
        # and it strikes out the circuit-breaker on the way.
        orch.run_task("TASK-002")
        assert orch.get_task("TASK-002")["status"] == config.TASK_WAIVED
        text = findings_file.read_text(encoding="utf-8")
        assert text.count("buffer overruns under load") == 1
        risks = (test_project / "RISKS.md").read_text(encoding="utf-8")
        assert risks.count("latency spike") == 1

    def test_no_harvest_without_findings(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orch = _orch(test_project, checkpoints_root, RuntimeFailingAgent)
        orch.run_task("TASK-002")
        assert not (test_project / "docs" / "findings" / "TASK-002.md").exists()
