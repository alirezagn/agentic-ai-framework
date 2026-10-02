"""GAP-CRIT-01 — the framework must be able to obtain ground truth.

The vulnerability
-----------------
The orchestrator had no way to execute a build, a test, or a flash. Agents were
asked for verification results they could not obtain, so they invented them, and
nothing in the Definition of Done could tell an invented result from a real one.
A pure-prose reply from ``TestAgent`` claiming "Executed ctest: 42/42 tests
passed in 10m 12s, coverage 94%, flashed ESP32 OK" became ``TASK-001: DONE``,
with the fabrication persisted verbatim into ``TASKS.yaml`` notes.

What is fixed
-------------
:meth:`MasterOrchestrator._run_requested_deploys` executes whatever an agent
proposes, off the state lock, through the single chokepoint
:mod:`orchestrator.deploy_runner`. The *runner* stamps ``executed`` and the real
exit code; the model never decides whether it ran. The DoD then requires ground
truth for anything that claims or implies an executed verification, and permits
an honest ``NOT RUN`` in its place.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project`` and the ``FakeDeployRunner`` fixtures).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import FakeDeployRunner, build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.base_agent import BaseAgent  # noqa: E402
from orchestrator.deploy_runner import (  # noqa: E402
    STATUS_EXECUTED,
    STATUS_REFUSED,
    STATUS_TIMEOUT,
    DeployError,
    DeployRecord,
    DeployRunner,
)
from orchestrator.orchestrator import MasterOrchestrator  # noqa: E402


# ---------------------------------------------------------------------------
# Policy: closed by default
# ---------------------------------------------------------------------------


class TestDeployPolicyDefaults:
    def test_channel_is_disabled_by_default(self) -> None:
        assert config.DEPLOY_ENABLED is False
        assert config.deploy_enabled() is False

    def test_allowlist_is_empty_by_default(self) -> None:
        assert config.DEPLOY_ALLOWLIST == ()
        assert config.deploy_allowlist() == ()

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "ORCHESTRATOR_DEPLOY_ENABLED",
            "ORCHESTRATOR_DEPLOY_ALLOWLIST",
            "ORCHESTRATOR_DEPLOY_TIMEOUT",
            "ORCHESTRATOR_DEPLOY_MAX_OUTPUT",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_enabled_flag_alone_is_not_enough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enabling with an empty allowlist must stay inert."""
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        assert config.deploy_enabled() is False

    def test_both_required_to_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "ctest,pytest")
        assert config.deploy_enabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_enable_spellings(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", value)
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "ctest")
        assert config.deploy_enabled() is True

    def test_allowlist_entries_are_reduced_to_basenames(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path cannot smuggle a different binary through the allowlist."""
        monkeypatch.setenv(
            "ORCHESTRATOR_DEPLOY_ALLOWLIST", "/usr/bin/gcc, ../../bin/evil ,ctest"
        )
        assert config.deploy_allowlist() == ("gcc", "evil", "ctest")

    def test_allowlist_deduplicates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "ctest,ctest,/usr/bin/ctest")
        assert config.deploy_allowlist() == ("ctest",)

    def test_timeout_is_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert config.deploy_timeout() == 300.0
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_TIMEOUT", "45")
        assert config.deploy_timeout() == 45.0
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_TIMEOUT", "0")
        assert config.deploy_timeout() == 300.0, "a zero timeout must not be accepted"
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_TIMEOUT", "999999")
        assert config.deploy_timeout() == 300.0
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_TIMEOUT", "not-a-number")
        assert config.deploy_timeout() == 300.0

    def test_output_cap_is_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_MAX_OUTPUT", "10")
        assert config.deploy_max_output_bytes() == config.DEPLOY_MAX_OUTPUT_BYTES
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_MAX_OUTPUT", "4096")
        assert config.deploy_max_output_bytes() == 4096


class TestClaimsExecutionDetector:
    @pytest.mark.parametrize(
        "text",
        [
            "Executed ctest: 42/42 tests passed",
            "all tests pass",
            "build succeeded",
            "compiled successfully",
            "flashed ESP32 OK",
            "idf.py build completed",
            "coverage 94%",
            "exit code 0",
        ],
    )
    def test_detects_execution_claims(self, text: str) -> None:
        assert config.claims_execution(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "wrote the cabling document",
            "no hardware access was available",
            "requirements captured",
        ],
    )
    def test_ignores_ordinary_summaries(self, text: str) -> None:
        assert config.claims_execution(text) is False

    def test_is_case_insensitive(self) -> None:
        assert config.claims_execution("ALL TESTS PASSED") is True


# ---------------------------------------------------------------------------
# Runner: refused when disabled or not allowlisted
# ---------------------------------------------------------------------------


class TestRunnerRefusals:
    def test_disabled_channel_returns_not_executed(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=False, allowlist=("ctest",))
        record = runner.run_one({"command": "ctest", "args": ["--output-on-failure"]})
        assert record.executed is False
        assert record.status == STATUS_REFUSED
        assert "disabled" in record.reason

    def test_empty_allowlist_refuses_everything(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=())
        assert runner.enabled is False, "enabled with an empty allowlist must be inert"
        record = runner.run_one({"command": "ctest"})
        assert record.executed is False

    def test_non_allowlisted_executable_is_refused(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        record = runner.run_one({"command": "rm", "args": ["-rf", "/"]})
        assert record.executed is False
        assert "not on the deploy allowlist" in record.reason

    def test_allowlisted_executable_is_permitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validation passes for a permitted binary (spawning is monkeypatched)."""
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        executable, args, expect = runner._validate_invocation(
            {"command": "ctest", "args": ["--output-on-failure"], "expect": "PASS"}
        )
        assert executable.endswith("ctest")
        assert args == ["--output-on-failure"]
        assert expect == "PASS"

    def test_empty_command_is_rejected(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        with pytest.raises(DeployError, match="no 'command'"):
            runner._validate_invocation({"args": []})

    def test_star_allowlist_permits_any_executable(self, tmp_path: Path) -> None:
        """`ORCHESTRATOR_DEPLOY_ALLOWLIST=*` is the allow-all opt-in.

        An operator who writes `*` means "any executable"; treating it as the
        literal basename `*` refused *every* command, so the channel was
        "enabled" yet nothing could ever run.
        """
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("*",))
        executable, args, expect = runner._validate_invocation(
            {"command": "python3", "args": ["-c", "print(1)"]}
        )
        assert executable and Path(executable).is_file()
        assert args == ["-c", "print(1)"]

    def test_star_still_refuses_a_disabled_channel(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=False, allowlist=("*",))
        record = runner.run_one({"command": "python3", "args": ["-c", "print(1)"]})
        assert record.executed is False
        assert "disabled" in record.reason

    def test_star_does_not_open_the_project_boundary(self, tmp_path: Path) -> None:
        """A traversal script is still confined to the project directory."""
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("*",))
        with pytest.raises(DeployError, match="not on the deploy allowlist"):
            runner._validate_invocation({"command": "../../etc/passwd"})

    def test_control_characters_in_command_are_rejected(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        with pytest.raises(DeployError, match="control characters"):
            runner._validate_invocation({"command": "ctest\nrm -rf /"})

    def test_nul_in_args_is_rejected(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        with pytest.raises(DeployError, match="NUL"):
            runner._validate_invocation({"command": "ctest", "args": ["a\x00b"]})

    def test_non_string_args_are_rejected(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        with pytest.raises(DeployError, match="must be strings"):
            runner._validate_invocation({"command": "ctest", "args": [{"x": 1}]})

    def test_cwd_outside_project_is_refused(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        with pytest.raises(DeployError, match="outside the project"):
            runner._validate_invocation({"command": "ctest", "cwd": "../escape"})

    def test_absolute_cwd_is_refused(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        with pytest.raises(DeployError, match="outside the project"):
            runner._validate_invocation({"command": "ctest", "cwd": "/etc"})

    def test_relative_subdir_cwd_is_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "build").mkdir()
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        executable, _, _ = runner._validate_invocation(
            {"command": "ctest", "cwd": "build"}
        )
        assert executable.endswith("ctest")

    def test_missing_cwd_directory_is_reported(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        with pytest.raises(DeployError, match="does not exist"):
            runner._validate_invocation({"command": "ctest", "cwd": "nowhere"})


# ---------------------------------------------------------------------------
# Runner: shell safety, env scrubbing, bounds
# ---------------------------------------------------------------------------


class TestRunnerSafety:
    @pytest.fixture()
    def spawned(self, monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
        """Intercept subprocess.run so no process is ever created."""
        calls: List[Dict[str, Any]] = []

        class Completed:
            returncode = 0
            stdout = b"ok"
            stderr = b""

        def fake_run(argv: List[str], **kwargs: Any) -> Any:
            calls.append({"argv": argv, **kwargs})
            return Completed()

        monkeypatch.setattr(
            "orchestrator.deploy_runner.subprocess.run", fake_run
        )
        return calls

    def test_shell_is_always_false(
        self, tmp_path: Path, spawned: List[Dict[str, Any]]
    ) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        runner.run_one({"command": "ctest", "args": ["-R", "unit"]})
        assert spawned[0]["shell"] is False

    def test_argv_is_a_list_not_a_string(
        self, tmp_path: Path, spawned: List[Dict[str, Any]]
    ) -> None:
        """A list argv is what makes shell metacharacters inert."""
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        runner.run_one({"command": "ctest", "args": ["; rm -rf /"]})
        assert isinstance(spawned[0]["argv"], list)
        assert "; rm -rf /" in spawned[0]["argv"]

    def test_cwd_is_forced_to_project_root(
        self, tmp_path: Path, spawned: List[Dict[str, Any]]
    ) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        runner.run_one({"command": "ctest"})
        assert Path(spawned[0]["cwd"]) == tmp_path.resolve()

    def test_secrets_are_scrubbed_from_child_env(
        self,
        tmp_path: Path,
        spawned: List[Dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without scrubbing, a spawned build script could read the API keys."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-other")
        monkeypatch.setenv("CHECKPOINT_SIGNING_KEY", "signing-secret")
        monkeypatch.setenv("http_proxy", "http://proxy.invalid")
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        runner.run_one({"command": "ctest"})
        environment = spawned[0]["env"]
        assert "ANTHROPIC_API_KEY" not in environment
        assert "OPENROUTER_API_KEY" not in environment
        assert "CHECKPOINT_SIGNING_KEY" not in environment
        assert "http_proxy" not in environment
        assert "PATH" in environment

    def test_timeout_is_passed_through(
        self, tmp_path: Path, spawned: List[Dict[str, Any]]
    ) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",), timeout=12.0)
        runner.run_one({"command": "ctest"})
        assert spawned[0]["timeout"] == 12.0

    def test_successful_run_reports_executed(
        self, tmp_path: Path, spawned: List[Dict[str, Any]]
    ) -> None:
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        record = runner.run_one({"command": "ctest"})
        assert record.executed is True
        assert record.status == STATUS_EXECUTED
        assert record.exit_code == 0
        assert record.succeeded is True

    def test_non_zero_exit_is_still_ground_truth(
        self,
        tmp_path: Path,
        spawned: List[Dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real failure is information; a fabricated pass is not."""
        class Failed:
            returncode = 8
            stdout = b"2 tests failed"
            stderr = b""

        monkeypatch.setattr(
            "orchestrator.deploy_runner.subprocess.run", lambda argv, **kw: Failed()
        )
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        record = runner.run_one({"command": "ctest"})
        assert record.executed is True
        assert record.exit_code == 8
        assert record.succeeded is False

    def test_timeout_is_reported_as_not_executed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as sp

        def timeout_run(argv: List[str], **kwargs: Any) -> Any:
            raise sp.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

        monkeypatch.setattr(
            "orchestrator.deploy_runner.subprocess.run", timeout_run
        )
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",), timeout=1.0)
        record = runner.run_one({"command": "ctest"})
        assert record.executed is False
        assert record.status == STATUS_TIMEOUT
        assert "timeout" in record.reason

    def test_spawn_failure_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail_run(argv: List[str], **kwargs: Any) -> Any:
            raise OSError("no such file")

        monkeypatch.setattr("orchestrator.deploy_runner.subprocess.run", fail_run)
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        record = runner.run_one({"command": "ctest"})
        assert record.executed is False
        assert "could not start process" in record.reason

    def test_output_is_truncated_with_a_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Chatty:
            returncode = 0
            stdout = b"x" * 5000
            stderr = b""

        monkeypatch.setattr(
            "orchestrator.deploy_runner.subprocess.run", lambda argv, **kw: Chatty()
        )
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",), max_output_bytes=1024)
        record = runner.run_one({"command": "ctest"})
        assert record.stdout_truncated is True
        assert len(record.stdout_tail) <= 1024
        assert "[truncated]" in record.render()

    def test_expectation_mismatch_is_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Failed:
            returncode = 1
            stdout = b""
            stderr = b""

        monkeypatch.setattr(
            "orchestrator.deploy_runner.subprocess.run", lambda argv, **kw: Failed()
        )
        runner = DeployRunner(tmp_path, enabled=True, allowlist=("ctest",))
        record = runner.run_one({"command": "ctest", "expect": "PASS"})
        assert record.expect_matched is False


# ---------------------------------------------------------------------------
# Evidence records
# ---------------------------------------------------------------------------


class TestEvidenceRecords:
    def test_evidence_is_written_under_docs(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=False, allowlist=())
        record = runner.run_one({"command": "ctest"})
        relative = runner.write_evidence("TASK-003", record)
        assert relative is not None
        assert relative.startswith("docs/evidence/TASK-003/")
        payload = json.loads((tmp_path / relative).read_text())
        assert payload["task_id"] == "TASK-003"
        assert payload["executed"] is False
        assert "transcript" in payload

    def test_task_id_is_sanitised(self, tmp_path: Path) -> None:
        runner = DeployRunner(tmp_path, enabled=False)
        relative = runner.write_evidence("../escape", runner.run_one({"command": "ctest"}))
        assert relative is not None
        assert ".." not in relative

    def test_has_ground_truth_distinguishes_ran_from_claimed(self) -> None:
        ran = DeployRecord(command="ctest", executed=True, exit_code=0)
        refused = DeployRecord(command="ctest", executed=False, status=STATUS_REFUSED)
        assert DeployRunner.has_ground_truth([ran]) is True
        assert DeployRunner.has_ground_truth([refused]) is False
        assert DeployRunner.has_ground_truth([]) is False


# ---------------------------------------------------------------------------
# Orchestration: deploy runs off the state lock
# ---------------------------------------------------------------------------


def _orch(tmp_path: Path, runner: FakeDeployRunner) -> MasterOrchestrator:
    orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
    orch.deploy_runner = runner
    return orch


def _agent_returning(
    data: Dict[str, Any], summary: str = "did the work", status: str = config.AGENT_STATUS_COMPLETED
) -> type:
    class _Agent(BaseAgent):
        AGENT_ID = "probe_agent"

        def execute(self, payload: Dict[str, Any]) -> Any:
            task_id = str(payload["task"]["id"])
            if status != config.AGENT_STATUS_COMPLETED:
                return self.failed(task_id, "boom")
            return self.completed(task_id, summary, data=dict(data))

    return _Agent


class TestDeployRunsOffTheStateLock:
    def test_runner_is_invoked_with_the_agents_request(
        self, tmp_path: Path, fake_deploy_runner: FakeDeployRunner
    ) -> None:
        orch = _orch(tmp_path, fake_deploy_runner)
        output = orch._run_requested_deploys(
            "TASK-002", orch.resolve_agent("requirements_agent").completed(
                "TASK-002", "plan", data={"deploy": [{"command": "ctest", "args": ["-R", "unit"]}]}
            )
        )
        assert len(output) == 1
        assert fake_deploy_runner.requests[0]["command"] == "ctest"
        assert fake_deploy_runner.evidence_written

    def test_records_are_attached_to_the_output(
        self, tmp_path: Path, fake_deploy_runner: FakeDeployRunner
    ) -> None:
        orch = _orch(tmp_path, fake_deploy_runner)
        result = orch.resolve_agent("requirements_agent").completed(
            "TASK-002", "ran", data={"deploy": [{"command": "ctest"}]}
        )
        orch._run_requested_deploys("TASK-002", result)
        records = result.data["deploy_results"]
        assert records[0]["executed"] is True
        assert records[0]["exit_code"] == 0

    def test_no_request_means_no_work(self, tmp_path: Path, fake_deploy_runner: FakeDeployRunner) -> None:
        orch = _orch(tmp_path, fake_deploy_runner)
        result = orch.resolve_agent("requirements_agent").completed("TASK-002", "noop")
        assert orch._run_requested_deploys("TASK-002", result) == []
        assert fake_deploy_runner.requests == []

    def test_a_real_process_is_not_spawned_by_the_default_runner(
        self, tmp_path: Path
    ) -> None:
        """The shipped default refuses everything, so the suite stays hermetic."""
        runner = DeployRunner(tmp_path)
        record = runner.run_one({"command": "ctest"})
        assert record.executed is False


# ---------------------------------------------------------------------------
# DoD: fabricated results are refused
# ---------------------------------------------------------------------------


class _FabricatingAgent(BaseAgent):
    """Reproduces the GAP-CRIT-01 PoC: prose claiming a passing test run."""

    AGENT_ID = "fabricating_test_agent"

    def execute(self, payload: Dict[str, Any]) -> Any:
        task_id = str(payload["task"]["id"])
        return self.completed(
            task_id,
            "Executed ctest: 42/42 tests passed in 10m 12s, coverage 94%, flashed ESP32 OK",
            data={},
        )


class TestDefinitionOfDoneRequiresEvidence:
    def _task(self, outputs: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "id": "TASK-001",
            "title": "test the firmware",
            "owner": "test_agent",
            "status": config.TASK_IN_PROGRESS,
            "dependencies": [],
            "acceptance_criteria": ["tests pass"],
            "expected_outputs": outputs if outputs is not None else ["docs/TEST_REPORT.md"],
            "review": {"required": False, "status": "NOT_STARTED"},
        }

    def test_fabricated_summary_without_ground_truth_is_refused(
        self, tmp_path: Path
    ) -> None:
        orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
        output = _FabricatingAgent(state_manager=orch.state).completed(
            "TASK-001",
            "Executed ctest: 42/42 tests passed in 10m 12s, coverage 94%",
            data={},
        )
        problems = orch.definition_of_done(self._task(), output, deploy_records=[])
        assert problems, "a fabricated claim must produce a DoD problem"
        assert any("ground-truth" in problem for problem in problems)

    def test_honest_not_run_is_accepted(
        self, tmp_path: Path
    ) -> None:
        """'I could not run this' is a legitimate outcome."""
        orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
        output = orch.resolve_agent("test_agent").completed(
            "TASK-001",
            "no physical board access; tests NOT RUN",
            data={"test_status": config.TEST_STATUS_NOT_RUN},
        )
        problems = orch.definition_of_done(
            self._task(outputs=["docs/TEST_REPORT.md"]),
            output,
            deploy_records=[],
        )
        assert not [p for p in problems if "ground-truth" in p or "NOT RUN" in p]

    def test_ground_truth_backs_the_claim(
        self, tmp_path: Path, fake_deploy_runner: FakeDeployRunner
    ) -> None:
        orch = _orch(tmp_path, fake_deploy_runner)
        output = _FabricatingAgent(state_manager=orch.state).completed(
            "TASK-001", "Executed ctest: 42/42 tests passed", data={"deploy": [{"command": "ctest"}]}
        )
        records = orch._run_requested_deploys("TASK-001", output)
        problems = orch.definition_of_done(self._task(), output, deploy_records=records)
        assert not [
            problem
            for problem in problems
            if "ground-truth" in problem or "verification failed" in problem
        ]

    def test_real_failing_build_blocks_done(
        self, tmp_path: Path, failing_deploy_runner: FakeDeployRunner
    ) -> None:
        """A non-zero exit is the whole point of having ground truth."""
        orch = _orch(tmp_path, failing_deploy_runner)
        output = orch.resolve_agent("test_agent").completed(
            "TASK-001", "Ran the test suite", data={"deploy": [{"command": "ctest"}]}
        )
        records = orch._run_requested_deploys("TASK-001", output)
        problems = orch.definition_of_done(self._task(), output, deploy_records=records)
        assert any("verification failed" in problem for problem in problems)

    def test_declared_deploy_that_did_not_run_is_refused(
        self, tmp_path: Path, disabled_deploy_runner: FakeDeployRunner
    ) -> None:
        orch = _orch(tmp_path, disabled_deploy_runner)
        output = orch.resolve_agent("test_agent").completed(
            "TASK-001", "wrote the test plan", data={"deploy": [{"command": "ctest"}]}
        )
        records = orch._run_requested_deploys("TASK-001", output)
        problems = orch.definition_of_done(self._task(), output, deploy_records=records)
        assert any("data.deploy but nothing was executed" in problem for problem in problems)

    def test_test_like_output_triggers_the_check_without_a_claim(
        self, tmp_path: Path
    ) -> None:
        """A quiet hand-over of test_results.log is still checked."""
        orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
        output = orch.resolve_agent("test_agent").completed(
            "TASK-001", "wrote the artifact", data={}
        )
        problems = orch.definition_of_done(
            self._task(outputs=["docs/test_results.log"]), output, deploy_records=[]
        )
        assert any("ground-truth" in problem for problem in problems)

    def test_ordinary_documentation_task_is_unaffected(
        self, tmp_path: Path
    ) -> None:
        orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
        output = orch.resolve_agent("documentation_agent").completed(
            "TASK-001", "wrote the cabling guide", data={}
        )
        problems = orch.definition_of_done(
            self._task(outputs=["docs/CABLING.md"]), output, deploy_records=[]
        )
        assert not [
            problem
            for problem in problems
            if "ground-truth" in problem or "verification" in problem
        ]

    def test_expectation_mismatch_blocks_done(
        self, tmp_path: Path
    ) -> None:
        class Mismatch(FakeDeployRunner):
            def run_one(self, invocation: Dict[str, Any]) -> Any:
                """Ran, exited non-zero, and broke its own declared expectation.

                ``exit_code`` is set rather than ``succeeded`` because
                ``succeeded`` is a derived property (executed and exit 0).
                """
                record = FakeDeployRunner.run_one(self, invocation)
                record.exit_code = 1
                record.declared_expect = "PASS"
                record.expect_matched = False
                return record

        orch = _orch(tmp_path, Mismatch())
        output = orch.resolve_agent("test_agent").completed(
            "TASK-001", "Ran the suite", data={"deploy": [{"command": "ctest", "expect": "PASS"}]}
        )
        records = orch._run_requested_deploys("TASK-001", output)
        problems = orch.definition_of_done(self._task(), output, deploy_records=records)
        assert any("expectation" in problem for problem in problems)


class TestDispatchIntegration:
    def test_fabricated_result_cannot_reach_done(
        self, tmp_path: Path, disabled_deploy_runner: FakeDeployRunner
    ) -> None:
        """The audit's PoC, end to end: prose-only reply must not be DONE."""
        orch = _orch(tmp_path, disabled_deploy_runner)
        orch.resolve_agent = lambda owner, fresh=False: _FabricatingAgent(  # type: ignore[method-assign]
            state_manager=orch.state
        )
        result = orch.run_task("TASK-002")
        assert result.new_status != config.TASK_DONE
        stored = orch.get_task("TASK-002")
        assert stored["status"] != config.TASK_DONE
        assert "Executed ctest" not in json.dumps(stored.get("execution", {}))

    def test_real_execution_allows_done(
        self, tmp_path: Path, fake_deploy_runner: FakeDeployRunner
    ) -> None:
        class Delivering(_FabricatingAgent):
            AGENT_ID = "delivering_agent"

            def execute(self, payload: Dict[str, Any]) -> Any:
                task_id = str(payload["task"]["id"])
                expected = str((payload["task"].get("expected_outputs") or [None])[0])
                return self.completed(
                    task_id,
                    "Executed ctest: 42/42 tests passed",
                    data={
                        "deploy": [{"command": "ctest"}],
                        "documents": {expected: "# TEST_REPORT\n\n42/42 passed\n"},
                    },
                )

        orch = _orch(tmp_path, fake_deploy_runner)
        orch.resolve_agent = lambda owner, fresh=False: Delivering(  # type: ignore[method-assign]
            state_manager=orch.state
        )
        result = orch.run_task("TASK-002")
        assert result.new_status == config.TASK_DONE, (
            f"expected DONE with real evidence, got {result.new_status}"
        )

    def test_fabrication_persisted_nothing_false(
        self, tmp_path: Path, disabled_deploy_runner: FakeDeployRunner
    ) -> None:
        orch = _orch(tmp_path, disabled_deploy_runner)
        orch.resolve_agent = lambda owner, fresh=False: _FabricatingAgent(  # type: ignore[method-assign]
            state_manager=orch.state
        )
        orch.run_task("TASK-002")
        document = orch.state.load_tasks_document()
        body = json.dumps(document)
        assert "42/42 tests passed" not in json.dumps(
            [t for t in document["tasks"] if t["id"] == "TASK-002"][0].get("execution", {})
        )
        assert "coverage 94%" not in (document["tasks"][1].get("notes") or "")


# ---------------------------------------------------------------------------
# Heredoc guard: the runner is the only spawn site
# ---------------------------------------------------------------------------


class TestSingleChokepoint:
    def test_only_deploy_runner_spawns_processes(self) -> None:
        """Audit invariant: `grep subprocess orchestrator/` must be one file.

        If a second module gains a spawn call, the security properties audited in
        this file no longer cover the package, so the test fails rather than the
        assumption quietly rotting.
        """
        import ast

        package = REPO_ROOT / "orchestrator"
        offenders: List[str] = []
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = ""
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                if name in ("run", "Popen", "call", "check_call", "check_output", "spawn"):
                    # Only count subprocess-shaped calls, not arbitrary .run().
                    target = ""
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        target = func.value.id
                    if target == "subprocess" or name in (
                        "Popen",
                        "check_call",
                        "check_output",
                    ):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        assert all(
            offender.startswith("orchestrator/deploy_runner.py:")
            for offender in offenders
        ), f"process spawning outside the chokepoint: {offenders}"
        assert offenders, "expected deploy_runner to be the single spawn site"

    def test_shell_true_appears_nowhere(self) -> None:
        for path in (REPO_ROOT / "orchestrator").rglob("*.py"):
            assert "shell=True" not in path.read_text(encoding="utf-8"), (
                f"shell=True found in {path}"
            )


# ---------------------------------------------------------------------------
# Live execution — real subprocesses, real output
# ---------------------------------------------------------------------------


class TestLiveExecution:
    """Exercises the real runner end to end.

    The rest of the suite uses ``FakeDeployRunner`` so it stays hermetic. These
    tests spawn genuine (harmless) shell scripts inside a ``tmp_path`` project
    because the security properties worth asserting — argument-vector safety,
    env scrubbing, cwd confinement — are only meaningful against a real child
    process. Nothing outside ``tmp_path`` is ever touched.
    """

    @pytest.fixture()
    def live(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
        project = build_test_project(tmp_path / "live")
        scripts = {
            "pass.sh": "#!/bin/sh\necho '42/42 tests passed'\nexit 0\n",
            "fail.sh": "#!/bin/sh\necho '2 tests failed' >&2\nexit 3\n",
            "pwd.sh": "#!/bin/sh\npwd\n",
            "echoargs.sh": '#!/bin/sh\nfor a in "$@"; do echo "[$a]"; done\n',
            "leak.sh": (
                "#!/bin/sh\n"
                'if [ -n "$ANTHROPIC_API_KEY" ]; then echo LEAK; else echo CLEAN; fi\n'
            ),
        }
        for name, body in scripts.items():
            path = project / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        monkeypatch.setenv(
            "ORCHESTRATOR_DEPLOY_ALLOWLIST", ",".join(scripts)
        )
        return {"project": project, "runner": DeployRunner(project)}

    def test_allowlisted_script_really_executes(self, live: Dict[str, Any]) -> None:
        record = live["runner"].run_one({"command": "./pass.sh", "expect": "PASS"})
        assert record.executed is True
        assert record.exit_code == 0
        assert "42/42 tests passed" in record.stdout_tail
        assert record.expect_matched is True

    def test_failing_script_yields_ground_truth_not_absence(
        self, live: Dict[str, Any]
    ) -> None:
        """A real non-zero exit is information a fabricated report destroys."""
        record = live["runner"].run_one({"command": "./fail.sh", "expect": "PASS"})
        assert record.executed is True
        assert record.exit_code == 3
        assert record.succeeded is False
        assert "2 tests failed" in record.stderr_tail
        assert record.expect_matched is False

    def test_secrets_do_not_reach_the_child(self, live: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live-secret")
        record = live["runner"].run_one({"command": "./leak.sh"})
        assert record.executed is True
        assert "CLEAN" in record.stdout_tail
        assert "sk-live-secret" not in record.render()

    def test_child_runs_in_the_project_root(self, live: Dict[str, Any]) -> None:
        record = live["runner"].run_one({"command": "./pwd.sh"})
        assert Path(record.stdout_tail.strip()) == live["project"].resolve()

    def test_shell_metacharacters_in_args_are_inert(
        self, live: Dict[str, Any], tmp_path: Path
    ) -> None:
        """The argument is data, never syntax: no shell exists to re-parse it."""
        canary = tmp_path / "PWNED"
        record = live["runner"].run_one(
            {"command": "./echoargs.sh", "args": [f"; touch {canary}"]}
        )
        assert record.executed is True
        assert not canary.exists(), "a shell metacharacter escaped into execution"
        assert f"[; touch {canary}]" in record.stdout_tail

    def test_interpreters_are_not_allowlisted_by_bare_name(
        self, live: Dict[str, Any]
    ) -> None:
        """Permitting a test script must not permit a shell."""
        record = live["runner"].run_one({"command": "/bin/sh", "args": ["-c", "echo pwned"]})
        assert record.executed is False
        assert "not on the deploy allowlist" in record.reason

    def test_project_script_outside_project_is_refused(
        self, live: Dict[str, Any], tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.sh"
        outside.write_text("#!/bin/sh\necho escaped\n", encoding="utf-8")
        outside.chmod(0o755)
        record = live["runner"].run_one({"command": "../outside.sh"})
        assert record.executed is False, "a traversal must not reach an outside script"

    def test_non_executable_allowlisted_name_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = build_test_project(tmp_path / "noexec")
        (project / "data.sh").write_text("not executable", encoding="utf-8")
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "data.sh")
        record = DeployRunner(project).run_one({"command": "./data.sh"})
        assert record.executed is False

    def test_timeout_kills_a_hanging_script(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = build_test_project(tmp_path / "hang")
        (project / "hang.sh").write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        (project / "hang.sh").chmod(0o755)
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "hang.sh")
        runner = DeployRunner(project, timeout=1.0)
        record = runner.run_one({"command": "./hang.sh"})
        assert record.status == STATUS_TIMEOUT
        assert record.executed is False
        assert "timeout" in record.reason

    def test_evidence_from_a_live_run_is_usable(self, live: Dict[str, Any]) -> None:
        record = live["runner"].run_one({"command": "./pass.sh"})
        relative = live["runner"].write_evidence("TASK-001", record)
        assert relative is not None
        payload = json.loads((live["project"] / relative).read_text())
        assert payload["executed"] is True
        assert payload["exit_code"] == 0
        assert "42/42 tests passed" in payload["transcript"]

    def test_path_command_yields_a_single_flat_filename(self, live: Dict[str, Any]) -> None:
        """A path-shaped command must not produce a nested evidence path."""
        (live["project"] / "sub").mkdir()
        (live["project"] / "sub" / "t.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        (live["project"] / "sub" / "t.sh").chmod(0o755)
        live["runner"]._explicit_allowlist = ("t.sh",)
        record = live["runner"].run_one({"command": "./sub/t.sh"})
        relative = live["runner"].write_evidence("TASK-002", record)
        assert relative is not None
        assert relative.count("/") == 3, f"unexpected evidence nesting: {relative}"
        assert (live["project"] / relative).exists()


class TestLiveDispatchIntegration:
    """The DoD gate against a real runner rather than a fake."""

    def _project(self, tmp_path: Path) -> Path:
        project = build_test_project(tmp_path / "d")
        script = project / "verify.sh"
        script.write_text(
            "#!/bin/sh\necho '42/42 tests passed'\nexit 0\n", encoding="utf-8"
        )
        script.chmod(0o755)
        return project

    def test_fabrication_blocked_when_channel_is_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit's PoC: prose-only reply must not reach DONE."""
        project = self._project(tmp_path)
        monkeypatch.delenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", raising=False)
        orch = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        orch.resolve_agent = lambda owner, fresh=False: _FabricatingAgent(  # type: ignore[method-assign]
            state_manager=orch.state
        )
        result = orch.run_task("TASK-002")
        assert result.new_status != config.TASK_DONE
        assert orch.get_task("TASK-002")["status"] != config.TASK_DONE
        assert "ground-truth" in (orch.get_task("TASK-002").get("notes") or "")

    def test_real_passing_run_allows_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._project(tmp_path)
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "verify.sh")

        class Delivering(_FabricatingAgent):
            AGENT_ID = "live_delivering_agent"

            def execute(self, payload: Dict[str, Any]) -> Any:
                task_id = str(payload["task"]["id"])
                expected = str((payload["task"].get("expected_outputs") or [None])[0])
                return self.completed(
                    task_id,
                    "Executed verify.sh: 42/42 tests passed",
                    data={
                        "deploy": [{"command": "./verify.sh", "expect": "PASS"}],
                        "documents": {expected: "# TEST_REPORT\n\n42/42 passed\n"},
                    },
                )

        orch = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck2")
        orch.resolve_agent = lambda owner, fresh=False: Delivering(  # type: ignore[method-assign]
            state_manager=orch.state
        )
        result = orch.run_task("TASK-002")
        assert result.new_status == config.TASK_DONE, (
            f"real evidence should permit DONE, got {result.new_status}"
        )
        evidence = list((project / "docs" / "evidence").rglob("*.json"))
        assert evidence, "a real run must persist a transcript"
        payload = json.loads(evidence[0].read_text())
        assert payload["executed"] is True
        assert payload["exit_code"] == 0

    def test_real_failing_run_blocks_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._project(tmp_path)
        (project / "bad.sh").write_text(
            "#!/bin/sh\necho '1 test failed' >&2\nexit 1\n", encoding="utf-8"
        )
        (project / "bad.sh").chmod(0o755)
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "bad.sh")

        class Failing(_FabricatingAgent):
            AGENT_ID = "live_failing_agent"

            def execute(self, payload: Dict[str, Any]) -> Any:
                task_id = str(payload["task"]["id"])
                expected = str((payload["task"].get("expected_outputs") or [None])[0])
                return self.completed(
                    task_id,
                    "Ran the test suite",
                    data={
                        "deploy": [{"command": "./bad.sh", "expect": "PASS"}],
                        "documents": {expected: "# TEST_REPORT\n\n1 failed\n"},
                    },
                )

        orch = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck3")
        orch.resolve_agent = lambda owner, fresh=False: Failing(  # type: ignore[method-assign]
            state_manager=orch.state
        )
        result = orch.run_task("TASK-002")
        assert result.new_status != config.TASK_DONE
        assert "verification failed" in (orch.get_task("TASK-002").get("notes") or "")
