"""Tests for the review_gaps.md remediation (A1-A8, B1-B7, C1-C3, E1).

One class per gap so a failure names the gap it regressed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from conftest import build_test_project
from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, BaseAgent
from orchestrator.checkpoint_manager import CheckpointManager
from orchestrator.cli import main
from orchestrator.orchestrator import (
    LoopLimitExceededError,
    MasterOrchestrator,
)
from orchestrator.state_manager import StateError, StateManager
from orchestrator.supervisor import SupervisorAgent


class FailingAgent(BaseAgent):
    """Fails with no artifacts — used for loop / evidence-stall tests."""

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.failed(task_id, "backend down", errors=["connection refused"])


class EvidenceAgent(BaseAgent):
    """Completes while emitting a document — resets the evidence stall."""

    AGENT_ID = "requirements_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(
            task_id,
            "captured requirements",
            data={"documents": {"REQUIREMENTS.md": "# REQUIREMENTS\n\n- REQ-001 shall work"}},
        )


class ProposerAgent(BaseAgent):
    """Completes any task and proposes a change gating TASK-003."""

    AGENT_ID = "plain_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.completed(
            task_id,
            "proposed a design change",
            data={
                "proposed_change": {
                    "title": "swap the sensor bus",
                    "affected_tasks": ["TASK-003"],
                }
            },
        )


def _plain_resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
    return ProposerAgent(state_manager=state_manager)


def _failing_resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
    return FailingAgent(state_manager=state_manager)


def _evidence_resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
    return EvidenceAgent(state_manager=state_manager)


# ---------------------------------------------------------------------------
# A1 — lifecycle phases
# ---------------------------------------------------------------------------


class TestPhaseLifecycleA1:
    def test_derive_phase_no_tasks_is_requirements(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        document = state.load_tasks_document()
        document["tasks"] = []
        state.save_tasks_document(document)
        assert state.derive_phase() == config.PHASE_REQUIREMENTS

    def test_derive_phase_skips_unowned_phases(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.update_task_status("TASK-001", config.TASK_DONE)
        state.update_task_status("TASK-002", config.TASK_DONE)
        # No research tasks exist: RESEARCH is skipped, ARCHITECTURE owns TASK-003.
        assert state.derive_phase() == config.PHASE_ARCHITECTURE

    def test_derive_phase_all_terminal_is_release(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        for task_id in ("TASK-001", "TASK-002", "TASK-003", "TASK-004"):
            state.update_task_status(task_id, config.TASK_DONE)
        assert state.derive_phase() == config.PHASE_RELEASE

    def test_recompute_advances_phase_forward_only(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        # Forward-only: a manually-set later phase is never pulled backwards.
        state.set_phase(config.PHASE_PLANNING)
        state.recompute_derived_state()
        project = state.load_project()
        assert (project.get("phase") or {}).get("current") == config.PHASE_PLANNING

        # A derived advance does move forward and mirrors project.status.
        state.set_phase(config.PHASE_REQUIREMENTS)
        state.update_task_status("TASK-001", config.TASK_DONE)
        state.update_task_status("TASK-002", config.TASK_DONE)
        state.recompute_derived_state()
        project = state.load_project()
        assert (project.get("phase") or {}).get("current") == config.PHASE_ARCHITECTURE
        assert (project.get("project") or {}).get("status") == config.PHASE_ARCHITECTURE

    def test_set_phase_rejects_unknown(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        with pytest.raises(StateError):
            state.set_phase("NOT_A_PHASE")

    def test_cli_phase_show_and_set(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = build_test_project(tmp_path / "p")
        assert main(["--project", str(project), "phase", "show"]) == 0
        out = capsys.readouterr().out
        assert "Current phase: REQUIREMENTS" in out

        assert main(["--project", str(project), "phase", "set", "testing"]) == 0
        current = StateManager(project).load_project()["phase"]["current"]
        assert current == "TESTING"
        changelog = StateManager(project).load_changelog()
        assert "Phase set to TESTING" in changelog

        assert main(["--project", str(project), "phase", "set", "NOPE"]) == 2


# ---------------------------------------------------------------------------
# A2 — goal -> task graph
# ---------------------------------------------------------------------------


class TestTaskGraphA2:
    def test_append_task_auto_id_and_validations(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        created = state.append_task(
            {
                "title": "solder the harness",
                "owner": "hardware_agent",
                "dependencies": ["TASK-002"],
            }
        )
        assert created["id"] == "TASK-005"
        # GAP-CRIT-05: status is derived from the dependency list. This spec
        # declares ["TASK-002"] (which is not DONE in the fixture), so the task
        # starts BLOCKED rather than TODO. Previously the spec's absence of a
        # status key silently defaulted to TODO, which understated the wait.
        assert created["status"] == config.TASK_BLOCKED
        assert created["execution"]["attempts_since_change"] == 0

        with pytest.raises(StateError):
            state.append_task({"title": "dup", "owner": "hardware_agent", "id": "TASK-005"})
        with pytest.raises(StateError):
            state.append_task({"title": "no owner"})
        with pytest.raises(StateError):
            state.append_task(
                {"title": "bad dep", "owner": "hardware_agent", "dependencies": ["TASK-999"]}
            )
        with pytest.raises(StateError):
            state.append_task({"title": "bad owner", "owner": "not_an_agent"})

    def test_dispatch_ingests_agent_tasks(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        class PlannerAgent(BaseAgent):
            AGENT_ID = "plain_agent"

            def execute(self, payload: Dict[str, Any]) -> AgentOutput:
                task_id = str(payload["task"]["id"])
                return self.completed(
                    task_id,
                    "planned follow-ups",
                    data={
                        "tasks": [
                            {
                                "title": "wire the battery",
                                "owner": "hardware_agent",
                                "dependencies": [],
                            }
                        ]
                    },
                )

        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=lambda owner, sm: PlannerAgent(state_manager=sm),
        )
        result = orchestrator.dispatch("TASK-002")
        assert result.succeeded
        created = orchestrator.get_task("TASK-005")
        assert created["title"] == "wire the battery"
        assert created["owner"] == "hardware_agent"


# ---------------------------------------------------------------------------
# A3 — stale blocks and decision conflicts
# ---------------------------------------------------------------------------


class TestDetectorsA3:
    def test_stale_block_detected_and_reported(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        state.update_task_status("TASK-001", config.TASK_DONE)
        # TASK-003 depends only on TASK-002; force it BLOCKED with deps met.
        state.update_task_status("TASK-002", config.TASK_DONE)
        state.update_task_status("TASK-003", config.TASK_BLOCKED)

        supervisor = SupervisorAgent(state_manager=StateManager(project))
        assert supervisor.detect_stale_blocks() == ["TASK-003"]
        report = supervisor.check_health()
        assert report.stale_blocks == ["TASK-003"]
        categories = {esc.category for esc in report.escalations}
        assert "stale_block" in categories
        stale = [e for e in report.escalations if e.category == "stale_block"][0]
        assert stale.blocking is False

    def test_conflicting_pending_changes_escalate(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        state.append_decision(
            title="change A", affected_tasks=["TASK-002"], status="PROPOSED_CHANGE"
        )
        state.append_decision(
            title="change B", affected_tasks=["TASK-002"], status="PROPOSED_CHANGE"
        )

        supervisor = SupervisorAgent(state_manager=StateManager(project))
        conflicts = supervisor.detect_decision_conflicts()
        assert conflicts == [{"task_id": "TASK-002", "decision_ids": ["DEC-002", "DEC-003"]}]
        report = supervisor.check_health()
        assert str(report.state) == config.HEALTH_HUMAN_DECISION_REQUIRED
        conflict = [e for e in report.escalations if e.category == "decision_conflict"][0]
        assert conflict.blocking is True


# ---------------------------------------------------------------------------
# A4 — no_new_evidence loop kind
# ---------------------------------------------------------------------------


class TestEvidenceStallA4:
    def test_kind_registered_with_threshold(self) -> None:
        assert config.LOOP_KIND_NO_NEW_EVIDENCE in config.LOOP_KINDS
        assert config.loop_thresholds().evidence_stall_max == 3

    def test_detect_from_state(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        state.update_task_execution("TASK-002", set_values={"evidence_stall_count": 3})
        supervisor = SupervisorAgent(state_manager=StateManager(project))
        kinds = [loop.kind for loop in supervisor.detect_loops() if loop.exceeded]
        assert config.LOOP_KIND_NO_NEW_EVIDENCE in kinds

    def test_dispatch_counts_stall_and_resets_on_evidence(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=_failing_resolver,
        )
        orchestrator.dispatch("TASK-002")
        assert orchestrator.get_task("TASK-002")["execution"]["evidence_stall_count"] == 1

        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=_evidence_resolver,
        )
        orchestrator.dispatch("TASK-002")
        execution = orchestrator.get_task("TASK-002")["execution"]
        assert execution["evidence_stall_count"] == 0
        assert "docs/REQUIREMENTS.md" in execution["known_artifacts"] or execution[
            "known_artifacts"
        ]


# ---------------------------------------------------------------------------
# A5 / C2 — attempts_since_change, recovering, last_loop, RECOVERY
# ---------------------------------------------------------------------------


class TestRecoveryTrackingA5:
    def test_attempts_since_change_resets_on_strategy_change(
        self, tmp_path: Path
    ) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.update_task_execution("TASK-002", attempt_delta=2)
        task = state.get_task("TASK-002")
        assert task["execution"]["attempts_since_change"] == 2
        assert task["execution"]["attempt_count"] == 2

        state.update_task_execution("TASK-002", strategy_changed=True)
        task = state.get_task("TASK-002")
        assert task["execution"]["attempts_since_change"] == 0
        assert task["execution"]["attempt_count"] == 2  # never reset
        assert task["execution"]["recovering"] is True

    def test_recovering_clears_when_recovery_also_fails(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.update_task_execution("TASK-002", strategy_changed=True)
        state.update_task_execution("TASK-002", attempt_delta=3)
        task = state.get_task("TASK-002")
        assert task["execution"]["recovering"] is False  # budget burned again

    def test_recovering_clears_on_terminal_status(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.update_task_execution("TASK-002", strategy_changed=True)
        state.update_task_status("TASK-002", config.TASK_DONE)
        assert state.get_task("TASK-002")["execution"]["recovering"] is False

    def test_last_loop_persisted_when_dispatch_refused(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=_failing_resolver,
        )
        for _ in range(3):
            orchestrator.dispatch("TASK-002")
        with pytest.raises(LoopLimitExceededError):
            orchestrator.dispatch("TASK-002")
        last_loop = orchestrator.get_task("TASK-002")["execution"].get("last_loop")
        assert last_loop and last_loop["kind"] == config.LOOP_KIND_SAME_STRATEGY

    def test_recovery_state_wins_over_escalation(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.update_task_execution(
            "TASK-002", attempt_delta=1, no_progress_delta=5, strategy_changed=True
        )
        supervisor = SupervisorAgent(state_manager=state)
        report = supervisor.check_health()
        assert [loop.kind for loop in report.loops] == [config.LOOP_KIND_NO_PROGRESS]
        assert report.escalations, "no_progress still escalates"
        assert str(report.state) == config.HEALTH_RECOVERY

    def test_same_strategy_ignores_changed_strategy(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        # Three attempts, but every one reported a strategy change.
        state.update_task_execution("TASK-002", attempt_delta=1, strategy_changed=True)
        state.update_task_execution("TASK-002", attempt_delta=1, strategy_changed=True)
        state.update_task_execution("TASK-002", attempt_delta=1, strategy_changed=True)
        supervisor = SupervisorAgent(state_manager=state)
        kinds = [loop.kind for loop in supervisor.detect_loops()]
        assert config.LOOP_KIND_SAME_STRATEGY not in kinds


# ---------------------------------------------------------------------------
# A6 — deadlock waiver
# ---------------------------------------------------------------------------


class TestDependencyWaiverA6:
    def test_relax_dependency_promotes_task(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.update_task_status("TASK-003", config.TASK_BLOCKED)
        task = state.relax_dependency("TASK-003", "TASK-002")
        assert "TASK-002" not in task["dependencies"]
        assert task["status"] == config.TASK_READY  # deps now satisfied

    def test_relax_unknown_edge_raises(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        with pytest.raises(StateError):
            state.relax_dependency("TASK-003", "TASK-001")

    def test_cli_waive(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = build_test_project(tmp_path / "p")
        code = main(
            ["--project", str(project), "waive", "TASK-003", "--dep", "TASK-002",
             "--reason", "sensor arrived early"]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "waived" in out
        assert "Dependency TASK-002 waived on TASK-003" in StateManager(
            project
        ).load_changelog()


# ---------------------------------------------------------------------------
# A7 / A8 — checkpoints and init scaffolding
# ---------------------------------------------------------------------------


class TestCheckpointsAndInitA7A8:
    def test_init_writes_baseline_checkpoint_and_scaffold(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["init", "gap-demo", "--dest", str(tmp_path)]) == 0
        target = tmp_path / "gap-demo"
        state = StateManager(target)
        project = state.load_project()
        assert project["last_checkpoint"]["id"] == "cp-000-init"
        assert "constraints" in project and "budget" in project and "resources" in project
        # the baseline was written to the (redirected) default root
        default_manager = CheckpointManager(target)
        ids = [str(entry.get("id")) for entry in default_manager.list_checkpoints()]
        assert "cp-000-init" in ids

    def test_failure_records_risk_checkpoint(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=True,
            agent_resolver=_failing_resolver,
        )
        orchestrator.dispatch("TASK-002")
        ids = [str(entry.get("id")) for entry in orchestrator.checkpoints.list_checkpoints()]
        assert any(item.startswith("cp-risk-") for item in ids), ids

    def test_phase_advance_checkpoints(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project, checkpoints_root=checkpoints_root, auto_checkpoint=True
        )
        orchestrator.dispatch("TASK-001")
        orchestrator.dispatch("TASK-002")
        ids = [str(entry.get("id")) for entry in orchestrator.checkpoints.list_checkpoints()]
        assert "cp-phase-architecture" in ids, ids


# ---------------------------------------------------------------------------
# B1 — PROJECT_MEMORY.md compaction
# ---------------------------------------------------------------------------


class TestMemoryCompactionB1:
    def test_compact_folds_middle_keeps_head_and_tail(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        memory = (
            "# PROJECT_MEMORY — p\n\n## Goal\n\nbuild it\n"
            + "".join(f"## Topic {i}\n\nbody {i} " + "y" * 800 + "\n\n" for i in range(90))
            + "## Next Steps\n\n- do the thing\n"
        )
        state.save_memory(memory)
        assert len(memory) > config.MEMORY_COMPACT_MAX_CHARS

        assert state.compact_memory() is True
        folded = state.load_memory()
        assert len(folded) < len(memory)
        assert folded.startswith("# PROJECT_MEMORY — p")
        assert "## Goal" in folded
        assert "## Next Steps" in folded
        assert "compacted" in folded and "sections:" in folded

        # Second call is a no-op: already within budget.
        assert state.compact_memory() is False

    def test_noop_below_limit(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.save_memory("# PROJECT_MEMORY\n\nshort\n")
        assert state.compact_memory() is False
        assert state.load_memory() == "# PROJECT_MEMORY\n\nshort\n"


# ---------------------------------------------------------------------------
# B2 — cumulative context tokens
# ---------------------------------------------------------------------------


class TestCumulativeContextB2:
    def test_tokens_accumulate_and_reset(self, tmp_path: Path) -> None:
        from orchestrator.context_monitor import context_window_tokens

        state = StateManager(build_test_project(tmp_path / "p"))
        state.reset_context_tokens()
        state.add_context_tokens(1000)
        state.add_context_tokens(500)
        project = state.load_project()
        assert project["context"]["cumulative_tokens"] == 1500
        expected = int(round(1500 / context_window_tokens() * 100))
        assert project["context"]["utilization_percent"] == expected

        state.reset_context_tokens()
        project = state.load_project()
        assert project["context"]["cumulative_tokens"] == 0
        assert project["context"]["utilization_percent"] == 0

    def test_manual_utilization_syncs_cumulative(self, tmp_path: Path) -> None:
        from orchestrator.context_monitor import context_window_tokens

        state = StateManager(build_test_project(tmp_path / "p"))
        state.set_context_utilization(50)
        project = state.load_project()
        assert project["context"]["cumulative_tokens"] == context_window_tokens() // 2


# ---------------------------------------------------------------------------
# B3 — Definition of Done additions
# ---------------------------------------------------------------------------


class TestDefinitionOfDoneB3:
    def test_requirement_ids_must_exist_in_requirements_doc(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        docs = project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "REQUIREMENTS.md").write_text(
            "# REQUIREMENTS\n\n- REQ-001 shall boot\n", encoding="utf-8"
        )
        task = dict(orchestrator.get_task("TASK-002"))
        task["requirement_ids"] = ["REQ-001", "REQ-999"]
        problems = orchestrator.definition_of_done(task)
        assert any("REQ-999" in item for item in problems)
        assert not any("REQ-001" in item for item in problems)

        task["requirement_ids"] = ["REQ-001"]
        assert not any("REQ-001" in item for item in orchestrator.definition_of_done(task))

    def test_requirement_ids_without_requirements_doc(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        task = dict(orchestrator.get_task("TASK-002"))
        task["requirement_ids"] = ["REQ-001"]
        problems = orchestrator.definition_of_done(task)
        assert any("REQUIREMENTS.md is missing" in item for item in problems)

    def test_failing_acceptance_result_blocks_done(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        task = dict(orchestrator.get_task("TASK-002"))
        task["acceptance_results"] = [
            {"name": "boots", "status": "PASS"},
            {"name": "sensors", "status": "FAIL"},
        ]
        problems = orchestrator.definition_of_done(task)
        assert any("acceptance check failed: sensors" in item for item in problems)

        task["acceptance_results"] = [{"name": "boots", "status": "PASS"}]
        assert not any(
            "acceptance check failed" in item for item in orchestrator.definition_of_done(task)
        )


# ---------------------------------------------------------------------------
# B4 — payload context injection
# ---------------------------------------------------------------------------


class TestPayloadInjectionB4:
    def test_payload_carries_gating_decision_and_requirements(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        state.append_decision(
            title="swap the bus",
            status="PROPOSED_CHANGE",
            affected_tasks=["TASK-002"],
        )
        docs = project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "REQUIREMENTS.md").write_text(
            "# REQUIREMENTS\n\n- REQ-004 shall sample at 1kHz\n", encoding="utf-8"
        )
        task = dict(state.get_task("TASK-002"))
        task["requirement_ids"] = ["REQ-004"]

        agent = BaseAgent(state_manager=state)
        payload = agent.build_payload(task)
        decisions = payload["context"]["decisions_affecting_task"]
        assert "DEC-002" in decisions and "swap the bus" in decisions
        assert "REQ-004" in payload["context"]["requirements"]

        # Unaffected task sees neither.
        other = dict(state.get_task("TASK-004"))
        other_payload = agent.build_payload(other)
        assert "decisions_affecting_task" not in other_payload["context"]


# ---------------------------------------------------------------------------
# B5 — supervisor diagnosis
# ---------------------------------------------------------------------------


class TestDiagnoseB5:
    def test_diagnose_rules_only_when_no_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "ORCHESTRATOR_LLM_PROVIDER",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "OLLAMA_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        supervisor = SupervisorAgent(project_path=build_test_project(tmp_path / "p"))
        text = supervisor.diagnose()
        assert "State:" in text
        assert "Recommendation:" in text

    def test_diagnose_never_raises_with_use_llm_false(self, tmp_path: Path) -> None:
        supervisor = SupervisorAgent(project_path=build_test_project(tmp_path / "p"))
        assert "State:" in supervisor.diagnose(use_llm=False)


# ---------------------------------------------------------------------------
# B6 — escalation categories / blocking
# ---------------------------------------------------------------------------


class TestEscalationCategoriesB6:
    def test_context_critical_is_advisory_warning(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        StateManager(project).set_context_utilization(90)
        supervisor = SupervisorAgent(state_manager=StateManager(project))
        report = supervisor.check_health()
        context_esc = [e for e in report.escalations if e.category == "context"]
        assert context_esc and context_esc[0].blocking is False
        assert str(report.state) == config.HEALTH_WARNING

    def test_exceeded_loop_escalation_is_blocking(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        state.update_task_execution("TASK-002", attempt_delta=3)
        supervisor = SupervisorAgent(state_manager=state)
        report = supervisor.sync_project_health()
        loop_esc = [e for e in report.escalations if e.category == "loop"]
        assert loop_esc and loop_esc[0].blocking is True
        assert str(report.state) == config.HEALTH_HUMAN_DECISION_REQUIRED
        persisted = state.load_project()["human_decisions"]
        assert any("[loop]" in entry for entry in persisted)


# ---------------------------------------------------------------------------
# C1 — WAITING status for gated tasks
# ---------------------------------------------------------------------------


class TestWaitingStatusC1:
    def test_pending_change_parks_and_approval_releases(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=_plain_resolver,
        )
        orchestrator.run_cycle()
        assert orchestrator.get_task("TASK-003")["status"] == config.TASK_WAITING

        orchestrator.approve_decision("DEC-002", approved=True)
        assert orchestrator.get_task("TASK-003")["status"] == config.TASK_READY

    def test_review_tasks_keep_status_while_gated(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        document = state.load_tasks_document()
        for task in document["tasks"]:
            if task["id"] == "TASK-002":
                task["review"] = {"required": True, "status": "NOT_STARTED"}
        state.save_tasks_document(document)

        class ReviewProposer(BaseAgent):
            AGENT_ID = "plain_agent"

            def execute(self, payload: Dict[str, Any]) -> AgentOutput:
                task_id = str(payload["task"]["id"])
                return self.completed(
                    task_id,
                    "needs review",
                    data={
                        "proposed_change": {
                            "title": "tune the pwm",
                            "affected_tasks": ["TASK-002"],
                        }
                    },
                )

        orchestrator = MasterOrchestrator(
            project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=lambda owner, sm: ReviewProposer(state_manager=sm),
        )
        first = orchestrator.run_cycle()
        assert first and first[0].new_status == config.TASK_REVIEW
        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_REVIEW


# ---------------------------------------------------------------------------
# C3 / E1 — hygiene
# ---------------------------------------------------------------------------


class TestHygieneC3E1:
    def test_git_author_name_system_key_removed(self) -> None:
        assert "git_author_name" not in config.SYSTEM_KEYS.names()

    def test_default_checkpoints_root_honours_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = build_test_project(tmp_path / "p")
        target = tmp_path / "elsewhere"
        monkeypatch.setenv("ORCHESTRATOR_CHECKPOINTS_DIR", str(target))
        manager = CheckpointManager(project)
        assert manager.checkpoints_root == target.resolve() / project.name
