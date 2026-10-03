"""The DoD repair round must answer the problem it was given.

Failure this pins: a test task delivered `TEST_REPORT.md`, the evidence gate
fired (`test_like_outputs`), and the DoD asked for `test_status=NOT RUN`. The
repair note was one hardcoded text for *every* rejection — "It was valid JSON
but did not deliver real file content ... patch the project files" — so the
model was told the wrong fix, re-delivered edits, never set the field, and the
task failed with `DoD unmet: output claims an executed verification with no
ground-truth record` even though its own summary said "NOT RUN".

Two independent escapes are tested here:

1. **The note is problem-aware** (`repair_remedies`): an evidence rejection is
   answered with the exact JSON that sets `data.test_status`, an import
   rejection with "patch the consumer", a content rejection with the original
   file guidance — and never the wrong remedy for the class.
2. **Prose `NOT RUN` counts** (`_reports_not_run`): an honest negative stated
   in the summary is accepted without the structured field, because a
   fabricated "NOT RUN" only loses information while a fabricated pass still
   needs a ground-truth record.
3. **Evidence follows the repair** (`TestDispatchReRunsDeployEvidenceAfterRepair`):
   dispatch re-runs the verification the repaired output declares before
   re-evaluating the DoD. Replaying the sys-usage TASK-006 failure, the old
   code re-checked a *pre*-repair pytest exit 2 against the fixed files, so
   the one automatic repair round could never clear the evidence gate.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project`` / ``FakeLLMClient``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import FakeDeployRunner, FakeLLMClient, build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.llm_agent import LLMAgent, repair_remedies  # noqa: E402
from orchestrator.orchestrator import MasterOrchestrator  # noqa: E402

EVIDENCE_PROBLEM = (
    "output claims an executed verification with no ground-truth record "
    "(channel enabled: False); report test_status=NOT RUN or provide real "
    "execution output"
)
IMPORT_PROBLEM = (
    "src/gui_controller.py imports get_metrics_snapshot from .metrics_collector, "
    "which does not define it — read the producer file "
    "(src/metrics_collector.py); it defines: get_system_metrics"
)
FILE_PROBLEM = "delivered docs/x shares no line with existing x"
DECLARATION_PROBLEM = (
    "src/test_metrics.py imports pytest, which is not a project module, "
    "not in the Python standard library, and not declared in "
    "requirements.txt — deliver the module or declare the dependency"
)
FAILED_RUN_PROBLEM = (
    "executed verification failed: pytest exited 2 — output: NameError: "
    "name 'TypedDict' is not defined"
)


def _answer(task_id: str, agent_id: str, summary: str, **data: Any) -> str:
    return json.dumps(
        {
            "agent_id": agent_id,
            "task_id": task_id,
            "status": "completed",
            "summary": summary,
            "data": data,
            "errors": [],
            "warnings": [],
        }
    )


def _dod_task(outputs: List[str]) -> Dict[str, Any]:
    return {
        "id": "TASK-001",
        "acceptance_criteria": ["tests map to requirements"],
        "expected_outputs": outputs,
        "review": {"required": False, "status": "NOT_STARTED"},
    }


# ===========================================================================
# Remedy selection
# ===========================================================================


class TestRemedySelection:
    def test_evidence_problem_gets_the_evidence_remedy(self) -> None:
        text = repair_remedies([EVIDENCE_PROBLEM])
        assert "EVIDENCE problem" in text
        assert '"test_status": "NOT RUN"' in text
        assert "do not re-deliver" in text

    def test_evidence_remedy_never_tells_the_model_to_patch_files(self) -> None:
        """Regression: the file remedy is exactly what wasted the round."""
        assert "did not deliver real file content" not in repair_remedies(
            [EVIDENCE_PROBLEM]
        )

    def test_import_problem_gets_the_interface_remedy(self) -> None:
        text = repair_remedies([IMPORT_PROBLEM])
        assert "IMPORT problem" in text
        assert "data.edits" in text
        assert "did not deliver real file content" not in text

    def test_content_problem_keeps_the_file_remedy(self) -> None:
        text = repair_remedies([FILE_PROBLEM])
        assert "did not deliver real file content" in text
        assert "EVIDENCE problem" not in text

    def test_unclassified_problem_gets_the_generic_remedy(self) -> None:
        text = repair_remedies(["no acceptance criteria defined"])
        assert "Address exactly what the problems say" in text

    def test_declaration_problem_gets_the_declaration_remedy(self) -> None:
        """Rerun-5 TASK-007: the model answered "not declared in
        requirements.txt" by re-delivering requirements.txt through
        data.documents — which never modifies an existing file — so the
        declaration silently did not land. The remedy must name the right
        channel up front."""
        text = repair_remedies([DECLARATION_PROBLEM])
        assert "DECLARATION problem" in text
        assert "requirements.txt" in text
        assert "data.edits" in text
        assert "data.documents" in text, "the wrong channel must be named"
        assert "Address exactly what the problems say" not in text

    def test_failed_run_problem_gets_the_run_remedy(self) -> None:
        """The traceback IS the instruction: with the output quoted in the
        problem, the remedy must point the model at patching the code the
        output names — not at reporting NOT RUN."""
        text = repair_remedies([FAILED_RUN_PROBLEM])
        assert "FAILED RUN" in text
        assert "quoted in the problem above" in text
        assert "data.edits" in text
        assert "EVIDENCE problem" not in text
        assert "Address exactly what the problems say" not in text

    def test_declaration_and_failed_run_get_both_remedies(self) -> None:
        text = repair_remedies([DECLARATION_PROBLEM, FAILED_RUN_PROBLEM])
        assert "DECLARATION problem" in text
        assert "FAILED RUN" in text

    def test_mixed_problems_get_every_applicable_remedy(self) -> None:
        text = repair_remedies([EVIDENCE_PROBLEM, FILE_PROBLEM])
        assert "EVIDENCE problem" in text
        assert "did not deliver real file content" in text

    def test_empty_problem_list_is_generic(self) -> None:
        assert "Address exactly what the problems say" in repair_remedies([])


# ===========================================================================
# What the model actually receives
# ===========================================================================


class TestRepairPromptCarriesTheRightRemedy:
    def _agent(self, tmp_path: Path, responses: List[str]) -> tuple:
        project = build_test_project(tmp_path / "p")
        client = FakeLLMClient(responses)

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        agent = Dummy(project_path=project, llm_client=client)
        task = dict(agent.state_manager.get_task("TASK-002"))
        return agent, client, task, project

    def test_evidence_rejection_is_explained_as_evidence(
        self, tmp_path: Path
    ) -> None:
        first = _answer(
            "TASK-002", "software_agent", "Generated the test report", documents={}
        )
        second = _answer(
            "TASK-002",
            "software_agent",
            "Report delivered; tests NOT RUN (offline)",
            test_status="NOT RUN",
        )
        agent, client, task, _ = self._agent(tmp_path, [first, second])
        output = agent.run(task)
        repaired = agent.repair_delivery(task, output, [EVIDENCE_PROBLEM])
        assert repaired is not None

        prompt = client.calls[-1]["messages"][-1]["content"]
        assert "EVIDENCE problem" in prompt
        assert '"test_status": "NOT RUN"' in prompt
        assert "did not deliver real file content" not in prompt
        assert EVIDENCE_PROBLEM in prompt

    def test_content_rejection_keeps_the_file_guidance(
        self, tmp_path: Path
    ) -> None:
        first = _answer(
            "TASK-002", "software_agent", "metadata only", implementation_scope="x"
        )
        second = _answer(
            "TASK-002", "software_agent", "patched", documents={"docs/x.md": "# x\n"}
        )
        agent, client, task, _ = self._agent(tmp_path, [first, second])
        output = agent.run(task)
        repaired = agent.repair_delivery(task, output, [FILE_PROBLEM])
        assert repaired is not None

        prompt = client.calls[-1]["messages"][-1]["content"]
        assert "did not deliver real file content" in prompt
        assert "EVIDENCE problem" not in prompt


# ===========================================================================
# Prose NOT RUN is an honest negative
# ===========================================================================


class TestProseNotRun:
    def _problems(self, tmp_path: Path, summary: str, **data: Any):
        project = build_test_project(tmp_path / "p")
        # The docs/ mirror must exist, or delivery_problems would report an
        # unrelated "not materialized" problem instead of the evidence gate.
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "TEST_REPORT.md").write_text(
            "# TEST_REPORT\n", encoding="utf-8"
        )
        orch = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        output = orch.resolve_agent("test_agent").completed(
            "TASK-001", summary, data=data
        )
        return orch.definition_of_done(
            _dod_task(["docs/TEST_REPORT.md"]), output, deploy_records=[]
        )

    def test_gate_fires_without_any_report(self, tmp_path: Path) -> None:
        problems = self._problems(tmp_path, "Generated the test report.")
        assert any("test_status" in problem for problem in problems), problems

    def test_structured_not_run_passes(self, tmp_path: Path) -> None:
        problems = self._problems(
            tmp_path,
            "Generated the test report.",
            test_status=config.TEST_STATUS_NOT_RUN,
        )
        assert problems == [], problems

    def test_prose_not_run_passes_without_the_field(self, tmp_path: Path) -> None:
        problems = self._problems(
            tmp_path,
            "Generated TEST_REPORT.md mapping requirements to test cases; "
            "tests NOT RUN (offline execution mode).",
        )
        assert problems == [], problems

    def test_a_claim_of_passing_still_needs_ground_truth(self, tmp_path: Path) -> None:
        problems = self._problems(
            tmp_path, "Executed the suite: 42/42 tests passed."
        )
        assert any("test_status" in problem for problem in problems), problems

    def test_not_run_with_a_passing_claim_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        """The prose escape must not open a hole for a contradictory claim."""
        problems = self._problems(
            tmp_path,
            "Tests NOT RUN but the report shows 42/42 tests passed.",
        )
        assert problems, "a passing claim still needs a ground-truth record"


# ===========================================================================
# The whole loop: rejection -> repair -> DONE
# ===========================================================================


class TestDispatchRepairsEvidenceRejection:
    def test_evidence_rejection_becomes_done_after_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = build_test_project(tmp_path / "p")
        document = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))
        for entry in document["tasks"]:
            if entry.get("id") == "TASK-002":
                entry["expected_outputs"] = ["docs/TEST_REPORT.md"]
        (project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

        body = "# TEST_REPORT\n\nREQ-001 -> test_monitor\n"
        first = _answer(
            "TASK-002",
            "software_agent",
            "Generated TEST_REPORT.md mapping requirements to test cases",
            documents={"docs/TEST_REPORT.md": body},
        )
        second = _answer(
            "TASK-002",
            "software_agent",
            "Evidence reported: tests NOT RUN (offline)",
            test_status=config.TEST_STATUS_NOT_RUN,
        )
        client = FakeLLMClient([first, second])

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        dummy = Dummy(project_path=project, llm_client=client)
        orch = MasterOrchestrator(
            project, checkpoints_root=tmp_path / "ck", auto_checkpoint=False
        )
        monkeypatch.setattr(orch, "resolve_agent", lambda owner, fresh=False: dummy)

        result = orch.dispatch("TASK-002")

        assert result.new_status == config.TASK_DONE, (
            result.output.summary,
            result.output.errors,
        )
        assert len(client.calls) == 2, "one rejection, one repair round"
        prompt = client.calls[-1]["messages"][-1]["content"]
        assert "EVIDENCE problem" in prompt
        task = orch.get_task("TASK-002")
        assert task["status"] == config.TASK_DONE
        assert task["execution"]["last_error"] is None


class TestDispatchReRunsDeployEvidenceAfterRepair:
    """The repair round must be judged against the repaired workspace.

    Failure this pins (sys-usage TASK-006, rerun 2): the first turn's pytest
    exited 2 because the test file itself was broken; the repair fixed the
    file at 16:48:34; dispatch then re-evaluated the DoD with the deploy
    record from 16:48:12 — the pre-repair failure — so the repair could never
    be accepted and the task failed with a stale ``pytest exited 2``.

    The old behaviour is exactly one assertion here: without the re-run the
    task stays FAILED and the runner was asked exactly once.
    """

    def test_repaired_evidence_replaces_the_stale_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = build_test_project(tmp_path / "p")
        document = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))
        for entry in document["tasks"]:
            if entry.get("id") == "TASK-002":
                entry["expected_outputs"] = ["docs/TEST_REPORT.md"]
        (project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

        body = "# TEST_REPORT\n\nREQ-001 -> test_monitor\n"
        deploy = [{"command": "pytest", "args": ["-q"], "expect": "PASS"}]
        first = _answer(
            "TASK-002",
            "software_agent",
            "Generated TEST_REPORT.md for the mapped requirements",
            documents={"docs/TEST_REPORT.md": body},
            deploy=deploy,
        )
        second = _answer(
            "TASK-002",
            "software_agent",
            "Repaired the failing test module and regenerated the report",
            documents={"docs/TEST_REPORT.md": body},
            deploy=deploy,
        )
        client = FakeLLMClient([first, second])

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        dummy = Dummy(project_path=project, llm_client=client)

        class SequencedRunner(FakeDeployRunner):
            """First run fails (the broken file), the post-repair run passes."""

            def __init__(self) -> None:
                super().__init__()
                self.codes = [1, 0]

            def run_one(self, invocation: Dict[str, Any]) -> Any:
                self.exit_code = self.codes.pop(0) if self.codes else 0
                return super().run_one(invocation)

        runner = SequencedRunner()
        orch = MasterOrchestrator(
            project, checkpoints_root=tmp_path / "ck", auto_checkpoint=False
        )
        orch.deploy_runner = runner
        monkeypatch.setattr(orch, "resolve_agent", lambda owner, fresh=False: dummy)

        result = orch.dispatch("TASK-002")

        assert result.new_status == config.TASK_DONE, (
            result.output.summary,
            result.output.errors,
        )
        assert len(client.calls) == 2, "one rejection, one repair round"
        assert len(runner.requests) == 2, (
            "the repaired workspace must be re-verified, not judged by the "
            "pre-repair record"
        )
        task = orch.get_task("TASK-002")
        assert task["status"] == config.TASK_DONE
        assert task["execution"]["last_error"] is None


class TestRepairCannotNarrowDeliveryScope:
    """The repair round is judged against the union, not its own reply.

    Failure this pins (sys-usage TASK-005, rerun 3): the original delivery
    touched ``processor.py`` (undeclared ``typing_extensions`` import, flagged
    by the delivery checker) plus the expected outputs; the repair reply only
    rewrote ``main.py``, its own delivery set had no problems, and the DoD
    re-evaluation — which read the repaired output alone — declared the
    delivery clean. The one gate that still caught the defect was the
    executed-verification re-run; a task whose DoD has no deploy would have
    gone DONE with the broken import on disk.
    """

    def test_untouched_defect_survives_the_repair_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = build_test_project(tmp_path / "p")
        document = yaml.safe_load((project / "TASKS.yaml").read_text(encoding="utf-8"))
        for entry in document["tasks"]:
            if entry.get("id") == "TASK-002":
                entry["expected_outputs"] = ["docs/REPORT.md"]
                entry["acceptance_criteria"] = [
                    "report summarizes the captured requirements"
                ]
        (project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

        report = "# REPORT\n\nREQ-001 -> metrics summary\n"
        bad_module = "from typing_extensions import TypedDict\n\n\n"
        first = _answer(
            "TASK-002",
            "software_agent",
            "Delivered the report and a helper module",
            documents={
                "docs/REPORT.md": report,
                "processor.py": bad_module,
            },
        )
        second = _answer(
            "TASK-002",
            "software_agent",
            "Regenerated the report only",
            documents={"docs/REPORT.md": report},
        )
        client = FakeLLMClient([first, second])

        class Dummy(LLMAgent):
            AGENT_ID = "software_agent"

        dummy = Dummy(project_path=project, llm_client=client)
        orch = MasterOrchestrator(
            project, checkpoints_root=tmp_path / "ck", auto_checkpoint=False
        )
        monkeypatch.setattr(orch, "resolve_agent", lambda owner, fresh=False: dummy)

        result = orch.dispatch("TASK-002")

        assert result.new_status == config.TASK_FAILED, (
            "the repair reply did not touch processor.py, so its undeclared "
            "import must still be under contract in the re-evaluation",
            result.output.summary,
            result.output.errors,
        )
        assert len(client.calls) == 2, "one rejection, one repair round"
        task = orch.get_task("TASK-002")
        assert "typing_extensions" in str(task["execution"]["last_error"])
