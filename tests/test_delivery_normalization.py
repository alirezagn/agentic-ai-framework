"""Delivery-shape normalization: the fix for silent fake-DONE tasks.

Regression suite for the ``sys-usage`` incident (2026-10-03): the model
delivered the real file body under a literal dotted key —
``data = {"data.documents": {"src/formatter.py": "..."}}`` — which no
reader looked up. Materialization therefore rendered only the ``docs/``
wrapper, the delivery channel read as empty, and ``delivery_problems``
(the "a docs/ mirror alone is not a delivery" check) was gated on *seeing*
a channel, so it stayed silent and the task was marked DONE with the
expected output missing. The next task then failed on the import of that
never-written file, stalling the project in ``HUMAN_DECISION_REQUIRED``
and forcing hand-edits of the generated task graph.

Covered here:

* :meth:`AgentOutput.from_dict` folds dotted ``data.*`` keys (inside
  ``data`` and at the payload top level) into the proper channels;
* list-form ``documents``/``edits`` (``[{path, content}]``) fold into the
  keyed-dict shape the contract shows;
* a dotted-key delivery materializes the REAL file, not just the wrapper;
* with a visible channel, a missing real expected output fails the check;
  report artifacts with no delivered content keep the legacy mirror-only
  acceptance (unchanged by design);
* dotted ``data.acceptance_results`` still gate the Definition of Done;
* the snapshot-less legacy mirror-only acceptance is intentionally
  unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from orchestrator import config
from orchestrator.agents.base_agent import (
    AgentOutput,
    delivery_problems,
    normalize_delivery_data,
)
from orchestrator.agents.requirements_agent import RequirementsAgent
from orchestrator.orchestrator import MasterOrchestrator

# ---------------------------------------------------------------------------
# Shape normalization
# ---------------------------------------------------------------------------


class TestNormalizeDeliveryData:
    def test_dotted_keys_inside_data_fold_to_channels(self) -> None:
        out = normalize_delivery_data(
            {
                "data.documents": {"src/x.py": "print(1)\n"},
                "data.acceptance_results": [{"name": "runs", "status": "FAIL"}],
                "truncated": True,
            }
        )
        assert out["documents"] == {"src/x.py": "print(1)\n"}
        assert out["acceptance_results"] == [{"name": "runs", "status": "FAIL"}]
        assert out["truncated"] is True
        assert "data.documents" not in out

    def test_repeated_prefix_is_stripped(self) -> None:
        out = normalize_delivery_data({"data.data.documents": {"a.txt": "x"}})
        assert out["documents"] == {"a.txt": "x"}

    def test_proper_key_wins_over_dotted_duplicate(self) -> None:
        out = normalize_delivery_data(
            {
                "documents": {"k.txt": "proper"},
                "data.documents": {"k.txt": "dotted", "fill.txt": "extra"},
            }
        )
        assert out["documents"] == {"k.txt": "proper", "fill.txt": "extra"}

    def test_documents_list_records_fold_to_dict(self) -> None:
        out = normalize_delivery_data(
            {
                "documents": [
                    {"path": "src/y.py", "content": "y = 1\n"},
                    {"file": "docs/z.md", "content": "# z\n"},
                    {"no_path": "ignored"},
                    "not-a-record",
                ]
            }
        )
        assert out["documents"] == {"src/y.py": "y = 1\n", "docs/z.md": "# z\n"}

    def test_edits_list_records_fold_to_dict(self) -> None:
        out = normalize_delivery_data(
            {
                "edits": [
                    {"path": "f.txt", "search": "a", "replace": "b"},
                    {"path": "f.txt", "search": "c", "replace": "d"},
                    {"path": "g.txt", "steps": [{"search": "x", "replace": "y"}]},
                ]
            }
        )
        assert out["edits"]["f.txt"] == [
            {"search": "a", "replace": "b"},
            {"search": "c", "replace": "d"},
        ]
        # A single-step bundle collapses to the plain dict form (both are
        # accepted by _apply_edits).
        assert out["edits"]["g.txt"] == {"search": "x", "replace": "y"}


class TestFromDictFolding:
    def test_dotted_keys_inside_data(self) -> None:
        output = AgentOutput.from_dict(
            {
                "agent_id": "software_agent",
                "task_id": "TASK-004",
                "status": "completed",
                "summary": "created module",
                "data": {
                    "data.documents": {"src/formatter.py": "def f(): ...\n"},
                    "data.acceptance_results": [{"name": "ok", "status": "PASS"}],
                },
            }
        )
        assert output.data["documents"] == {"src/formatter.py": "def f(): ...\n"}
        assert output.data["acceptance_results"] == [{"name": "ok", "status": "PASS"}]
        assert not any(key.startswith("data.") for key in output.data)

    def test_flat_payload_level_dotted_keys(self) -> None:
        # No "data" wrapper at all: the model put data.documents at the
        # top level. Previously those keys were dropped entirely.
        output = AgentOutput.from_dict(
            {
                "status": "completed",
                "summary": "wrote the file",
                "data.documents": {"a.txt": "hello\n"},
            }
        )
        assert output.data["documents"] == {"a.txt": "hello\n"}

    def test_list_form_channels(self) -> None:
        output = AgentOutput.from_dict(
            {
                "status": "completed",
                "summary": "list delivery",
                "data": {
                    "documents": [{"path": "src/new.py", "content": "x = 2\n"}],
                    "edits": [{"path": "old.py", "search": "a", "replace": "b"}],
                },
            }
        )
        assert output.data["documents"] == {"src/new.py": "x = 2\n"}
        assert output.data["edits"] == {"old.py": {"search": "a", "replace": "b"}}

    def test_plain_outputs_unchanged(self) -> None:
        output = AgentOutput.from_dict(
            {
                "status": "completed",
                "summary": "fine",
                "data": {"documents": {"b.txt": "body\n"}, "usage": {"model": "m"}},
            }
        )
        assert output.data == {
            "documents": {"b.txt": "body\n"},
            "usage": {"model": "m"},
        }


# ---------------------------------------------------------------------------
# End-to-end materialization + delivery checks
# ---------------------------------------------------------------------------


class TestDottedDeliveryMaterializes:
    def test_dotted_delivery_writes_real_file_and_passes(
        self, test_project: Path
    ) -> None:
        """The incident, replayed: dotted content must still land on disk."""
        agent = RequirementsAgent(project_path=test_project)
        agent._delivery_snapshot = set()
        task = {"id": "TASK-002", "expected_outputs": ["src/formatter.py"]}
        output = AgentOutput.from_dict(
            {
                "agent_id": "software_agent",
                "task_id": "TASK-002",
                "status": "completed",
                "summary": "implemented formatter",
                "data": {
                    "data.documents": {
                        "src/formatter.py": "def format_metrics():\n    return ''\n"
                    }
                },
            }
        )
        agent._materialize_artifacts(task, output)
        real = test_project / "src" / "formatter.py"
        mirror = test_project / "docs" / "formatter.py"
        assert real.is_file(), "dotted-key delivery must write the real path"
        assert "format_metrics" in real.read_text(encoding="utf-8")
        assert mirror.is_file()
        problems = delivery_problems(
            test_project, task, output, preexisting=set()
        )
        assert problems == []

    def test_missing_real_file_fails_when_channel_visible(
        self, test_project: Path
    ) -> None:
        """A visible channel that never wrote the real path is rejected.

        (Key-shape normalization makes dotted/list deliveries visible, so
        the incident's content no longer hides from this check.)
        """
        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "launcher.py").write_text(
            "# launcher.py\n\n- Task: TASK-005\n\n## Summary\n\nclaimed\n",
            encoding="utf-8",
        )
        agent = RequirementsAgent(project_path=test_project)
        task = {"id": "TASK-005", "expected_outputs": ["launcher.py"]}
        output = agent.completed(
            "TASK-005",
            "claimed delivery",
            data={"documents": {"launcher.py": "def main(): ...\n"}},
        )
        problems = delivery_problems(test_project, task, output, preexisting=set())
        assert any("missing from the project" in item for item in problems)

    def test_report_artifact_keeps_legacy_mirror_only_acceptance(
        self, test_project: Path
    ) -> None:
        """No delivered content at all = rendered report artifact, by design.

        Report-style tasks (requirements/review summaries) intentionally
        deliver their ``docs/`` wrapper; the snapshot check only turns
        strict when a delivery channel actually claims the file.
        """
        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "launcher.py").write_text(
            "# launcher.py\n\n- Task: TASK-005\n\n## Summary\n\nclaimed\n",
            encoding="utf-8",
        )
        agent = RequirementsAgent(project_path=test_project)
        task = {"id": "TASK-005", "expected_outputs": ["launcher.py"]}
        output = agent.completed("TASK-005", "claimed delivery", data={})
        assert delivery_problems(test_project, task, output, preexisting=set()) == []

    def test_snapshotless_legacy_mirror_only_unchanged(
        self, test_project: Path
    ) -> None:
        """preexisting=None keeps the documented legacy acceptance."""
        docs = test_project / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "new_module.c").write_text("int m(void) { return 0; }\n", encoding="utf-8")
        agent = RequirementsAgent(project_path=test_project)
        task = {"id": "TASK-002", "expected_outputs": ["src/new_module.c"]}
        output = agent.completed("TASK-002", "authored", data={})
        assert delivery_problems(test_project, task, output, preexisting=None) == []


# ---------------------------------------------------------------------------
# Definition of Done still gates on folded acceptance results
# ---------------------------------------------------------------------------


class TestDottedAcceptanceResults:
    def test_dotted_fail_result_blocks_dod(
        self, test_project: Path, checkpoints_root: Path
    ) -> None:
        orchestrator = MasterOrchestrator(
            test_project, checkpoints_root=checkpoints_root, auto_checkpoint=False
        )
        task = {"acceptance_criteria": ["tool runs"], "expected_outputs": []}
        output = AgentOutput.from_dict(
            {
                "agent_id": "test_agent",
                "task_id": "TASK-002",
                "status": "completed",
                "summary": "claims pass",
                "data": {
                    "data.acceptance_results": [
                        {"name": "tool runs", "status": "FAIL", "detail": "crashed"}
                    ]
                },
            }
        )
        problems = orchestrator.definition_of_done(task, output, preexisting=set())
        assert any("acceptance check failed: tool runs" in item for item in problems)


# ---------------------------------------------------------------------------
# Non-expected documents land at their real path (sys-usage TASK-003)
# ---------------------------------------------------------------------------


class TestNonExpectedDocumentsLand:
    """A delivered file the plan never named is still a delivery.

    Failure this pins: TASK-003 delivered ``requirements.txt`` through
    ``data.documents`` — exactly what the dependency contract asks for — but
    ``expected_outputs`` never named it, so ``_materialize_artifacts``
    returned without writing anything and the remedy vanished silently.
    """

    def test_non_expected_document_writes_its_real_path(
        self, test_project: Path
    ) -> None:
        agent = RequirementsAgent(project_path=test_project)
        agent._delivery_snapshot = set()
        task = {"id": "TASK-003", "expected_outputs": ["src/formatter.py"]}
        output = agent.completed(
            "TASK-003",
            "delivered formatter and its declaration",
            data={
                "documents": {
                    "src/formatter.py": "def format_metrics():\n    return ''\n",
                    "requirements.txt": "psutil>=5.9\n",
                }
            },
        )
        agent._materialize_artifacts(task, output)

        real = test_project / "src" / "formatter.py"
        declaration = test_project / "requirements.txt"
        assert real.is_file()
        assert declaration.is_file(), (
            "a non-expected document must land on disk — the dependency "
            "contract tells the model to deliver exactly this file"
        )
        assert "psutil>=5.9" in declaration.read_text(encoding="utf-8")
        assert not (test_project / "docs" / "requirements.txt").exists(), (
            "only expected outputs get the docs/ mirror"
        )

    def test_existing_file_is_never_clobbered(self, test_project: Path) -> None:
        keep = test_project / "requirements.txt"
        keep.write_text("original\n", encoding="utf-8")
        agent = RequirementsAgent(project_path=test_project)
        agent._delivery_snapshot = set()
        task = {"id": "TASK-003", "expected_outputs": ["docs/report.md"]}
        output = agent.completed(
            "TASK-003",
            "re-delivered the declaration",
            data={
                "documents": {
                    "docs/report.md": "# report\n",
                    "requirements.txt": "attacker>=1.0\n",
                }
            },
        )
        agent._materialize_artifacts(task, output)

        assert keep.read_text(encoding="utf-8") == "original\n", (
            "pre-existing files belong to data.edits, never to the "
            "non-expected write path"
        )

    def test_paths_escaping_the_project_are_ignored(self, test_project: Path) -> None:
        agent = RequirementsAgent(project_path=test_project)
        agent._delivery_snapshot = set()
        task = {"id": "TASK-003", "expected_outputs": ["docs/report.md"]}
        output = agent.completed(
            "TASK-003",
            "escape attempt",
            data={
                "documents": {
                    "docs/report.md": "# report\n",
                    "../evil.py": "X = 1\n",
                    "/tmp/evil_non_expected.py": "X = 1\n",
                }
            },
        )
        agent._materialize_artifacts(task, output)

        assert not (test_project.parent / "evil.py").exists()
        assert not Path("/tmp/evil_non_expected.py").exists()


# ===========================================================================
# A documents claim can never modify a file that already exists
# ===========================================================================


class TestDocumentsClaimOnExistingFileIsRejected:
    """The TASK-007 failure class (sys-usage rerun 5).

    The repair round was told "not declared in requirements.txt" and
    answered by re-delivering the whole requirements.txt through
    ``data.documents``. The materializer deliberately never clobbers an
    existing real file, so the file on disk never changed, the undeclared
    import survived the repair, and the task FAILED while the model
    believed it had delivered the declaration. The claim itself must
    become a delivery problem, so the repair note names the right channel.
    """

    @staticmethod
    def _output(data: Dict[str, Any]) -> AgentOutput:
        return AgentOutput(
            agent_id="test_agent",
            task_id="TASK-007",
            status=config.AGENT_STATUS_COMPLETED,
            summary="declared the missing dependency",
            data=data,
        )

    def test_documents_on_an_existing_file_is_rejected(
        self, test_project: Path
    ) -> None:
        (test_project / "requirements.txt").write_text(
            "psutil>=5.9\n", encoding="utf-8"
        )
        task = {"id": "TASK-007", "expected_outputs": []}
        output = self._output(
            {
                "documents": {
                    "requirements.txt": "psutil>=5.9\npytest>=8\n",
                }
            }
        )
        problems = delivery_problems(
            test_project, task, output, preexisting=set()
        )
        assert problems == [
            "requirements.txt exists in the project — update it with "
            "data.edits; data.documents never modifies a file that already "
            "exists"
        ]

    def test_edits_on_an_existing_file_stay_accepted(
        self, test_project: Path
    ) -> None:
        (test_project / "requirements.txt").write_text(
            "psutil>=5.9\n", encoding="utf-8"
        )
        task = {"id": "TASK-007", "expected_outputs": []}
        output = self._output(
            {
                "edits": {
                    "requirements.txt": {
                        "search": "psutil>=5.9",
                        "replace": "psutil>=5.9\npytest>=8",
                    }
                }
            }
        )
        problems = delivery_problems(
            test_project, task, output, preexisting=set()
        )
        assert problems == []

    def test_documents_on_a_missing_file_still_delivers(
        self, test_project: Path
    ) -> None:
        """The F4 contract must survive: a document for a file that does not
        exist yet creates it and raises no problem."""
        task = {"id": "TASK-003", "expected_outputs": []}
        output = self._output({"documents": {"requirements.txt": "pytest>=8\n"}})
        problems = delivery_problems(
            test_project, task, output, preexisting=set()
        )
        assert problems == []

    def test_docs_relative_documents_are_not_flagged(
        self, test_project: Path
    ) -> None:
        """A docs/ mirror is created by design, never "modifying" anything."""
        task = {"id": "TASK-007", "expected_outputs": []}
        output = self._output({"documents": {"docs/REPORT.md": "# report\n"}})
        problems = delivery_problems(
            test_project, task, output, preexisting=set()
        )
        assert problems == []


# ---------------------------------------------------------------------------
# Declared no-op delivery (F8 — sys-usage rerun 6, TASK-006)
# ---------------------------------------------------------------------------


class TestDeclaredNoOpDelivery:
    """An existing, already-correct expected output needs no edit (F8).

    The manifest used to order "EXISTS — update it with data.edits" on
    every attempt, which the model read as its primary job even when the
    file already satisfied the acceptance criteria; it regenerated whole
    files (giant ``search`` strings, truncation, oscillation) while the
    real ``tests/test_monitor.py`` passed pytest all along. The reply can
    now declare the no-op structurally with
    ``data.no_change_needed = ["<path>"]``: the mirror then mirrors the
    real file, the prose-wrapper shares-line check (whose job is to force
    a delivery) does not punish the declaration, and acceptance evidence
    still gates the claim at dispatch. Undeclared no-content replies keep
    the legacy G18 rejection unchanged.
    """

    def test_declared_noop_mirrors_the_real_file(
        self, test_project: Path
    ) -> None:
        tests = test_project / "tests"
        tests.mkdir()
        real = tests / "test_monitor.py"
        real.write_text(
            "def test_cpu():\n    assert True\n", encoding="utf-8"
        )
        docs = test_project / "docs"
        docs.mkdir()
        (docs / "test_monitor.py").write_text(
            "# test_monitor.py\n\n- Task: `TASK-006` — Test Suite\n",
            encoding="utf-8",
        )
        agent = RequirementsAgent(project_path=test_project)
        task = {"id": "TASK-006", "expected_outputs": ["tests/test_monitor.py"]}
        output = agent.completed(
            "TASK-006",
            "tests already pass",
            data={"no_change_needed": ["tests/test_monitor.py"]},
        )
        agent._materialize_artifacts(task, output)
        mirror = docs / "test_monitor.py"
        assert mirror.read_text(encoding="utf-8") == real.read_text(
            encoding="utf-8"
        ), "a declared no-op must sync the mirror with the real file"
        problems = delivery_problems(
            test_project, task, output, preexisting={"tests/test_monitor.py"}
        )
        assert problems == []

    def test_declared_noop_skips_the_shares_line_check(
        self, test_project: Path
    ) -> None:
        """Without a materialize pass the declaration still stands."""
        tests = test_project / "tests"
        tests.mkdir()
        (tests / "test_monitor.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8"
        )
        docs = test_project / "docs"
        docs.mkdir()
        (docs / "test_monitor.py").write_text(
            "# prose wrapper\n", encoding="utf-8"
        )
        output = AgentOutput(
            agent_id="test_agent",
            task_id="TASK-006",
            status=config.AGENT_STATUS_COMPLETED,
            summary="already satisfied",
            data={"no_change_needed": ["tests/test_monitor.py"]},
        )
        task = {"id": "TASK-006", "expected_outputs": ["tests/test_monitor.py"]}
        problems = delivery_problems(
            test_project, task, output, preexisting={"tests/test_monitor.py"}
        )
        assert problems == []

    def test_declared_noop_on_a_missing_file_is_still_rejected(
        self, test_project: Path
    ) -> None:
        """Declaring "no change" for a file that does not exist is fake."""
        docs = test_project / "docs"
        docs.mkdir()
        (docs / "monitor.py").write_text("# wrapper\n", encoding="utf-8")
        output = AgentOutput(
            agent_id="test_agent",
            task_id="TASK-004",
            status=config.AGENT_STATUS_COMPLETED,
            summary="nothing to do",
            data={"no_change_needed": ["src/monitor.py"]},
        )
        task = {"id": "TASK-004", "expected_outputs": ["src/monitor.py"]}
        problems = delivery_problems(
            test_project, task, output, preexisting=set()
        )
        assert any(
            "no_change_needed" in item and "does not exist" in item
            for item in problems
        )

    def test_undeclared_no_content_keeps_the_legacy_rejection(
        self, test_project: Path
    ) -> None:
        """G18 prose gate: no declaration, no content — still rejected."""
        tests = test_project / "tests"
        tests.mkdir()
        (tests / "test_monitor.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8"
        )
        docs = test_project / "docs"
        docs.mkdir()
        (docs / "test_monitor.py").write_text(
            "# prose wrapper\n", encoding="utf-8"
        )
        output = AgentOutput(
            agent_id="test_agent",
            task_id="TASK-006",
            status=config.AGENT_STATUS_COMPLETED,
            summary="completed with measurable output",
            data={"result": "ok"},
        )
        task = {"id": "TASK-006", "expected_outputs": ["tests/test_monitor.py"]}
        problems = delivery_problems(
            test_project, task, output, preexisting={"tests/test_monitor.py"}
        )
        assert any("shares no line" in item for item in problems)


class TestManifestAllowsANoOpDeclaration:
    """EXISTS must offer the structured no-op, not order a needless edit.

    The manifest said "EXISTS — update it with data.edits" on every
    attempt, which the model read as its primary job even when the file
    already satisfied the acceptance criteria; it then regenerated the
    whole file (giant ``search`` strings, truncation, oscillation).
    """

    def test_existing_file_manifest_states_the_declaration(
        self, test_project: Path
    ) -> None:
        (test_project / "tests").mkdir()
        (test_project / "tests" / "test_monitor.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8"
        )
        agent = RequirementsAgent(project_path=test_project)
        manifest = agent._delivery_manifest(
            {"id": "TASK-006", "expected_outputs": ["tests/test_monitor.py"]}
        )
        assert "EXISTS" in manifest
        assert "no_change_needed" in manifest
        assert "data.edits" in manifest

    def test_missing_file_manifest_still_orders_creation(
        self, test_project: Path
    ) -> None:
        agent = RequirementsAgent(project_path=test_project)
        manifest = agent._delivery_manifest(
            {"id": "TASK-003", "expected_outputs": ["requirements.txt"]}
        )
        assert "MISSING" in manifest
        assert "data.documents" in manifest
