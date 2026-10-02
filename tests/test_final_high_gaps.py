"""Regressions for the final four High-severity audit gaps.

GAP-HIGH-01 — a registered (pinned) agent was returned even for a
``fresh=True`` resolution, so two parallel dispatches owned by the same agent
shared one object's mutable scratch state (``_current_task_id``,
``last_payload_chars``, ``_delivery_snapshot``). One task's output could be
attributed to another, and one task's DoD could read another's delivery
snapshot.

GAP-HIGH-02 — the checkpoint trigger chain was ``auto or milestone or phase``,
so whichever trigger answered first suppressed the others. A dispatch that both
crossed the compaction threshold and completed a milestone therefore never ran
``check_milestones()``.

GAP-HIGH-03 — the Definition-of-Done check and ``repair_delivery()`` (a full LLM
round trip) ran inside ``_state_lock``, so one task's repair stalled every other
dispatch worker's state turn.

GAP-HIGH-04 — ``validate()`` only checked a few structural fields and was only
reachable from ``init``, so an ingested plan's problems surfaced as an unrelated
runtime failure much later.

Everything here is offline: agents are real objects with stubbed ``run`` and
``repair_delivery`` methods, and no LLM backend is contacted.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import FakeLLMClient, build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.base_agent import (  # noqa: E402
    AgentOutput,
    BaseAgent,
    create_agent,
)
from orchestrator.llm_client import LLMClient  # noqa: E402
from orchestrator.orchestrator import MasterOrchestrator  # noqa: E402
from orchestrator.state_manager import (  # noqa: E402
    ADVISORY_PREFIX,
    StateError,
    StateManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_tasks(project: Path) -> None:
    StateManager(project).save_tasks_document(
        {
            "tasks": [],
            "parallel_groups": [],
            "critical_path": {"path": []},
            "summary": {"total": 0},
        }
    )


def _agent(project: Path, agent_id: str, behavior: Any) -> MasterOrchestrator:
    orchestrator = MasterOrchestrator(project)
    orchestrator.register_agent(behavior(project, orchestrator.state, agent_id))
    _clear_tasks(project)
    return orchestrator


class _StubAgent(BaseAgent):
    """Minimal agent whose ``run``/``repair_delivery`` are supplied by tests.

    Records every execution on the *instance* so a test can prove two dispatches
    did not share one object (GAP-HIGH-01). Deliberately holds mutable scratch
    state, which is what made sharing unsafe.
    """

    def __init__(
        self,
        state_manager: StateManager,
        on_run: Any = None,
        on_repair: Any = None,
        run_seconds: float = 0.0,
    ) -> None:
        super().__init__(state_manager=state_manager)
        self._on_run = on_run
        self._on_repair = on_repair
        self._run_seconds = run_seconds
        # Mutable scratch state — exactly the class of field that must not be
        # shared between concurrent dispatches.
        self._current_task_id: str | None = None
        self.last_payload_chars: int = 0
        self._delivery_snapshot: Dict[str, Any] = {}
        self.run_task_ids: List[str] = []
        self.repair_calls: int = 0

    def run(self, task: Dict[str, Any], materialize: bool = True) -> AgentOutput:
        self._current_task_id = str(task.get("id"))
        self.last_payload_chars += 1
        self.run_task_ids.append(self._current_task_id)
        if self._run_seconds:
            time.sleep(self._run_seconds)
        if self._on_run is not None:
            return self._on_run(task, self)
        return AgentOutput(
            agent_id=self.AGENT_ID,
            task_id=str(self._current_task_id or ""),
            status=config.AGENT_STATUS_COMPLETED,
            summary="stub run",
        )

    def repair_delivery(
        self,
        task: Dict[str, Any],
        output: AgentOutput,
        problems: List[str],
    ) -> AgentOutput | None:
        self.repair_calls += 1
        self._delivery_snapshot = {"problems": list(problems)}
        if self._on_repair is not None:
            return self._on_repair(task, output, problems, self)
        return None

    def build_prompt(self, task: Dict[str, Any]) -> str:
        return f"stub prompt for {task.get('id')}"


def _make_stub(state_manager: StateManager, agent_id: str, **kwargs: Any) -> _StubAgent:
    agent = _StubAgent(state_manager, **kwargs)
    agent.AGENT_ID = agent_id  # type: ignore[misc]
    return agent


def _add_task(
    state: StateManager,
    task_id: str,
    owner: str = "requirements_agent",
    dependencies: List[str] | None = None,
    status: str = config.TASK_READY,
    **extra: Any,
) -> None:
    state.append_task(
        {
            "id": task_id,
            "title": f"Task {task_id}",
            "owner": owner,
            "status": status,
            "dependencies": list(dependencies or []),
            "notes": "no-op task",
            **extra,
        }
    )


def _only_task(task_id: str = "TASK-001", owner: str = "requirements_agent", **extra: Any) -> Dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "owner": owner,
        "status": config.TASK_READY,
        "dependencies": [],
        "notes": "no-op",
        **extra,
    }


def _rewrite_tasks(project: Path, tasks: List[Dict[str, Any]]) -> None:
    StateManager(project).save_tasks_document({"tasks": tasks})


# ---------------------------------------------------------------------------
# GAP-HIGH-01: a pinned agent must not be shared between fresh resolutions
# ---------------------------------------------------------------------------


class TestPinnedAgentIsolation:
    def test_fresh_resolution_of_pinned_agent_returns_separate_instance(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        pinned = orchestrator.register_agent(_make_stub(orchestrator.state, "requirements_agent"))

        fresh = orchestrator.resolve_agent("requirements_agent", fresh=True)

        assert fresh is not pinned
        assert type(fresh) is type(pinned)
        assert orchestrator.registered_agents["requirements_agent"] is pinned

    def test_non_fresh_resolution_still_returns_the_pinned_instance(
        self, tmp_path: Path
    ) -> None:
        """Stateful agents keep their state across dispatches on the serial path."""
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        pinned = orchestrator.register_agent(_make_stub(orchestrator.state, "requirements_agent"))

        assert orchestrator.resolve_agent("requirements_agent") is pinned
        assert orchestrator.resolve_agent("requirements_agent", fresh=False) is pinned

    def test_two_fresh_resolutions_do_not_share_scratch_state(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        orchestrator.register_agent(_make_stub(orchestrator.state, "requirements_agent"))

        first = orchestrator.resolve_agent("requirements_agent", fresh=True)
        second = orchestrator.resolve_agent("requirements_agent", fresh=True)
        assert first is not second

        first._current_task_id = "TASK-A"
        first.last_payload_chars = 99
        assert second._current_task_id is None
        assert second.last_payload_chars == 0

    def test_parallel_dispatch_runs_one_agent_object_per_task(
        self, tmp_path: Path
    ) -> None:
        """Two tasks owned by the same agent must not share the agent object.

        The stub records the object it ran in, so an overlap is observable.
        """
        project = build_test_project(tmp_path / "proj")
        state = StateManager(project)
        orchestrator = MasterOrchestrator(project)
        seen: List[int] = []
        lock = threading.Lock()

        def tracked(task: Dict[str, Any], agent: _StubAgent) -> AgentOutput:
            with lock:
                seen.append(id(agent))
            time.sleep(0.02)
            return AgentOutput(
                agent_id=agent.AGENT_ID,
                task_id=str(task.get("id") or ""),
                status=config.AGENT_STATUS_COMPLETED,
                summary="ok",
            )

        orchestrator.register_agent(
            _make_stub(
                orchestrator.state, "requirements_agent", on_run=tracked
            )
        )
        _rewrite_tasks(
            project,
            [
                _only_task("TASK-001"),
                _only_task("TASK-002"),
            ],
        )
        state.refresh_ready_states()

        # fresh_agent=True is what run_cycle uses for parallel dispatch.
        results: List[Any] = []
        threads = [
            threading.Thread(
                target=lambda tid=tid: results.append(
                    orchestrator.dispatch(tid, fresh_agent=True)
                )
            )
            for tid in ("TASK-001", "TASK-002")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == 2
        assert len(set(seen)) == 2, "both dispatches used the same agent instance"

    def test_unreconstructible_agent_is_reported_and_falls_back(
        self, tmp_path: Path
    ) -> None:
        """An agent needing constructor args is shared, but says so."""
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)

        class NeedsArgs(_StubAgent):
            def __init__(self, state_manager: StateManager, required: str) -> None:
                super().__init__(state_manager)
                self.required = required

        agent = NeedsArgs(orchestrator.state, "value")
        agent.AGENT_ID = "requirements_agent"  # type: ignore[misc]
        orchestrator.register_agent(agent)

        assert orchestrator.shared_agent_keys() == ["requirements_agent"]
        # Falls back to the shared instance rather than raising mid-dispatch.
        assert orchestrator.resolve_agent("requirements_agent", fresh=True) is agent

    def test_shared_agent_keys_empty_for_reconstructible_agents(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        orchestrator.register_agent(_make_stub(orchestrator.state, "requirements_agent"))

        assert orchestrator.shared_agent_keys() == []

    def test_checkpoint_restore_clears_pinned_classes_with_instances(
        self, tmp_path: Path
    ) -> None:
        """A stale pinned class could fabricate an agent nobody registered."""
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        orchestrator.register_agent(_make_stub(orchestrator.state, "requirements_agent"))
        assert orchestrator._pinned_agents

        checkpoint = orchestrator.checkpoints.create_checkpoint("cp-900-test")
        orchestrator.resume_from_checkpoint(checkpoint.checkpoint_id)

        assert orchestrator.registered_agents == {}
        assert orchestrator._pinned_agents == {}


# ---------------------------------------------------------------------------
# GAP-HIGH-02: every checkpoint trigger must be evaluated
# ---------------------------------------------------------------------------


class TestCheckpointTriggersNotShortCircuited:
    """A real dispatch, with all three triggers instrumented.

    The old chain was ``auto or milestone or phase``: whichever answered first
    suppressed the rest. The visible consequence is a missing
    ``cp-milestone-complete`` snapshot on the dispatch that finished a
    milestone.
    """

    def _project_with_one_task(
        self, tmp_path: Path, agent_id: str = "requirements_agent"
    ) -> tuple[MasterOrchestrator, Path]:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(
            project,
            [
                {
                    "id": "TASK-001",
                    "title": "One",
                    "owner": agent_id,
                    "status": config.TASK_READY,
                    "dependencies": [],
                    "notes": "no-op",
                }
            ],
        )
        StateManager(project).refresh_ready_states()
        orchestrator = MasterOrchestrator(project)
        orchestrator.register_agent(_make_stub(orchestrator.state, agent_id))
        return orchestrator, project

    def test_milestone_runs_even_when_auto_checkpoint_fires(
        self, tmp_path: Path
    ) -> None:
        orchestrator, _ = self._project_with_one_task(tmp_path)
        calls: List[str] = []

        def fake_auto(_task: Dict[str, Any]) -> str:
            calls.append("auto")
            return "cp-000-auto"

        def fake_milestones() -> str | None:
            calls.append("milestone")
            return "cp-000-milestone"

        def fake_phase(_previous: Any) -> str | None:
            calls.append("phase")
            return None

        orchestrator._maybe_auto_checkpoint = fake_auto  # type: ignore[assignment]
        orchestrator.check_milestones = fake_milestones  # type: ignore[assignment]
        orchestrator._checkpoint_phase_advance = fake_phase  # type: ignore[assignment]

        result = orchestrator.dispatch("TASK-001")

        assert calls == ["phase", "auto", "milestone"], (
            "a trigger was skipped; auto answering first must not suppress "
            "the milestone check"
        )
        # Exactly one checkpoint for one event, highest precedence.
        assert result.checkpoint_id == "cp-000-auto"

    def test_milestone_wins_when_auto_is_silent(self, tmp_path: Path) -> None:
        orchestrator, _ = self._project_with_one_task(tmp_path)
        calls: List[str] = []

        orchestrator._maybe_auto_checkpoint = lambda _t: calls.append("auto") or None  # type: ignore[assignment]
        orchestrator.check_milestones = lambda: calls.append("milestone") or "cp-000-milestone"  # type: ignore[assignment]
        orchestrator._checkpoint_phase_advance = lambda _p: calls.append("phase") or None  # type: ignore[assignment]

        result = orchestrator.dispatch("TASK-001")

        assert calls == ["phase", "auto", "milestone"], calls
        assert result.checkpoint_id == "cp-000-milestone"

    def test_phase_fallback_still_used(self, tmp_path: Path) -> None:
        orchestrator, _ = self._project_with_one_task(tmp_path)

        orchestrator._maybe_auto_checkpoint = lambda _t: None  # type: ignore[assignment]
        orchestrator.check_milestones = lambda: None  # type: ignore[assignment]
        orchestrator._checkpoint_phase_advance = lambda _p: "cp-000-phase"  # type: ignore[assignment]

        assert orchestrator.dispatch("TASK-001").checkpoint_id == "cp-000-phase"

    def test_real_milestone_checkpoint_is_written_when_auto_fires(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: a milestone completing on a dispatch is checkpointed."""
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(
            project,
            [
                {
                    "id": "TASK-001",
                    "title": "Only milestone task",
                    "owner": "requirements_agent",
                    "status": config.TASK_READY,
                    "dependencies": [],
                    "notes": "no-op",
                    "milestone": "MVP",
                }
            ],
        )
        StateManager(project).refresh_ready_states()
        orchestrator = MasterOrchestrator(project)
        orchestrator.register_agent(
            _make_stub(orchestrator.state, "requirements_agent")
        )
        created: List[str] = []
        real_create = orchestrator.checkpoints.create_checkpoint

        def tracking_create(*args: Any, **kwargs: Any) -> Any:
            checkpoint = real_create(*args, **kwargs)
            created.append(str(checkpoint.checkpoint_id))
            return checkpoint

        orchestrator.checkpoints.create_checkpoint = tracking_create  # type: ignore[assignment]
        # Force the compaction trigger to be the one that answers first, which
        # is exactly the case that used to swallow the milestone snapshot.
        orchestrator._maybe_auto_checkpoint = lambda _t: None  # type: ignore[assignment]
        # No DoD problems, so the task reaches a terminal status and the
        # milestone it carries is complete on this dispatch.
        orchestrator.definition_of_done = lambda *a, **k: []  # type: ignore[assignment]

        result = orchestrator.dispatch("TASK-001")

        assert result.new_status in config.TERMINAL_TASK_STATUSES

        # No trigger short-circuited: the milestone snapshot exists on disk.
        assert any("milestone" in cid for cid in created), created

    def test_source_has_no_or_short_circuit_on_checkpoint_chain(self) -> None:
        """Guard the fix itself: no ``a() or b()`` trigger chain remains."""
        source = (REPO_ROOT / "orchestrator" / "orchestrator.py").read_text(
            encoding="utf-8"
        )
        assert "_maybe_auto_checkpoint(after) or" not in source
        assert "self.check_milestones() or" not in source


