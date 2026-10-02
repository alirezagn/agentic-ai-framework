"""A checkpoint name collision must never stop a dispatch.

Failure observed in a live run: `RISKS.md` was rewritten by a fresh plan
while the old `checkpoints/<project>/cp-risk-RISK-001` directory stayed on
disk. The next failing task re-allocated `RISK-001`, tried to snapshot under
`cp-risk-RISK-001`, raised ``Checkpoint 'cp-risk-RISK-001' already exists`` —
and that bookkeeping error became the task's error, so the task failed with a
message about a checkpoint instead of its own, and `run --all` stopped with a
failed task instead of finishing the graph.

Three guarantees are pinned here:

1. ``CheckpointManager.create_checkpoint`` stays strict by default (reusing
   an id by accident is still an error) but accepts ``overwrite=True``.
2. ``MasterOrchestrator.create_checkpoint`` **replaces** an existing id —
   these are bookkeeping snapshots, not user data.
3. Every auto-checkpoint in the dispatch finalize path is best-effort: an
   I/O failure while snapshotting may cost the snapshot, never the task.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import orchestrator.checkpoint_manager as checkpoint_module  # noqa: E402
from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.base_agent import AgentOutput, BaseAgent  # noqa: E402
from orchestrator.checkpoint_manager import (  # noqa: E402
    CheckpointError,
    CheckpointExistsError,
    CheckpointManager,
)
from orchestrator.orchestrator import MasterOrchestrator  # noqa: E402
from orchestrator.state_manager import StateManager  # noqa: E402


class FailingAgent(BaseAgent):
    AGENT_ID = "failing_agent"

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task_id = str(payload["task"]["id"])
        return self.failed(
            task_id, "model backend down", errors=["connection refused"]
        )


def _orchestrator(
    project: Path, checkpoints_root: Path, *, failing: bool = True
) -> MasterOrchestrator:
    agent_cls = FailingAgent if failing else None

    def resolver(owner: str, state_manager: Any) -> Optional[BaseAgent]:
        if agent_cls is None:
            return None
        return agent_cls(state_manager=state_manager)

    return MasterOrchestrator(
        project,
        checkpoints_root=checkpoints_root,
        auto_checkpoint=True,
        agent_resolver=resolver,
    )


def _manager(project: Path, checkpoints_root: Path) -> CheckpointManager:
    return CheckpointManager(project, checkpoints_root=checkpoints_root)


# ===========================================================================
# The manager stays strict unless told otherwise
# ===========================================================================


class TestManagerStrictness:
    def test_duplicate_id_still_raises_by_default(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        project.mkdir(parents=True, exist_ok=True)
        (project / "PROJECT.yaml").write_text("project:\n  name: x\n")
        manager = _manager(project, tmp_path / "ck")
        manager.create_checkpoint("cp-one")
        with pytest.raises(CheckpointExistsError):
            manager.create_checkpoint("cp-one")

    def test_overwrite_replaces_the_directory_and_keeps_one_index_row(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "p"
        project.mkdir(parents=True, exist_ok=True)
        (project / "PROJECT.yaml").write_text("project:\n  name: x\n")
        manager = _manager(project, tmp_path / "ck")

        manager.create_checkpoint("cp-risk-RISK-001", notes="first")
        second = manager.create_checkpoint(
            "cp-risk-RISK-001", notes="second", overwrite=True
        )

        assert second.checkpoint_id == "cp-risk-RISK-001"
        rows = [
            entry
            for entry in manager.list_checkpoints()
            if entry.get("id") == "cp-risk-RISK-001"
        ]
        assert len(rows) == 1
        assert rows[0].get("notes") == "second"
        assert (tmp_path / "ck" / "cp-risk-RISK-001").is_dir()

    def test_source_is_validated_before_the_old_snapshot_is_removed(
        self, tmp_path: Path
    ) -> None:
        """A failed overwrite must not destroy the previous snapshot."""
        project = tmp_path / "p"
        project.mkdir(parents=True, exist_ok=True)
        (project / "PROJECT.yaml").write_text("project:\n  name: x\n")
        manager = _manager(project, tmp_path / "ck")
        manager.create_checkpoint("cp-risk-RISK-001", notes="original")

        with pytest.raises(CheckpointError, match="Snapshot source does not exist"):
            manager.create_checkpoint(
                "cp-risk-RISK-001",
                source_path=tmp_path / "nope",
                overwrite=True,
            )

        assert (tmp_path / "ck" / "cp-risk-RISK-001").is_dir()


# ===========================================================================
# The orchestrator replaces bookkeeping ids instead of raising
# ===========================================================================


class TestOrchestratorOverwrites:
    def test_second_save_of_the_same_id_succeeds(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        orch = _orchestrator(project, tmp_path / "ck", failing=False)

        first = orch.create_checkpoint("cp-risk-RISK-001", notes="one")
        second = orch.create_checkpoint("cp-risk-RISK-001", notes="two")

        assert first == second == "cp-risk-RISK-001"
        assert (tmp_path / "ck" / "cp-risk-RISK-001").is_dir()


# ===========================================================================
# A collision never reaches the task
# ===========================================================================


class TestDispatchSurvivesCollision:
    def _strip_new_risks(self, project: Path) -> None:
        """Rewrite RISKS.md to its fixture state — a fresh plan does this,
        which is exactly how the stale checkpoint id came back."""
        (project / "RISKS.md").write_text(
            "# RISKS — Test Project\n\n"
            "| RISK-ID | Risk | Probability | Impact | Mitigation |\n"
            "|---------|------|-------------|--------|------------|\n"
            "| RISK-001 | Latency above 1s | MEDIUM | Broken UX | Use tiny model |\n",
            encoding="utf-8",
        )

    def test_reused_risk_id_does_not_fail_the_task(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        checkpoints = tmp_path / "ck"
        orch = _orchestrator(project, checkpoints)

        first = orch.run_cycle()
        assert first and not first[0].succeeded
        stale = checkpoints / "cp-risk-RISK-002"
        assert stale.is_dir(), "first failing dispatch must leave the snapshot"

        # RISKS.md loses the entry; the directory stays: id will be reused.
        self._strip_new_risks(project)
        assert not any(
            "Task TASK-002" in str(risk.get("title", ""))
            for risk in StateManager(project).list_risks()
        )

        second = orch.dispatch("TASK-002")

        assert not second.succeeded
        text = " ".join([second.output.summary, *second.output.errors])
        assert "already exists" not in text, text
        assert "connection refused" in text, text
        assert stale.is_dir(), "the risk snapshot must be refreshed, not lost"
        rows = [
            entry
            for entry in orch.checkpoints.list_checkpoints()
            if entry.get("id") == "cp-risk-RISK-002"
        ]
        assert len(rows) == 1

    def test_snapshot_io_error_is_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = build_test_project(tmp_path / "p")
        orch = _orchestrator(project, tmp_path / "ck")

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(orch.checkpoints, "create_checkpoint", boom)

        result = orch.dispatch("TASK-002")

        assert not result.succeeded
        text = " ".join([result.output.summary, *result.output.errors])
        assert "disk full" not in text, text
        assert "connection refused" in text, text
        assert result.new_status == config.TASK_FAILED


# ===========================================================================
# The index can never point at a directory that is gone
# ===========================================================================


class TestIndexNeverOutlivesItsDirectory:
    """The failure this pins actually happened on disk.

    ``checkpoints/sys_mon_full/index.json`` and ``checkpoints/sys_mon_gui/
    index.json`` each held a ``cp-risk-RISK-002`` row whose directory had been
    rmtree'd by a failed overwrite: the old ``create_checkpoint`` removed the
    old snapshot *first* and only then tried to write the new one, so any
    failure in between left the index promising a snapshot that no longer
    existed. Every reader of ``metadata.json`` (including the repository
    containment test) then died with FileNotFoundError, and
    ``has_checkpoint``/``list_checkpoints`` kept reporting the ghost.
    """

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        project = tmp_path / "p"
        project.mkdir(parents=True, exist_ok=True)
        (project / "PROJECT.yaml").write_text("project:\n  name: x\n")
        return project

    def test_empty_source_overwrite_keeps_the_previous_snapshot(
        self, tmp_path: Path
    ) -> None:
        project = self._project(tmp_path)
        manager = _manager(project, tmp_path / "ck")
        manager.create_checkpoint("cp-risk-RISK-001", notes="original")

        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(CheckpointError, match="No state files found"):
            manager.create_checkpoint(
                "cp-risk-RISK-001", source_path=empty, overwrite=True
            )

        assert (tmp_path / "ck" / "cp-risk-RISK-001").is_dir()
        assert manager.load_checkpoint("cp-risk-RISK-001").notes == "original"
        rows = [
            entry
            for entry in manager.list_checkpoints()
            if entry.get("id") == "cp-risk-RISK-001"
        ]
        assert [row.get("notes") for row in rows] == ["original"]
        assert not list((tmp_path / "ck").glob(".staging-*"))

    def test_a_failed_copy_keeps_the_old_snapshot_and_cleans_staging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._project(tmp_path)
        manager = _manager(project, tmp_path / "ck")
        manager.create_checkpoint("cp-risk-RISK-001", notes="original")

        class _NoCopy:
            """Real shutil with copy2 broken — every other call behaves."""

            def __getattr__(self, name: str) -> Any:
                return getattr(shutil, name)

            @staticmethod
            def copy2(*args: Any, **kwargs: Any) -> Any:
                raise OSError("disk full")

        monkeypatch.setattr(checkpoint_module, "shutil", _NoCopy())

        with pytest.raises(CheckpointError, match="disk full"):
            manager.create_checkpoint(
                "cp-risk-RISK-001", notes="second", overwrite=True
            )

        assert manager.load_checkpoint("cp-risk-RISK-001").notes == "original"
        rows = [
            entry
            for entry in manager.list_checkpoints()
            if entry.get("id") == "cp-risk-RISK-001"
        ]
        assert [row.get("notes") for row in rows] == ["original"]
        assert not list((tmp_path / "ck").glob(".staging-*"))

    def test_delete_clears_the_row_even_when_the_directory_is_already_gone(
        self, tmp_path: Path
    ) -> None:
        project = self._project(tmp_path)
        manager = _manager(project, tmp_path / "ck")
        manager.create_checkpoint("cp-one", notes="soon orphaned")
        shutil.rmtree(tmp_path / "ck" / "cp-one")

        manager.delete_checkpoint("cp-one")

        assert not manager.has_checkpoint("cp-one")
        assert not any(
            entry.get("id") == "cp-one" for entry in manager.list_checkpoints()
        )

    def test_a_crashed_staging_directory_is_cleaned_up(
        self, tmp_path: Path
    ) -> None:
        project = self._project(tmp_path)
        manager = _manager(project, tmp_path / "ck")
        manager.create_checkpoint("cp-one")
        leftover = tmp_path / "ck" / ".staging-cp-one-99999"
        leftover.mkdir()
        (leftover / "PROJECT.yaml").write_text("half written\n")

        second = manager.create_checkpoint("cp-one", notes="second", overwrite=True)

        assert second.checkpoint_id == "cp-one"
        assert not leftover.exists()
        assert not list((tmp_path / "ck").glob(".staging-*"))
