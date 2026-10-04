"""A generated project must survive a bare ``pytest`` run (OPEN-3/OPEN-4).

The defect this pins is not a ``sys.path`` problem and the fix is not
``PYTHONPATH``. ``python3 -m pytest`` puts the cwd on ``sys.path`` (its
``sys.path[0]`` is ``''``), so it passes from inside the project; a bare
``pytest`` does not, so pytest searches upwards for an ini file and adopts the
**enclosing repository's** ``pyproject.toml``::

    $ pytest tests/            # from projects/my-app
    rootdir:  /…/agentic-ai-framework     <- the framework repo, not my-app
    configfile: pyproject.toml
    E   ModuleNotFoundError: No module named 'src'

Three things go wrong and all three are pinned here:

1. rootdir escapes the project, because no ini file declares this directory;
2. the project's root never enters ``sys.path``, so ``from src.x import y``
   cannot resolve — note that fixing (1) alone does *not* fix this, since
   pytest's ``prepend`` import mode inserts the *test file's* directory, not
   the rootdir; and
3. the ``docs/`` artifact mirror is collected as a duplicate of the real test
   module, aborting collection with ``import file mismatch``.

The fix is a scaffolded ``pytest.ini`` at the project root: ``pythonpath = .``
for (2), the ini file itself for (1), ``norecursedirs = docs`` for (3).

It is deliberately **not** a ``conftest.py``. A generated project lands at
``projects/<name>`` — inside the repository that ran ``init`` — and a second
``conftest.py`` there shadows the host's own conftest *module*, breaking every
host test that does ``from conftest import ...``. Measured on this repository:
27 suites failed that way. See ``TestScaffoldDoesNotShadowTheHostRepository``.

Every test here is offline: ``--no-plan`` keeps the LLM out of ``init``, and the
autouse fixtures in the repository ``conftest.py`` contain checkpoints and block
outbound network.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import cli  # noqa: E402

#: The keys ``init`` must write. Kept in one place so the assertions below and
#: any future scaffolding share them.
EXPECTED_INI_KEYS = ("pythonpath", "testpaths", "norecursedirs")


def _init(*args: str) -> int:
    """Run ``orchestrator init`` with planning disabled."""
    return cli.main(["init", *args, "--no-plan"])


def _write_test_project(project: Path) -> None:
    """Mimic what the agents deliver: a package plus a test importing it.

    The import is deliberately the ``from src.x import y`` shape the template
    projects use, because that is the import that breaks when rootdir escapes.
    """
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "data_layer.py").write_text(
        textwrap.dedent(
            '''\
            def audit(user_id: str) -> str:
                return f"audited {user_id}"
            '''
        ),
        encoding="utf-8",
    )
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "tests" / "test_suite.py").write_text(
        textwrap.dedent(
            """\
            from src.data_layer import audit


            def test_audit():
                assert audit("alice") == "audited alice"
            """
        ),
        encoding="utf-8",
    )


#: A ``pyproject.toml`` shaped like this repository's, which is what a bare run
#: finds when it walks up out of a project nested inside the framework repo
#: (``testpaths = ["."]`` is ``pyproject.toml:20``). Reproducing the escape needs
#: a real enclosing config: pytest only abandons the project directory when
#: there is an ini file above it to adopt.
ENCLOSING_PYPROJECT = """\
[tool.pytest.ini_options]
testpaths = ["."]
"""


def _nested_project(tmp_path: Path, name: str = "my_app") -> Path:
    """Init a project inside a repo-like parent, as ``projects/<name>`` is.

    Without the enclosing ``pyproject.toml`` the bug cannot reproduce at all:
    rootdir has nothing to escape into, so every bare run passes and the tests
    would be green for the wrong reason.
    """
    outer = tmp_path / "outer_repo"
    outer.mkdir(parents=True, exist_ok=True)
    (outer / "pyproject.toml").write_text(ENCLOSING_PYPROJECT, encoding="utf-8")
    dest = outer / "projects" / name
    assert _init(name, "--dest", str(dest)) == 0
    return dest


def _run_bare_pytest(project: Path) -> subprocess.CompletedProcess[str]:
    """Bare ``pytest`` from inside the project — no cwd on ``sys.path``.

    This must invoke the ``pytest`` **console script**, not
    ``python -m pytest``: running the module puts the cwd on ``sys.path``
    (``sys.path[0] == ''``), which is precisely the step that makes the passing
    form pass. Using it here would test the wrong invocation and the suite would
    be green for the wrong reason.

    ``-v`` is deliberate: pytest prints the resolved ``rootdir:`` and
    ``configfile:`` header and the per-test names only in verbose mode, and both
    are what these tests assert on.
    """
    script = shutil.which("pytest")
    if script is None:  # pragma: no cover - depends on the host install
        pytest.skip("no `pytest` console script on PATH")
    return subprocess.run(
        [script, "tests/", "-v", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
    )


# ===========================================================================
# The scaffold itself
# ===========================================================================


class TestInitScaffoldsCollectionConfig:
    def test_init_writes_a_project_root_pytest_ini(self, tmp_path: Path) -> None:
        """``init`` must leave the collection config behind, not rely on luck."""
        dest = tmp_path / "my_app"

        assert _init("my_app", "--dest", str(dest)) == 0

        ini = dest / "pytest.ini"
        assert ini.is_file(), (
            "init scaffolded no pytest.ini, so a bare `pytest` run in a "
            "generated project inherits whatever pyproject.toml pytest finds "
            "above it (OPEN-3/OPEN-4)"
        )

    def test_ini_excludes_the_docs_mirror(self, tmp_path: Path) -> None:
        """The artifact mirror is a copy, not a second test module.

        Every expected output is mirrored into ``docs/<basename>``, so without
        this a bare run collects ``docs/test_suite.py`` alongside
        ``tests/test_suite.py`` and dies with ``import file mismatch``.
        """
        dest = tmp_path / "my_app"
        _init("my_app", "--dest", str(dest))

        body = (dest / "pytest.ini").read_text(encoding="utf-8")

        assert "norecursedirs" in body, "pytest.ini sets no collection guard"
        assert "docs" in body, (
            "the docs/ artifact mirror is not excluded, so a mirrored test "
            "module collides with the real one on basename"
        )

    def test_ini_puts_the_project_root_on_sys_path(self, tmp_path: Path) -> None:
        """``pythonpath`` is what makes ``from src.x import y`` resolve.

        Measured 2026-10-05: naming an ini file pins rootdir, but pytest's
        ``prepend`` import mode inserts the *test file's* directory — not the
        rootdir — so without ``pythonpath`` the import still fails with
        ``ModuleNotFoundError`` even though rootdir is correct.
        """
        dest = tmp_path / "my_app"
        _init("my_app", "--dest", str(dest))

        body = (dest / "pytest.ini").read_text(encoding="utf-8")

        assert "pythonpath" in body, (
            "pytest.ini sets no pythonpath, so `from src.module import X` "
            "fails on a bare run even with the correct rootdir"
        )

    def test_ini_is_syntactically_valid(self, tmp_path: Path) -> None:
        """A malformed ini makes pytest fall back to the enclosing config."""
        dest = tmp_path / "my_app"
        _init("my_app", "--dest", str(dest))

        import configparser

        parser = configparser.ConfigParser()
        parser.read(dest / "pytest.ini", encoding="utf-8")

        assert parser.has_section("pytest"), "pytest.ini has no [pytest] section"
        for key in EXPECTED_INI_KEYS:
            assert parser.has_option("pytest", key), (
                f"pytest.ini is missing {key}"
            )

    def test_reinit_does_not_clobber_an_existing_ini(self, tmp_path: Path) -> None:
        """A user's pytest config is theirs; ``--force`` must not overwrite it."""
        dest = tmp_path / "my_app"
        assert _init("my_app", "--dest", str(dest)) == 0

        edited = dest / "pytest.ini"
        edited.write_text("[pytest]\naddopts = -q\n", encoding="utf-8")

        assert _init("my_app", "--dest", str(dest), "--force") == 0

        assert edited.read_text(encoding="utf-8") == "[pytest]\naddopts = -q\n", (
            "re-init overwrote the project's own pytest configuration"
        )


