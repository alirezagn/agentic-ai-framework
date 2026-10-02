"""GAP-HIGH-01..04 — durable state IO, single-parse derived state, deploy contract.

Three concerns, one file because they share a failure mode: any of them can
silently corrupt or lose a state file, and a state file is the framework's only
memory.

* **GAP-HIGH-01/02** — YAML state must be serialized *before* anything touches
  the filesystem, written to a temp file, and swapped in with ``os.replace``
  under an advisory lock. A bare ``open(path, "w")`` truncates first and fails
  mid-dump, leaving unparseable YAML where the only copy of the task graph was.
* **GAP-HIGH-03** — ``recompute_derived_state`` ran after every status mutation
  and parsed ``TASKS.yaml`` four times per call. It now parses once and threads
  the list through.
* **GAP-HIGH-04** — ``DATA_DEPLOY_CONTRACT`` is appended to
  ``AUTHORING_CONTRACT`` so every agent knows how to *request* execution. The
  enforcement landed in GAP-CRIT-01; without this the channel has no callers.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator import state_manager as sm  # noqa: E402
from orchestrator.agents.base_agent import (  # noqa: E402
    DATA_DEPLOY_CONTRACT,
    BaseAgent,
)
from orchestrator.state_manager import (  # noqa: E402
    StateCorruptedError,
    StateManager,
    atomic_dump_yaml,
    atomic_write_text,
)


# ---------------------------------------------------------------------------
# GAP-HIGH-01/02 — serialize first, then atomic swap under a lock
# ---------------------------------------------------------------------------


class _Unserialisable:
    """A value ``yaml.safe_dump`` cannot represent.

    ``repr`` raises, which is the hostile case: PyYAML's own error path calls
    ``repr`` on the offending object, so the original failure surfaces as
    ``RepresenterError: <exception str() failed>`` and the real cause is lost.
    The broad handler in :func:`atomic_dump_yaml` must survive that.
    """

    def __repr__(self) -> str:
        raise RuntimeError("unserialisable")


class TestSerializeBeforeTouchingDisk:
    def test_failed_dump_leaves_the_file_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """The whole point: a serialisation error must not truncate state."""
        project = build_test_project(tmp_path / "p")
        target = project / "TASKS.yaml"
        before = target.read_bytes()
        with pytest.raises((StateCorruptedError, RuntimeError, ValueError, TypeError)):
            atomic_dump_yaml(target, {"tasks": [{"id": "TASK-001", "x": _Unserialisable()}]})
        assert target.read_bytes() == before

    def test_self_referential_payload_round_trips_without_truncation(
        self, tmp_path: Path
    ) -> None:
        """PyYAML renders a self-reference with an anchor rather than failing.

        Worth pinning because it means "reject recursion" is not a property this
        layer can have — what matters is that whatever happens, the previous
        contents are either preserved intact or wholly replaced, never truncated
        mid-document.
        """
        project = build_test_project(tmp_path / "p")
        target = project / "TASKS.yaml"
        payload: Dict[str, Any] = {"tasks": []}
        payload["self"] = payload
        try:
            atomic_dump_yaml(target, payload)
        except StateCorruptedError:
            assert yaml.safe_load(target.read_text()) is not None
            return
        reloaded = yaml.safe_load(target.read_text())
        assert reloaded["self"] is reloaded, "anchor/alias form should round-trip"

    def test_deeply_nested_payload_cannot_truncate_the_file(
        self, tmp_path: Path
    ) -> None:
        """A structure past the recursion limit must fail safely.

        ``yaml.safe_dump`` raises ``RepresenterError`` once it bottoms out, and
        the handler must leave the old file intact rather than half-written.
        """
        project = build_test_project(tmp_path / "p")
        target = project / "TASKS.yaml"
        before = target.read_bytes()
        deep: Any = {"leaf": True}
        for _ in range(4000):
            deep = {"nested": deep}
        with pytest.raises((StateCorruptedError, RecursionError)):
            atomic_dump_yaml(target, deep)
        assert target.read_bytes() == before

    def test_error_survives_a_raising_repr(self, tmp_path: Path) -> None:
        """PyYAML's own error path calls repr() and loses the real cause.

        The broad handler in atomic_dump_yaml has to cope, and the resulting
        message has to name the path so an operator knows which file to fix.
        """
        project = build_test_project(tmp_path / "p")
        target = project / "TASKS.yaml"
        before = target.read_bytes()
        with pytest.raises(StateCorruptedError) as caught:
            atomic_dump_yaml(target, {"bad": _Unserialisable()})
        assert "Cannot serialize" in str(caught.value)
        assert str(target) in str(caught.value)
        assert target.read_bytes() == before

    def test_error_names_the_exception_type(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        with pytest.raises(StateCorruptedError) as caught:
            atomic_dump_yaml(project / "TASKS.yaml", {"bad": _Unserialisable()})
        assert "RepresenterError" in str(caught.value)

    def test_save_yaml_file_is_the_same_path(self, tmp_path: Path) -> None:
        """The historical name delegates, so callers and tests need no change."""
        project = build_test_project(tmp_path / "p")
        target = project / "TASKS.yaml"
        before = target.read_bytes()
        with pytest.raises((StateCorruptedError, RuntimeError, ValueError, TypeError)):
            sm.save_yaml_file(target, {"tasks": [{"x": _Unserialisable()}]})
        assert target.read_bytes() == before


class TestAtomicSwapAndLock:
    def test_no_temp_file_survives_a_write(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        state.update_health(status="WARNING")
        leftovers = [
            path.name
            for path in project.iterdir()
            if path.name.endswith(".tmp")
        ]
        assert leftovers == []
        assert state.get_health()["status"] == "WARNING"

    def test_lock_is_a_sibling_file_not_the_target(self, tmp_path: Path) -> None:
        """Locking the target would be released by the rename that replaces it."""
        project = build_test_project(tmp_path / "p")
        StateManager(project).update_health(status="WARNING")
        assert (project / ".PROJECT.yaml.lock").exists()
        assert (project / "PROJECT.yaml").exists(), "target must survive the write"
        assert not (project / ".PROJECT.yaml").exists(), "lock must not replace target"

    def test_lock_file_is_a_real_file_not_a_directory(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        StateManager(project).update_health(status="WARNING")
        assert (project / ".PROJECT.yaml.lock").is_file()

    def test_failed_write_cleans_up_its_temp_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = build_test_project(tmp_path / "p")
        target = project / "TASKS.yaml"
        before = target.read_bytes()

        real_replace = os.replace

        def exploding_replace(src: Any, dst: Any) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(sm.os, "replace", exploding_replace)
        with pytest.raises(OSError):
            atomic_write_text(target, "broken: [")
        monkeypatch.setattr(sm.os, "replace", real_replace)
        assert target.read_bytes() == before, "original must survive a failed swap"
        assert not [p for p in project.iterdir() if p.name.endswith(".tmp")]

    def test_concurrent_threads_never_corrupt_yaml(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")

        def worker(index: int) -> None:
            state = StateManager(project)
            for iteration in range(10):
                state.append_current_state(f"thread {index} iteration {iteration}")

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # A torn write would leave unparseable YAML.
        document = yaml.safe_load((project / "TASKS.yaml").read_text())
        assert isinstance(document["tasks"], list)

    def test_concurrent_processes_never_corrupt_yaml(self, tmp_path: Path) -> None:
        """Cross-process is the real exposure: no shared lock object exists."""
        project = build_test_project(tmp_path / "p")
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from orchestrator.state_manager import StateManager\n"
            "state = StateManager(Path(%r))\n"
            "for iteration in range(8):\n"
            "    state.append_current_state('proc %%d %%d' %% (PLACEHOLDER, iteration))\n"
        )
        processes = []
        for index in range(4):
            code = (
                script
                % (str(REPO_ROOT), str(project))
            ).replace("PLACEHOLDER", str(index)).replace("%%d", "%d")
            processes.append(subprocess.Popen([sys.executable, "-c", code]))
        for process in processes:
            assert process.wait(timeout=120) == 0
        for name in ("TASKS.yaml", "PROJECT.yaml"):
            assert yaml.safe_load((project / name).read_text()) is not None

    def test_every_state_write_goes_through_the_atomic_helper(self) -> None:
        """No direct ``open(path, "w")`` may reappear in the write path."""
        source = inspect.getsource(sm)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not (
                stripped.startswith("open(") and '"w"' in stripped
            ), f"direct write reappeared in state_manager: {stripped}"


# ---------------------------------------------------------------------------
# GAP-HIGH-03 — one parse per recompute
# ---------------------------------------------------------------------------


def _graph_project(tmp_path: Path, size: int) -> Path:
    project = build_test_project(tmp_path / f"n{size}")
    document = yaml.safe_load((project / "TASKS.yaml").read_text())
    template = document["tasks"][0]
    document["tasks"] = [
        dict(template, id=f"TASK-{index + 1:03d}", dependencies=[])
        for index in range(size)
    ]
    (project / "TASKS.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    return project


class TestDerivedStateSingleParse:
    def _count_parses(self, action: Any) -> int:
        counter = [0]
        real = yaml.safe_load

        def counting(handle: Any) -> Any:
            counter[0] += 1
            return real(handle)

        yaml.safe_load = counting
        try:
            action()
        finally:
            yaml.safe_load = real
        return counter[0]

    def test_recompute_parses_tasks_yaml_once(self, tmp_path: Path) -> None:
        """Was four; now two total (TASKS.yaml + PROJECT.yaml), one each."""
        state = StateManager(_graph_project(tmp_path, 60))
        parses = self._count_parses(state.recompute_derived_state)
        assert parses == 2, f"expected one parse per file, got {parses}"

    def test_recompute_is_not_quadratic_in_task_count(self, tmp_path: Path) -> None:
        """A 4x graph must not cost ~4x the time; the scans are linear now."""
        timings: Dict[int, float] = {}
        for size in (100, 400):
            state = StateManager(_graph_project(tmp_path, size))
            start = time.perf_counter()
            state.recompute_derived_state()
            timings[size] = time.perf_counter() - start
        growth = timings[400] / max(timings[100], 1e-6)
        assert growth < 12, (
            f"400 tasks took {growth:.1f}x the time of 100 (expected near 4x, "
            "quadratic scans remain)"
        )

    def test_refresh_ready_states_parses_once_per_file(self, tmp_path: Path) -> None:
        """One TASKS.yaml parse, then one inside recompute, plus PROJECT.yaml.

        Three is the floor: refresh must read the graph, recompute must re-read
        it after the promotion was persisted (it is a separate write boundary,
        and reusing the pre-write snapshot would be wrong), and PROJECT.yaml is a
        different file. The saving is in not parsing repeatedly within each.
        """
        state = StateManager(_graph_project(tmp_path, 40))
        parses = self._count_parses(state.refresh_ready_states)
        assert parses == 3, f"expected 3 (TASKS, TASKS after write, PROJECT), got {parses}"

    def test_recompute_alone_is_two_parses(self, tmp_path: Path) -> None:
        state = StateManager(_graph_project(tmp_path, 40))
        parses = self._count_parses(state.recompute_derived_state)
        assert parses == 2, f"expected one per file, got {parses}"

    def test_get_ready_tasks_accepts_a_preloaded_list(self, tmp_path: Path) -> None:
        state = StateManager(_graph_project(tmp_path, 20))
        document = state.load_tasks_document()
        tasks = state._tasks_from_document(document)
        with_preload = self._count_parses(lambda: state.get_ready_tasks(tasks=tasks))
        assert with_preload == 0, "a supplied task list must not touch the disk"

    def test_status_breakdown_accepts_a_preloaded_list(self, tmp_path: Path) -> None:
        state = StateManager(_graph_project(tmp_path, 20))
        tasks = state.load_tasks()
        assert self._count_parses(lambda: state.status_breakdown(tasks=tasks)) == 0

    def test_status_breakdown_matches_the_loading_version(self, tmp_path: Path) -> None:
        state = StateManager(_graph_project(tmp_path, 25))
        tasks = state.load_tasks()
        assert state.status_breakdown(tasks=tasks) == state.status_breakdown()

    def test_derived_values_are_unchanged(self, tmp_path: Path) -> None:
        """Behaviour must be identical; only the I/O count changed."""
        state = StateManager(_graph_project(tmp_path, 30))
        result = state.recompute_derived_state()
        document = yaml.safe_load((state.project_path / "TASKS.yaml").read_text())
        assert result["summary"]["total_tasks"] == 30
        assert sum(result["summary"]["status_breakdown"].values()) == 30
        assert document["critical_path"]["path"] == "TASK-001"
        assert isinstance(result["next_tasks"], list)
        assert result["progress"]["implementation"] == 0

    def test_dependency_check_agrees_with_the_method_form(self, tmp_path: Path) -> None:
        state = StateManager(_graph_project(tmp_path, 20))
        tasks = state.load_tasks()
        index = sm._index_tasks(tasks)
        for task in tasks:
            assert sm._dependencies_satisfied_with(
                task, tasks, index
            ) == state.dependencies_satisfied(task, tasks)

    def test_id_coercion_keeps_numeric_ids_from_looking_blocked(
        self, tmp_path: Path
    ) -> None:
        """A YAML-parsed numeric id must still match its string dependency."""
        tasks = [
            {"id": 1, "status": config.TASK_DONE},
            {"id": 2, "status": config.TASK_TODO, "dependencies": [1]},
        ]
        index = sm._index_tasks(tasks)
        assert sm._dependencies_satisfied_with(tasks[1], tasks, index) is True

    def test_task_list_extraction_validates_shape(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        with pytest.raises(StateCorruptedError, match="must be a list"):
            state._tasks_from_document({"tasks": {"not": "a list"}})
        assert state._tasks_from_document({}) == []


# ---------------------------------------------------------------------------
# GAP-HIGH-04 — the deploy schema reaches every agent
# ---------------------------------------------------------------------------


class TestDeployContractReachesAgents:
    def test_contract_defines_the_documented_schema(self) -> None:
        for fragment in (
            "data.deploy",
            "data.deploy_results",
            "command",
            "args",
            "cwd",
            "expect",
            "executed",
            "exit_code",
            "NOT RUN",
            "allowlist",
        ):
            assert fragment in DATA_DEPLOY_CONTRACT, f"contract omits {fragment!r}"

    def test_contract_states_the_never_invent_rule(self) -> None:
        assert "NEVER report a result you did not obtain" in DATA_DEPLOY_CONTRACT

    def test_contract_warns_that_args_are_not_a_shell(self) -> None:
        assert "never a shell string" in DATA_DEPLOY_CONTRACT

    def test_contract_is_appended_to_the_authoring_contract(self) -> None:
        assert DATA_DEPLOY_CONTRACT in BaseAgent.AUTHORING_CONTRACT

    def test_every_registered_agent_receives_the_contract(self) -> None:
        from orchestrator.agents import AGENT_REGISTRY

        assert AGENT_REGISTRY
        for name, agent_class in AGENT_REGISTRY.items():
            agent = agent_class.__new__(agent_class)
            agent.extra_rules = []
            rules = agent.system_rules()
            assert "data.deploy" in rules, f"{name} misses the deploy contract"
            assert "data.deploy_results" in rules, f"{name} misses the result schema"
            assert "NOT RUN" in rules, f"{name} misses the honest-failure path"

    def test_contract_does_not_shadow_the_existing_rules(self) -> None:
        """Appending must preserve the document/edits guidance."""
        agent = BaseAgent.__new__(BaseAgent)
        agent.extra_rules = []
        rules = agent.system_rules()
        assert "data.documents" in rules
        assert "data.edits" in rules
        assert "Authoring contract" in rules
        assert "Execution contract" in rules

    def test_extra_rules_still_come_last(self, tmp_path: Path) -> None:
        agent = BaseAgent(project_path=tmp_path, extra_rules=["EXTRA-RULE-MARKER"])
        rules = agent.system_rules()
        assert rules.index("EXTRA-RULE-MARKER") > rules.index("Execution contract")

    def test_no_deploy_is_fine_for_a_plain_task(self, tmp_path: Path) -> None:
        """The contract is additive; a doc task need not use the channel."""
        from orchestrator.orchestrator import MasterOrchestrator

        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        output = orchestrator.resolve_agent("documentation_agent").completed(
            "TASK-002", "wrote the guide", data={}
        )
        assert orchestrator._run_requested_deploys("TASK-002", output) == []
        problems = orchestrator.definition_of_done(
            {
                "id": "TASK-002",
                "acceptance_criteria": ["guide written"],
                "expected_outputs": ["docs/GUIDE.md"],
                "review": {"required": False, "status": "NOT_STARTED"},
            },
            output,
            deploy_records=[],
        )
        assert not [
            problem
            for problem in problems
            if "ground-truth" in problem or "verification" in problem
        ]


# ===========================================================================
# Descriptor hygiene — GAP: exhausted file descriptors over a full run
# ===========================================================================


def _open_fd_count() -> int:
    """Descriptors held by this process (Linux only; skipped elsewhere)."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        pytest.skip("no /proc/self/fd on this platform")
    return len(list(fd_dir.iterdir()))


