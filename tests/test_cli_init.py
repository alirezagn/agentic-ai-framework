"""``init`` destination resolution — no duplicated nested directory.

``init`` used to always join ``<dest>/<name>``. That is correct when ``--dest``
is a parent directory and wrong when the caller has already named the output
folder::

    init my_app --dest /tmp/my_app   ->  /tmp/my_app/my_app     (the bug)

The failure is not cosmetic. The project lands one level below where the user
pointed, so the ``--project`` path they then pass to ``run``/``plan``/``status``
is wrong, and the stray directory is what they find when they go looking.

Everything here is offline: ``--no-plan`` keeps the LLM out of it, and the
autouse fixtures in the repository ``conftest.py`` contain checkpoints and block
outbound network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import cli  # noqa: E402
from orchestrator.state_manager import StateManager  # noqa: E402


def _init(*args: str) -> int:
    """Run ``orchestrator init`` with planning disabled."""
    return cli.main(["init", *args, "--no-plan"])


# ===========================================================================
# The two cases from the report
# ===========================================================================


class TestInitDestination:
    def test_dest_already_named_is_used_directly(self, tmp_path: Path) -> None:
        """``init my_app --dest /tmp/my_app`` -> files directly in /tmp/my_app."""
        dest = tmp_path / "my_app"

        assert _init("my_app", "--dest", str(dest)) == 0

        assert (dest / "TASKS.yaml").is_file()
        assert (dest / "PROJECT.yaml").is_file()

    def test_dest_is_a_parent_directory(self, tmp_path: Path) -> None:
        """``init my_app --dest /tmp`` -> /tmp/my_app, the documented behaviour."""
        dest = tmp_path

        assert _init("my_app", "--dest", str(dest)) == 0

        assert (dest / "my_app" / "TASKS.yaml").is_file()

    def test_named_dest_never_nests_a_second_copy(self, tmp_path: Path) -> None:
        """The regression itself: no ``my_app/my_app`` anywhere."""
        dest = tmp_path / "my_app"

        _init("my_app", "--dest", str(dest))

        assert not (dest / "my_app").exists(), (
            "init nested a second copy of the project directory"
        )

    def test_parent_dest_still_uses_the_name(self, tmp_path: Path) -> None:
        dest = tmp_path

        _init("my_app", "--dest", str(dest))

        assert not (tmp_path / "my_app" / "my_app").exists()


class TestInitProducesAUsableProject:
    def test_named_dest_project_loads_and_is_named_correctly(
        self, tmp_path: Path
    ) -> None:
        """The written project must be readable, with the requested name.

        Guards the reason this matters: a project that landed at the wrong depth
        still *works* in isolation, so only a round trip proves it is usable at
        the path the user was told about.
        """
        dest = tmp_path / "sys_mon_gui"

        assert _init("sys_mon_gui", "--dest", str(dest)) == 0

        state = StateManager(dest)
        assert state.project_path == dest
        project = state.load_project()
        assert str((project.get("project") or {}).get("name")) == "sys_mon_gui"

    def test_baseline_checkpoint_is_created_at_the_target(
        self, tmp_path: Path, checkpoints_root: Path
    ) -> None:
        """``init`` promises a restorable baseline; it must land in the target."""
        dest = tmp_path / "my_app"

        _init("my_app", "--dest", str(dest))

        orchestrator = cli.MasterOrchestrator(dest)
        ids = [entry.get("id") for entry in orchestrator.checkpoints.list_checkpoints()]
        assert ids, "init created no baseline checkpoint at the resolved target"


# ===========================================================================
# Resolution rules
# ===========================================================================


class TestResolveInitTarget:
    def test_named_dest_is_not_doubled(self, tmp_path: Path) -> None:
        assert cli.resolve_init_target(str(tmp_path / "my_app"), "my_app") == (
            tmp_path / "my_app"
        )

    def test_parent_dest_is_joined(self, tmp_path: Path) -> None:
        assert cli.resolve_init_target(str(tmp_path), "my_app") == (
            tmp_path / "my_app"
        )

    def test_trailing_separator_is_not_a_distinct_path(self, tmp_path: Path) -> None:
        """``/tmp/my_app/`` is the same directory as ``/tmp/my_app``."""
        assert cli.resolve_init_target(f"{tmp_path}/my_app/", "my_app") == (
            tmp_path / "my_app"
        )

    def test_bare_relative_dest_matching_the_name(self) -> None:
        assert cli.resolve_init_target("my_app", "my_app") == Path("my_app")

    def test_default_dest_is_used_when_unset(self, monkeypatch) -> None:
        args = cli.main.__globals__["argparse"].Namespace(name="my_app", dest=None)
        assert cli.resolve_init_target(getattr(args, "dest", None), "my_app") == Path(
            "projects/my_app"
        )

    def test_default_dest_for_a_named_project_under_projects(self) -> None:
        """The common case is untouched: still ``projects/<name>``."""
        assert cli.resolve_init_target("projects", "my_app") == Path(
            "projects/my_app"
        )

    def test_parent_sharing_the_suffix_still_nests(self, tmp_path: Path) -> None:
        """Only the final component counts, so ``my_appiles`` is a real parent."""
        assert cli.resolve_init_target(str(tmp_path / "my_appiles"), "my_app") == (
            tmp_path / "my_appiles" / "my_app"
        )

    def test_deeper_path_ending_in_the_name_is_used_directly(
        self, tmp_path: Path
    ) -> None:
        """A nested destination that ends in the name is still the target."""
        target = tmp_path / "work" / "client" / "my_app"
        assert cli.resolve_init_target(str(target), "my_app") == target

    def test_dot_and_dotdot_are_parents_not_targets(self) -> None:
        assert cli.resolve_init_target(".", "my_app") == Path("my_app")
        assert cli.resolve_init_target("..", "my_app") == Path("../my_app")

    def test_case_mismatch_still_nests(self, tmp_path: Path) -> None:
        """Deliberate: an extra level is visible, a wrong path is not."""
        assert cli.resolve_init_target(str(tmp_path / "My_App"), "my_app") == (
            tmp_path / "My_App" / "my_app"
        )

    def test_empty_dest_falls_back_to_the_default_parent(self) -> None:
        assert cli.resolve_init_target("", "my_app") == Path("projects/my_app")


# ===========================================================================
# Safety of the new behaviour
# ===========================================================================


class TestResolvedTargetIsSafe:
    def test_existing_project_still_requires_force(self, tmp_path: Path) -> None:
        """Using ``--dest`` directly must not bypass the overwrite guard."""
        dest = tmp_path / "my_app"
        assert _init("my_app", "--dest", str(dest)) == 0

        assert _init("my_app", "--dest", str(dest)) == 2
        assert _init("my_app", "--dest", str(dest), "--force") == 0

    def test_parent_that_holds_other_projects_is_not_refused(
        self, tmp_path: Path
    ) -> None:
        """A guard keyed on the resolved path must not block a fresh sibling."""
        assert _init("first", "--dest", str(tmp_path), "--no-plan") == 0
        assert _init("second", "--dest", str(tmp_path), "--no-plan") == 0

        assert (tmp_path / "first" / "TASKS.yaml").is_file()
        assert (tmp_path / "second" / "TASKS.yaml").is_file()

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_missing_name_is_rejected(self, tmp_path: Path, bad: str) -> None:
        """An empty name must not silently initialise ``--dest`` itself.

        Whitespace-only counts as empty: before the fix, ``init "  "`` created a
        directory literally named two spaces and reported success.
        """
        assert cli.main(["init", bad, "--dest", str(tmp_path), "--no-plan"]) == 2
        assert not (tmp_path / "TASKS.yaml").exists()


class TestNameCannotEscapeDest:
    """The name is a path component, so it must not traverse.

    Found while testing the destination fix: ``init "../../tmp/evil" --dest
    /some/where`` resolved outside ``--dest`` and scaffolded a complete project
    in the traversed location. It had been blocked only by accident, whenever the
    resolved path happened to exist and tripped the ``--force`` guard.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            "../../tmp/evil",
            "..",
            "a/../../b",
            "/etc/evil",
            "./evil",
            "sub/",
        ],
    )
    def test_traversing_name_is_refused(self, tmp_path: Path, bad: str) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()

        assert _init(bad, "--dest", str(dest)) == 2
        assert list(dest.iterdir()) == [], "nothing may be written for a refused name"
        assert not (tmp_path / "evil").exists()

    def test_traversal_is_refused_even_with_force(self, tmp_path: Path) -> None:
        """``--force`` overwrites state; it must not extend the reachable path."""
        dest = tmp_path / "dest"
        dest.mkdir()
        outside = tmp_path / "evil"
        assert not outside.exists()

        assert _init("../evil", "--dest", str(dest), "--force") == 2
        assert not outside.exists()

    def test_backslash_name_is_refused(self, tmp_path: Path) -> None:
        """Treated as a separator on every platform, so never a valid name."""
        dest = tmp_path / "dest"
        dest.mkdir()

        assert _init("a\\b", "--dest", str(dest)) == 2

    def test_plain_name_still_works(self, tmp_path: Path) -> None:
        """The validation must not reject ordinary project names."""
        dest = tmp_path / "dest"
        dest.mkdir()

        assert _init("my_app", "--dest", str(dest)) == 0
        assert (dest / "my_app" / "TASKS.yaml").is_file()

    def test_name_is_trimmed_rather_than_rejected(self, tmp_path: Path) -> None:
        """Padding is a typo, not a rejection; the created name is the real one."""
        dest = tmp_path / "dest"
        dest.mkdir()

        assert _init("  my_app  ", "--dest", str(dest)) == 0
        assert (dest / "my_app" / "TASKS.yaml").is_file()