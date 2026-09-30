"""MasterOrchestrator dispatch and checkpoint isolation.

Split from the former ``test_orchestrator_pipeline.py`` (GAP_ANALYSIS task T20,
finding 20 — roadmap expects ``test_orchestrator.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from conftest import build_test_project
from orchestrator import config
from orchestrator.checkpoint_manager import (
    CheckpointIntegrityError,
    CheckpointManager,
    CheckpointNotFoundError,
)
from orchestrator.orchestrator import MasterOrchestrator
from orchestrator.state_manager import StateManager


class TestCheckpointIsolation:
    def test_snapshot_and_restore_into_isolated_directory(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        manager = CheckpointManager(test_project, checkpoints_root=checkpoints_root)
        checkpoint = manager.create_checkpoint("cp-001", notes="before mutation")
        assert checkpoint.checkpoint_id == "cp-001"
        assert "PROJECT.yaml" in checkpoint.files
        assert "TASKS.yaml" in checkpoint.files
        assert manager.verify_checkpoint("cp-001") is True

        original_memory = (test_project / "PROJECT_MEMORY.md").read_text(encoding="utf-8")
        state = StateManager(test_project)
        state.update_task_status("TASK-002", "FAILED", error="mutated after snapshot")
        mutated = (test_project / "TASKS.yaml").read_text(encoding="utf-8")
        assert "FAILED" in mutated

        restore_dir = test_project.parent / "restored"
        manager.restore_checkpoint("cp-001", target_path=restore_dir)

        restored_tasks = (restore_dir / "TASKS.yaml").read_text(encoding="utf-8")
        restored_memory = (restore_dir / "PROJECT_MEMORY.md").read_text(encoding="utf-8")
        assert restored_tasks != mutated
        assert "status: READY" in restored_tasks or "status: READY" in restored_tasks.replace(
            "'", ""
        )
        assert restored_memory == original_memory

        live_after = (test_project / "TASKS.yaml").read_text(encoding="utf-8")
        assert live_after == mutated, "isolated restore must not touch the live project"

    def test_index_lists_checkpoint_and_metadata_round_trips(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        manager = CheckpointManager(test_project, checkpoints_root=checkpoints_root)
        manager.create_checkpoint("cp-a", notes="first")
        manager.create_checkpoint("cp-b", notes="second")
        entries = manager.list_checkpoints()
        ids = [entry["id"] for entry in entries]
        assert ids == ["cp-a", "cp-b"]

        loaded = manager.load_checkpoint("cp-b")
        assert loaded.notes == "second"
        metadata_path = loaded.directory / config.CHECKPOINT_METADATA_FILE
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["checksum_algorithm"] == "sha256"
        assert metadata["file_count"] == len(loaded.files)

        index_path = checkpoints_root / config.CHECKPOINT_INDEX_FILE
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert [entry["id"] for entry in index["checkpoints"]] == ["cp-a", "cp-b"]

    def test_corruption_is_detected_before_restore(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        manager = CheckpointManager(test_project, checkpoints_root=checkpoints_root)
        manager.create_checkpoint("cp-guard")
        snapshot_file = manager.checkpoint_dir("cp-guard") / "PROJECT.yaml"
        snapshot_file.write_text("project:\n  name: tampered\n", encoding="utf-8")

        with pytest.raises(CheckpointIntegrityError):
            manager.verify_checkpoint("cp-guard")
        with pytest.raises(CheckpointIntegrityError):
            manager.restore_checkpoint("cp-guard")

        live = (test_project / "PROJECT.yaml").read_text(encoding="utf-8")
        assert "tampered" not in live

    def test_missing_checkpoint_raises(self, test_project: Path, checkpoints_root: Path) -> None:
        manager = CheckpointManager(test_project, checkpoints_root=checkpoints_root)
        with pytest.raises(CheckpointNotFoundError):
            manager.load_checkpoint("cp-nope")

    def test_directory_backup_copies_project(self, test_project: Path, checkpoints_root: Path) -> None:
        manager = CheckpointManager(test_project, checkpoints_root=checkpoints_root)
        backup_dir = manager.create_backup("bak-001", notes="full copy")
        assert (backup_dir / "PROJECT.yaml").exists()
        assert (backup_dir / "TASKS.yaml").exists()
        metadata = json.loads((backup_dir / "backup_metadata.json").read_text(encoding="utf-8"))
        assert metadata["id"] == "bak-001"
        assert metadata["file_count"] >= 5
        assert manager.list_backups() == ["bak-001"]

    def test_restore_back_into_live_project(self, test_project: Path, checkpoints_root: Path) -> None:
        manager = CheckpointManager(test_project, checkpoints_root=checkpoints_root)
        manager.create_checkpoint("cp-live")
        state = StateManager(test_project)
        state.update_task_status("TASK-002", "CANCELLED")
        assert StateManager(test_project).get_task("TASK-002")["status"] == "CANCELLED"
        manager.restore_checkpoint("cp-live")
        assert StateManager(test_project).get_task("TASK-002")["status"] == "READY"


# ---------------------------------------------------------------------------
# Phase C — loop fault thresholds & deadlock detection
# ---------------------------------------------------------------------------


class TestMasterOrchestrator:
    def test_run_cycle_completes_ready_task_and_promotes_dependents(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        results = orchestrator.run_cycle()
        assert len(results) == 1
        result = results[0]
        assert result.task_id == "TASK-002"
        assert result.succeeded is True
        assert result.new_status == config.TASK_DONE

        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_DONE
        orchestrator.sync_health()
        assert orchestrator.get_task("TASK-003")["status"] == config.TASK_READY
        assert orchestrator.get_task("TASK-004")["status"] == config.TASK_TODO

        current_state = orchestrator.state.load_current_state()
        assert "TASK-002" in current_state
        report = orchestrator.health()
        assert str(report.state) in (config.HEALTH_HEALTHY, config.HEALTH_WARNING)

    def test_review_gate_keeps_task_out_of_done(self, test_project: Path, checkpoints_root: Path) -> None:
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        for task in document["tasks"]:
            if task["id"] == "TASK-002":
                task["review"]["required"] = True
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        result = orchestrator.run_task("TASK-002")
        assert result.new_status == config.TASK_REVIEW
        assert orchestrator.get_task("TASK-002")["review"]["status"] == "READY"

        final = orchestrator.complete_review("TASK-002", passed=True)
        assert final == config.TASK_DONE
        assert orchestrator.get_task("TASK-002")["review"]["status"] == "PASS"

    def test_auto_checkpoint_at_compaction_threshold(
        self, tmp_path: Path, checkpoints_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The orchestrator now measures utilisation after every dispatch.
        # Shrink the context window so the measured payload crosses 70%.
        monkeypatch.setenv("ORCHESTRATOR_CONTEXT_WINDOW_TOKENS", "1000")
        project_dir = build_test_project(tmp_path / "compaction-project", context_utilization=35)
        orchestrator = MasterOrchestrator(project_dir, checkpoints_root=checkpoints_root)
        results = orchestrator.run_cycle()
        assert results and results[0].succeeded

        project = orchestrator.state.load_project()
        # Crossing 70% must fire the auto checkpoint (proves dispatch measured
        # utilisation) and, under cumulative accounting (B2), compaction then
        # resets the token budget for the next session segment.
        checkpoint_id = str(results[0].checkpoint_id or "")
        assert checkpoint_id.startswith("cp-auto-"), checkpoint_id
        assert project["context"]["utilization_percent"] < 70, (
            "compaction must reset the measured utilisation"
        )
        assert int(project["context"].get("cumulative_tokens", 0)) < 700
        assert orchestrator.state.load_changelog().count("Auto checkpoint") >= 1

        ids = [entry["id"] for entry in orchestrator.checkpoints.list_checkpoints()]
        assert any(cid.startswith("cp-auto-") for cid in ids)

        memory = orchestrator.state.load_memory()
        assert "Context Compaction" in memory
        assert project["last_checkpoint"]["id"].startswith("cp-auto-")
        changelog = orchestrator.state.load_changelog()
        assert "Auto checkpoint" in changelog

    def test_no_checkpoint_below_threshold(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=True
        )
        results = orchestrator.run_cycle()
        ids = [str(entry.get("id")) for entry in orchestrator.checkpoints.list_checkpoints()]
        # No compaction checkpoint below the threshold; the only allowed
        # checkpoint is the A7 phase-advance one.
        assert not any(item.startswith("cp-auto") for item in ids)
        assert not any(item.startswith("cp-milestone") for item in ids)
        checkpoint_id = str(results[0].checkpoint_id or "")
        assert checkpoint_id == "" or checkpoint_id.startswith("cp-phase-")

    def test_checkpoint_and_resume_round_trip(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        orchestrator.create_checkpoint("cp-milestone", notes="milestone reached")
        assert orchestrator.get_task("TASK-002")["status"] == "READY"

        orchestrator.run_cycle()
        assert orchestrator.get_task("TASK-002")["status"] == config.TASK_DONE

        orchestrator.resume_from_checkpoint("cp-milestone")
        assert orchestrator.get_task("TASK-002")["status"] == "READY"
        project = orchestrator.state.load_project()
        assert project["last_checkpoint"]["id"] == "cp-milestone"

    def test_run_until_stalled_terminates_on_idle_limit(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        document = yaml.safe_load((test_project / "TASKS.yaml").read_text(encoding="utf-8"))
        document["tasks"] = [
            task for task in document["tasks"] if task["id"] in ("TASK-001", "TASK-003")
        ]
        for task in document["tasks"]:
            if task["id"] == "TASK-003":
                task["dependencies"] = []
                task["status"] = "BLOCKED"
                task["owner"] = "requirements_agent"
                task["input_files"] = ["PROJECT_MEMORY.md"]
        (test_project / "TASKS.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        cycles = orchestrator.run_until_stalled(max_cycles=10)
        assert 1 <= len(cycles) <= config.loop_thresholds().no_progress_max_cycles

        report = orchestrator.health()
        assert report.deadlock_detected is False

    def test_status_payload_shape(self, test_project: Path, checkpoints_root: Path) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        status = orchestrator.status()
        assert status["project"]["name"] == "test-project"
        assert status["phase"] == "REQUIREMENTS"
        assert status["health"] in config.HEALTH_STATES
        assert status["tasks"]["TOTAL"] == 4
        assert status["ready"] == ["TASK-002"]
        assert status["context"]["compaction_threshold"] == 70
        assert "requirements_agent" in status["registered_agents"]


# ---------------------------------------------------------------------------
# Phase F — CLI
# ---------------------------------------------------------------------------