def _touch_project(root: Path, index: int) -> None:
    """Exercise every state write path, so each lock file gets used."""
    project = root / f"p{index}"
    project.mkdir()
    sm.save_yaml_file(project / "PROJECT.yaml", {"project": {"name": "p"}})
    manager = sm.StateManager(project)
    manager.save_tasks_document(
        {"tasks": [{"id": "T-1", "title": "t", "owner": "planning_agent",
                    "dependencies": []}]}
    )
    manager.load_project()
    manager.append_current_state("touched")


class TestLockDescriptorHygiene:
    """``_file_lock`` caches one descriptor per lock file, forever.

    Re-entrancy is why the cache exists, so it is not removed -- but a process
    that touches many distinct lock paths must be able to release it. Each test
    uses a fresh ``tmp_path``, so the suite is the worst case by construction.
    """

    def test_lock_states_do_not_grow_without_bound(self, tmp_path: Path) -> None:
        """Many projects, releasing between each: no accumulation."""
        for index in range(12):
            _touch_project(tmp_path, index)
            sm.release_file_locks()

        assert len(sm._LOCK_STATES) == 0

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        _touch_project(tmp_path, 0)
        first = sm.release_file_locks()
        second = sm.release_file_locks()

        assert first > 0
        assert second == 0
        assert len(sm._LOCK_STATES) == 0

    def test_release_closes_the_descriptors(self, tmp_path: Path) -> None:
        """Closed is the whole point -- the count must actually drop."""
        before = _open_fd_count()
        for index in range(8):
            _touch_project(tmp_path, index)

        leaked = _open_fd_count() - before
        assert leaked > 0, "expected the unlocked run to hold descriptors"

        closed = sm.release_file_locks()

        assert closed >= leaked
        assert _open_fd_count() - before < leaked

    def test_gc_cannot_do_this(self, tmp_path: Path) -> None:
        """Documents why the fixture closes handles instead of collecting.

        ``_LOCK_STATES`` references every handle, so the handles are reachable
        and a collection pass leaves them open. A regression here would mean
        someone reintroduced the leak and reverted to ``gc.collect()`` as the
        "fix".
        """
        import gc

        _touch_project(tmp_path, 0)
        before = _open_fd_count()
        _touch_project(tmp_path, 1)
        gc.collect()

        assert _open_fd_count() > before, "handles became collectable; re-check the cache"

        sm.release_file_locks()

    def test_a_held_lock_is_not_closed(self, tmp_path: Path) -> None:
        """Releasing must not drop a lock this process is still using."""
        project = tmp_path / "held"
        project.mkdir()
        sm.save_yaml_file(project / "STATE.yaml", {"a": 1})
        target = project / "STATE.yaml"

        with sm._file_lock(target):
            held = [
                state
                for state in sm._LOCK_STATES.values()
                if state.depth > 0
            ]
            assert held, "expected a held lock while inside the context"
            assert sm.release_file_locks() == 0
            assert all(
                state.handle is None or not state.handle.closed for state in held
            )

        # Once released, the descriptor goes away.
        sm.release_file_locks()
        assert not [s for s in sm._LOCK_STATES.values() if s.depth > 0]

    def test_locking_still_works_after_release(self, tmp_path: Path) -> None:
        """Releasing must not break the next acquisition."""
        project = tmp_path / "reuse"
        project.mkdir()
        target = project / "STATE.yaml"
        sm.save_yaml_file(target, {"round": 1})

        for _ in range(3):
            with sm._file_lock(target):
                pass
            sm.release_file_locks()
            sm.save_yaml_file(target, {"round": 2})

        assert target.read_text(encoding="utf-8")

        # The last save legitimately re-acquires a lock, so release once more:
        # the point is that re-acquisition works, not that the registry is empty
        # while a write is in flight.
        sm.release_file_locks()
        assert len(sm._LOCK_STATES) == 0