# ===========================================================================
# The behaviour: a bare run works on a generated project
# ===========================================================================


class TestBarePytestPassesOnAGeneratedProject:
    def test_bare_pytest_collects_and_passes(self, tmp_path: Path) -> None:
        """The regression in full: ``pytest tests/`` from the project root.

        Reproduced live on 2026-10-05 against ``projects/my-app``: bare pytest
        reported ``rootdir`` as the agentic-ai-framework repo, adopted its
        ``pyproject.toml`` (``testpaths = ["."]``), and failed collection with
        ``ModuleNotFoundError: No module named 'src'`` while
        ``python3 -m pytest tests/`` passed 5/5 from the same directory.
        """
        dest = _nested_project(tmp_path)
        _write_test_project(dest)

        result = _run_bare_pytest(dest)

        assert "ModuleNotFoundError" not in result.stdout + result.stderr, (
            "bare pytest could not import the project's own package: rootdir "
            "escaped the project"
        )
        assert result.returncode == 0, (
            "bare `pytest` failed on a freshly generated project:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
        assert "1 passed" in result.stdout

    def test_an_enclosing_config_does_not_hijack_a_bare_run(
        self, tmp_path: Path
    ) -> None:
        """The enclosing repo must not collect *its* tests for this project.

        Measured 2026-10-05: with an enclosing ``pyproject.toml`` carrying
        ``testpaths = ["."]``, an unguarded bare ``pytest`` from the project
        root adopts the *enclosing* config, collects that repository's tests,
        and reports ``rootdir`` outside this project. The scaffolded
        ``pytest.ini`` makes this directory the rootdir instead.

        This asserts the user-visible outcome rather than the cosmetic header,
        because the header alone would pass for the wrong reasons.
        """
        outer = tmp_path / "outer_repo"
        dest = _nested_project(tmp_path)
        _write_test_project(dest)

        # A second, unrelated test module in the enclosing repository: if its
        # config hijacks the run, this is what would show up in the output.
        stray = outer / "other" / "test_stray.py"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(
            "def test_stray():\n    assert True\n", encoding="utf-8"
        )

        result = _run_bare_pytest(dest)

        assert result.returncode == 0, (
            "a bare run from the project root failed:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
        assert "test_audit" in result.stdout, (
            "the project's own test did not run:\n" f"{result.stdout[-2000:]}"
        )
        assert "test_stray" not in result.stdout, (
            "the enclosing repository's test was collected for this project, so "
            f"its pyproject.toml is dictating the run:\n{result.stdout[-2000:]}"
        )

    def test_docs_mirror_does_not_break_collection(self, tmp_path: Path) -> None:
        """The ``docs/`` mirror of a test file must not be collected.

        This is OPEN-3 proper. The materializer mirrors expected outputs into
        ``docs/<basename>``, so ``docs/test_suite.py`` is byte-identical to
        ``tests/test_suite.py``; without ``norecursedirs`` a bare run collects
        both and reports ``import file mismatch``, collection interrupted.
        """
        dest = _nested_project(tmp_path)
        _write_test_project(dest)

        mirror = dest / "docs" / "test_suite.py"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text(
            (dest / "tests" / "test_suite.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        result = _run_bare_pytest(dest)

        assert "import file mismatch" not in result.stdout + result.stderr, (
            "the docs/ mirror was collected as a second copy of the test module"
        )
        assert result.returncode == 0, (
            "the docs/ artifact mirror broke collection:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )

    def test_a_real_tests_directory_is_still_collected(
        self, tmp_path: Path
    ) -> None:
        """The guard must not over-reach: ``tests/`` is the point of the run.

        ``testpaths = ["."]`` was rejected in memory.md precisely because it
        "silently falls back to full-tree collection (and re-collects the
        mirror) when ``tests/`` is missing". ``norecursedirs`` excludes one
        directory and nothing else, so this must hold.
        """
        dest = _nested_project(tmp_path)
        _write_test_project(dest)

        result = _run_bare_pytest(dest)

        assert "test_audit" in result.stdout, (
            "the real test was skipped, so the guard is too broad:\n"
            f"{result.stdout[-2000:]}"
        )


# ===========================================================================
# The guard must not become a footgun
# ===========================================================================


class TestScaffoldDoesNotShadowTheHostRepository:
    """A generated project must not break the suite that generated it.

    Measured 2026-10-05, and this is a property of the *fix*, not of the bug.
    The first implementation scaffolded a ``conftest.py`` at the project root.
    That works for the project — but a generated project lives at
    ``projects/<name>``, i.e. inside whatever repository ran ``init``, and a
    second ``conftest.py`` there **shadows the host's own conftest module**.
    Under pytest's prepend import mode the name ``conftest`` bound to the
    generated file, and this repository's own suite collapsed: 28 root modules
    do ``from conftest import build_test_project`` and 27 suites failed with::

        ImportError: cannot import name 'build_test_project' from 'conftest'

    The scaffold is therefore a ``pytest.ini`` (which pins rootdir, sets
    ``pythonpath``, and excludes ``docs/``) and never a ``conftest.py``. These
    tests pin that choice so it cannot be "simplified" back.
    """

    def test_init_does_not_write_a_conftest_py(self, tmp_path: Path) -> None:
        """The decisive assertion: no second conftest at the project root.

        This is what broke the host suite, so it is the property worth locking.
        """
        dest = tmp_path / "my_app"
        _init("my_app", "--dest", str(dest))

        assert not (dest / "conftest.py").exists(), (
            "init writes a conftest.py, which shadows the conftest module of "
            "any repository the project is created inside (init defaults to "
            "projects/<name>, i.e. the current repository) and breaks every "
            "host test that imports from conftest"
        )

    def test_generated_project_does_not_shadow_a_host_conftest(
        self, tmp_path: Path
    ) -> None:
        """The real constraint, exercised end to end.

        A host repository keeps its own ``conftest.py``; ``init`` then creates a
        project *inside* it. The host's tests must still pass.
        """
        host = tmp_path / "host_repo"
        host.mkdir(parents=True, exist_ok=True)
        # The ini file matters: rootdir resolution walks up to the nearest
        # pyproject.toml, and ``testpaths = ["."]`` is what drives the
        # full-tree collection under which the shadowing reproduces.
        (host / "pyproject.toml").write_text(ENCLOSING_PYPROJECT, encoding="utf-8")
        (host / "conftest.py").write_text(
            "def build_test_project():\n    return {'ok': True}\n", encoding="utf-8"
        )
        (host / "test_host.py").write_text(
            "from conftest import build_test_project\n\n"
            "def test_host_fixture_import():\n"
            "    assert build_test_project()['ok'] is True\n",
            encoding="utf-8",
        )

        # The nested project, as `init --dest projects/<name>` creates it.
        dest = host / "projects" / "my_app"
        assert _init("my_app", "--dest", str(dest)) == 0

        # A test module inside the nested project, so the run collects the whole
        # tree. This matters: the shadowing only reproduces under full-tree
        # collection. Targeting `test_host.py` alone passes even with a
        # shadowing conftest present, because pytest never imports it — that
        # variant would be a test that cannot fail.
        nested = dest / "tests"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "test_nested.py").write_text(
            "def test_nested():\n    assert True\n", encoding="utf-8"
        )

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=host,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 0, (
            "creating a project inside this repository broke the host suite, so "
            "the scaffold is claiming the conftest module name:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
        assert "2 passed" in result.stdout, (
            "both the host test and the nested test must run:\n"
            f"{result.stdout[-2000:]}"
        )


# ===========================================================================
# The scaffold must not become a footgun
# ===========================================================================


class TestScaffoldedConfigIsInert:
    def test_the_ini_does_not_touch_the_network(self, tmp_path: Path) -> None:
        """A config that reaches out on import breaks every later run."""
        dest = tmp_path / "my_app"
        _init("my_app", "--dest", str(dest))

        body = (dest / "pytest.ini").read_text(encoding="utf-8")

        for forbidden in ("requests", "httpx", "urllib", "socket", "subprocess"):
            assert forbidden not in body, (
                f"the scaffolded pytest.ini mentions {forbidden}"
            )

    def test_the_ini_pins_no_hardcoded_absolute_path(self, tmp_path: Path) -> None:
        """A generated config must be portable across machines."""
        dest = tmp_path / "my_app"
        _init("my_app", "--dest", str(dest))

        body = (dest / "pytest.ini").read_text(encoding="utf-8")

        assert str(tmp_path) not in body, (
            "the scaffolded pytest.ini embeds the absolute path it was "
            "generated at, so it breaks when the project is moved"
        )


if __name__ == "__main__":  # pragma: no cover - manual reproduction aid
    raise SystemExit(pytest.main([__file__, "-v"]))