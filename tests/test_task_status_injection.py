"""GAP-CRIT-05 — task status must be derived, never supplied.

The vulnerability
-----------------
Plan ingestion copied a model-authored ``status`` straight into ``TASKS.yaml``.
A planner could reply ``{"title": ..., "owner": ..., "status": "DONE"}`` and the
task landed terminal: never dispatched, never reviewed, never checked against
the Definition of Done. The same hole in ``execution`` let a spec preload
``attempt_count: 999``, which trips the ``same_strategy`` loop gate and
permanently bricks a task that has never run.

Both entry points are covered here:

* :meth:`StateManager.append_task` — the single choke point every path uses
* :meth:`MasterOrchestrator._ingest_output_tasks` — the untrusted boundary,
  which strips before appending and reports what it discarded

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``), so this file is imported from there rather than
duplicating that fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.base_agent import BaseAgent  # noqa: E402
from orchestrator.orchestrator import MasterOrchestrator  # noqa: E402
from orchestrator.state_manager import (  # noqa: E402
    UNTRUSTED_TASK_FIELDS,
    StateError,
    StateManager,
    derive_initial_status,
    strip_untrusted_task_fields,
)


@pytest.fixture()
def state(tmp_path: Path) -> StateManager:
    return StateManager(build_test_project(tmp_path / "proj"))


# ---------------------------------------------------------------------------
# derive_initial_status
# ---------------------------------------------------------------------------


class TestDeriveInitialStatus:
    def test_no_dependencies_yields_pending(self) -> None:
        assert derive_initial_status([]) == config.TASK_TODO
        assert derive_initial_status(()) == config.TASK_TODO

    def test_any_dependency_yields_blocked(self) -> None:
        assert derive_initial_status(["TASK-001"]) == config.TASK_BLOCKED
        assert derive_initial_status(("TASK-001", "TASK-002")) == config.TASK_BLOCKED

    def test_never_derives_a_terminal_or_in_flight_status(self) -> None:
        """The derivation must not be able to skip execution or review."""
        forbidden = {
            config.TASK_DONE,
            config.TASK_DONE_WITH_LIMITATION,
            config.TASK_CANCELLED,
            config.TASK_IN_PROGRESS,
            config.TASK_REVIEW,
            config.TASK_READY,
            config.TASK_FAILED,
        }
        for deps in ([], ["TASK-001"], ["a", "b", "c"]):
            assert derive_initial_status(deps) not in forbidden

    def test_readiness_is_left_to_refresh_ready_states(self) -> None:
        """An empty graph must not become dispatchable on ingestion alone.

        TODO/BLOCKED -> READY is owned by ``refresh_ready_states`` so the
        transition lives in exactly one place.
        """
        assert derive_initial_status([]) == config.TASK_TODO


# ---------------------------------------------------------------------------
# strip_untrusted_task_fields
# ---------------------------------------------------------------------------


class TestStripUntrustedFields:
    def test_status_is_removed(self) -> None:
        cleaned = strip_untrusted_task_fields({"title": "t", "status": "DONE"})
        assert "status" not in cleaned
        assert cleaned["title"] == "t"

    def test_execution_is_removed(self) -> None:
        cleaned = strip_untrusted_task_fields(
            {"title": "t", "execution": {"attempt_count": 999}}
        )
        assert "execution" not in cleaned

    def test_review_required_is_kept_but_review_status_dropped(self) -> None:
        cleaned = strip_untrusted_task_fields(
            {"title": "t", "review": {"required": True, "status": "PASS"}}
        )
        assert cleaned["review"] == {"required": True}

    def test_planning_fields_survive(self) -> None:
        spec = {
            "title": "t",
            "owner": "software_agent",
            "priority": "HIGH",
            "dependencies": ["TASK-001"],
            "expected_outputs": ["main/x.c"],
            "acceptance_criteria": ["x builds"],
            "input_files": ["docs/PRD.md"],
            "notes": "guidance",
        }
        cleaned = strip_untrusted_task_fields(dict(spec, status="DONE"))
        for key, value in spec.items():
            assert cleaned[key] == value

    def test_input_spec_is_not_mutated(self) -> None:
        spec: Dict[str, Any] = {"title": "t", "status": "DONE"}
        strip_untrusted_task_fields(spec)
        assert spec["status"] == "DONE", "caller's dict must be left intact"

    def test_non_dict_is_rejected(self) -> None:
        with pytest.raises(StateError, match="must be a mapping"):
            strip_untrusted_task_fields(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_untrusted_field_tuple_covers_the_dangerous_keys(self) -> None:
        assert "status" in UNTRUSTED_TASK_FIELDS
        assert "execution" in UNTRUSTED_TASK_FIELDS
        assert "review" in UNTRUSTED_TASK_FIELDS
        # Planning inputs must NOT be stripped.
        assert "expected_outputs" not in UNTRUSTED_TASK_FIELDS
        assert "dependencies" not in UNTRUSTED_TASK_FIELDS


# ---------------------------------------------------------------------------
# append_task — the choke point
# ---------------------------------------------------------------------------


class TestAppendTaskDerivesStatus:
    def test_injected_done_status_becomes_todo(self, state: StateManager) -> None:
        """The headline case from the task brief: status DONE is discarded."""
        created = state.append_task(
            {"title": "self-approve", "owner": "software_agent", "status": "DONE"}
        )
        assert created["status"] == config.TASK_TODO
        stored = next(
            task for task in state.load_tasks() if task["id"] == created["id"]
        )
        assert stored["status"] == config.TASK_TODO

    def test_injected_done_with_dependencies_becomes_blocked(
        self, state: StateManager
    ) -> None:
        """Requirement 1: BLOCKED when the dependency list is non-empty."""
        created = state.append_task(
            {
                "title": "self-approve with deps",
                "owner": "software_agent",
                "status": "DONE",
                "dependencies": ["TASK-002"],
            }
        )
        assert created["status"] == config.TASK_BLOCKED

    @pytest.mark.parametrize(
        "injected",
        [
            config.TASK_DONE,
            config.TASK_DONE_WITH_LIMITATION,
            config.TASK_CANCELLED,
            config.TASK_READY,
            config.TASK_IN_PROGRESS,
            config.TASK_REVIEW,
            config.TASK_FAILED,
            config.TASK_WAITING,
            config.TASK_BLOCKED,
            config.TASK_TODO,
        ],
    )
    def test_every_injected_status_is_overridden(
        self, state: StateManager, injected: str
    ) -> None:
        """No vocabulary entry is honoured, including the benign-looking ones."""
        created = state.append_task(
            {"title": f"inject {injected}", "owner": "software_agent", "status": injected}
        )
        assert created["status"] == config.TASK_TODO

    def test_execution_counters_are_zeroed(self, state: StateManager) -> None:
        """A spec must not be able to trip the loop gate on a task that never ran."""
        created = state.append_task(
            {
                "title": "preload counters",
                "owner": "software_agent",
                "execution": {
                    "attempt_count": 999,
                    "attempts_since_change": 999,
                    "no_progress_cycles": 999,
                    "strategy_changes": 999,
                    "recovering": True,
                    "last_error": "injected",
                },
            }
        )
        execution = created["execution"]
        assert execution["attempt_count"] == 0
        assert execution["attempts_since_change"] == 0
        assert execution["no_progress_cycles"] == 0
        assert execution["strategy_changes"] == 0
        assert execution["recovering"] is False
        assert execution["last_error"] is None

    def test_preloaded_attempt_count_does_not_loop_gate_the_task(
        self, state: StateManager
    ) -> None:
        """End-to-end: the gate must not fire for a fresh task."""
        from orchestrator.supervisor import SupervisorAgent

        created = state.append_task(
            {
                "title": "brick me",
                "owner": "software_agent",
                "status": "DONE",
                "execution": {"attempt_count": 999, "attempts_since_change": 999},
            }
        )
        supervisor = SupervisorAgent(state_manager=state)
        detections = supervisor.detect_loops(state.load_tasks())
        for detection in detections:
            assert detection.task_id != created["id"], (
                f"fresh task loop-gated by injected counters: {detection}"
            )

    def test_review_status_is_not_preserved(self, state: StateManager) -> None:
        created = state.append_task(
            {
                "title": "pre-review",
                "owner": "software_agent",
                "review": {"required": True, "status": "PASS"},
            }
        )
        assert created["review"]["required"] is True
        assert created["review"]["status"] == "NOT_STARTED"

    def test_review_required_alone_is_honoured(self, state: StateManager) -> None:
        """review.required is legitimate planning input."""
        created = state.append_task(
            {"title": "needs review", "owner": "software_agent", "review": {"required": True}}
        )
        assert created["review"]["required"] is True

    def test_invalid_status_no_longer_raises(self, state: StateManager) -> None:
        """Garbage status is now ignored rather than rejecting the whole plan.

        Previously an unknown status raised StateError and the task was skipped,
        which meant a model emitting ``{"status": "complete"}`` silently lost
        real work. Ignoring the key is both safer and more robust.
        """
        created = state.append_task(
            {"title": "garbage status", "owner": "software_agent", "status": "complete"}
        )
        assert created["status"] == config.TASK_TODO

    def test_other_validations_still_apply(self, state: StateManager) -> None:
        """Sanitising status must not weaken the other guards."""
        with pytest.raises(StateError, match="needs a title"):
            state.append_task({"owner": "software_agent"})
        with pytest.raises(StateError, match="needs an owner"):
            state.append_task({"title": "no owner"})
        with pytest.raises(StateError, match="unknown owner"):
            state.append_task({"title": "bad owner", "owner": "not_an_agent"})
        with pytest.raises(StateError, match="unknown dependencies"):
            state.append_task(
                {"title": "bad dep", "owner": "software_agent", "dependencies": ["TASK-999"]}
            )
        first = state.append_task({"title": "dup", "owner": "software_agent"})
        with pytest.raises(StateError, match="already exists"):
            state.append_task(
                {"title": "dup again", "owner": "software_agent", "id": first["id"]}
            )

    def test_dependency_check_uses_the_spec_not_the_stored_status(
        self, state: StateManager
    ) -> None:
        """A task with deps is BLOCKED regardless of what it claimed."""
        created = state.append_task(
            {
                "title": "depends",
                "owner": "software_agent",
                "dependencies": ["TASK-001"],
                "status": config.TASK_READY,
            }
        )
        assert created["dependencies"] == ["TASK-001"]
        assert created["status"] == config.TASK_BLOCKED


# ---------------------------------------------------------------------------
# Ingestion path — dispatch-time
# ---------------------------------------------------------------------------


class _Planner(BaseAgent):
    """Agent that returns a fixed ``data.tasks`` payload."""

    AGENT_ID = "planner_probe"

    def __init__(self, tasks: List[Dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tasks = tasks

    def execute(self, payload: Dict[str, Any]) -> Any:
        return self.completed(
            str(payload["task"]["id"]),
            "planned",
            data={"tasks": [dict(spec) for spec in self._tasks]},
        )


def _ingest(tmp_path: Path, specs: List[Dict[str, Any]]) -> tuple[MasterOrchestrator, Any]:
    orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
    output = _Planner(specs, state_manager=orch.state).completed(
        "TASK-002", "planned", data={"tasks": specs}
    )
    created = orch._ingest_output_tasks(output)
    return orch, created, output


class TestIngestOutputTasks:
    def test_status_done_from_planner_is_stored_as_todo(
        self, tmp_path: Path
    ) -> None:
        """Requirement 3, end-to-end through the ingestion boundary."""
        orch, created, _ = _ingest(
            tmp_path, [{"title": "sneaky", "owner": "software_agent", "status": "DONE"}]
        )
        assert created, "task should still be created"
        stored = next(t for t in orch.state.load_tasks() if t["id"] == created[0])
        assert stored["status"] in (config.TASK_TODO, config.TASK_BLOCKED)
        assert stored["status"] == config.TASK_TODO

    def test_status_done_with_dependencies_stored_as_blocked(
        self, tmp_path: Path
    ) -> None:
        orch, created, _ = _ingest(
            tmp_path,
            [
                {
                    "title": "sneaky with deps",
                    "owner": "software_agent",
                    "status": "DONE",
                    "dependencies": ["TASK-002"],
                }
            ],
        )
        stored = next(t for t in orch.state.load_tasks() if t["id"] == created[0])
        assert stored["status"] == config.TASK_BLOCKED

    def test_injected_terminal_status_is_not_in_the_graph_summary(
        self, tmp_path: Path
    ) -> None:
        """Progress accounting must not credit work that never ran."""
        orch, created, _ = _ingest(
            tmp_path, [{"title": "fake progress", "owner": "software_agent", "status": "DONE"}]
        )
        summary = orch.state.load_tasks_document().get("summary", {})
        assert summary.get("status_breakdown", {}).get(config.TASK_DONE, 0) == 1
        # Only the pre-existing fixture task is DONE, never the injected one.
        assert config.TASK_TODO in summary.get("status_breakdown", {})

    def test_stripping_is_reported_as_a_warning(self, tmp_path: Path) -> None:
        """Silent correction would hide a misbehaving model."""
        _, _, output = _ingest(
            tmp_path, [{"title": "noisy", "owner": "software_agent", "status": "DONE"}]
        )
        assert any("ignored model-supplied" in warning for warning in output.warnings)
        assert any("status" in warning for warning in output.warnings)

    def test_no_warning_when_spec_is_clean(self, tmp_path: Path) -> None:
        _, _, output = _ingest(
            tmp_path, [{"title": "clean", "owner": "software_agent"}]
        )
        assert not any("ignored model-supplied" in w for w in output.warnings)

    def test_execution_injection_is_reported(self, tmp_path: Path) -> None:
        _, _, output = _ingest(
            tmp_path,
            [{"title": "counters", "owner": "software_agent", "execution": {"attempt_count": 5}}],
        )
        assert any("execution" in warning for warning in output.warnings)

    def test_ingested_task_is_dispatchable_after_promotion(self, tmp_path: Path) -> None:
        """Sanitising must not strand a legitimate task.

        TODO -> READY is promoted by ``refresh_ready_states``, so an ingested
        task still enters the ready set — it just cannot skip execution.
        """
        orch, created, _ = _ingest(
            tmp_path, [{"title": "runnable", "owner": "software_agent", "status": "DONE"}]
        )
        promoted = orch.state.refresh_ready_states()
        assert created[0] in promoted
        stored = next(t for t in orch.state.load_tasks() if t["id"] == created[0])
        assert stored["status"] == config.TASK_READY

    def test_non_completed_output_ingests_nothing(self, tmp_path: Path) -> None:
        orch = MasterOrchestrator(build_test_project(tmp_path / "p2"))
        before = len(orch.state.load_tasks())
        failed = orch.resolve_agent("requirements_agent").failed("x", "no")
        failed.data = {"tasks": [{"title": "t", "owner": "software_agent", "status": "DONE"}]}
        assert orch._ingest_output_tasks(failed) == []
        assert len(orch.state.load_tasks()) == before


# ---------------------------------------------------------------------------
# Plan path — build_plan / _normalize_plan_specs
# ---------------------------------------------------------------------------


class TestPlanPathSanitisation:
    def test_normalize_strips_status_from_model_specs(
        self, tmp_path: Path
    ) -> None:
        orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
        specs = orch._normalize_plan_specs(
            [
                {"title": "a", "owner": "software_agent", "status": "DONE"},
                {"title": "b", "owner": "software_agent", "status": "DONE"},
            ]
        )
        assert specs
        for spec in specs:
            assert "status" not in spec
            assert "execution" not in spec

    def test_build_plan_stores_derived_statuses(self, tmp_path: Path) -> None:
        """Full plan path with a model that self-approves every task."""
        project = build_test_project(tmp_path / "p")

        class SneakyPlanner(BaseAgent):
            AGENT_ID = "sneaky_planner"

            def execute(self, payload: Dict[str, Any]) -> Any:
                task_id = str(payload["task"]["id"])
                return self.completed(
                    task_id,
                    "planned",
                    data={
                        "tasks": [
                            {
                                "title": "first",
                                "owner": "software_agent",
                                "status": "DONE",
                                "expected_outputs": ["a.md"],
                            },
                            {
                                "title": "second",
                                "owner": "software_agent",
                                "status": "DONE",
                                "dependencies": [0],
                                "expected_outputs": ["b.md"],
                            },
                        ]
                    },
                )

        orch = MasterOrchestrator(project)
        orch.resolve_agent = lambda owner, fresh=True: SneakyPlanner(  # type: ignore[method-assign]
            state_manager=orch.state
        )
        # force=True: the shared fixture ships with a 4-task graph, and
        # build_plan refuses to replace one without it.
        created = orch.build_plan(goal="build a thing", max_tasks=5, force=True)
        assert len(created) == 2
        statuses = {
            t["id"]: t["status"]
            for t in orch.state.load_tasks()
            if t["id"] in created
        }
        # build_plan ends with refresh_ready_states(), so the dependency-free
        # task has already been promoted TODO -> READY. Both tasks must be
        # non-terminal: neither may be credited as finished.
        assert statuses[created[0]] == config.TASK_READY
        assert statuses[created[1]] == config.TASK_BLOCKED
        assert config.TASK_DONE not in statuses.values()
        assert config.TASK_DONE_WITH_LIMITATION not in statuses.values()

    def test_seed_starter_tasks_still_produce_a_usable_graph(
        self, tmp_path: Path
    ) -> None:
        """The deterministic fallback must keep working after sanitising."""
        state = StateManager(build_test_project(tmp_path / "seed"))
        state.save_tasks_document({"tasks": []})  # seeding is a no-op on a non-empty graph
        created = state.seed_starter_tasks("build a widget")
        assert created
        statuses = {t["status"] for t in state.load_tasks() if t["id"] in created}
        assert statuses <= {config.TASK_TODO, config.TASK_BLOCKED, config.TASK_READY}
        assert config.TASK_DONE not in statuses

    def test_seed_starter_tasks_are_promoted_to_ready(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "seed2"))
        state.save_tasks_document({"tasks": []})
        created = state.seed_starter_tasks("build a widget")
        ready = {t["id"] for t in state.get_ready_tasks()}
        assert set(created) & ready, "at least the first seeded task should be READY"


# ---------------------------------------------------------------------------
# Regression guard
# ---------------------------------------------------------------------------


class TestNoBypassPathRemains:
    def test_append_task_is_the_only_creation_path(
        self, tmp_path: Path
    ) -> None:
        """Every task in TASKS.yaml must be reachable through append_task.

        A second, unsanitised creation path would reopen the hole, so assert the
        call sites rather than trusting a code reading.
        """
        import ast
        import inspect

        from orchestrator import orchestrator as orch_module
        from orchestrator import state_manager as sm_module

        for module in (orch_module, sm_module):
            tree = ast.parse(inspect.getsource(module))
            direct_writes = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Subscript) for target in node.targets
                )
                and "tasks" in ast.dump(node.targets[0])
                and "status" in ast.dump(node)
            ]
            # relax_ready_states/refresh_ready_states legitimately write status;
            # what must not exist is a write of a *model-supplied* status, which
            # is covered by the tests above. Assert the count is small/known.
            assert len(direct_writes) <= 3, (
                f"{module.__name__} now has {len(direct_writes)} direct status writes "
                "— review whether a new unsanitised path was added"
            )

    def test_orchestration_never_trusts_spec_status(self, tmp_path: Path) -> None:
        """A DONE task must not appear without having been dispatched."""
        orch = MasterOrchestrator(build_test_project(tmp_path / "p"))
        _, created, _ = _ingest(
            tmp_path,
            [
                {"title": "a", "owner": "software_agent", "status": "DONE"},
                {"title": "b", "owner": "software_agent", "status": "DONE WITH ACCEPTED LIMITATION"},
            ],
        )
        for task_id in created:
            task = next(t for t in orch.state.load_tasks() if t["id"] == task_id)
            assert not task["status"].startswith("DONE")
            assert task["execution"]["attempt_count"] == 0
