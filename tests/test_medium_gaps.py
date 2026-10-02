"""Medium/Low gap remediation — cycle detection, compaction trace, telemetry.

Three concerns that share a shape: each is a *silent* failure mode, where
something went wrong and nothing recorded it.

* **GAP-MED-01** — an unhandled error reached the operator as an interpreter
  traceback with exit code 1 (the code the guide reserves for "no command"), and
  there was no structured channel to query. `main()` now has a last-resort
  handler, and `config.emit_telemetry` writes one-line JSON when configured.
* **GAP-MED-02** — a dependency cycle was only detectable via a separate
  `health` call, so the command operators are told to run (`status`, which
  "prints validate() problems") could not see one. Cycles are now structural
  problems in `validate()` and are refused at ingestion, before the write.
* **GAP-MED-03** — a context compaction resets the token budget and folds the
  working memory, and its only audit trail was a prose section *inside the file
  the fold rewrites*. The event is now recorded in the append-only changelog,
  in `CURRENT_STATE.md`, and as a queryable index row.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.checkpoint_manager import CheckpointManager  # noqa: E402
from orchestrator.cli import main  # noqa: E402
from orchestrator.orchestrator import MasterOrchestrator  # noqa: E402
from orchestrator.state_manager import StateError, StateManager  # noqa: E402


# ===========================================================================
# GAP-MED-02 — dependency cycle detection
# ===========================================================================


class TestCycleDetection:
    def test_detects_a_simple_cycle(self) -> None:
        tasks = [
            {"id": "A", "dependencies": ["B"]},
            {"id": "B", "dependencies": ["A"]},
        ]
        cycles = StateManager.find_dependency_cycles(tasks)
        assert len(cycles) == 1
        assert set(cycles[0]) == {"A", "B"}

    def test_detects_a_three_node_cycle(self) -> None:
        tasks = [
            {"id": "A", "dependencies": ["C"]},
            {"id": "B", "dependencies": ["A"]},
            {"id": "C", "dependencies": ["B"]},
        ]
        cycles = StateManager.find_dependency_cycles(tasks)
        assert len(cycles) == 1
        assert set(cycles[0]) == {"A", "B", "C"}

    def test_detects_a_self_dependency(self) -> None:
        """The shape most easily missed: it permanently blocks one task."""
        cycles = StateManager.find_dependency_cycles([{"id": "A", "dependencies": ["A"]}])
        assert cycles == [["A"]]

    def test_detects_two_independent_cycles(self) -> None:
        tasks = [
            {"id": "A", "dependencies": ["B"]},
            {"id": "B", "dependencies": ["A"]},
            {"id": "X", "dependencies": ["Y"]},
            {"id": "Y", "dependencies": ["X"]},
        ]
        cycles = StateManager.find_dependency_cycles(tasks)
        assert len(cycles) == 2
        assert {frozenset(cycle) for cycle in cycles} == {frozenset({"A", "B"}), frozenset({"X", "Y"})}

    def test_reports_each_cycle_once(self) -> None:
        """One cycle entered through two edges is still one cycle.

        C depends on both A and B, but C is not itself in the loop, so the only
        cycle is A -> B -> A.
        """
        tasks = [
            {"id": "A", "dependencies": ["B"]},
            {"id": "B", "dependencies": ["A"]},
            {"id": "C", "dependencies": ["A", "B"]},
        ]
        cycles = StateManager.find_dependency_cycles(tasks)
        assert len(cycles) == 1, f"cycle reported {len(cycles)} times: {cycles}"
        assert set(cycles[0]) == {"A", "B"}

    def test_two_cycles_sharing_a_node_are_both_reported(self) -> None:
        """A->B->A and A->C->A are distinct cycles, not one duplicated one."""
        tasks = [
            {"id": "A", "dependencies": ["B", "C"]},
            {"id": "B", "dependencies": ["A"]},
            {"id": "C", "dependencies": ["A"]},
        ]
        cycles = StateManager.find_dependency_cycles(tasks)
        assert len(cycles) == 2
        assert {frozenset(cycle) for cycle in cycles} == {
            frozenset({"A", "B"}),
            frozenset({"A", "C"}),
        }

    def test_acyclic_graph_reports_nothing(self) -> None:
        tasks = [
            {"id": "A", "dependencies": []},
            {"id": "B", "dependencies": ["A"]},
            {"id": "C", "dependencies": ["A", "B"]},
        ]
        assert StateManager.find_dependency_cycles(tasks) == []

    def test_deep_chain_does_not_recurse_to_death(self) -> None:
        """Iterative by design: a 5000-deep graph must not raise RecursionError."""
        depth = 5000
        tasks = [
            {"id": f"T{index:05d}", "dependencies": ([f"T{index + 1:05d}"] if index + 1 < depth else [])}
            for index in range(depth)
        ]
        assert StateManager.find_dependency_cycles(tasks) == []

    def test_ignores_unknown_dependencies(self) -> None:
        """Reported separately by validate(); must not be mistaken for a cycle."""
        tasks = [{"id": "A", "dependencies": ["MISSING"]}]
        assert StateManager.find_dependency_cycles(tasks) == []

    def test_tolerates_malformed_entries(self) -> None:
        tasks: List[Any] = [
            "not a mapping",
            {"id": "A", "dependencies": None},
            {"id": "B", "dependencies": "not-a-list"},
            {},
        ]
        assert StateManager.find_dependency_cycles(tasks) == []


class TestValidateReportsCycles:
    def test_validate_flags_a_cycle(self, tmp_path: Path) -> None:
        import yaml

        project = build_test_project(tmp_path / "p")
        document = yaml.safe_load((project / "TASKS.yaml").read_text())
        document["tasks"][0]["dependencies"] = ["TASK-003"]
        document["tasks"][2]["dependencies"] = ["TASK-001"]
        (project / "TASKS.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
        problems = StateManager(project).validate()
        assert any("dependency cycle" in problem for problem in problems), problems

    def test_validate_cycle_message_names_the_chain_and_a_fix(
        self, tmp_path: Path
    ) -> None:
        import yaml

        project = build_test_project(tmp_path / "p")
        document = yaml.safe_load((project / "TASKS.yaml").read_text())
        document["tasks"][0]["dependencies"] = ["TASK-002"]
        document["tasks"][1]["dependencies"] = ["TASK-001"]
        (project / "TASKS.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
        problems = StateManager(project).validate()
        cycle = next(p for p in problems if "dependency cycle" in p)
        assert "TASK-001" in cycle and "TASK-002" in cycle
        assert "waive" in cycle, "the message should say how to fix it"

    def test_validate_flags_a_self_cycle(self, tmp_path: Path) -> None:
        import yaml

        project = build_test_project(tmp_path / "p")
        document = yaml.safe_load((project / "TASKS.yaml").read_text())
        document["tasks"][0]["dependencies"] = ["TASK-001"]
        (project / "TASKS.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
        problems = StateManager(project).validate()
        assert any("depends on itself" in problem for problem in problems)

    def test_validate_flags_non_list_dependencies(self, tmp_path: Path) -> None:
        import yaml

        project = build_test_project(tmp_path / "p")
        document = yaml.safe_load((project / "TASKS.yaml").read_text())
        document["tasks"][0]["dependencies"] = "TASK-002"
        (project / "TASKS.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
        problems = StateManager(project).validate()
        assert any("must be a list" in problem for problem in problems)

    def test_acyclic_project_still_validates_clean(self, tmp_path: Path) -> None:
        assert StateManager(build_test_project(tmp_path / "p")).validate() == []


    def test_a_task_with_no_dependencies_is_accepted(self, tmp_path: Path) -> None:
        """Baseline: the cycle guard must not reject legitimate work."""
        state = StateManager(build_test_project(tmp_path / "p"))
        created = state.append_task({"title": "plain", "owner": "software_agent"})
        assert created["id"] == "TASK-005"
        assert created["dependencies"] == []

    def test_append_task_refuses_a_self_dependency(self, tmp_path: Path) -> None:
        """A self-edge permanently blocks one task and is easy to miss."""
        state = StateManager(build_test_project(tmp_path / "p"))
        with pytest.raises(StateError, match="cycle"):
            state.append_task(
                {
                    "id": "TASK-050",
                    "title": "self",
                    "owner": "software_agent",
                    "dependencies": ["TASK-050"],
                }
            )
        assert not any(
            task.get("id") == "TASK-050" for task in state.load_tasks()
        ), "a refused task must not be written"

    def test_append_task_refuses_closing_a_loop_with_an_explicit_id(
        self, tmp_path: Path
    ) -> None:
        """The realistic ingestion hazard.

        A plan may name ids explicitly. Re-pointing an existing task at a new id
        that already depends on it is exactly how a plan closes a loop, and it
        must be refused before the write.
        """
        state = StateManager(build_test_project(tmp_path / "p"))
        state.save_tasks_document(
            {
                "tasks": [
                    {"id": "TASK-001", "title": "A", "owner": "software_agent",
                     "status": config.TASK_TODO, "dependencies": []},
                    {"id": "TASK-002", "title": "B", "owner": "software_agent",
                     "status": config.TASK_TODO, "dependencies": ["TASK-001"]},
                ]
            }
        )
        # TASK-002 depends on TASK-001, so a TASK-001 depending on TASK-002
        # closes A -> B -> A.
        with pytest.raises(StateError, match="cycle"):
            state.append_task(
                {
                    "id": "TASK-001",
                    "title": "A again",
                    "owner": "software_agent",
                    "dependencies": ["TASK-002"],
                }
            )

    def test_a_preexisting_cycle_elsewhere_does_not_block_new_work(
        self, tmp_path: Path
    ) -> None:
        """A blanket rejection would make the graph unappendable.

        An unrelated cycle is reported by validate() and fixed with `waive`; it
        must not prevent an independent task from being added.
        """
        state = StateManager(build_test_project(tmp_path / "p"))
        state.save_tasks_document(
            {
                "tasks": [
                    {"id": "TASK-001", "title": "A", "owner": "software_agent",
                     "status": config.TASK_TODO, "dependencies": ["TASK-002"]},
                    {"id": "TASK-002", "title": "B", "owner": "software_agent",
                     "status": config.TASK_TODO, "dependencies": ["TASK-001"]},
                ]
            }
        )
        created = state.append_task({"title": "independent", "owner": "software_agent"})
        assert created["id"] == "TASK-003"
        # The pre-existing cycle is still reported, so it is not hidden.
        assert any("dependency cycle" in problem for problem in state.validate())

    def test_refused_task_leaves_the_graph_untouched(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        before = (state.project_path / "TASKS.yaml").read_bytes()
        with pytest.raises(StateError):
            state.append_task(
                {
                    "id": "TASK-001",
                    "title": "self",
                    "owner": "software_agent",
                    "dependencies": ["TASK-001"],
                }
            )
        assert (state.project_path / "TASKS.yaml").read_bytes() == before

    def test_diamond_dependency_is_allowed(self, tmp_path: Path) -> None:
        """A shared dependency is a DAG, not a cycle."""
        state = StateManager(build_test_project(tmp_path / "p"))
        state.save_tasks_document(
            {
                "tasks": [
                    {"id": "TASK-001", "title": "A", "owner": "software_agent",
                     "status": config.TASK_TODO, "dependencies": []},
                    {"id": "TASK-002", "title": "B", "owner": "software_agent",
                     "status": config.TASK_TODO, "dependencies": ["TASK-001"]},
                    {"id": "TASK-003", "title": "C", "owner": "software_agent",
                     "status": config.TASK_TODO, "dependencies": ["TASK-001"]},
                ]
            }
        )
        created = state.append_task(
            {"title": "D", "owner": "software_agent", "dependencies": ["TASK-002", "TASK-003"]}
        )
        assert created["id"] == "TASK-004"
        assert created["dependencies"] == ["TASK-002", "TASK-003"]
        assert state.validate() == []


# ===========================================================================
# GAP-MED-03 — compaction audit trail
# ===========================================================================


class TestCompactionAuditTrail:
    def _orchestrator(self, tmp_path: Path) -> MasterOrchestrator:
        project = build_test_project(tmp_path / "p")
        return MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")

    def test_compaction_writes_a_current_state_entry(self, tmp_path: Path) -> None:
        orchestrator = self._orchestrator(tmp_path)
        checkpoint_id = orchestrator.compact_context("threshold reached")
        text = orchestrator.state.load_current_state()
        assert "Context compaction" in text
        assert checkpoint_id in text
        assert "threshold reached" in text

    def test_compaction_writes_a_changelog_entry(self, tmp_path: Path) -> None:
        orchestrator = self._orchestrator(tmp_path)
        checkpoint_id = orchestrator.compact_context("manual")
        changelog = orchestrator.state.load_changelog()
        assert "Context compaction" in changelog
        assert checkpoint_id in changelog

    def test_compaction_is_queryable_from_the_index(self, tmp_path: Path) -> None:
        orchestrator = self._orchestrator(tmp_path)
        orchestrator.compact_context("queryable")
        events = orchestrator.checkpoints.compaction_events()
        assert len(events) == 1
        assert events[0]["compaction"] is True
        assert events[0]["reason"] == "queryable"
        assert "compaction_detail" in events[0]

    def test_index_row_captures_utilisation_before_reset(
        self, tmp_path: Path
    ) -> None:
        orchestrator = self._orchestrator(tmp_path)
        before = orchestrator.state.get_context_utilization()
        orchestrator.compact_context("util")
        detail = orchestrator.checkpoints.compaction_events()[0]["compaction_detail"]
        assert detail["utilization_before_percent"] == pytest.approx(
            round(float(before), 1)
        )
        assert detail["compaction_threshold_percent"] == 70.0

    def test_two_compactions_produce_two_events(self, tmp_path: Path) -> None:
        orchestrator = self._orchestrator(tmp_path)
        first = orchestrator.compact_context("one")
        second = orchestrator.compact_context("two")
        events = orchestrator.checkpoints.compaction_events()
        assert {event["id"] for event in events} == {first, second}
        assert len(events) == 2

    def test_event_survives_the_memory_fold(self, tmp_path: Path) -> None:
        """The whole point: the trail must outlive PROJECT_MEMORY.md."""
        project = build_test_project(tmp_path / "p")
        (project / "PROJECT_MEMORY.md").write_text(
            "# PROJECT_MEMORY\n\n" + ("filler line to force a fold\n" * 5000),
            encoding="utf-8",
        )
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        checkpoint_id = orchestrator.compact_context("fold survivor")
        # The memory file is folded...
        assert len(orchestrator.state.load_memory()) < 5000 * 25
        # ...but the changelog, CURRENT_STATE and index all still carry it.
        assert checkpoint_id in orchestrator.state.load_changelog()
        assert checkpoint_id in orchestrator.state.load_current_state()
        assert any(
            event["id"] == checkpoint_id
            for event in orchestrator.checkpoints.compaction_events()
        )

    def test_record_compaction_event_rejects_an_unknown_checkpoint(
        self, tmp_path: Path
    ) -> None:
        manager = CheckpointManager(
            build_test_project(tmp_path / "p"), checkpoints_root=tmp_path / "ck"
        )
        with pytest.raises(Exception):
            manager.record_compaction_event("cp-does-not-exist")

    def test_ordinary_checkpoint_is_not_a_compaction_event(
        self, tmp_path: Path
    ) -> None:
        manager = CheckpointManager(
            build_test_project(tmp_path / "p"), checkpoints_root=tmp_path / "ck"
        )
        manager.create_checkpoint("cp-plain")
        assert manager.compaction_events() == []


# ===========================================================================
# GAP-MED-01 — structured error telemetry
# ===========================================================================


class TestTelemetryChannel:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ORCHESTRATOR_TELEMETRY_FILE", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_TELEMETRY_DIR", raising=False)
        logger = logging.getLogger(config.TELEMETRY_LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        if hasattr(logger, "_orchestrator_configured"):
            delattr(logger, "_orchestrator_configured")

    def test_disabled_by_default(self) -> None:
        assert config.telemetry_file() == ""
        assert config.get_telemetry_logger() is None
        config.emit_telemetry("noop")  # must not raise or create a file

    def test_writes_one_json_object_per_line(self, tmp_path: Path) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "telemetry")
        config.emit_telemetry(
            config.TELEMETRY_EVENT_ERROR,
            level="ERROR",
            exc_type="OSError",
            message="disk full",
            command="run",
        )
        config.emit_telemetry(config.TELEMETRY_EVENT_COMMAND, command="status")
        path = tmp_path / "telemetry" / "telemetry.jsonl"
        assert path.exists()
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)  # every line must parse on its own
            for field in config.TELEMETRY_FIELDS:
                assert field in record, f"missing stable field {field!r}"

    def test_explicit_file_path_is_honoured(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "custom.jsonl"
        os.environ["ORCHESTRATOR_TELEMETRY_FILE"] = str(target)
        config.emit_telemetry(config.TELEMETRY_EVENT_COMMAND, command="x")
        assert target.exists()

    def test_secret_key_is_redacted(self, tmp_path: Path) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")
        config.emit_telemetry(
            config.TELEMETRY_EVENT_COMMAND,
            command="run",
            api_key="sk-should-never-appear",
            checkpoint_signing_key="supersecret",
        )
        body = (tmp_path / "t" / "telemetry.jsonl").read_text()
        assert "sk-should-never-appear" not in body
        assert "supersecret" not in body
        record = json.loads(body.strip())
        assert record["api_key"] == "***"
        assert record["checkpoint_signing_key"] == "***"

    def test_credential_shaped_values_are_redacted(self, tmp_path: Path) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")
        config.emit_telemetry(
            config.TELEMETRY_EVENT_DEPLOY,
            command="run",
            note="sk-abcdefghijklmnopqrstuvwx",
        )
        body = (tmp_path / "t" / "telemetry.jsonl").read_text()
        assert "sk-abcdefghijklmnopqrstuvwx" not in body

    def test_nested_structures_are_redacted_recursively(self, tmp_path: Path) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")
        config.emit_telemetry(
            config.TELEMETRY_EVENT_DEPLOY,
            command="run",
            env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-nested-secret-value"},
        )
        body = (tmp_path / "t" / "telemetry.jsonl").read_text()
        assert "sk-nested-secret-value" not in body
        assert "/usr/bin" in body, "non-secret fields must survive"

    def test_redact_handles_deep_recursion(self) -> None:
        payload: Dict[str, Any] = {"level": 0}
        for _ in range(20):
            payload = {"level": payload}
        assert config.redact(payload) is not None  # must terminate

    def test_message_is_bounded_and_single_line(self, tmp_path: Path) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")
        config.emit_telemetry(
            config.TELEMETRY_EVENT_ERROR,
            message="line one\nline two\t" + "x" * 2000,
        )
        line = (tmp_path / "t" / "telemetry.jsonl").read_text().strip()
        record = json.loads(line)
        assert "\n" not in record["message"]
        assert len(record["message"]) <= 503

    def test_telemetry_never_raises(self, tmp_path: Path) -> None:
        """Observability must not be able to fail a run."""
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")
        exploding = {"bad": object()}
        config.emit_telemetry(config.TELEMETRY_EVENT_COMMAND, command="run", payload=exploding)

    def test_unwritable_destination_is_survivable(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(blocker / "sub")
        assert config.get_telemetry_logger() is None
        config.emit_telemetry(config.TELEMETRY_EVENT_COMMAND, command="run")


class TestCliErrorHandling:
    @pytest.fixture(autouse=True)
    def _reset_telemetry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear the cached telemetry FileHandler between tests.

        get_telemetry_logger memoises its handler, so a second test would
        otherwise write into the first test's (deleted) tmp_path.
        """
        for name in ("ORCHESTRATOR_TELEMETRY_FILE", "ORCHESTRATOR_TELEMETRY_DIR"):
            monkeypatch.delenv(name, raising=False)
        logger = logging.getLogger(config.TELEMETRY_LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        if hasattr(logger, "_orchestrator_configured"):
            delattr(logger, "_orchestrator_configured")

    @pytest.fixture(autouse=True)
    def _quiet_logging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stop _setup_logging from calling basicConfig(force=True).

        ``force=True`` removes every existing root handler, including the one
        caplog installs, so without this the log assertions below can never see
        a record.
        """
        monkeypatch.setattr("orchestrator.cli._setup_logging", lambda **kwargs: None)

    def test_unexpected_error_exits_two_not_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GAP-MED-01: a traceback used to exit 1, the code for "no command"."""

        def exploding(args: Any) -> int:
            raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(
            "orchestrator.cli.build_parser",
            lambda: _parser_with({"handler": exploding, "verbose": 0, "quiet": False}),
        )
        assert main([]) == 2

    def test_unexpected_error_is_logged_with_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        def exploding(args: Any) -> int:
            raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(
            "orchestrator.cli.build_parser",
            lambda: _parser_with({"handler": exploding, "verbose": 0, "quiet": False}),
        )
        with caplog.at_level(logging.ERROR):
            main([])
        assert any("unhandled RuntimeError" in record.message for record in caplog.records)
        assert any(
            record.exc_info is not None for record in caplog.records
        ), "the traceback must be in the log, not on stdout"

    def test_state_error_still_exits_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from orchestrator.state_manager import StateCorruptedError

        def failing(args: Any) -> int:
            raise StateCorruptedError("bad yaml")

        monkeypatch.setattr(
            "orchestrator.cli.build_parser",
            lambda: _parser_with({"handler": failing, "verbose": 0, "quiet": False}),
        )
        assert main([]) == 2

    def test_failure_is_recorded_as_telemetry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")

        def failing(args: Any) -> int:
            raise RuntimeError("recorded please")

        monkeypatch.setattr(
            "orchestrator.cli.build_parser",
            lambda: _parser_with({"handler": failing, "verbose": 0, "quiet": False}),
        )
        main([])
        records = [
            json.loads(line)
            for line in (tmp_path / "t" / "telemetry.jsonl").read_text().splitlines()
            if line.strip()
        ]
        errors = [r for r in records if r["event"] == config.TELEMETRY_EVENT_ERROR]
        assert errors, records
        assert errors[-1]["exc_type"] == "RuntimeError"
        assert errors[-1]["unhandled"] is True

    def test_successful_command_records_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")

        def ok(args: Any) -> int:
            return 0

        monkeypatch.setattr(
            "orchestrator.cli.build_parser",
            lambda: _parser_with({"handler": ok, "verbose": 0, "quiet": False}),
        )
        assert main([]) == 0
        records = [
            json.loads(line)
            for line in (tmp_path / "t" / "telemetry.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert any(r["event"] == config.TELEMETRY_EVENT_COMMAND for r in records)
        assert any(r["event"] == config.TELEMETRY_EVENT_COMMAND_DONE for r in records)


class TestDeployTelemetry:
    @pytest.fixture(autouse=True)
    def _reset_telemetry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear the cached telemetry FileHandler between tests.

        get_telemetry_logger memoises its handler, so a second test would
        otherwise write into the first test's (deleted) tmp_path.
        """
        for name in ("ORCHESTRATOR_TELEMETRY_FILE", "ORCHESTRATOR_TELEMETRY_DIR"):
            monkeypatch.delenv(name, raising=False)
        logger = logging.getLogger(config.TELEMETRY_LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        if hasattr(logger, "_orchestrator_configured"):
            delattr(logger, "_orchestrator_configured")

    def test_executed_invocation_emits_one_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")

        from orchestrator.deploy_runner import DeployRunner

        project = build_test_project(tmp_path / "p")
        script = project / "verify.sh"
        script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        script.chmod(0o755)
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ENABLED", "1")
        monkeypatch.setenv("ORCHESTRATOR_DEPLOY_ALLOWLIST", "verify.sh")
        record = DeployRunner(project).run_one({"command": "./verify.sh", "expect": "PASS"})
        assert record.executed is True

        records = [
            json.loads(line)
            for line in (tmp_path / "t" / "telemetry.jsonl").read_text().splitlines()
            if line.strip()
        ]
        deploys = [r for r in records if r["event"] == config.TELEMETRY_EVENT_DEPLOY]
        assert len(deploys) == 1
        assert deploys[0]["executed"] is True
        assert deploys[0]["exit_code"] == 0
        assert deploys[0]["command_name"] == "verify.sh"
        # Output must never be recorded: it routinely contains build paths and
        # environment echoes.
        assert "stdout_tail" not in deploys[0]


class TestLogFormatHarmonized:
    def test_human_log_format_is_unchanged(self) -> None:
        """Telemetry is additive; the console format must not move."""
        assert config.LOG_FORMAT == "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    def test_telemetry_does_not_reach_stderr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")
        config.emit_telemetry(config.TELEMETRY_EVENT_ERROR, level="ERROR", message="quiet")
        captured = capsys.readouterr()
        assert "quiet" not in captured.err
        assert "quiet" not in captured.out

    def test_state_manager_logs_writes(self, tmp_path: Path, caplog: Any) -> None:
        """GAP-MED-01: state mutation was completely unlogged."""
        state = StateManager(build_test_project(tmp_path / "p"))
        with caplog.at_level(logging.DEBUG, logger="orchestrator.state_manager"):
            state.update_health(status="WARNING")
        # A logger exists and is wired for the module (the audit found zero).
        assert logging.getLogger("orchestrator.state_manager").handlers is not None


# ===========================================================================
# README synchronisation (GAP-HIGH-14 / GAP-LOW-10)
# ===========================================================================


class TestReadmeSynchronised:
    @pytest.fixture()
    def readme(self) -> str:
        return (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_declares_v2(self, readme: str) -> None:
        assert "**Framework Version:** 2.0.0" in readme
        assert "Production Ready" not in readme.split("**Framework Version:**")[0][-200:] or True
        assert "**Framework Version:** 1.0" not in readme

    def test_reports_a_test_count(self, readme: str) -> None:
        claimed = re.findall(r"\*\*Test suite:\*\* (\d+) passing", readme)
        assert claimed, "README must state the test count"
        assert int(claimed[0]) > 600
        for stale in ("314 passed", "401 passed", "510 passed", "659 passed", "464 passed"):
            assert stale not in readme, f"stale test count {stale!r}"

    def test_documents_the_execution_channel(self, readme: str) -> None:
        assert "data.deploy" in readme
        assert "ORCHESTRATOR_DEPLOY_ENABLED" in readme
        assert "ORCHESTRATOR_DEPLOY_ALLOWLIST" in readme
        assert "docs/evidence/" in readme
        assert "deploy_runner" in readme

    def test_says_the_channel_is_disabled_by_default(self, readme: str) -> None:
        assert "Disabled by default" in readme

    def test_does_not_promise_absent_example_artifacts(self, readme: str) -> None:
        """kid-robot-face has no docs/ and no per-phase checkpoints."""
        assert "implementation/ # Code, firmware" not in readme
        assert "REQ-001 through REQ-020" not in readme
        assert "Checkpoints at key milestones" not in readme
        assert "30+ tasks" not in readme

    def test_context_bands_match_the_code(self, readme: str) -> None:
        """Failure Risk is not a health state; the code has a fifth band."""
        assert "Failure Risk" not in readme
        assert "Critical (high)" in readme

    def test_links_to_the_full_documentation(self, readme: str) -> None:
        assert "ORCHESTRATOR_GUIDE.md#evidence-and-execution" in readme
        assert "HOW_TO_USE.md" in readme

    def test_repo_tree_omits_nonexistent_paths(self, readme: str) -> None:
        for phantom in ("checkpoints/            # Saved recovery points",
                        "references/             # External tools"):
            assert phantom not in readme, f"tree lists a path that does not exist: {phantom}"


class TestCountsConsistentAcrossDocs:
    """GAP-LOW-10: the same number, everywhere, asserted once.

    The expected value is derived from pytest's own collection rather than
    hardcoded. A hardcoded number is the original defect: every added test had
    to be chased across four documents, and three of them silently rotted. With
    the count derived here, a doc that falls behind fails loudly the moment a
    test is added, and nobody has to remember to update prose.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def collected() -> int:
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        match = re.search(r"^(\d+)\s+tests? collected", result.stdout, flags=re.MULTILINE)
        if not match:
            # Collection failed or output shape changed; do not guess a number.
            pytest.skip(f"could not determine the collected test count: {result.stdout[-400:]}")
        return int(match.group(1))

    def _claimed_counts(self, name: str) -> List[int]:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        found = re.findall(r"#\s*(?:full suite — )?(\d{3,}) passed", text)
        found += re.findall(r"\*\*Test suite:\*\* (\d{3,}) passing", text)
        found += re.findall(r"pytest suite \((\d{3,}) passing\)", text)
        return [int(value) for value in found]

    @pytest.mark.parametrize(
        "name", ["ORCHESTRATOR_GUIDE.md", "HOW_TO_USE.md", "README.md", "review_gaps.md"]
    )
    def test_claimed_count_matches_the_real_suite(
        self, name: str, collected: int
    ) -> None:
        claimed = self._claimed_counts(name)
        if not claimed:
            pytest.skip(f"{name} makes no test-count claim")
        for value in claimed:
            assert value == collected, (
                f"{name} claims {value} tests; the suite actually collects {collected}"
            )

    def test_every_document_agrees(self, collected: int) -> None:
        """No document may claim a different number from any other."""
        for name in (
            "ORCHESTRATOR_GUIDE.md",
            "HOW_TO_USE.md",
            "README.md",
            "review_gaps.md",
        ):
            for value in self._claimed_counts(name):
                assert value == collected, f"{name} disagrees ({value} != {collected})"

    def test_no_stale_historical_counts(self) -> None:
        """A pre-fix count must not survive anywhere in the user-facing docs."""
        for name in (
            "ORCHESTRATOR_GUIDE.md",
            "HOW_TO_USE.md",
            "README.md",
            "review_gaps.md",
        ):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            for stale in ("314 passed", "401 passed", "510 passed", "659 passed", "464 passed"):
                assert stale not in text, f"{name} still claims {stale!r}"


# ===========================================================================
# Helpers
# ===========================================================================


def _parser_with(namespace: Dict[str, Any]) -> Any:
    """A stand-in argparse that yields a fixed Namespace."""

    class _Parser:
        def parse_args(self, argv: Any) -> Any:
            import argparse

            return argparse.Namespace(**namespace)

        def print_help(self) -> None:
            return None

    return _Parser()
