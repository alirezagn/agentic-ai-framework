"""The repository's own ``docs/`` collection guard must actually match.

OPEN-4 recorded that ``collect_ignore_glob = ["*/docs/*", "*/docs/**/*"]`` in
the repository ``conftest.py`` described exactly the defect it was meant to
prevent, and was dead code for two independent reasons:

1. this repository has **no ``docs/`` directory at all**, so nothing was ever
   at risk here and no test exercised the guard; and
2. the pattern requires a path segment *before* ``docs/``, so it never matched
   a rootdir-relative ``docs/test_metrics.py`` — the shape a generated project
   actually produces, since the materializer mirrors into ``docs/<basename>``.

Re-reproduced 2026-10-05 on a generated project nested in a repo-like parent:
with that exact glob in place, a bare ``pytest`` still died with
``import file mismatch`` / collection interrupted. Replacing it with the
relative-directory form ``collect_ignore = ["docs"]`` fixed it and left the real
``tests/`` directory collecting.

These tests pin the *pattern*, not just the intent, because the intent is
already documented in the comment above the guard and was nonetheless wrong.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pytest_version() -> str:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return out.stdout.strip()


def _write_mirror_collision(project: Path) -> None:
    """A real test module plus a byte-identical copy under ``docs/``."""
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "docs").mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(
        """\
        def test_value():
            assert 1 + 1 == 2
        """
    )
    (project / "tests" / "test_units.py").write_text(body, encoding="utf-8")
    (project / "docs" / "test_units.py").write_text(body, encoding="utf-8")


def _collect(project: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Collect only — the failure mode is a collection error, not a run error."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *extra],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestRepositoryGuardIsNotDeadCode:
    def test_the_guard_variable_exists(self) -> None:
        """A named guard must be present in the repository conftest."""
        body = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")

        assert "collect_ignore" in body, (
            "the repository conftest.py no longer carries a docs/ collection "
            "guard; the materializer mirrors test modules into docs/ and they "
            "collide on basename"
        )

    def test_the_guard_ignores_the_docs_directory(self) -> None:
        """It must be the directory-name form, which matches at the root.

        The ``*/docs/*`` glob this replaced requires a segment *before*
        ``docs/``, so a rootdir-relative ``docs/test_units.py`` never matched —
        the guard was inert precisely where it was needed.
        """
        body = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")

        assert 'collect_ignore = ["docs"]' in body, (
            "the docs/ guard must use the relative-directory form "
            'collect_ignore = ["docs"]; a "*/docs/*" glob needs a segment '
            "before docs/ and matches nothing at a project root"
        )


class TestGuardBehaviourOnAMirrorCollision:
    """The real proof: a ``docs/`` mirror must not break collection."""

    def test_docs_mirror_does_not_interrupt_collection(
        self, tmp_path: Path
    ) -> None:
        """Reproduces OPEN-3's ``import file mismatch`` and shows it fixed.

        The collision is genuine: ``docs/test_units.py`` and
        ``tests/test_units.py`` share a basename and neither directory is a
        package, so pytest's ``prepend`` import mode registers both under
        ``test_units`` and refuses the second.
        """
        project = tmp_path / "my_app"
        _write_mirror_collision(project)
        # The guard under test, copied verbatim from the repository conftest.
        (project / "conftest.py").write_text(
            'collect_ignore = ["docs"]\n', encoding="utf-8"
        )

        result = _collect(project)

        assert "import file mismatch" not in result.stdout + result.stderr, (
            "the docs/ mirror still collides on basename:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
        assert "1 test" in result.stdout, (
            "the real test module was not collected:\n" f"{result.stdout[-2000:]}"
        )

    def test_the_old_glob_pattern_reproduces_the_bug(
        self, tmp_path: Path
    ) -> None:
        """Guard the guard: the replaced pattern must still be provably wrong.

        A test that only asserts the fix is green cannot tell a real fix from a
        change that made the collision stop happening for some other reason.
        Reinstating the exact historical pattern has to bring the failure back.
        """
        project = tmp_path / "my_app"
        _write_mirror_collision(project)
        (project / "conftest.py").write_text(
            'collect_ignore_glob = ["*/docs/*", "*/docs/**/*"]\n', encoding="utf-8"
        )

        result = _collect(project)

        assert "import file mismatch" in result.stdout + result.stderr, (
            "the historical '*/docs/*' pattern no longer reproduces OPEN-3, so "
            "the note claiming it was inert is wrong and the replacement is "
            "unjustified:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}\n"
            f"pytest: {_pytest_version()}"
        )


if __name__ == "__main__":  # pragma: no cover - manual reproduction aid
    raise SystemExit(pytest.main([__file__, "-v"]))