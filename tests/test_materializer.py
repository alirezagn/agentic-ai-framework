"""``data.edits`` materialization — a full write when there is nothing to patch.

An agent describing a whole file naturally emits ``{"search": "", "replace":
"<entire body>"}``. That request used to fail the entire dispatch with::

    ValueError: edits['<file>'][0] needs a non-empty string 'search' and a string 'replace'

and the same held for a target that did not exist yet::

    FileNotFoundError: edits target does not exist: <file>

Both describe content the agent *had* delivered, so the failure discarded work
and reported a contract violation where the intent was clear. An edit with
nothing to patch is now a full write.

What must keep failing is stated just as explicitly in
:class:`TestRejectionsPreserved`: an ambiguous search against real content is
the error class this whole mechanism exists to catch, and it must not be
softened by the graceful path.

Layout note: the suite lives at the repository root (``conftest.py`` supplies
``build_test_project``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conftest import build_test_project  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator.agents.base_agent import AgentOutput, create_agent  # noqa: E402


def _agent(project: Path) -> Any:
    return create_agent("software_agent", project_path=project)


def _output(edits: Dict[str, Any]) -> AgentOutput:
    """A completed output carrying only ``data.edits``."""
    return AgentOutput(
        agent_id="software_agent",
        task_id="TASK-001",
        status=config.AGENT_STATUS_COMPLETED,
        summary="patch",
        data={"edits": edits},
    )


def _apply(project: Path, edits: Dict[str, Any]) -> AgentOutput:
    """Run the materializer over ``edits`` and return the mutated output."""
    output = _output(edits)
    _agent(project)._apply_edits({}, output)
    return output


# ===========================================================================
# The reported failure: search="" is a full write
# ===========================================================================


class TestEmptySearchIsAFullWrite:
    def test_search_empty_creates_a_new_file(self, tmp_path: Path) -> None:
        """``search=""`` on a file that does not exist creates it."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"notes/new_file.py": {"search": "", "replace": "x = 1\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (project / "notes" / "new_file.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_search_empty_replaces_a_new_files_full_content(self, tmp_path: Path) -> None:
        """The requirement, stated directly: full content, nothing appended."""
        project = build_test_project(tmp_path / "proj")
        body = "import os\n\ndef main():\n    return os.getcwd()\n"

        output = _apply(project, {"app.py": {"search": "", "replace": body}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (project / "app.py").read_text(encoding="utf-8") == body

    def test_search_empty_overwrites_an_empty_file(self, tmp_path: Path) -> None:
        """An existing empty file has nothing to patch, so nothing to lose."""
        project = build_test_project(tmp_path / "proj")
        target = project / "empty.py"
        target.write_text("", encoding="utf-8")

        output = _apply(project, {"empty.py": {"search": "", "replace": "y = 2\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "y = 2\n"

    def test_search_empty_overwrites_existing_content(self, tmp_path: Path) -> None:
        """A deliberate reset: search="" means 'this is the whole file now'."""
        project = build_test_project(tmp_path / "proj")
        target = project / "app.py"
        target.write_text("old = 'content that must not survive'\n", encoding="utf-8")

        output = _apply(project, {"app.py": {"search": "", "replace": "new = 'kept'\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "new = 'kept'\n"

    def test_missing_search_key_is_treated_as_empty(self, tmp_path: Path) -> None:
        """An omitted 'search' is the same request as search=""."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"app.py": {"replace": "z = 3\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (project / "app.py").read_text(encoding="utf-8") == "z = 3\n"

    def test_null_search_is_treated_as_empty(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"app.py": {"search": None, "replace": "w = 4\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (project / "app.py").read_text(encoding="utf-8") == "w = 4\n"

    def test_empty_replace_yields_an_empty_file(self, tmp_path: Path) -> None:
        """Truncating a file is a legitimate edit, not a validation error."""
        project = build_test_project(tmp_path / "proj")
        target = project / "app.py"
        target.write_text("content\n", encoding="utf-8")

        output = _apply(project, {"app.py": {"search": "", "replace": ""}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == ""


# ===========================================================================
# A missing or empty target is a creation
# ===========================================================================


class TestMissingTargetIsCreated:
    def test_missing_target_with_real_search_writes_replace(self, tmp_path: Path) -> None:
        """Nothing exists to search, so the first step establishes the file."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"created.py": {"search": "needle", "replace": "body\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (project / "created.py").read_text(encoding="utf-8") == "body\n"

    def test_empty_target_with_real_search_writes_replace(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        target = project / "blank.py"
        target.write_text("", encoding="utf-8")

        output = _apply(project, {"blank.py": {"search": "needle", "replace": "body\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_creation_makes_missing_parent_directories(self, tmp_path: Path) -> None:
        """A nested first delivery must not fail on the absent parent."""
        project = build_test_project(tmp_path / "proj")
        target = project / "pkg" / "sub" / "module.py"

        output = _apply(project, {"pkg/sub/module.py": {"search": "", "replace": "v = 1\n"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "v = 1\n"

    def test_a_full_write_then_a_patch_applies_to_it(self, tmp_path: Path) -> None:
        """Steps stay ordered: the patch targets what the write produced."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(
            project,
            {
                "app.py": [
                    {"search": "", "replace": "first = 1\nsecond = 2\n"},
                    {"search": "second = 2", "replace": "second = 22"},
                ]
            },
        )

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (project / "app.py").read_text(encoding="utf-8") == "first = 1\nsecond = 22\n"

    def test_patch_after_creation_is_still_checked(self, tmp_path: Path) -> None:
        """A later ambiguous search is reported, not silently absorbed."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(
            project,
            {
                "app.py": [
                    {"search": "", "replace": "dup\ndup\n"},
                    {"search": "dup", "replace": "x"},
                ]
            },
        )

        assert output.status == config.AGENT_STATUS_FAILED
        assert any("matched 2 time(s)" in err for err in output.errors), output.errors


# ===========================================================================
# Delivery bookkeeping
# ===========================================================================


class TestCreationIsVisible:
    """The graceful path must not simply silence the old signal.

    Failing outright discarded delivered content; succeeding silently would hide
    a mistyped path. The middle ground is a warning on every creation.
    """

    def test_creation_from_a_missing_target_warns(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"typo.py": {"search": "a", "replace": "b"}})

        assert any("created missing file" in w for w in output.warnings), output.warnings

    def test_creation_into_an_empty_file_warns(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        (project / "blank.py").write_text("", encoding="utf-8")

        output = _apply(project, {"blank.py": {"search": "", "replace": "b\n"}})

        assert any("created empty file" in w for w in output.warnings), output.warnings

    def test_existing_non_empty_file_does_not_warn(self, tmp_path: Path) -> None:
        """A real patch is not a creation, so it must stay quiet."""
        project = build_test_project(tmp_path / "proj")
        (project / "app.py").write_text("alpha\n", encoding="utf-8")

        output = _apply(project, {"app.py": {"search": "alpha", "replace": "beta"}})

        assert not any("created" in w for w in output.warnings), output.warnings

    def test_deliberate_full_rewrite_of_existing_file_does_not_warn(
        self, tmp_path: Path
    ) -> None:
        """search="" on a populated file is a reset, not a creation."""
        project = build_test_project(tmp_path / "proj")
        (project / "app.py").write_text("old\n", encoding="utf-8")

        output = _apply(project, {"app.py": {"search": "", "replace": "new\n"}})

        assert not any("created" in w for w in output.warnings), output.warnings


class TestDeliverySideEffects:
    def test_created_file_is_mirrored_into_documents(self, tmp_path: Path) -> None:
        """The DoD checks documents, so a creation must land there too."""
        project = build_test_project(tmp_path / "proj")
        body = "created = True\n"

        output = _apply(project, {"app.py": {"search": "", "replace": body}})

        assert output.data["documents"]["app.py"] == body

    def test_created_file_is_reported_as_an_artifact(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"app.py": {"search": "", "replace": "a\n"}})

        assert "app.py" in output.artifacts

    def test_multiple_files_each_created(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")

        output = _apply(
            project,
            {
                "one.py": {"search": "", "replace": "1\n"},
                "two.py": {"search": "", "replace": "2\n"},
            },
        )

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert (project / "one.py").read_text(encoding="utf-8") == "1\n"
        assert (project / "two.py").read_text(encoding="utf-8") == "2\n"
        assert sorted(output.artifacts) == ["one.py", "two.py"]


# ===========================================================================
# The rejections that must survive
# ===========================================================================


class TestRejectionsPreserved:
    def test_ambiguous_search_still_fails(self, tmp_path: Path) -> None:
        """Exactly one match is the whole point of a patch."""
        project = build_test_project(tmp_path / "proj")
        target = project / "app.py"
        target.write_text("dup\ndup\n", encoding="utf-8")

        output = _apply(project, {"app.py": {"search": "dup", "replace": "x"}})

        assert output.status == config.AGENT_STATUS_FAILED
        assert any("matched 2 time(s)" in err for err in output.errors), output.errors

    def test_search_with_no_match_still_fails(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        target = project / "app.py"
        target.write_text("real content\n", encoding="utf-8")

        output = _apply(project, {"app.py": {"search": "absent", "replace": "x"}})

        assert output.status == config.AGENT_STATUS_FAILED
        assert any("matched 0 time(s)" in err for err in output.errors), output.errors
        assert target.read_text(encoding="utf-8") == "real content\n"

    def test_non_string_search_is_rejected(self, tmp_path: Path) -> None:
        """A number is malformed, not a full-write request."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"app.py": {"search": 42, "replace": "x"}})

        assert output.status == config.AGENT_STATUS_FAILED
        assert any("needs a string 'search'" in err for err in output.errors), output.errors
        assert not (project / "app.py").exists()

    def test_non_string_replace_is_rejected(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"app.py": {"search": "", "replace": None}})

        assert output.status == config.AGENT_STATUS_FAILED
        assert any("needs a string 'replace'" in err for err in output.errors), output.errors

    def test_missing_replace_is_rejected(self, tmp_path: Path) -> None:
        """An empty search does not make a missing 'replace' acceptable."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"app.py": {"search": ""}})

        assert output.status == config.AGENT_STATUS_FAILED
        assert output.errors

    def test_empty_step_list_is_rejected(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {"app.py": []})

        assert output.status == config.AGENT_STATUS_FAILED
        assert any("empty list" in err for err in output.errors), output.errors

    @pytest.mark.parametrize("escape", ["../outside.py", "/etc/passwd", "sub/../../out.py"])
    def test_path_escape_is_still_refused(self, tmp_path: Path, escape: str) -> None:
        """A full write must not become a way to write outside the project."""
        project = build_test_project(tmp_path / "proj")

        output = _apply(project, {escape: {"search": "", "replace": "owned\n"}})

        assert output.status == config.AGENT_STATUS_FAILED
        assert output.errors

    def test_normal_patch_still_works(self, tmp_path: Path) -> None:
        """The ordinary path must be completely unaffected."""
        project = build_test_project(tmp_path / "proj")
        target = project / "app.py"
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        output = _apply(project, {"app.py": {"search": "beta", "replace": "BETA"}})

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"

    def test_disjoint_steps_still_apply_in_order(self, tmp_path: Path) -> None:
        project = build_test_project(tmp_path / "proj")
        target = project / "app.py"
        target.write_text("one\ntwo\n", encoding="utf-8")

        output = _apply(
            project,
            {
                "app.py": [
                    {"search": "one", "replace": "1"},
                    {"search": "two", "replace": "2"},
                ]
            },
        )

        assert output.status == config.AGENT_STATUS_COMPLETED, output.errors
        assert target.read_text(encoding="utf-8") == "1\n2\n"