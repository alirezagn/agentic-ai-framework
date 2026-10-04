"""Environment / stage not ready is not a delivery failure.

The evidence gate (GAP-CRIT-01) treats an executed non-zero exit as ground
truth, which is right for a real failure and wrong for two situations that
the fresh sys-usage run hit:

1. the verification needs a package that **is** declared in
   ``requirements.txt`` but is not installed in this environment — the
   framework never installs (operator directive), so the human project owner
   installs it after reading the project files;
2. the verification targets a path this task does not deliver and that does
   not exist yet (``pytest tests/`` at TASK-003, before any test task ran).

Both are facts about the environment/stage, not about the delivered files.
The classification is read off the record's real captured output plus
``requirements.txt`` — never off the model's claim — so the fabrication
refusal stays armed: a claimed pass, a genuine assertion failure and an
undeclared module must all still reject.

Runs offline: no process is spawned, records are constructed directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput
from orchestrator.deploy_runner import STATUS_EXECUTED, DeployRecord
from orchestrator.orchestrator import MasterOrchestrator

PSUTIL_MISSING = (
    "Traceback (most recent call last):\n"
    '  File "src/metrics_collector.py", line 3, in <module>\n'
    "    import psutil\n"
    "ModuleNotFoundError: No module named 'psutil'\n"
)
PYTEST_NO_TARGET = (
    "ERROR: file or directory not found: tests/\n"
    "no tests ran in 0.00s\n"
)


def _task(expected: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "id": "TASK-001",
        "title": "Implement the metrics collector",
        "owner": "software_agent",
        "expected_outputs": list(expected or ["src/metrics_collector.py"]),
    }


def _output(
    summary: str = "Implemented the metrics collector module.",
    declares: bool = True,
) -> AgentOutput:
    data: Dict[str, Any] = {}
    if declares:
        data["deploy"] = [{"command": "python3", "args": ["-m", "pytest", "tests/"]}]
    return AgentOutput(
        agent_id="software_agent",
        task_id="TASK-001",
        status=config.AGENT_STATUS_COMPLETED,
        summary=summary,
        data=data,
    )


def _record(
    project: Path,
    exit_code: int,
    stderr: str,
    args: Optional[List[str]] = None,
) -> DeployRecord:
    return DeployRecord(
        command="python3",
        args=list(args or ["-m", "pytest", "tests/"]),
        cwd=str(project),
        exit_code=exit_code,
        stderr_tail=stderr,
        executed=True,
        status=STATUS_EXECUTED,
        expect_matched=False if exit_code else None,
        declared_expect="PASS",
    )


def _problems(
    project: Path,
    record: DeployRecord,
    task: Optional[Dict[str, Any]] = None,
    output: Optional[AgentOutput] = None,
) -> List[str]:
    orch = MasterOrchestrator(project)
    return orch._evidence_problems(
        task if task is not None else _task(),
        output if output is not None else _output(),
        [record],
    )


class TestMissingDeclaredDependencyIsNotADeliveryFailure:
    def test_declared_package_not_installed_is_waived(self, test_project: Path) -> None:
        (test_project / "requirements.txt").write_text("psutil>=5.9.0\n", encoding="utf-8")
        output = _output()
        problems = _problems(
            test_project, _record(test_project, 1, PSUTIL_MISSING), output=output
        )
        assert problems == [], (
            "a package declared for the human owner to install is an "
            "environment fact — it must not fail the delivered files"
        )
        assert output.data.get("test_status") == config.TEST_STATUS_NOT_RUN
        assert any("requirements.txt" in w for w in output.warnings), (
            "the warning must name where the owner gets the dependency from"
        )

    def test_undeclared_package_still_fails(self, test_project: Path) -> None:
        (test_project / "requirements.txt").write_text("psutil>=5.9.0\n", encoding="utf-8")
        stderr = PSUTIL_MISSING.replace("psutil", "requests")
        problems = _problems(test_project, _record(test_project, 1, stderr))
        assert problems, (
            "a module nobody declared is the hallucinated-import class and "
            "must keep failing the task"
        )
        assert "requests" in problems[0]

    def test_no_requirements_file_never_waives(self, test_project: Path) -> None:
        problems = _problems(test_project, _record(test_project, 1, PSUTIL_MISSING))
        assert problems, "with nothing declared there is no owner obligation to honor"


class TestVerificationTargetNotAtThisStage:
    def test_absent_target_outside_this_tasks_outputs_is_waived(
        self, test_project: Path
    ) -> None:
        assert not (test_project / "tests").exists()
        output = _output()
        problems = _problems(
            test_project, _record(test_project, 4, PYTEST_NO_TARGET), output=output
        )
        assert problems == [], (
            "pytest exit 4 on a directory this task does not deliver is the "
            "stage, not the code — TASK-003 of sys-usage failed this way"
        )
        assert output.data.get("test_status") == config.TEST_STATUS_NOT_RUN
        assert any("tests" in w for w in output.warnings)

    def test_target_this_task_must_deliver_still_fails(self, test_project: Path) -> None:
        task = _task(expected=["tests/test_metrics.py"])
        problems = _problems(
            test_project,
            _record(test_project, 4, PYTEST_NO_TARGET),
            task=task,
        )
        assert problems, "a missing expected output is a delivery failure, full stop"

    def test_target_that_exists_is_a_real_failure(self, test_project: Path) -> None:
        (test_project / "tests").mkdir()
        problems = _problems(
            test_project, _record(test_project, 4, PYTEST_NO_TARGET)
        )
        assert problems, (
            "the path is on disk, so 'file or directory not found' is not a "
            "stage problem — the real cause must still be reported"
        )


class TestTheFabricationRefusalStaysArmed:
    def test_execution_claim_blocks_the_waiver(self, test_project: Path) -> None:
        (test_project / "requirements.txt").write_text("psutil>=5.9.0\n", encoding="utf-8")
        output = _output(summary="pytest ran and passed all 6 tests")
        problems = _problems(
            test_project, _record(test_project, 1, PSUTIL_MISSING), output=output
        )
        assert problems, "a claimed pass can never be excused by the environment"
        assert "environment not ready" in problems[0]

    def test_genuine_test_failure_still_fails(self, test_project: Path) -> None:
        stderr = "FAILED tests/test_metrics.py::test_cpu - assert 0 == 1\n1 failed"
        problems = _problems(test_project, _record(test_project, 1, stderr))
        assert problems
        assert "exited 1" in problems[0]

    def test_expectation_mismatch_without_a_failure_is_not_waived(
        self, test_project: Path
    ) -> None:
        record = DeployRecord(
            command="python3",
            args=["main.py"],
            cwd=str(test_project),
            exit_code=0,
            executed=True,
            status=STATUS_EXECUTED,
            declared_expect="live values",
            expect_matched=False,
        )
        problems = _problems(test_project, record)
        assert problems, "an expectation that did not match reality is a finding"
        assert "expected" in problems[0]


class TestTheRepairRoundGetsTheRightRemedy:
    @staticmethod
    def test_env_problem_routes_to_the_env_remedy() -> None:
        from orchestrator.agents.llm_agent import repair_remedies

        problems = [
            'environment not ready: the verification targets "tests/", which '
            "does not exist and is not an output of this task; report "
            'data.test_status="NOT RUN"'
        ]
        remedy = repair_remedies(problems)
        assert "ENVIRONMENT/STAGE problem" in remedy
        assert "Do NOT re-deliver" in remedy
        assert "This is an EVIDENCE problem" not in remedy, (
            "the two blocks say the same thing; the specific one must win"
        )

    @staticmethod
    def test_plain_evidence_problem_keeps_its_own_remedy() -> None:
        from orchestrator.agents.llm_agent import repair_remedies

        remedy = repair_remedies(
            ["task declared data.deploy but nothing was executed (channel disabled)"]
        )
        assert "This is an EVIDENCE problem" in remedy
        assert "ENVIRONMENT/STAGE problem" not in remedy
