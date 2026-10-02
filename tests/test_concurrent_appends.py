"""GAP-CRIT-06 — concurrent append and id allocation must not lose writes.

The vulnerability
-----------------
Three read-modify-write cycles had no cross-process lock, so a second writer
silently discarded the first one's work:

* ``DEC-NNN`` allocation — two processes both read a file with no ``DEC-004``,
  both allocated ``DEC-004``, and the second ``os.replace`` dropped the first
  decision entirely.
* ``RISK-NNN`` allocation — same shape, so a failing task's risk could vanish.
* ``checkpoints/index.json`` — a lost entry is worse than a lost line: the
  orchestrator keeps a dedupe set of index ids to decide whether a milestone has
  already been checkpointed, so a dropped record makes it re-create an existing
  ``cp-milestone-*`` directory, or conclude the graph is never complete.

``CURRENT_STATE.md`` and ``CHANGELOG.md`` had the same read-concatenate-rewrite
shape, which is a lost-update bug for an append-only log by construction, and
also O(n²) in bytes written.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import state_manager as sm  # noqa: E402
from orchestrator.checkpoint_manager import CheckpointManager  # noqa: E402
from orchestrator.state_manager import StateError, StateManager  # noqa: E402


def _appender(project: Path, count: int, kind: str) -> str:
    """Build a subprocess body that appends ``count`` entries concurrently."""
    return (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from orchestrator.state_manager import StateManager\n"
        "state = StateManager(Path(%r))\n"
        "for i in range(%d):\n"
        "    if %r == 'decision':\n"
        "        state.append_decision(title='proc ' + str(i), reason='r')\n"
        "    elif %r == 'risk':\n"
        "        state.append_risk(title='proc ' + str(i))\n"
        "    elif %r == 'changelog':\n"
        "        state.append_changelog('proc entry ' + str(i))\n"
        "    else:\n"
        "        state.append_current_state('proc ' + str(i))\n"
    ) % (str(REPO_ROOT), str(project), count, kind, kind, kind)


def _run_processes(project: Path, count: int, kind: str, processes: int) -> None:
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _appender(project, count, kind)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for _ in range(processes)
    ]
    for child in children:
        _, err = child.communicate(timeout=180)
        assert child.returncode == 0, f"appender failed: {err.decode()[:400]}"


# ---------------------------------------------------------------------------
# Decision id allocation
# ---------------------------------------------------------------------------


class TestDecisionIdAllocation:
    def test_ids_are_unique_across_threads(self, tmp_path: Path) -> None:
        _ = StateManager(build_test_project(tmp_path / "p"))  # baseline state
        allocated: List[str] = []
        guard = threading.Lock()

        def worker(index: int) -> None:
            local = StateManager(tmp_path / "p")
            for iteration in range(6):
                entry = local.append_decision(title=f"t{index}-{iteration}")
                with guard:
                    allocated.append(entry["id"])

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(allocated) == len(set(allocated)), f"duplicate ids: {allocated}"
        assert len(allocated) == 36

    def test_ids_are_unique_across_processes(self, tmp_path: Path) -> None:
        """The real exposure: no shared lock object exists between processes."""
        project = build_test_project(tmp_path / "p")
        _run_processes(project, count=8, kind="decision", processes=5)
        content = (project / "DECISIONS.md").read_text()
        assert content.count("**Date:**") == 40, (
            f"expected 40 decisions, found {content.count('**Date:**')}"
        )

    def test_every_allocated_id_appears_in_the_document(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        _run_processes(project, count=6, kind="decision", processes=4)
        content = (project / "DECISIONS.md").read_text()
        import re

        ids = re.findall(r"^## (DEC-\d+):", content, flags=re.MULTILINE)
        assert len(ids) == len(set(ids)), f"duplicate DEC headers: {ids}"
        assert len(ids) == 24

    def test_decisions_remain_parseable_after_concurrent_writes(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        _run_processes(project, count=5, kind="decision", processes=4)
        state = StateManager(project)
        entries = state._parse_decisions((project / "DECISIONS.md").read_text())
        # 20 appended + the 1 table-format DEC-001 from the scaffold.
        assert len(entries) == 21
        for entry in entries:
            assert entry.get("status"), f"entry {entry.get('id')} lost its Status field"
        ids = [entry["id"] for entry in entries]
        assert len(ids) == len(set(ids)), f"duplicate ids after concurrent appends: {ids}"

    def test_id_matches_the_rendered_section(self, tmp_path: Path) -> None:
        """The id and the heading can never disagree now the body is built inside the lock."""
        state = StateManager(build_test_project(tmp_path / "p"))
        entry = state.append_decision(title="aligned", status="PROPOSED_CHANGE")
        content = state.load_decisions()
        assert f"## {entry['id']}: aligned" in content
        parsed = state._parse_decisions(content)
        assert any(item["id"] == entry["id"] for item in parsed)

    def test_resolve_decision_still_works(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        entry = state.append_decision(title="gate me")
        state.resolve_decision(entry["id"], approved=True)
        parsed = state._parse_decisions(state.load_decisions())
        target = next(item for item in parsed if item["id"] == entry["id"])
        assert target["status"] == "APPROVED"

    def test_resolve_rejects_an_unknown_id(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        with pytest.raises(StateError, match="No decision"):
            state.resolve_decision("DEC-999", approved=True)


# ---------------------------------------------------------------------------
# Risk id allocation
# ---------------------------------------------------------------------------


class TestRiskIdAllocation:
    def test_risk_ids_are_unique_across_processes(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        _run_processes(project, count=6, kind="risk", processes=4)
        import re

        content = (project / "RISKS.md").read_text()
        ids = re.findall(r"^### (RISK-\d+):", content, flags=re.MULTILINE)
        assert len(ids) == len(set(ids)), f"duplicate RISK headers: {ids}"
        assert len(ids) == 24

    def test_risks_are_unique_across_threads(self, tmp_path: Path) -> None:
        _ = StateManager(build_test_project(tmp_path / "p"))  # baseline state
        allocated: List[str] = []
        guard = threading.Lock()

        def worker(index: int) -> None:
            local = StateManager(tmp_path / "p")
            for iteration in range(5):
                entry = local.append_risk(title=f"r{index}-{iteration}")
                with guard:
                    allocated.append(entry["id"])

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(allocated) == len(set(allocated))
        assert len(allocated) == 25

    def test_update_risk_status_still_works(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        entry = state.append_risk(title="mitigatable")
        state.update_risk_status(entry["id"], "MITIGATED")
        parsed = state._parse_risks(state.load_risks())
        target = next(item for item in parsed if item["id"] == entry["id"])
        assert target["status"] == "MITIGATED"

    def test_update_risk_status_rejects_unknown(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        with pytest.raises(StateError, match="No risk"):
            state.update_risk_status("RISK-999", "CLOSED")


# ---------------------------------------------------------------------------
# Append-only logs
# ---------------------------------------------------------------------------


class TestAppendOnlyLogs:
    def test_changelog_keeps_every_entry_across_processes(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        _run_processes(project, count=8, kind="changelog", processes=5)
        content = (project / "CHANGELOG.md").read_text()
        assert content.count("## ") >= 40, (
            f"lost changelog entries: found {content.count('## ')}"
        )

    def test_current_state_keeps_every_entry_across_processes(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        _run_processes(project, count=8, kind="current", processes=5)
        content = (project / "CURRENT_STATE.md").read_text()
        assert content.count("**Update (") == 40

    def test_changelog_entries_are_not_interleaved(self, tmp_path: Path) -> None:
        """A single O_APPEND write is atomic, so no block can be torn.

        The scaffold's own ``## <stamp> — initialized`` heading is skipped; the
        assertion is about the appender-written entries.
        """
        project = build_test_project(tmp_path / "p")
        _run_processes(project, count=10, kind="changelog", processes=6)
        for line in (project / "CHANGELOG.md").read_text().splitlines():
            if line.startswith("## ") and "initialized" not in line:
                assert "— proc entry" in line, f"torn changelog line: {line!r}"

    def test_entries_survive_a_missing_trailing_newline(self, tmp_path: Path) -> None:
        """The old code normalised this; the appender must still do it."""
        project = build_test_project(tmp_path / "p")
        (project / "CHANGELOG.md").write_text("# CHANGELOG — x\n\nno trailing newline")
        StateManager(project).append_changelog("first")
        text = (project / "CHANGELOG.md").read_text()
        assert "no trailing newline\n## " in text
        assert text.endswith("\n")

    def test_append_does_not_rewrite_history(self, tmp_path: Path) -> None:
        """Appends must not re-serialise the whole file: that was the O(n²)."""
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        state.append_changelog("baseline")
        first_size = (project / "CHANGELOG.md").stat().st_size
        for index in range(5):
            state.append_changelog(f"entry {index}")
        growth = (project / "CHANGELOG.md").stat().st_size - first_size
        # Five entries cannot plausibly rewrite a multi-KB file many times over.
        assert growth < first_size * 3, (
            f"grew by {growth}B on a {first_size}B file — not an append"
        )

    def test_append_changelog_returns_the_full_document(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        returned = state.append_changelog("returned")
        assert "returned" in returned
        assert returned == state.load_changelog()

    def test_append_current_state_returns_the_full_document(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        returned = state.append_current_state("returned")
        assert "returned" in returned
        assert returned == state.load_current_state()

    def test_memory_section_still_appends(self, tmp_path: Path) -> None:
        state = StateManager(build_test_project(tmp_path / "p"))
        before = state.load_memory()
        after = state.append_memory_section("Checkpoint", "body text")
        assert after.startswith(before.rstrip("\n")[:40]) or "body text" in after
        assert "## Checkpoint" in after
        assert "body text" in after

    def test_append_changelog_works_on_a_missing_file(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        (project / "CHANGELOG.md").unlink()
        state = StateManager(project)
        assert "fresh" in state.append_changelog("fresh")


# ---------------------------------------------------------------------------
# Checkpoint index
# ---------------------------------------------------------------------------


class TestCheckpointIndexLocking:
    def test_concurrent_saves_keep_every_entry(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        errors: List[str] = []
        guard = threading.Lock()

        def worker(index: int) -> None:
            local = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
            try:
                local.create_checkpoint(f"cp-t{index}", notes=f"t{index}")
            except Exception as exc:  # noqa: BLE001 - reported below
                with guard:
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, f"concurrent saves failed: {errors}"
        ids = {entry["id"] for entry in manager.list_checkpoints()}
        assert ids == {f"cp-t{index}" for index in range(6)}, f"lost entries: {ids}"

    def test_index_stays_valid_json_under_concurrent_processes(
        self, tmp_path: Path
    ) -> None:
        project = build_test_project(tmp_path / "p")
        checkpoints = tmp_path / "ck"
        CheckpointManager(project, checkpoints_root=checkpoints).create_checkpoint("cp-seed")
        body = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from orchestrator.checkpoint_manager import CheckpointManager\n"
            "manager = CheckpointManager(Path(%r), checkpoints_root=Path(%r))\n"
            "for i in range(4):\n"
            "    manager.create_checkpoint('cp-p' + str(i) + '-' + sys.argv[1], notes='n')\n"
        ) % (str(REPO_ROOT), str(project), str(checkpoints))
        children = [
            subprocess.Popen(
                [sys.executable, "-c", body, str(index)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            for index in range(4)
        ]
        for child in children:
            _, err = child.communicate(timeout=180)
            assert child.returncode == 0, err.decode()[:300]
        raw = (checkpoints / "index.json").read_text()
        data = json.loads(raw)  # must parse
        ids = {entry["id"] for entry in data["checkpoints"]}
        expected = {"cp-seed"} | {f"cp-p{index}-{i}" for index in range(4) for i in range(4)}
        assert ids == expected, f"lost index entries: {expected - ids}"

    def test_next_checkpoint_id_is_unique_under_threads(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        _ = CheckpointManager(project, checkpoints_root=tmp_path / "ck")  # creates ck root
        allocated: List[str] = []
        guard = threading.Lock()

        def worker() -> None:
            local = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
            for _ in range(6):
                value = local._next_checkpoint_id("cp-auto")
                with guard:
                    allocated.append(value)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(allocated) == len(set(allocated)), f"duplicate ids: {allocated}"

    def test_next_checkpoint_id_starts_at_one(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        assert manager._next_checkpoint_id("cp-auto") == "cp-auto-001"
        assert manager._next_checkpoint_id("cp-auto") == "cp-auto-002"

    def test_next_checkpoint_id_skips_a_deleted_high_water_mark(
        self, tmp_path: Path
    ) -> None:
        """Deleting an entry must not hand out an id whose directory still exists."""
        project = build_test_project(tmp_path / "p")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        for index in (1, 2, 3):
            manager.create_checkpoint(f"cp-auto-{index:03d}")
        manager.delete_checkpoint("cp-auto-002")
        value = manager._next_checkpoint_id("cp-auto")
        assert value == "cp-auto-004", f"reused a live id: {value}"

    def test_milestone_dedupe_survives_a_lost_entry(self, tmp_path: Path) -> None:
        """The concrete harm of losing an index record, pinned as a regression."""
        project = build_test_project(tmp_path / "p")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        manager.create_checkpoint("cp-milestone-complete", notes="m")
        listed = {entry["id"] for entry in manager.list_checkpoints()}
        assert "cp-milestone-complete" in listed
        assert manager.has_checkpoint("cp-milestone-complete") is True

    def test_delete_removes_the_entry(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        manager = CheckpointManager(project, checkpoints_root=tmp_path / "ck")
        manager.create_checkpoint("cp-gone")
        manager.delete_checkpoint("cp-gone")
        assert manager.has_checkpoint("cp-gone") is False
        assert not (tmp_path / "ck" / "cp-gone").exists()

    def test_compaction_allocates_a_fresh_id(self, tmp_path: Path) -> None:
        """compact_context must not collide with an existing auto checkpoint."""
        from orchestrator.orchestrator import MasterOrchestrator

        project = build_test_project(tmp_path / "p")
        orchestrator = MasterOrchestrator(project, checkpoints_root=tmp_path / "ck")
        first = orchestrator.compact_context("test one")
        second = orchestrator.compact_context("test two")
        assert first != second
        assert first.startswith("cp-auto-") and second.startswith("cp-auto-")
        ids = {entry["id"] for entry in orchestrator.checkpoints.list_checkpoints()}
        assert {first, second} <= ids


# ---------------------------------------------------------------------------
# Lock mechanics
# ---------------------------------------------------------------------------


class TestLockMechanics:
    def test_lock_is_reentrant_within_one_thread(self, tmp_path: Path) -> None:
        """Regression: a non-reentrant lock deadlocked append_decision.

        The critical section calls the write path, which takes the same
        document's lock again. That must be free, not a 10-second spin.
        """
        path = tmp_path / "doc.md"
        start = time.monotonic()
        with sm._document_lock(path):
            with sm._document_lock(path):
                # The real re-entrant shape: the write path locking the same
                # document while the outer critical section still holds it.
                sm.atomic_write_text(path, "nested\n")
        assert time.monotonic() - start < 2.0, "nested lock acquisition is blocking"
        assert path.read_text() == "nested\n"

    def test_append_decision_is_not_slow(self, tmp_path: Path) -> None:
        """A 20-second append meant the lock was deadlocking, not contending."""
        state = StateManager(build_test_project(tmp_path / "p"))
        start = time.monotonic()
        state.append_decision(title="timed", reason="r")
        assert time.monotonic() - start < 2.0

    def test_lock_file_lives_beside_the_target(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.md"
        with sm._file_lock(path):
            assert (tmp_path / ".doc.md.lock").exists()
            assert not path.exists()

    def test_lock_serialises_two_threads(self, tmp_path: Path) -> None:
        path = tmp_path / "counter"
        path.write_text("0", encoding="utf-8")
        order: List[str] = []

        def worker(name: str) -> None:
            with sm._file_lock(path):
                current = int(path.read_text())
                time.sleep(0.02)
                path.write_text(str(current + 1), encoding="utf-8")
                order.append(name)

        threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert path.read_text() == "4", "critical sections overlapped"

    def test_no_temp_files_left_by_appends(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "p")
        state = StateManager(project)
        state.append_changelog("a")
        state.append_current_state("b")
        state.append_decision(title="c")
        leftovers = [p.name for p in project.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_lock_state_is_shared_per_path(self, tmp_path: Path) -> None:
        first = sm._lock_state(tmp_path / "same.md")
        second = sm._lock_state(tmp_path / "same.md")
        assert first is second
        other = sm._lock_state(tmp_path / "other.md")
        assert other is not first
