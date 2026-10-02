"""Tests for :mod:`sys_mon`.

The install is performed *here*, in ``setUpModule``, which is the point of the
rewrite: the previous version of this file simply imported ``sys_mon``, which
imported ``psutil``, and died during collection whenever the package was
absent. The suite could not start, so it could not report anything.

Now the dependency is declared in ``requirements.txt``, installed from it
before anything runs, and reported honestly when the install is impossible.
"""

import unittest
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from importlib.util import find_spec

from sys_mon import (
    PACKAGE,
    PROJECT_ROOT,
    REQUIREMENTS_FILE,
    dependency_available,
    ensure_dependencies,
    get_system_metrics,
    install_command,
)


def setUpModule() -> None:
    """Install declared dependencies before any test runs.

    Honours the framework rule that an unavailable install is a finding, not a
    reason to fabricate a result: when the install genuinely cannot happen the
    module is *skipped* with the real reason, and the tests below are never
    reported as passing on unverified ground.
    """
    result = ensure_dependencies()
    if result["action"] == "failed":
        raise unittest.SkipTest(
            f"dependency setup failed, so these tests were NOT RUN: {result['detail']}"
        )


class TestDependencyDeclaration(unittest.TestCase):
    """The declaration must exist as a verifiable file, not just as prose."""

    def test_requirements_file_exists_in_project_root(self):
        self.assertTrue(
            REQUIREMENTS_FILE.is_file(),
            f"{REQUIREMENTS_FILE} is missing; every third-party import must be declared",
        )
        self.assertEqual(
            REQUIREMENTS_FILE.parent,
            PROJECT_ROOT,
            "requirements.txt belongs at the project root",
        )

    def test_psutil_is_declared(self):
        lines = [
            line.strip()
            for line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertTrue(
            any(line.lower().startswith(PACKAGE.lower()) for line in lines),
            f"{PACKAGE} is imported by sys_mon.py but not declared in "
            f"{REQUIREMENTS_FILE.name}: {lines}",
        )

    def test_requirements_are_version_pinned(self):
        pins = [
            line.strip()
            for line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for pin in pins:
            self.assertRegex(
                pin,
                r"[<>=!~]",
                f"'{pin}' is unpinned; declare a floor such as "
                f"'{PACKAGE}>=5.9' so the install is reproducible",
            )

    def test_install_command_targets_the_declared_file(self):
        command = install_command()
        self.assertIn("install", command)
        self.assertIn("-r", command)
        self.assertEqual(command[command.index("-r") + 1], str(REQUIREMENTS_FILE))
        self.assertIn("-m", command, "use `sys.executable -m pip`, not a bare pip")
        self.assertEqual(command[0], __import__("sys").executable)


class TestDependencyAutomation(unittest.TestCase):
    def test_dependency_is_available_after_setup(self):
        self.assertTrue(
            dependency_available(),
            f"{PACKAGE} is still not importable after ensure_dependencies()",
        )

    def test_ensure_reports_no_work_when_satisfied(self):
        result = ensure_dependencies()
        self.assertEqual(result["action"], "none")
        self.assertFalse(result["installed"])
        self.assertTrue(result["available"])

    def test_ensure_is_a_no_op_when_already_satisfied(self):
        """Never reinstalls a satisfied environment on every test run."""
        result = ensure_dependencies()
        self.assertEqual(result["action"], "none", result["detail"])


class TestMetrics(unittest.TestCase):
    def test_metrics_structure(self):
        """Verify that the returned dictionary contains all required keys."""
        metrics = get_system_metrics()

        if "error" in metrics:
            self.fail(f"Metrics returned an error: {metrics['error']}")

        self.assertIn("cpu_percent", metrics)
        self.assertIn("ram", metrics)
        self.assertIn("disk", metrics)

        # RAM sub-keys
        self.assertIn("total_gb", metrics["ram"])
        self.assertIn("available_gb", metrics["ram"])
        self.assertIn("percent_used", metrics["ram"])

        # Disk sub-keys
        self.assertIn("total_gb", metrics["disk"])
        self.assertIn("free_gb", metrics["disk"])
        self.assertIn("percent_used", metrics["disk"])

    def test_metrics_types(self):
        """Verify that the values are of the expected types."""
        metrics = get_system_metrics()
        if "error" in metrics:
            self.fail(f"Metrics returned an error: {metrics['error']}")

        self.assertIsInstance(metrics["cpu_percent"], (int, float))
        self.assertIsInstance(metrics["ram"]["total_gb"], float)
        self.assertIsInstance(metrics["disk"]["total_gb"], float)


if __name__ == "__main__":
    unittest.main()