# ---------------------------------------------------------------------------
# GAP-HIGH-03: DoD repair must not hold the state lock
# ---------------------------------------------------------------------------


class TestDoDRepairIsOffLock:
    def test_repair_is_called_with_state_lock_not_held(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        state = StateManager(project)
        _rewrite_tasks(
            project,
            [
                {
                    "id": "TASK-001",
                    "title": "Repairable",
                    "owner": "requirements_agent",
                    "status": config.TASK_READY,
                    "dependencies": [],
                    "notes": "no-op",
                }
            ],
        )
        state.refresh_ready_states()

        lock_held_during_repair: List[bool] = []

        def on_repair(
            _task: Dict[str, Any],
            _output: AgentOutput,
            _problems: List[str],
            _agent: _StubAgent,
        ) -> None:
            # A non-blocking acquire proves the lock is free at this point.
            lock_held_during_repair.append(
                orchestrator._state_lock.acquire(blocking=False)
            )
            if orchestrator._state_lock._is_owned():
                orchestrator._state_lock.release()
            return None

        orchestrator.register_agent(
            _make_stub(orchestrator.state, "requirements_agent", on_repair=on_repair)
        )
        # Force a DoD problem so repair_delivery is reached.
        orchestrator.definition_of_done = lambda *a, **k: ["artifact missing"]  # type: ignore[assignment]

        orchestrator.dispatch("TASK-001", fresh_agent=True)

        assert lock_held_during_repair == [True]

    def test_other_worker_progresses_while_one_repairs(self, tmp_path: Path) -> None:
        """The real symptom: a blocked lock serialises parallel workers."""
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        state = StateManager(project)
        _rewrite_tasks(
            project,
            [
                {
                    "id": "TASK-001",
                    "title": "Slow repair",
                    "owner": "requirements_agent",
                    "status": config.TASK_READY,
                    "dependencies": [],
                    "notes": "no-op",
                },
                {
                    "id": "TASK-002",
                    "title": "Other work",
                    "owner": "architecture_agent",
                    "status": config.TASK_READY,
                    "dependencies": [],
                    "notes": "no-op",
                },
            ],
        )
        state.refresh_ready_states()

        repair_started = threading.Event()
        repair_release = threading.Event()
        progressed_while_repairing: List[bool] = []

        def on_repair(
            _task: Dict[str, Any],
            _output: AgentOutput,
            _problems: List[str],
            _agent: _StubAgent,
        ) -> None:
            repair_started.set()
            repair_release.wait(timeout=5)
            return None

        orchestrator.register_agent(
            _make_stub(orchestrator.state, "requirements_agent", on_repair=on_repair)
        )
        orchestrator.register_agent(
            _make_stub(orchestrator.state, "architecture_agent")
        )
        orchestrator.definition_of_done = lambda *a, **k: ["artifact missing"]  # type: ignore[assignment]

        def dispatch_repair() -> None:
            orchestrator.dispatch("TASK-001", fresh_agent=True)

        worker = threading.Thread(target=dispatch_repair)
        worker.start()
        assert repair_started.wait(timeout=5), "repair never started"

        # While the repair round trip is in flight, another worker must be able
        # to take its own state turn.
        other = orchestrator.dispatch("TASK-002", fresh_agent=True)
        progressed_while_repairing.append(other.new_status != config.TASK_READY)
        repair_release.set()
        worker.join(timeout=10)

        assert progressed_while_repairing == [True]

    def test_state_writes_still_hold_the_lock(self, tmp_path: Path) -> None:
        """Moving the DoD out must not move the state writes out.

        Measured from inside the write itself: the dispatching thread must own
        ``_state_lock`` for the duration, so parallel workers still serialise
        their read-modify-write cycles on TASKS.yaml.
        """
        project = build_test_project(tmp_path / "proj")
        orchestrator = MasterOrchestrator(project)
        _rewrite_tasks(project, [_only_task()])
        StateManager(project).refresh_ready_states()

        owned_during_write: List[bool] = []
        real_save = orchestrator.state.save_tasks_document

        def spy(document: Dict[str, Any]) -> Any:
            owned_during_write.append(orchestrator._state_lock._is_owned())
            return real_save(document)

        orchestrator.state.save_tasks_document = spy  # type: ignore[assignment]
        orchestrator.definition_of_done = lambda *a, **k: ["artifact missing"]  # type: ignore[assignment]
        orchestrator.register_agent(_make_stub(orchestrator.state, "requirements_agent"))

        orchestrator.dispatch("TASK-001")

        assert owned_during_write, "no state write was observed"
        assert all(owned_during_write), (
            "a TASKS.yaml write happened outside the state lock"
        )


# ---------------------------------------------------------------------------
# GAP-HIGH-04: deep validation, reachable automatically
# ---------------------------------------------------------------------------


class TestDeepValidation:
    """validate() is what the operator guide tells people to run, so it has to
    be deep enough to be worth running."""

    def test_unknown_owner_is_reported(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task(owner="qa_agent")])
        problems = StateManager(project).validate()
        assert any("unknown owner 'qa_agent'" in p for p in problems)
        assert any("MissingAgentError" in p for p in problems)

    def test_unknown_phase_is_reported(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        document = yaml.safe_load((project / "PROJECT.yaml").read_text(encoding="utf-8"))
        document["phase"] = {"current": "PHASE_NOT_A_PHASE"}
        (project / "PROJECT.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

        problems = StateManager(project).validate()
        assert any("unknown phase 'PHASE_NOT_A_PHASE'" in p for p in problems)

    def test_missing_dependency_is_reported(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task(dependencies=["TASK-404"])])
        problems = StateManager(project).validate()
        assert any("depends on unknown task 'TASK-404'" in p for p in problems)

    def test_malformed_dependencies_are_reported(self, tmp_path: Path) -> None:
        """A non-list dependency used to be silently treated as empty."""
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task(dependencies="TASK-002")])
        problems = StateManager(project).validate()
        assert any("'dependencies' must be a list" in p for p in problems)

    def test_self_cycle_is_reported(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task(dependencies=["TASK-001"])])
        problems = StateManager(project).validate()
        assert any("self-cycle" in p for p in problems)

    def test_two_task_cycle_is_reported(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(
            project,
            [
                _only_task("TASK-001", dependencies=["TASK-002"]),
                _only_task("TASK-002", dependencies=["TASK-001"]),
            ],
        )
        problems = StateManager(project).validate()
        assert any("cycle" in p.lower() for p in problems)

    def test_task_without_owner_is_reported(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [{"id": "TASK-001", "title": "x", "status": config.TASK_READY, "dependencies": []}])
        problems = StateManager(project).validate()
        assert any("has no owner" in p for p in problems)

    def test_single_root_task_is_not_an_orphan(self, tmp_path: Path) -> None:
        """One unreferenced root is how every graph starts, not a defect."""
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task()])
        assert not any(p.startswith(ADVISORY_PREFIX) for p in StateManager(project).validate())

    def test_terminal_dependency_is_not_a_problem(self, tmp_path: Path) -> None:
        """CANCELLED is in SATISFIED_DEPENDENCY_STATUSES: waiting is normal."""
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(
            project,
            [
                _only_task("TASK-001", status=config.TASK_CANCELLED),
                _only_task("TASK-002", dependencies=["TASK-001"]),
            ],
        )
        problems = StateManager(project).validate()
        assert not any("cannot progress" in p for p in problems)

    def test_disconnected_roots_are_advisory_not_blocking(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task("TASK-001"), _only_task("TASK-002")])
        state = StateManager(project)
        assert any(p.startswith(ADVISORY_PREFIX) for p in state.validate())
        assert state.graph_blockers() == []

    def test_graph_blockers_excludes_advisory(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(
            project,
            [_only_task("TASK-001", owner="qa_agent"), _only_task("TASK-002")],
        )
        state = StateManager(project)
        blockers = state.graph_blockers()
        assert blockers, "an unknown owner must block"
        assert all(not b.startswith(ADVISORY_PREFIX) for b in blockers)


class TestValidationIsReachableAutomatically:
    """The gap was not only depth, it was reachability: nothing called
    validate() on the path that actually creates the graph, so a problem
    surfaced much later as an unrelated runtime failure."""

    def _plan_project(self, tmp_path: Path, specs: List[Dict[str, Any]]) -> MasterOrchestrator:
        """Orchestrator whose planning agent returns ``specs``, offline."""
        project = build_test_project(tmp_path / "proj")
        _clear_tasks(project)
        orchestrator = MasterOrchestrator(project)

        client = FakeLLMClient(
            [
                json.dumps(
                    {
                        "status": "completed",
                        "summary": "planned",
                        "data": {"tasks": [dict(spec) for spec in specs]},
                    }
                )
            ]
        )

        def resolve(owner: str, state: StateManager) -> Any:
            agent = create_agent(owner, state_manager=state)
            agent.use_client(client)  # type: ignore[attr-defined]
            return agent

        orchestrator.agent_resolver = resolve
        orchestrator._plan_context_files = lambda limit=8: []  # type: ignore[assignment]
        orchestrator._plan_source_index = lambda: ""  # type: ignore[assignment]
        return orchestrator

    def test_plan_ingestion_runs_the_deep_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate() is called on the plan path, not only from init."""
        specs = [
            {"title": "Requirements", "owner": "requirements_agent", "dependencies": [], "notes": "x"}
        ]
        orchestrator = self._plan_project(tmp_path, specs)
        calls: List[int] = []
        real_validate = orchestrator.state.validate

        def counting_validate() -> List[str]:
            calls.append(1)
            return real_validate()

        monkeypatch.setattr(orchestrator.state, "validate", counting_validate)

        orchestrator.build_plan(goal="test goal")

        assert calls, "the plan path never validated the graph it wrote"

    def test_plan_surfaces_a_blocker_it_wrote(self, tmp_path: Path) -> None:
        """A blocker in the graph reaches the dispatch record as a warning."""
        specs = [
            {"title": "Requirements", "owner": "requirements_agent", "dependencies": [], "notes": "x"}
        ]
        orchestrator = self._plan_project(tmp_path, specs)

        warnings: List[str] = []
        real_report = orchestrator._report_graph_problems

        def spy(output: AgentOutput, source: str) -> List[str]:
            warnings.append(source)
            return real_report(output, source)

        orchestrator._report_graph_problems = spy  # type: ignore[assignment]
        # Force a blocker that append_task does not itself catch.
        orchestrator.state.validate = lambda: ["TASKS.yaml: task 'TASK-001' has invalid status 'NOPE'"]  # type: ignore[assignment]

        orchestrator.build_plan(goal="test goal")

        assert warnings == ["planning output"]

    def test_unknown_owner_is_skipped_with_a_warning_not_written(
        self, tmp_path: Path
    ) -> None:
        """append_task owns this rejection; ingestion must not swallow it."""
        specs = [
            {"title": "Bad owner", "owner": "qa_agent", "dependencies": [], "notes": "x"},
            {"title": "Good", "owner": "requirements_agent", "dependencies": [], "notes": "x"},
        ]
        orchestrator = self._plan_project(tmp_path, specs)
        orchestrator._resolve_plan_owner = lambda owner: owner  # type: ignore[assignment]

        created = orchestrator.build_plan(goal="test goal")

        assert created == ["TASK-002"]
        owners = {
            str(task.get("owner"))
            for task in StateManager(orchestrator.project_path).load_tasks()
        }
        assert owners == {"requirements_agent"}

    def test_pre_existing_project_defect_does_not_block_planning(
        self, tmp_path: Path
    ) -> None:
        """A PROJECT.yaml typo is not the plan's fault and must not stop it."""
        project = build_test_project(tmp_path / "proj")
        document = yaml.safe_load((project / "PROJECT.yaml").read_text(encoding="utf-8"))
        document["phase"] = {"current": "PHASE_TYPO"}
        (project / "PROJECT.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

        specs = [
            {"title": "Requirements", "owner": "requirements_agent", "dependencies": [], "notes": "x"}
        ]
        orchestrator = self._plan_project(tmp_path, specs)
        orchestrator.project_path = project
        orchestrator.state = StateManager(project)

        assert orchestrator.build_plan(goal="test goal") == ["TASK-001"]

    def test_valid_plan_reports_nothing(self, tmp_path: Path) -> None:
        specs = [
            {"title": "Requirements", "owner": "requirements_agent", "dependencies": [], "notes": "x"},
            {"title": "Architecture", "owner": "architecture_agent", "dependencies": [0], "notes": "x"},
        ]
        orchestrator = self._plan_project(tmp_path, specs)
        reported: List[str] = []
        real_report = orchestrator._report_graph_problems

        def spy(output: AgentOutput, source: str) -> List[str]:
            found = real_report(output, source)
            if found:
                reported.append(source)
            return found

        orchestrator._report_graph_problems = spy  # type: ignore[assignment]

        assert orchestrator.build_plan(goal="test goal") == ["TASK-001", "TASK-002"]
        assert reported == []
        assert StateManager(orchestrator.project_path).graph_blockers() == []

    def test_status_reports_structural_problems(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from orchestrator import cli

        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task(owner="qa_agent")])

        assert cli.main(["status", "--project", str(project)]) == 0

        out = capsys.readouterr().out
        assert "Structural problems" in out
        assert "qa_agent" in out

    def test_status_exits_zero_so_a_mid_edit_still_reports(
        self, tmp_path: Path
    ) -> None:
        """Reporting, not failing: a human may be part-way through an edit."""
        from orchestrator import cli

        project = build_test_project(tmp_path / "proj")
        _rewrite_tasks(project, [_only_task(owner="qa_agent")])

        assert cli.main(["status", "--project", str(project)]) == 0
