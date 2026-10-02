"""Tests for specialist agents, artifact emission, DoD enforcement and review flow.

Covers GAP_ANALYSIS tasks T4-T8:
* the 9-agent registry and framework spec loading,
* artifact materialization into ``docs/`` on every completed run,
* Definition-of-Done gating of DONE,
* independent review dispatch (PASS / PASS WITH ACTIONS / FAIL) with
  correction-task creation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from conftest import FakeLLMClient, MEMORY_TEMPLATE

REPO_ROOT = Path(__file__).resolve().parent
LIVE_PROJECT = REPO_ROOT / "projects" / "kid-robot-face"
from orchestrator import config
from orchestrator.agents import (
    AGENT_REGISTRY,
    ArchitectureAgent,
    DocumentationAgent,
    HardwareAgent,
    LLMAgent,
    PlanningAgent,
    ResearchAgent,
    ReviewAgent,
    SoftwareAgent,
    TestAgent,
    create_agent,
)
from orchestrator.agents.base_agent import AgentError, AgentOutput, BaseAgent
from orchestrator.agents.requirements_agent import RequirementsAgent
from orchestrator.orchestrator import MasterOrchestrator

SPECIALIST_NAMES = [
    "research_agent",
    "architecture_agent",
    "planning_agent",
    "hardware_agent",
    "software_agent",
    "test_agent",
    "review_agent",
    "documentation_agent",
]

SPECIALIST_CLASSES = {
    "research_agent": ResearchAgent,
    "architecture_agent": ArchitectureAgent,
    "planning_agent": PlanningAgent,
    "hardware_agent": HardwareAgent,
    "software_agent": SoftwareAgent,
    "test_agent": TestAgent,
    "review_agent": ReviewAgent,
    "documentation_agent": DocumentationAgent,
}


def _answer(task_id: str, agent_id: str, **data: Any) -> str:
    payload = {
        "agent_id": agent_id,
        "task_id": task_id,
        "status": "completed",
        "summary": f"{agent_id} completed {task_id} with measurable output",
        "data": data or {"result": "ok"},
        "errors": [],
        "warnings": [],
    }
    return json.dumps(payload)


def _make_review_required(project: Path) -> None:
    document = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))
    for task in document["tasks"]:
        if task["id"] == "TASK-002":
            task["review"]["required"] = True
    (project / "TASKS.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


def _set_acceptance_criteria(project: Path, task_id: str, criteria: List[str]) -> None:
    document = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))
    for task in document["tasks"]:
        if task["id"] == task_id:
            task["acceptance_criteria"] = criteria
    (project / "TASKS.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


class FakeReviewAgent(BaseAgent):
    """Deterministic review agent returning a canned outcome."""

    AGENT_ID = "review_agent"

    def __init__(
        self,
        project_path: Any = None,
        state_manager: Any = None,
        outcome: str = "PASS",
        corrections: Optional[List[str]] = None,
        actions: Optional[List[str]] = None,
        invalid: bool = False,
        fail_execution: bool = False,
    ) -> None:
        super().__init__(project_path=project_path, state_manager=state_manager)
        self.outcome = outcome
        self.corrections = list(corrections or [])
        self.actions = list(actions or [])
        self.invalid = invalid
        self.fail_execution = fail_execution
        self.calls: List[str] = []

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task = payload.get("task") or {}
        task_id = str(task.get("id", ""))
        self.calls.append(task_id)
        if self.fail_execution:
            return self.failed(task_id, "review backend down", errors=["no model"])
        if self.invalid:
            return self.completed(task_id, "reviewed without a structured outcome", data={})
        return self.completed(
            task_id,
            f"review finished with {self.outcome}",
            data={
                "review_status": self.outcome,
                "findings": ["measured"],
                "corrections": self.corrections,
                "actions": self.actions,
            },
        )


# ---------------------------------------------------------------------------
# T4-T6 — registry, spec loading, resolution
# ---------------------------------------------------------------------------


class TestSpecialistRegistry:
    def test_all_specialists_registered(self) -> None:
        for name in SPECIALIST_NAMES:
            assert name in AGENT_REGISTRY, f"{name} missing from registry"

    def test_create_agent_instantiates_each_specialist(self, test_project: Path) -> None:
        for name in SPECIALIST_NAMES:
            agent = create_agent(name, project_path=test_project)
            assert isinstance(agent, LLMAgent)
            assert agent.AGENT_ID == name

    def test_every_specialist_loads_its_framework_spec(self, test_project: Path) -> None:
        for name in SPECIALIST_NAMES:
            agent = create_agent(name, project_path=test_project)
            spec = agent.spec_text()
            assert spec, f"no framework spec loaded for {name}"
            assert spec.startswith("# ")

    def test_firmware_alias_resolves_to_software_agent(self, test_project: Path) -> None:
        agent = create_agent("firmware_agent", project_path=test_project)
        assert isinstance(agent, SoftwareAgent)

    def test_every_specialist_carries_the_offline_execution_rule(
        self, test_project: Path
    ) -> None:
        for name in SPECIALIST_NAMES:
            agent = create_agent(name, project_path=test_project)
            assert BaseAgent.OFFLINE_EXECUTION_RULE in agent.system_rules(), (
                f"{name} misses the offline anti-refusal rule"
            )
        hardware = create_agent("hardware_agent", project_path=test_project)
        assert "never have physical access" in hardware.system_rules()

    def test_every_specialist_carries_the_global_directives(
        self, test_project: Path
    ) -> None:
        """GAP-CRIT-08: base rules must survive subclass shadowing.

        Every registered agent overrides ``SYSTEM_RULES``, so anything placed
        only in that block never reached a model — which is how rule 6
        (``data.acceptance_results``) came to be specified but never delivered.
        ``GLOBAL_SYSTEM_RULES`` is read by ``system_rules()`` alone, so it
        cannot be shadowed.
        """
        for name in SPECIALIST_NAMES:
            agent = create_agent(name, project_path=test_project)
            rules = agent.system_rules()
            assert BaseAgent.GLOBAL_SYSTEM_RULES in rules, (
                f"{name} misses the global directives"
            )
            assert "NEVER fabricate" in rules, f"{name} misses the anti-fabrication directive"
            assert "data.acceptance_results" in rules, (
                f"{name} misses the acceptance-evidence mandate"
            )

    def test_global_directives_precede_subclass_rules(
        self, test_project: Path
    ) -> None:
        """Ordering matters: base directives first, subclass cannot dilute."""
        for name in SPECIALIST_NAMES:
            rules = create_agent(name, project_path=test_project).system_rules()
            global_at = rules.find("CRITICAL SYSTEM DIRECTIVES")
            own = getattr(type(create_agent(name, project_path=test_project)), "SYSTEM_RULES", "")
            own_head = own.strip().splitlines()[0][:30] if own.strip() else ""
            if own_head:
                assert global_at != -1, f"{name} has no global block"
                assert global_at < rules.find(own_head), (
                    f"{name} places its own rules before the global directives"
                )

    def test_orchestrator_resolves_formerly_missing_owners(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        for owner in ("architecture_agent", "hardware_agent", "planning_agent"):
            resolved = orchestrator.resolve_agent(owner)
            assert resolved.AGENT_ID == owner

    def test_specialist_end_to_end_with_injected_model(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        arch_calls: List[Dict[str, Any]] = []
        hw_calls: List[Dict[str, Any]] = []

        def resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
            if owner == "architecture_agent":
                client = FakeLLMClient([_answer("TASK-003", owner)])
                arch_calls.append(client)
                return ArchitectureAgent(state_manager=state_manager, llm_client=client)
            if owner == "hardware_agent":
                client = FakeLLMClient([_answer("TASK-004", owner)])
                hw_calls.append(client)
                return HardwareAgent(state_manager=state_manager, llm_client=client)
            return None

        orchestrator = MasterOrchestrator(
            test_project,
            checkpoints_root=checkpoints_root,
            auto_checkpoint=False,
            agent_resolver=resolver,
        )
        orchestrator.run_cycle()  # TASK-002 -> DONE
        orchestrator.run_cycle()  # TASK-003 -> DONE
        orchestrator.run_cycle()  # TASK-004 -> DONE

        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_DONE
        assert orchestrator.get_task("TASK-003")["status"] == config.TASK_DONE
        assert orchestrator.get_task("TASK-004")["status"] == config.TASK_DONE
        for task_id in ("TASK-003", "TASK-004"):
            assert (test_project / "docs" / f"{task_id}.md").exists()

        # The framework spec must have reached the model prompt.
        prompt = arch_calls[0].calls[0]["messages"][0]["content"]
        assert "# Agent specification" in prompt
        assert "Architecture" in prompt
        system = arch_calls[0].calls[0]["system"]
        assert "architect" in system.lower() or "Architecture rules" in system

    def test_registered_specialist_used_before_registry(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        fake_review = FakeReviewAgent(project_path=test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        orchestrator.register_agent(fake_review)
        assert orchestrator.resolve_agent("review_agent") is fake_review


# ---------------------------------------------------------------------------
# T7 — artifact emission
# ---------------------------------------------------------------------------


class TestArtifactEmission:
    def test_completed_run_materializes_expected_outputs(
        self, test_project: Path
    ) -> None:
        from orchestrator.agents.requirements_agent import RequirementsAgent

        agent = RequirementsAgent(project_path=test_project)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED
        artifact = test_project / "docs" / "TASK-002.md"
        assert artifact.exists()
        assert "docs/TASK-002.md" in output.artifacts
        content = artifact.read_text(encoding="utf-8")
        assert "TASK-002" in content
        assert output.summary in content

    def test_model_supplied_document_wins(self, test_project: Path) -> None:
        from orchestrator.agents.requirements_agent import RequirementsAgent

        agent = RequirementsAgent(project_path=test_project)
        task = agent.state_manager.get_task("TASK-002")
        task = dict(task)
        task["expected_outputs"] = ["REQUIREMENTS.md"]
        output = agent.run(task)
        # requirements agent does not supply documents; default render is used
        assert (test_project / "docs" / "REQUIREMENTS.md").exists()

        # now simulate an LLM agent that supplies its own document body
        answer = _answer(
            "TASK-002",
            "dummy",
            documents={"REQUIREMENTS.md": "# REQUIREMENTS\n\n- REQ-001 verified"},
        )

        class Dummy(LLMAgent):
            AGENT_ID = "dummy_agent"

        dummy = Dummy(project_path=test_project, llm_client=FakeLLMClient([answer]))
        out = dummy.run(task)
        assert out.status == config.AGENT_STATUS_COMPLETED
        body = (test_project / "docs" / "REQUIREMENTS.md").read_text(encoding="utf-8")
        assert body == "# REQUIREMENTS\n\n- REQ-001 verified"

    def test_flat_data_key_materializes_without_documents_wrapper(
        self, test_project: Path
    ) -> None:
        # Models sometimes put file bodies directly on data instead of under
        # data.documents — materialization must still pick them up (G16).
        from orchestrator.agents.requirements_agent import RequirementsAgent

        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["REQUIREMENTS.md"]
        answer = {
            "agent_id": "dummy",
            "task_id": "TASK-002",
            "status": "completed",
            "summary": "flat key deliverable",
            "data": {"REQUIREMENTS.md": "# REQUIREMENTS\n\n- REQ-042 flat key"},
            "errors": [],
            "warnings": [],
        }

        class Dummy(LLMAgent):
            AGENT_ID = "dummy_agent"

        dummy = Dummy(
            project_path=test_project, llm_client=FakeLLMClient([json.dumps(answer)])
        )
        out = dummy.run(task)
        assert out.status == config.AGENT_STATUS_COMPLETED, out.errors
        body = (test_project / "docs" / "REQUIREMENTS.md").read_text(encoding="utf-8")
        assert body == "# REQUIREMENTS\n\n- REQ-042 flat key"

    def test_failed_run_writes_nothing(self, test_project: Path) -> None:
        from orchestrator.agents.requirements_agent import RequirementsAgent

        (test_project / "PROJECT_MEMORY.md").write_text("", encoding="utf-8")
        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["input_files"] = []
        task["notes"] = ""
        task["expected_outputs"] = []
        task["acceptance_criteria"] = []
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert not (test_project / "docs" / "TASK-002.md").exists()

    def test_review_run_does_not_overwrite_creator_artifact(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        creator_content_before: str
        orchestrator.run_cycle()  # TASK-002 -> REVIEW (artifacts written)
        artifact = test_project / "docs" / "TASK-002.md"
        assert artifact.exists()
        creator_content_before = artifact.read_text(encoding="utf-8")

        fake_review = FakeReviewAgent(state_manager=orchestrator.state)
        orchestrator.register_agent(fake_review)
        orchestrator.run_cycle()  # review pass
        after = artifact.read_text(encoding="utf-8")
        assert after == creator_content_before, "review must not clobber the artifact"
        assert (test_project / "docs" / "REVIEW-TASK-002.md").exists()


class TestTruncationRepair:
    """G17: one compact follow-up recovers tasks whose reply got token-capped."""

    TRUNCATED = (
        '```json\n{"agent_id": "dummy", "task_id": "TASK-002", '
        '"status": "completed", "summary": "half a reply that never closes'
    )

    @staticmethod
    def _task(test_project: Path) -> Dict[str, Any]:
        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["target.txt"]
        return task

    def test_truncated_reply_recovered_by_repair_call(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        repair = _answer(
            "TASK-002",
            "dummy",
            edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
        )
        client = FakeLLMClient([self.TRUNCATED, repair])

        class Dummy(LLMAgent):
            AGENT_ID = "dummy_agent"

        dummy = Dummy(project_path=test_project, llm_client=client)
        output = dummy.run(self._task(test_project))
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        assert len(client.calls) == 2
        assert any("truncation-repair" in item for item in output.warnings)

    def test_failed_repair_keeps_original_error_detail(
        self, test_project: Path
    ) -> None:
        client = FakeLLMClient([self.TRUNCATED, self.TRUNCATED + " also cut"])

        class Dummy(LLMAgent):
            AGENT_ID = "dummy_agent"

        dummy = Dummy(project_path=test_project, llm_client=client)
        output = dummy.run(self._task(test_project))
        assert output.status == config.AGENT_STATUS_FAILED
        assert "not parseable JSON" in output.summary
        assert any("truncation-repair attempt also failed" in item for item in output.errors)
        assert len(client.calls) == 2

    def test_no_repair_call_on_clean_parse(self, test_project: Path) -> None:
        client = FakeLLMClient([_answer("TASK-002", "dummy")])

        class Dummy(LLMAgent):
            AGENT_ID = "dummy_agent"

        dummy = Dummy(project_path=test_project, llm_client=client)
        output = dummy.run(self._task(test_project))
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert len(client.calls) == 1


class TestEditsAuthoring:
    """G16: data.edits patches real project files; contract reaches agents."""

    @staticmethod
    def _dummy_task(test_project: Path) -> Dict[str, Any]:
        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["target.txt"]
        return task

    @staticmethod
    def _dummy(test_project: Path, answer: str) -> LLMAgent:
        class Dummy(LLMAgent):
            AGENT_ID = "dummy_agent"

        return Dummy(project_path=test_project, llm_client=FakeLLMClient([answer]))

    def test_edits_apply_to_real_file_and_mirror_docs(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        task = self._dummy_task(test_project)
        answer = _answer(
            "TASK-002",
            "dummy",
            edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
        )
        output = self._dummy(test_project, answer).run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        mirror = (test_project / "docs" / "target.txt").read_text(encoding="utf-8")
        assert "BETA13" in mirror
        assert "target.txt" in output.artifacts
        assert "docs/target.txt" in output.artifacts

    def test_ambiguous_search_fails_with_precise_error(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("BETA\nmiddle\nBETA\n", encoding="utf-8")
        task = self._dummy_task(test_project)
        answer = _answer(
            "TASK-002",
            "dummy",
            edits={"target.txt": {"search": "BETA", "replace": "X"}},
        )
        output = self._dummy(test_project, answer).run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("matched 2 time(s)" in item for item in output.errors)
        assert target.read_text(encoding="utf-8") == "BETA\nmiddle\nBETA\n"

    def test_missing_search_fails(self, test_project: Path) -> None:
        target = test_project / "target.txt"
        target.write_text("only here\n", encoding="utf-8")
        task = self._dummy_task(test_project)
        answer = _answer(
            "TASK-002",
            "dummy",
            edits={"target.txt": {"search": "absent", "replace": "X"}},
        )
        output = self._dummy(test_project, answer).run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("matched 0 time(s)" in item for item in output.errors)

    def test_path_escape_rejected(self, test_project: Path) -> None:
        task = self._dummy_task(test_project)
        answer = _answer(
            "TASK-002",
            "dummy",
            edits={"../outside.txt": {"search": "a", "replace": "b"}},
        )
        output = self._dummy(test_project, answer).run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("project-relative" in item for item in output.errors)

    def test_missing_edit_target_is_created_with_a_warning(
        self, test_project: Path
    ) -> None:
        """A patch aimed at a file that is not there is a creation, not a failure.

        Behaviour change: this previously failed the dispatch with "edits target
        does not exist", discarding content the agent had actually delivered.
        There is nothing to patch and nothing to overwrite, so `replace` is
        written as the file body. The mistyped-path case is still visible --
        as a warning -- rather than being lost with the error.
        """
        task = self._dummy_task(test_project)
        answer = _answer(
            "TASK-002",
            "dummy",
            edits={"no_such_file.txt": {"search": "a", "replace": "b"}},
        )
        output = self._dummy(test_project, answer).run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (test_project / "no_such_file.txt").read_text(encoding="utf-8") == "b"
        assert any("created missing file" in item for item in output.warnings), (
            output.warnings
        )

    def test_list_of_edits_applies_in_order(self, test_project: Path) -> None:
        target = test_project / "target.txt"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")
        task = self._dummy_task(test_project)
        answer = _answer(
            "TASK-002",
            "dummy",
            edits={
                "target.txt": [
                    {"search": "one", "replace": "ONE"},
                    {"search": "three", "replace": "THREE"},
                ]
            },
        )
        output = self._dummy(test_project, answer).run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "ONE\ntwo\nTHREE\n"

    def test_list_edit_second_step_failure_leaves_file_untouched(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        original = "one\ntwo\nthree\n"
        target.write_text(original, encoding="utf-8")
        task = self._dummy_task(test_project)
        answer = _answer(
            "TASK-002",
            "dummy",
            edits={
                "target.txt": [
                    {"search": "one", "replace": "ONE"},
                    {"search": "absent", "replace": "X"},
                ]
            },
        )
        output = self._dummy(test_project, answer).run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("[1]" in item and "matched 0" in item for item in output.errors)
        assert target.read_text(encoding="utf-8") == original

    def test_authoring_contract_in_every_agents_rules(self, tmp_path: Path) -> None:
        agent = RequirementsAgent(project_path=tmp_path)
        rules = agent.system_rules()
        assert "Authoring contract" in rules
        assert 'data.edits' in rules
        assert "NOT RUN" in rules


# ---------------------------------------------------------------------------
# T8a — Definition of Done
# ---------------------------------------------------------------------------


class TestDefinitionOfDone:
    def test_done_blocked_without_acceptance_criteria(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _set_acceptance_criteria(test_project, "TASK-002", [])
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        result = orchestrator.run_task("TASK-002")
        assert result.new_status == config.TASK_FAILED
        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_FAILED
        assert "DoD unmet" in (task["execution"]["last_error"] or "")
        assert "no acceptance criteria" in (task["execution"]["last_error"] or "")

    def test_definition_of_done_checks(self, test_project: Path, checkpoints_root: Path) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        task = dict(orchestrator.get_task("TASK-002"))
        problems = orchestrator.definition_of_done(task)
        assert any("expected output not materialized" in item for item in problems)

        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "TASK-002.md").write_text("done", encoding="utf-8")
        assert orchestrator.definition_of_done(task) == []

        task["review"] = {"required": True, "status": "NOT_STARTED"}
        problems = orchestrator.definition_of_done(task)
        assert any("independent review not passed" in item for item in problems)

    def test_dod_rejects_prose_metadata_for_existing_file(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        source = test_project / "main" / "input_service.c"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            '#include "freertos/FreeRTOS.h"\n'
            "static void input_dispatch_task(void *arg) {\n"
            "    while (1) { vTaskDelay(pdMS_TO_TICKS(1000)); }\n"
            "}\n",
            encoding="utf-8",
        )
        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "input_service.c").write_text(
            "# input_service.c\n\n- Task: TASK-003\n\n## Data\n\n"
            '```json\n{"summary": "implemented decoupled refresh"}\n```\n',
            encoding="utf-8",
        )
        task = {
            "acceptance_criteria": ["fix applied"],
            "expected_outputs": ["main/input_service.c"],
        }
        problems = orchestrator.definition_of_done(task)
        assert any("shares no line" in item for item in problems)
        assert any("data.edits" in item for item in problems)

        # an edits-style delivery (mirror = post-edit file content) passes:
        (docs / "input_service.c").write_text(
            '#include "freertos/FreeRTOS.h"\n'
            "static void input_dispatch_task(void *arg) {\n"
            "    /* clock moved to display_refresh_task */\n"
            "    while (1) { vTaskDelay(pdMS_TO_TICKS(1000)); }\n"
            "}\n",
            encoding="utf-8",
        )
        assert orchestrator.definition_of_done(task) == []

    def test_dod_content_check_skips_new_files(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "new_module.c").write_text(
            "#include <stdio.h>\nint main(void) { return 0; }\n", encoding="utf-8"
        )
        task = {
            "acceptance_criteria": ["authored"],
            "expected_outputs": ["src/new_module.c"],
        }
        assert orchestrator.definition_of_done(task) == []

    def test_review_pass_cannot_bypass_dod(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        _set_acceptance_criteria(test_project, "TASK-002", [])
        state = orchestrator.state
        state.update_task_status("TASK-002", config.TASK_REVIEW)
        state.set_review_status("TASK-002", "READY")
        status = orchestrator.complete_review("TASK-002", passed=True)
        assert status == config.TASK_FAILED
        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_FAILED
        assert "DoD unmet" in (task.get("notes") or "")


# ---------------------------------------------------------------------------
# G19 — Definition-of-Done auto-repair + failure feedback loop
# ---------------------------------------------------------------------------


class TestDoDAutoRepair:
    """A DoD rejection is fed back to the model once instead of failing cold."""

    @staticmethod
    def _task(test_project: Path) -> Dict[str, Any]:
        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["target.txt"]
        return task

    @staticmethod
    def _meta_answer() -> str:
        # Prose metadata: valid schema, no documents/edits — exactly what the
        # model returned in the screensaver runs that G18 rejected.
        return _answer(
            "TASK-002",
            "software_agent",
            implementation_scope="decoupled refresh",
            files_changed=["target.txt"],
        )

    def test_repair_delivery_patches_after_rejection(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        task = self._task(test_project)
        good = _answer(
            "TASK-002",
            "software_agent",
            edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
        )
        client = FakeLLMClient([self._meta_answer(), good])

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        dummy = Dummy(project_path=test_project, llm_client=client)
        output = dummy.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED

        repaired = dummy.repair_delivery(
            task, output, ["delivered docs/target.txt shares no line with existing target.txt"]
        )
        assert repaired is not None
        assert repaired.status == config.AGENT_STATUS_COMPLETED
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        assert len(client.calls) == 2
        assert any("DoD auto-repair" in item for item in repaired.warnings)

    def test_repair_delivery_none_when_repair_reply_unusable(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        task = self._task(test_project)
        client = FakeLLMClient([self._meta_answer(), "sorry, no JSON here"])

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        dummy = Dummy(project_path=test_project, llm_client=client)
        output = dummy.run(task)
        repaired = dummy.repair_delivery(task, output, ["a problem"])
        assert repaired is None
        assert len(client.calls) == 2
        assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"

    def test_deterministic_agent_has_no_repair(self, test_project: Path) -> None:
        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["TASK-002.md"]
        output = agent.run(task)
        assert agent.repair_delivery(task, output, ["a problem"]) is None

    def test_dispatch_auto_repair_turns_prose_delivery_into_done(
        self, test_project: Path, checkpoints_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        for entry in document["tasks"]:
            if entry.get("id") == "TASK-002":
                entry["expected_outputs"] = ["target.txt"]
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        good = _answer(
            "TASK-002",
            "software_agent",
            edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
        )
        client = FakeLLMClient([self._meta_answer(), good])

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        dummy = Dummy(project_path=test_project, llm_client=client)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        monkeypatch.setattr(orchestrator, "resolve_agent", lambda owner, fresh=False: dummy)

        result = orchestrator.dispatch("TASK-002")
        assert result.new_status == config.TASK_DONE
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        assert len(client.calls) == 2
        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_DONE
        assert task["execution"]["last_error"] is None
        assert task["execution"]["retry_reason"] is None

    def test_dispatch_keeps_rejection_when_repair_fails(
        self, test_project: Path, checkpoints_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        for entry in document["tasks"]:
            if entry.get("id") == "TASK-002":
                entry["expected_outputs"] = ["target.txt"]
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        client = FakeLLMClient([self._meta_answer(), "still just prose, no JSON"])

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        dummy = Dummy(project_path=test_project, llm_client=client)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        monkeypatch.setattr(orchestrator, "resolve_agent", lambda owner, fresh=False: dummy)

        result = orchestrator.dispatch("TASK-002")
        assert result.new_status == config.TASK_FAILED
        task = orchestrator.get_task("TASK-002")
        assert "shares no line" in (task["execution"]["last_error"] or "")
        assert "DoD unmet" in (task.get("notes") or "")
        assert len(client.calls) == 2

    def test_recovery_feedback_reaches_the_prompt(self, test_project: Path) -> None:
        agent = RequirementsAgent(project_path=test_project)
        task: Dict[str, Any] = {
            "id": "TASK-002",
            "execution": {"retry_reason": "deliver via data.edits, not prose"},
        }
        payload = agent.build_payload(task)
        from orchestrator.prompt_builder import build_prompt

        prompt = build_prompt(payload)
        assert "recovery_feedback_from_previous_attempt" in prompt
        assert "deliver via data.edits, not prose" in prompt


# ---------------------------------------------------------------------------
# G20 — multi-turn edit-session delivery
# ---------------------------------------------------------------------------


class TestEditSession:
    """Software-class agents deliver through bounded apply-and-verify turns."""

    @staticmethod
    def _task(test_project: Path) -> Dict[str, Any]:
        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["target.txt"]
        return task

    @staticmethod
    def _session_agent(test_project: Path, responses: List[Any]) -> Any:
        class SessionDummy(LLMAgent):
            AGENT_ID = "software_agent"
            EDIT_SESSION_TURNS = 3

        client = FakeLLMClient(list(responses))
        agent = SessionDummy(project_path=test_project, llm_client=client)
        return agent, client

    def test_session_converges_with_feedback_between_turns(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        agent, client = self._session_agent(
            test_project,
            [
                _answer("TASK-002", "software_agent"),  # no edits: wrapper lands
                _answer(
                    "TASK-002",
                    "software_agent",
                    edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
                ),
            ],
        )
        task = self._task(test_project)
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        assert len(client.calls) == 2
        turn2_prompt = client.calls[1]["messages"][0]["content"]
        assert "Edit session — turn 2 of 3" in turn2_prompt
        assert "Definition-of-Done problems remain" in turn2_prompt
        assert "shares no line" in turn2_prompt
        assert any("edit session" in item for item in output.warnings)

    def test_session_feeds_apply_errors_back(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        agent, client = self._session_agent(
            test_project,
            [
                _answer(
                    "TASK-002",
                    "software_agent",
                    edits={"target.txt": {"search": "absent", "replace": "X"}},
                ),
                _answer(
                    "TASK-002",
                    "software_agent",
                    edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
                ),
            ],
        )
        task = self._task(test_project)
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        assert len(client.calls) == 2
        turn2_prompt = client.calls[1]["messages"][0]["content"]
        assert "could not be applied" in turn2_prompt
        assert "matched 0" in turn2_prompt

    def test_session_gives_up_after_max_turns(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        agent, client = self._session_agent(
            test_project,
            [_answer("TASK-002", "software_agent") for _ in range(3)],
        )
        task = self._task(test_project)
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("shares no line" in item for item in output.errors)
        assert len(client.calls) == 3
        assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"

    def test_documents_cannot_deliver_existing_file(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        target = test_project / "target.txt"
        body = "alpha\nBETA\ngamma\n"
        target.write_text(body, encoding="utf-8")
        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "target.txt").write_text(body, encoding="utf-8")
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        agent = RequirementsAgent(project_path=test_project)
        task = self._task(test_project)
        output = agent.completed(
            "TASK-002", "delivered full body", data={"documents": {"target.txt": body}}
        )
        problems = orchestrator.definition_of_done(task, output)
        assert any(
            "exists in the project" in item and "data.edits" in item
            for item in problems
        )

    def test_single_shot_agents_stay_single_shot(self, test_project: Path) -> None:
        from orchestrator.agents import ResearchAgent

        assert ResearchAgent.EDIT_SESSION_TURNS == 0

    def test_dispatch_runs_edit_session_end_to_end(
        self, test_project: Path, checkpoints_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        for entry in document["tasks"]:
            if entry.get("id") == "TASK-002":
                entry["expected_outputs"] = ["target.txt"]
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        agent, client = self._session_agent(
            test_project,
            [
                _answer("TASK-002", "software_agent"),
                _answer(
                    "TASK-002",
                    "software_agent",
                    edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
                ),
            ],
        )
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        monkeypatch.setattr(orchestrator, "resolve_agent", lambda owner, fresh=False: agent)

        result = orchestrator.dispatch("TASK-002")
        assert result.new_status == config.TASK_DONE
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# T8b — independent review flow
# ---------------------------------------------------------------------------


class TestReviewFlow:
    def test_review_pass_completes_task(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(state_manager=orchestrator.state, outcome="PASS")
        orchestrator.register_agent(fake)

        first = orchestrator.run_cycle()
        assert [r.task_id for r in first] == ["TASK-002"]
        assert first[0].new_status == config.TASK_REVIEW
        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_REVIEW

        second = orchestrator.run_cycle()
        review_results = [r for r in second if r.agent_id == "review_agent"]
        assert review_results, "the pending review must be dispatched"
        assert review_results[0].new_status == config.TASK_DONE
        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_DONE
        assert task["review"]["status"] == "PASS"
        assert fake.calls == ["TASK-002"]

    def test_review_fail_creates_correction_tasks(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(
            state_manager=orchestrator.state,
            outcome="FAIL",
            corrections=["Recalculate power budget", "Fix REQ-005 traceability"],
        )
        orchestrator.register_agent(fake)
        orchestrator.run_cycle()
        orchestrator.run_cycle()

        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_FAILED
        assert task["review"]["status"] == "FAIL"
        followups = [
            t for t in orchestrator.state.load_tasks() if t["id"] in ("TASK-005", "TASK-006")
        ]
        assert len(followups) == 2
        assert followups[0]["id"] == "TASK-005"
        assert followups[0]["owner"] == "requirements_agent"
        assert followups[0]["acceptance_criteria"] == ["Recalculate power budget"]
        assert followups[1]["acceptance_criteria"] == ["Fix REQ-005 traceability"]
        assert "Follow-up tasks created from TASK-002" in orchestrator.state.load_current_state()

    def test_pass_with_actions_marks_accepted_limitation(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(
            state_manager=orchestrator.state,
            outcome="PASS WITH ACTIONS",
            actions=["Add latency regression test"],
        )
        orchestrator.register_agent(fake)
        orchestrator.run_cycle()
        orchestrator.run_cycle()

        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_DONE_WITH_LIMITATION
        assert task["review"]["status"] == "PASS WITH ACTIONS"
        assert orchestrator.state.find_task("TASK-005") is not None

    def test_invalid_review_outcome_requeues_review(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(state_manager=orchestrator.state, invalid=True)
        orchestrator.register_agent(fake)
        orchestrator.run_cycle()
        results = orchestrator.run_cycle()

        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_REVIEW
        assert task["review"]["status"] == "READY"
        assert task["execution"]["attempt_count"] >= 2

    def test_reviewer_is_never_the_task_owner(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(state_manager=orchestrator.state)
        orchestrator.register_agent(fake)
        orchestrator.run_cycle()
        results = orchestrator.run_cycle()
        review_results = [r for r in results if r.agent_id == "review_agent"]
        assert review_results
        assert review_results[0].agent_id != orchestrator.get_task("TASK-002")["owner"]

    def test_run_task_routes_review_task_to_dispatch_review(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(state_manager=orchestrator.state, outcome="PASS")
        orchestrator.register_agent(fake)
        orchestrator.run_task("TASK-002")
        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_REVIEW

        result = orchestrator.run_task("TASK-002")
        assert result.agent_id == "review_agent"
        assert result.new_status == config.TASK_DONE

    def test_broken_review_backend_requeues(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(
            state_manager=orchestrator.state, fail_execution=True
        )
        orchestrator.register_agent(fake)
        orchestrator.run_cycle()
        orchestrator.run_cycle()
        task = orchestrator.get_task("TASK-002")
        assert task["status"] == config.TASK_REVIEW
        assert task["review"]["status"] == "READY"
        assert task["execution"]["attempt_count"] >= 2

    def test_review_report_artifact_written(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        _make_review_required(test_project)
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        fake = FakeReviewAgent(state_manager=orchestrator.state, outcome="PASS")
        orchestrator.register_agent(fake)
        orchestrator.run_cycle()
        results = orchestrator.run_cycle()
        review_results = [r for r in results if r.agent_id == "review_agent"]
        assert any(
            "docs/REVIEW-TASK-002.md" in r.output.artifacts for r in review_results
        )
        report = test_project / "docs" / "REVIEW-TASK-002.md"
        assert report.exists()
        assert "review finished with PASS" in report.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# G12 — review outcome repair (model omitted/misplaced data.review_status)
# ---------------------------------------------------------------------------


class TestReviewOutcomeRepair:
    @pytest.fixture()
    def agent(self, test_project: Path) -> ReviewAgent:
        return ReviewAgent(project_path=test_project)

    @staticmethod
    def _parsed(**data_fields: Any) -> Dict[str, Any]:
        return {"status": "completed", "summary": "reviewed", "data": dict(data_fields)}

    def test_rules_state_the_output_contract(self, agent: ReviewAgent) -> None:
        rules = agent.system_rules()
        assert "data.review_status" in rules
        assert "PASS WITH ACTIONS" in rules

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PASS", "PASS"),
            ("pass", "PASS"),
            ("LGTM", "PASS"),
            ("approved", "PASS"),
            ("PASS WITH ACTIONS", "PASS WITH ACTIONS"),
            ("pass_with_actions", "PASS WITH ACTIONS"),
            ("conditional pass", "PASS WITH ACTIONS"),
            ("approved_with_comments", "PASS WITH ACTIONS"),
            ("FAIL", "FAIL"),
            ("changes requested", "FAIL"),
            ("needs_work", "FAIL"),
        ],
    )
    def test_variant_spellings_normalize(
        self, agent: ReviewAgent, raw: str, expected: str
    ) -> None:
        out = agent.output_from_parsed(self._parsed(review_status=raw))
        assert out.data["review_status"] == expected

    def test_alternate_keys_are_consulted(self, agent: ReviewAgent) -> None:
        out = agent.output_from_parsed(self._parsed(outcome="CONDITIONAL PASS"))
        assert out.data["review_status"] == "PASS WITH ACTIONS"
        out = agent.output_from_parsed(self._parsed(verdict="fail"))
        assert out.data["review_status"] == "FAIL"
        out = agent.output_from_parsed(self._parsed(review={"status": "APPROVED"}))
        assert out.data["review_status"] == "PASS"

    def test_missing_everything_raises_with_received_values(
        self, agent: ReviewAgent
    ) -> None:
        from orchestrator.agents.base_agent import AgentOutputError

        with pytest.raises(AgentOutputError) as excinfo:
            agent.output_from_parsed(
                {"status": "completed", "summary": "ok", "data": {}}
            )
        message = str(excinfo.value)
        assert "data.review_status" in message
        assert "received" in message


# ---------------------------------------------------------------------------
# Requirements agent (moved from test_orchestrator_pipeline.py)
# ---------------------------------------------------------------------------

class TestRequirementsAgent:
    def test_block_and_table_parsing(self) -> None:
        requirements = RequirementsAgent.extract_requirements(MEMORY_TEMPLATE)
        ids = [req["id"] for req in requirements]
        assert ids == [
            "REQ-001",
            "REQ-002",
            "REQ-003",
            "REQ-004",
            "REQ-005",
            "REQ-006",
        ]
        by_id = {req["id"]: req for req in requirements}
        assert by_id["REQ-001"]["source_format"] == "table"
        assert by_id["REQ-005"]["source_format"] == "block"
        assert by_id["REQ-005"]["type"] == "NON-FUNCTIONAL"
        assert by_id["REQ-005"]["priority"] == "MUST"
        assert len(by_id["REQ-005"]["acceptance_criteria"]) == 2
        assert by_id["REQ-005"]["related_tests"] == ["TEST-001"]

    def test_validation_flags_duplicates_and_vague_criteria(self) -> None:
        agent = RequirementsAgent(project_path=LIVE_PROJECT)
        requirements = RequirementsAgent.extract_requirements(MEMORY_TEMPLATE)
        requirements.append(dict(requirements[0]))
        errors, warnings = agent.validate_requirements(requirements)
        assert any("Duplicate" in error for error in errors)
        assert any("vague term 'fast'" in warning for warning in warnings)

    def test_end_to_end_execution_returns_structured_data(self, test_project: Path) -> None:
        agent = RequirementsAgent(project_path=test_project)
        state = agent.state_manager
        task = state.get_task("TASK-002")
        assert task["status"] == "READY"

        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED
        assert output.task_id == "TASK-002"
        assert output.agent_id == "requirements_agent"
        assert output.data["count"] == 6
        assert output.data["approved"] == 4
        assert output.data["proposed"] == 2
        assert set(output.data["traceability"]) == {
            "REQ-001",
            "REQ-002",
            "REQ-003",
            "REQ-004",
            "REQ-005",
            "REQ-006",
        }
        assert output.data["traceability"]["REQ-005"]["tests"] == ["TEST-001"]
        assert output.data["status_summary"]["total"] == 6
        assert any("vague term 'fast'" in warning for warning in output.warnings)
        assert output.data["assumptions"] == ["A home WiFi network is always available for testing."]

    def test_empty_context_fails_cleanly(self, test_project: Path) -> None:
        (test_project / "PROJECT_MEMORY.md").write_text("", encoding="utf-8")
        agent = RequirementsAgent(project_path=test_project)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["input_files"] = []
        task["notes"] = ""
        task["expected_outputs"] = []
        task["acceptance_criteria"] = []
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert output.errors

    def test_agent_registry_creates_requirements_agent(self, test_project: Path) -> None:
        agent = create_agent("requirements_agent", project_path=test_project)
        assert isinstance(agent, RequirementsAgent)
        with pytest.raises(AgentError):
            create_agent("no_such_agent", project_path=test_project)


# ---------------------------------------------------------------------------
# Phase D.5 — G22: delivery manifest, real-file creation, snapshot semantics
# ---------------------------------------------------------------------------


class TestDeliveryManifest:
    """Existence facts reach the prompt BEFORE generation.

    Replaces guess-the-channel: the manifest states per expected output
    whether the file exists and which delivery channel is required, and
    ``data.documents`` actually creates missing expected files at their
    real path (previously it only wrote docs/ mirrors — no channel could
    create a new source file at all).
    """

    def test_manifest_states_channel_per_expected_output(
        self, test_project: Path
    ) -> None:
        (test_project / "main").mkdir(exist_ok=True)
        (test_project / "main" / "input_service.c").write_text(
            "int x;\n", encoding="utf-8"
        )
        agent = RequirementsAgent(project_path=test_project)
        task = {
            "id": "TASK-002",
            "expected_outputs": [
                "main/input_service.c",
                "src/new_module.c",
                "docs/REQUIREMENTS.md",
            ],
        }
        context = agent.relevant_context(task)
        manifest = context["delivery_manifest"]
        assert "main/input_service.c: EXISTS" in manifest
        assert "update it with data.edits" in manifest
        assert "src/new_module.c: MISSING" in manifest
        assert "create it with data.documents" in manifest
        assert "docs/REQUIREMENTS.md: docs deliverable" in manifest

    def test_authoring_contract_mentions_manifest(self) -> None:
        assert "delivery_manifest" in BaseAgent.AUTHORING_CONTRACT

    def test_documents_create_missing_real_file(self, test_project: Path) -> None:
        agent = RequirementsAgent(project_path=test_project)
        task = {"id": "TASK-002", "expected_outputs": ["src/new_module.c"]}
        output = agent.completed(
            "TASK-002",
            "created module",
            data={"documents": {"src/new_module.c": "int helper(void) { return 1; }\n"}},
        )
        agent._materialize_artifacts(task, output)
        real = test_project / "src" / "new_module.c"
        assert real.is_file()
        assert "int helper" in real.read_text(encoding="utf-8")
        mirror = test_project / "docs" / "new_module.c"
        assert mirror.is_file()

    def test_documents_never_touch_preexisting_real_file(
        self, test_project: Path
    ) -> None:
        target = test_project / "target.txt"
        target.write_text("original body\n", encoding="utf-8")
        agent = RequirementsAgent(project_path=test_project)
        task = {"id": "TASK-002", "expected_outputs": ["target.txt"]}
        agent._delivery_snapshot = {"target.txt"}  # run() takes this before execute
        output = agent.completed(
            "TASK-002",
            "attempted overwrite",
            data={"documents": {"target.txt": "model claims new body\n"}},
        )
        agent._materialize_artifacts(task, output)
        assert target.read_text(encoding="utf-8") == "original body\n"
        mirror = test_project / "docs" / "target.txt"
        assert mirror.read_text(encoding="utf-8") == "model claims new body\n"

    def test_expected_outputs_load_into_context(self, test_project: Path) -> None:
        (test_project / "main").mkdir(exist_ok=True)
        source = test_project / "main" / "usb_hid_service.c"
        source.write_text(
            "int usb_hid_init(void) {\n    /* UNIQUE_MIDDLE_MARKER */\n    return 1;\n}\n",
            encoding="utf-8",
        )
        agent = RequirementsAgent(project_path=test_project)
        task = {
            "id": "TASK-002",
            "expected_outputs": ["main/usb_hid_service.c", "src/missing.c"],
        }
        context = agent.relevant_context(task)
        body = context["expected_output:main/usb_hid_service.c"]
        assert "UNIQUE_MIDDLE_MARKER" in body
        assert "expected_output:src/missing.c" not in context

    def test_expected_output_survives_prompt_rendering(
        self, test_project: Path
    ) -> None:
        # An 8750-byte edit target used to be middle-truncated at render
        # time (8000-char default) — a hidden window made exact search
        # snippets impossible.
        from orchestrator.prompt_builder import build_prompt

        (test_project / "main").mkdir(exist_ok=True)
        body = "\n".join(f"line {i:05d} filler content here" for i in range(400))
        source = test_project / "main" / "usb_hid_service.c"
        source.write_text(body, encoding="utf-8")
        agent = RequirementsAgent(project_path=test_project)
        task = {
            "id": "TASK-002",
            "expected_outputs": ["main/usb_hid_service.c"],
            "acceptance_criteria": ["c1"],
            "title": "T",
            "owner": "software_agent",
            "status": "READY",
        }
        payload = agent.build_payload(task)
        prompt = build_prompt(payload, agent_spec="")
        assert "line 00199 filler content here" in prompt  # middle survives
        assert "expected_output:main/usb_hid_service.c" in prompt
        assert "Delivery manifest" in prompt

    def test_huge_expected_output_is_truncated_with_note(
        self, test_project: Path
    ) -> None:
        big = test_project / "huge.c"
        big.write_text("x" * 50_000, encoding="utf-8")
        agent = RequirementsAgent(project_path=test_project)
        task = {"id": "TASK-002", "expected_outputs": ["huge.c"]}
        context = agent.relevant_context(task)
        body = context["expected_output:huge.c"]
        assert "[truncated at" in body
        assert len(body) <= 32_000 + 200

    def test_apply_error_feedback_includes_current_file_content(
        self, test_project: Path
    ) -> None:
        # The TASK-003 pattern: the model guesses a search snippet for a
        # file whose content it cannot see. The next turn's feedback must
        # carry the authoritative current body.
        target = test_project / "target.txt"
        target.write_text("alpha\nBETA\ngamma\n", encoding="utf-8")

        class SessionDummy(LLMAgent):
            AGENT_ID = "software_agent"
            EDIT_SESSION_TURNS = 3

        client = FakeLLMClient(
            [
                _answer(
                    "TASK-002",
                    "software_agent",
                    edits={"target.txt": {"search": "WRONG_GUESS", "replace": "X"}},
                ),
                _answer(
                    "TASK-002",
                    "software_agent",
                    edits={"target.txt": {"search": "BETA", "replace": "BETA13"}},
                ),
            ]
        )
        agent = SessionDummy(project_path=test_project, llm_client=client)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["target.txt"]
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "alpha\nBETA13\ngamma\n"
        turn2_prompt = client.calls[1]["messages"][0]["content"]
        assert "search matched 0 time(s)" in turn2_prompt
        assert "current content of target.txt (authoritative)" in turn2_prompt
        assert "BETA" in turn2_prompt

    def test_dispatch_dod_requires_real_file(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        # The TASK-004 pattern: docs mirror exists, real source never
        # created — dispatch-context DoD (snapshot provided) must reject.
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        body = "int ra8875_init(void) { return 1; }\n"
        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "display_ra8875.c").write_text(body, encoding="utf-8")
        task = {
            "acceptance_criteria": ["driver created"],
            "expected_outputs": ["hal/display/display_ra8875.c"],
        }
        output = RequirementsAgent(project_path=test_project).completed(
            "TASK-002",
            "created driver",
            data={"documents": {"hal/display/display_ra8875.c": body}},
        )
        problems = orchestrator.definition_of_done(
            task, output, preexisting=set()
        )
        assert any("missing from the project" in item for item in problems)
        real = test_project / "hal" / "display"
        real.mkdir(parents=True, exist_ok=True)
        (real / "display_ra8875.c").write_text(body, encoding="utf-8")
        assert orchestrator.definition_of_done(task, output, preexisting=set()) == []

    def test_session_can_redeliver_file_it_created(
        self, test_project: Path
    ) -> None:
        # The G20 trap: turn 1 declares the file (empty content — wrapper
        # lands in docs/ only), turn 2 redelivers real content. Without the
        # task-start snapshot the second delivery of a file that now exists
        # was rejected as a pre-existing project file, forever.
        class SessionDummy(LLMAgent):
            AGENT_ID = "software_agent"
            EDIT_SESSION_TURNS = 3

        client = FakeLLMClient(
            [
                _answer(
                    "TASK-002",
                    "software_agent",
                    documents={"newfile.txt": "   "},
                ),
                _answer(
                    "TASK-002",
                    "software_agent",
                    documents={"newfile.txt": "int created(void) {\n    return 1;\n}\n"},
                ),
            ]
        )
        agent = SessionDummy(project_path=test_project, llm_client=client)
        task = dict(agent.state_manager.get_task("TASK-002"))
        task["expected_outputs"] = ["newfile.txt"]
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        real = test_project / "newfile.txt"
        assert real.is_file()
        assert "int created" in real.read_text(encoding="utf-8")
        assert len(client.calls) == 2
        turn2_prompt = client.calls[1]["messages"][0]["content"]
        assert "missing from the project" in turn2_prompt
        assert not any("exists in the project" in err for err in output.errors)


# ---------------------------------------------------------------------------
# Phase E — master orchestrator loop
# ---------------------------------------------------------------------------
