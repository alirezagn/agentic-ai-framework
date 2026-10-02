"""System metrics collector.

Dependency handling is explicit here rather than implied, because the previous
version of this module imported ``psutil`` at module scope with no
``requirements.txt`` anywhere in the project. The result was a test module that
could not even be *collected* (``ModuleNotFoundError`` at import), which is the
worst failure mode: nothing runs and nothing reports.

The fix has three parts, matching the framework's dependency contract:

1. ``requirements.txt`` declares ``psutil`` — the declaration is a file the
   Definition of Done can verify.
2. :func:`ensure_dependencies` performs the install, so the declaration is
   acted upon rather than merely announced.
3. ``psutil`` is imported lazily inside :func:`get_system_metrics`, so importing
   this module always succeeds even when the package is genuinely absent. The
   module can report "not installed"; it cannot fail to be read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"

#: The single third-party runtime dependency of this project.
PACKAGE = "psutil"

#: Seconds to wait for the install before giving up and reporting failure.
INSTALL_TIMEOUT_SECONDS = 120


def install_command() -> List[str]:
    """argv that installs every declared dependency.

    Uses ``sys.executable -m pip`` rather than a bare ``pip``: it resolves to
    the interpreter running the tests, so the package lands in the environment
    that actually needs it instead of whatever ``pip`` happens to be on PATH.
    """
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        str(REQUIREMENTS_FILE),
    ]


def dependency_available() -> bool:
    """True when :data:`PACKAGE` can be imported.

    Probed with :func:`importlib.util.find_spec` rather than ``import psutil``
    so the check costs nothing and cannot raise.
    """
    try:
        return find_spec(PACKAGE) is not None
    except (ImportError, ValueError):
        return False


def ensure_dependencies(
    runner: Any = subprocess.run,
    timeout: int = INSTALL_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Install declared dependencies when they are missing.

    Returns a structured result instead of raising, because the caller — a test
    ``setUpModule`` — needs to *report* the outcome honestly. This mirrors the
    framework's own rule that an unavailable install is a finding to report
    (``test_status = "NOT RUN"``), not a failure to paper over.

    ``runner`` is injectable so the behaviour can be tested without touching a
    real environment or the network.

    Keys: ``requirements_file``, ``exists``, ``package``, ``available``,
    ``installed``, ``action`` (``"none"`` | ``"installed"`` | ``"failed"``),
    ``command``, ``returncode``, ``detail``.
    """
    result: Dict[str, Any] = {
        "requirements_file": str(REQUIREMENTS_FILE),
        "exists": REQUIREMENTS_FILE.is_file(),
        "package": PACKAGE,
        "available": dependency_available(),
        "installed": False,
        "action": "none",
        "command": install_command(),
        "returncode": None,
        "detail": "",
    }

    if result["available"]:
        result["detail"] = f"{PACKAGE} is already installed"
        return result

    if not result["exists"]:
        result["action"] = "failed"
        result["detail"] = (
            f"requirements.txt is missing at {REQUIREMENTS_FILE}; "
            f"{PACKAGE} cannot be installed automatically"
        )
        return result

    result["action"] = "installed"
    result["detail"] = f"installing from {REQUIREMENTS_FILE.name}"
    try:
        completed = runner(
            result["command"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
    except Exception as exc:  # timeout, missing executable, OS-level refusal
        result["action"] = "failed"
        result["detail"] = f"install could not run: {type(exc).__name__}: {exc}"
        return result

    result["returncode"] = int(getattr(completed, "returncode", 1) or 0)
    if result["returncode"] != 0 or not dependency_available():
        result["action"] = "failed"
        stderr = (getattr(completed, "stderr", "") or "").strip()
        result["detail"] = (
            f"pip install failed with exit code {result['returncode']}"
            + (f": {stderr.splitlines()[-1]}" if stderr else "")
        )
        result["installed"] = False
        return result

    result["installed"] = True
    result["available"] = True
    result["detail"] = f"{PACKAGE} installed from {REQUIREMENTS_FILE.name}"
    return result


def get_system_metrics() -> Dict[str, Any]:
    """Fetch CPU, RAM and disk usage.

    ``psutil`` is imported here, not at module scope, so a missing dependency
    produces a reportable error instead of an unimportable module. A failure is
    returned as ``{"error": ...}`` because the test suite asserts on that key
    and a silent partial dict would be worse.
    """
    try:
        import psutil  # imported lazily: see module docstring
    except ImportError as exc:
        return {
            "error": (
                f"{PACKAGE} is not installed ({exc}). "
                f"Run: {' '.join(install_command())}"
            )
        }

    try:
        # CPU Usage
        cpu_usage = psutil.cpu_percent(interval=1)

        # RAM Usage
        virtual_mem = psutil.virtual_memory()
        ram_usage = {
            "total_gb": round(virtual_mem.total / (1024**3), 2),
            "available_gb": round(virtual_mem.available / (1024**3), 2),
            "percent_used": virtual_mem.percent,
        }

        # Disk Usage (root partition)
        disk = psutil.disk_usage("/")
        disk_usage = {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "percent_used": disk.percent,
        }

        return {
            "cpu_percent": cpu_usage,
            "ram": ram_usage,
            "disk": disk_usage,
        }
    except Exception as exc:
        return {"error": str(exc)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print metrics as JSON; exit non-zero when collection failed."""
    metrics = get_system_metrics()
    print(json.dumps(metrics, indent=2))
    return 1 if "error" in metrics else 0


if __name__ == "__main__":
    sys.exit(main())