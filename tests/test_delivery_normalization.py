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
