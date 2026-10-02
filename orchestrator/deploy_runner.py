"""The single chokepoint for executing anything outside the Python process.

Why this module exists
----------------------
Before it, the orchestrator had no way to run a build, a test, or a flash — and
that absence was the root of GAP-CRIT-01: agents were asked for test results
they could not obtain, so they invented them, and nothing in the Definition of
Done could tell an invented result from a real one. The output of
``TestAgent`` claiming "42/42 tests passed, coverage 94%" became ``DONE``.

The temptation with a new execution capability is to call ``subprocess`` from
wherever it is first needed. That is how command injection, secret leakage and
``rm -rf`` incidents happen. So this module is the **only** place in the
package that spawns a process, which makes the security properties of
executing code auditable with a single grep:

    grep -rn subprocess orchestrator/

Every property below exists because it was a way to abuse that capability:

``shell=False`` always
    Never a shell string. Arguments arrive as a list and are passed as a list,
    so no argument can ever be re-parsed as shell syntax.

Allowlist before spawn
    An executable not on ``config.deploy_allowlist()`` is refused *before* the
    process is created. An empty allowlist therefore means nothing runs at all —
    the safe default, since the runtime is offline by design.

Forced working directory
    ``cwd`` is pinned to the project root regardless of what the agent asked
    for, so a build cannot be pointed at ``/`` or at ``$HOME``.

Scrubbed environment
    The child inherits a minimal allowlist, not the orchestrator's environment.
    Without this, ``ANTHROPIC_API_KEY`` and ``CHECKPOINT_SIGNING_KEY`` would be
    readable by any spawned build script — the "why did my key end up in that
    build log" incident.

Bounded output and bounded time
    stdout/stderr are capped per stream and the child is killed on timeout.
    Without the cap, one chatty test suite can exhaust memory; without the
    timeout, a hung build pins a dispatch worker for the rest of the session.

``executed: false`` is a first-class outcome
    An unconfigured or refused invocation returns a record with
    ``executed=False`` and a reason rather than nothing at all. This is what
    makes "NOT RUN" an honest, checkable state instead of a silent gap: the DoD
    can tell the difference between "ran and passed" and "never ran", which is
    precisely the distinction the framework previously could not make.

Trust model
-----------
An agent may propose *what* to run. It may not decide *whether* it ran. The
runner is the authority: it stamps ``executed``, the exit code and the captured
output itself, and the DoD compares that against what the agent claimed. A
model claiming a pass it did not obtain produces a record whose exit code
contradicts it, and the task is refused.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import config
from .path_policy import PathPolicyError, resolve_inside

logger = logging.getLogger(__name__)


class DeployError(RuntimeError):
    """The deployment channel could not satisfy a request.

    Distinct from a *failed* command: a command that runs and exits non-zero is
    a normal, reportable outcome, not an error of the channel itself.
    """


#: Environment variables passed to the child when present. Everything else is
#: dropped. Kept short deliberately: a build tool needs a PATH and a HOME, not
#: the orchestrator's secrets.
ENV_ALLOWLIST: Sequence[str] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "USER",
    "SHELL",
    "TERM",
)

#: Result codes used in the evidence record. Distinct from a process exit code
#: so "the channel refused" is never mistaken for "the build failed".
STATUS_EXECUTED = "executed"
STATUS_REFUSED = "refused"
STATUS_TIMEOUT = "timeout"
STATUS_SPAWN_FAILED = "spawn_failed"
STATUS_ERROR = "error"
#: A command that was deliberately not run because it had nothing to do
#: (redundant install of an already-satisfied requirement). Never confused
#: with ``executed``: nothing happened, and no exit code exists.
STATUS_SKIPPED = "skipped"


@dataclass
class DeployRecord:
    """Immutable evidence of one invocation attempt.

    ``executed`` is the field the Definition of Done trusts, because only this
    module sets it. An agent cannot assert it — it can only propose a command
    and then be checked against what actually happened.
    """

    command: str
    args: List[str] = field(default_factory=list)
    cwd: str = ""
    exit_code: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: int = 0
    artifact_path: Optional[str] = None
    executed: bool = False
    status: str = STATUS_REFUSED
    reason: str = ""
    declared_expect: str = ""
    expect_matched: Optional[bool] = None

    @property
    def argv(self) -> List[str]:
        """Full argument vector, for display and evidence files."""
        return [self.command, *self.args]

    @property
    def succeeded(self) -> bool:
        """True only when the process really ran and exited zero."""
        return self.executed and self.exit_code == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "duration_ms": self.duration_ms,
            "artifact_path": self.artifact_path,
            "executed": self.executed,
            "status": self.status,
            "reason": self.reason,
            "declared_expect": self.declared_expect,
            "expect_matched": self.expect_matched,
        }

    def render(self) -> str:
        """Human-readable transcript for the agent prompt and evidence file."""
        lines = [
            f"$ {' '.join(shlex.quote(part) for part in self.argv)}",
            f"executed: {self.executed} | status: {self.status} | exit_code: {self.exit_code}",
        ]
        if self.reason:
            lines.append(f"reason: {self.reason}")
        if self.declared_expect:
            lines.append(f"declared expectation: {self.declared_expect} (matched: {self.expect_matched})")
        if self.stdout_tail:
            suffix = " [truncated]" if self.stdout_truncated else ""
            lines.append(f"--- stdout{suffix} ---")
            lines.append(self.stdout_tail)
        if self.stderr_tail:
            suffix = " [truncated]" if self.stderr_truncated else ""
            lines.append(f"--- stderr{suffix} ---")
            lines.append(self.stderr_tail)
        return "\n".join(lines)


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    """Decode and cap one output stream.

    The **tail** is kept, not the head: the last lines of a failing build or test
    are the diagnostic ones. Truncation is reported rather than silent, because
    an agent reasoning over a silently-short transcript will draw wrong
    conclusions from it.
    """
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="replace"), False
    tail = raw[-limit:]
    return tail.decode("utf-8", errors="replace"), True


def _scrubbed_environment() -> Dict[str, str]:
    """Build the minimal environment handed to the child process."""
    environment: Dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value:
            environment[name] = value
    # A deterministic locale keeps compiler and test output stable, which makes
    # the stored evidence comparable across runs.
    environment.setdefault("LANG", "C.UTF-8")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Refuse any implicit proxy inheritance from the orchestrator's shell.
    for proxy in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        environment.pop(proxy, None)
    return environment


def _resolve_project_script(search_path: Path, raw: str) -> Optional[str]:
    """Resolve a project-relative script to an executable file inside the project.

    Uses plain resolution plus a containment check rather than
    :func:`resolve_inside`, because the shared path policy deliberately rejects
    ``.`` segments and leading ``./`` — correct for state-file manifest names,
    where every legal name is a bare filename from ``config.STATE_FILES``, but
    wrong for a command, where ``./scripts/test.sh`` is the normal spelling.

    The containment check is done after ``Path.resolve()``, so it also catches
    a symlink inside the project pointing outside it.
    """
    if "\x00" in raw:
        return None
    base = search_path.expanduser().resolve()
    candidate = (base / raw).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        return None
    if not candidate.is_file():
        return None
    if not os.access(candidate, os.X_OK):
        return None
    return str(candidate)


def _resolve_executable(
    command: str,
    allowlist: Sequence[str],
    search_path: Optional[Path] = None,
) -> Optional[str]:
    """Resolve ``command`` to an absolute path, or ``None`` if not permitted.

    Resolution order:

    1. A project-relative path (``./scripts/test.sh``) resolves inside the
       project. A repo-shipped test harness is the common case and is not on
       PATH, so consulting PATH alone would refuse it.
    2. Otherwise PATH lookup via :func:`shutil.which`.

    The allowlist comparison uses the *basename* on both sides, so
    ``/usr/bin/gcc`` and ``gcc`` grant the same permission. That means the
    allowlist trusts PATH — documented rather than hidden, since allowlisting
    ``gcc`` is allowing whatever ``gcc`` currently resolves to. The relative
    branch is additionally confined to the project directory, so a traversal
    cannot use the allowlist to reach a binary outside it.
    """
    raw = str(command).strip()
    if not raw:
        return None
    requested = os.path.basename(raw)
    permitted = {os.path.basename(str(item)) for item in allowlist}
    if requested not in permitted:
        return None

    looks_like_path = raw.startswith(("./", "../")) or "/" in raw
    if looks_like_path:
        if search_path is None:
            return None
        candidate = _resolve_project_script(search_path, raw)
        if candidate is not None:
            return candidate
        return None

    resolved = shutil.which(requested, path=os.environ.get("PATH"))
    if resolved and os.path.isfile(resolved):
        return resolved
    return None


# ---------------------------------------------------------------------------
# PEP 668 — externally managed environments, and installs that are already done
# ---------------------------------------------------------------------------

#: Marker file that makes an interpreter refuse an in-place ``pip install``
#: unless ``--break-system-packages`` is passed (PEP 668). Debian/Ubuntu put
#: it in ``lib/pythonX.Y/``; some distributions put it next to the binary.
_PEP668_MARKER = "EXTERNALLY-MANAGED"

#: pip options whose value is the *next* token. Needed to walk an install
#: command without mistaking a value for a requirement.
_PIP_VALUE_OPTIONS = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-e",
        "--editable",
        "-i",
        "--index-url",
        "--extra-index-url",
        "--trusted-host",
        "-f",
        "--find-links",
        "-d",
        "--dest",
        "-t",
        "--target",
        "--root",
        "--prefix",
        "--platform",
        "--implementation",
        "--python-version",
        "--abi",
        "--cache-dir",
        "--log",
        "--timeout",
        "--retries",
        "--progress-bar",
        "--config-settings",
        "-C",
        "--python",
        "--report",
        "--no-binary",
        "--only-binary",
        "--upgrade-strategy",
        "--use-feature",
        "--use-deprecated",
        "--proxy",
        "--cert",
        "--client-cert",
    }
)

#: pip flags that change nothing about *what* gets installed, so they do not
#: make the satisfaction check ambiguous.
_PIP_HARMLESS_FLAGS = frozenset(
    {
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "--no-input",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--prefer-binary",
        "--no-warn-script-location",
        "--no-warn-conflicts",
        "--break-system-packages",
        "--no-break-system-packages",
    }
)

#: Options that make an install either move elsewhere (``--target``) or
#: deliberately redo work (``--upgrade``, ``--force-reinstall``): running them
#: is the point, so no satisfaction check may short-circuit them.
_PIP_UNSKIPABLE = frozenset(
    {
        "-e",
        "--editable",
        "-t",
        "--target",
        "--root",
        "--prefix",
        "-c",
        "--constraint",
        "-U",
        "--upgrade",
        "--force-reinstall",
        "--ignore-installed",
        "--ignore-requires-python",
        "--no-deps",
        "--pre",
        "--require-hashes",
    }
)


class _UnverifiableInstall(Exception):
    """This install cannot be proven redundant — it must run."""


def pep668_marker_candidates(executable: Optional[str] = None) -> List[Path]:
    """Where an ``EXTERNALLY-MANAGED`` marker could live for ``executable``.

    Derived from the *target* interpreter only: the environment pip writes
    into decides whether PEP 668 applies, not the one the orchestrator
    happens to run in. Checked locations:

    * next to the binary and its parent (``/usr/bin``, ``/usr``),
    * ``<prefix>/lib/python*/EXTERNALLY-MANAGED``, which is where
      Debian/Ubuntu keep it for ``/usr/bin/pip`` and ``/usr/bin/python3``.
    """
    raw = str(executable or "").strip() or sys.executable
    try:
        resolved = Path(raw).resolve()
    except OSError:  # pragma: no cover - defensive
        resolved = Path(raw)
    prefixes = [resolved.parent, resolved.parent.parent]
    candidates: List[Path] = []
    seen: set = set()
    for prefix in prefixes:
        for path in (prefix / _PEP668_MARKER, prefix / "lib" / _PEP668_MARKER):
            if str(path) not in seen:
                seen.add(str(path))
                candidates.append(path)
        lib = prefix / "lib"
        if lib.is_dir():
            for child in sorted(lib.glob("python*")):
                marker = child / _PEP668_MARKER
                if str(marker) not in seen:
                    seen.add(str(marker))
                    candidates.append(marker)
    return candidates


def is_pep668_managed(executable: Optional[str] = None) -> bool:
    """True when ``pip install`` there needs ``--break-system-packages``."""
    for candidate in pep668_marker_candidates(executable):
        try:
            if candidate.is_file():
                return True
        except OSError:  # pragma: no cover - defensive
            continue
    return False


def pip_targets_running_environment(executable: Optional[str]) -> bool:
    """True when ``executable`` installs into *this* interpreter's environment.

    Only then can :func:`importlib.metadata` answer "already installed":
    checking the running interpreter while pip would write into a venv (or
    via ``--target``) would skip an install that was never performed.
    """
    raw = str(executable or "").strip() or sys.executable
    try:
        target = Path(raw).resolve()
        prefix = Path(sys.prefix).resolve()
    except OSError:  # pragma: no cover - defensive
        return False
    if target == Path(sys.executable).resolve():
        return True
    return prefix == target or prefix in target.parents


def is_pip_install(executable: Optional[str], args: Sequence[str]) -> bool:
    """True for ``pip install ...`` and ``python -m pip install ...``."""
    return _install_tail(executable, args) is not None


def _install_tail(executable: Optional[str], args: Sequence[str]) -> Optional[List[str]]:
    """Tokens after the ``install`` subcommand, or ``None`` if not an install."""
    tokens = [str(arg) for arg in args]
    base = os.path.basename(str(executable or "")).lower()
    for suffix in (".exe", ".bat", ".cmd"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    if base.startswith("pip"):
        rest = tokens
    else:
        if "-m" not in tokens:
            return None
        position = tokens.index("-m")
        if position + 1 >= len(tokens) or tokens[position + 1] != "pip":
            return None
        rest = tokens[position + 2 :]
    if not rest or rest[0] != "install":
        return None
    return rest[1:]


def _distribution_version(name: str) -> Optional[str]:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return str(metadata.version(name))
    except Exception:  # noqa: BLE001 - absence and bad names both mean "unknown"
        return None


def _requirement_satisfied(spec: str) -> Optional[str]:
    """Prove one requirement is already present, or return ``None``.

    Deliberately narrow: bare names and exact ``==`` pins only. Ranges,
    extras, markers, URLs and VCS specs are *not* reasoned about — an
    unprovable requirement just means pip runs, which is the old behaviour.
    """
    text = str(spec or "").strip()
    if not text or text.startswith("#"):
        return None
    if any(token in text for token in (";", "#", "://", "git+", "@")):
        return None
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", text)
    if not match:
        return None
    name, extras, rest = match.group(1), match.group(2) or "", match.group(3).strip()
    if extras:
        return None  # extras change the dependency set; cannot be verified here
    if not rest:
        version = _distribution_version(name)
        return f"{name} (installed {version})" if version else None
    if rest.startswith("=="):
        wanted = rest[2:].strip()
        if not wanted or "*" in wanted or any(ch in wanted for ch in "<>!~"):
            return None
        version = _distribution_version(name)
        if version is None:
            return None
        return f"{name}=={wanted}" if version == wanted else None
    return None


def _requirement_file_specs(raw_path: str, project_path: Optional[Path]) -> List[str]:
    """Requirements declared by ``pip install -r <file>`` (project-internal)."""
    raw = str(raw_path or "").strip()
    if not raw or raw.startswith("-"):
        raise _UnverifiableInstall(f"unusable requirement path: {raw_path!r}")
    base = Path(project_path) if project_path else Path.cwd()
    try:
        if project_path is not None:
            path = resolve_inside(base, raw, context="pip -r")
        else:
            path = (base / raw).resolve()
            if base.resolve() not in path.parents:
                raise _UnverifiableInstall(f"requirement file outside {base}")
    except PathPolicyError as exc:
        raise _UnverifiableInstall(str(exc)) from exc
    if not path.is_file():
        raise _UnverifiableInstall(f"requirement file not found: {raw}")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise _UnverifiableInstall(f"requirement file unreadable: {exc}") from exc
    specs: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            # nested options (-e, --index-url, constraints): out of scope
            raise _UnverifiableInstall(f"unsupported option in {raw}: {stripped}")
        if ";" in stripped:
            raise _UnverifiableInstall(f"environment marker in {raw}: {stripped}")
        specs.append(stripped.split(" #", 1)[0].strip())
    return specs


def pip_requirements_satisfied(
    executable: Optional[str],
    args: Sequence[str],
    project_path: Optional[Path] = None,
) -> Optional[str]:
    """Reason string when every requirement is already installed here.

    Returns ``None`` — "run it" — for anything not provably redundant: a
    missing package, an unverifiable spec, an option that would upgrade or
    relocate the install, or a non-install command. Skipping is an
    optimisation; it must never change what the install would have done.
    """
    try:
        tail = _install_tail(executable, args)
    except Exception:  # noqa: BLE001 - never let analysis break a deploy
        return None
    if tail is None:
        return None
    specs: List[str] = []
    index = 0
    try:
        while index < len(tail):
            token = tail[index]
            if not token.startswith("-"):
                specs.append(token)
                index += 1
                continue
            name = token.split("=", 1)[0]
            if name in _PIP_UNSKIPABLE:
                return None
            if name in _PIP_VALUE_OPTIONS:
                value = tail[index + 1] if index + 1 < len(tail) else ""
                if not value or value.startswith("-"):
                    return None
                if name in ("-r", "--requirement"):
                    specs.extend(_requirement_file_specs(value, project_path))
                index += 2
                continue
            if name in _PIP_HARMLESS_FLAGS:
                index += 1
                continue
            return None  # unknown option: reason about nothing
    except _UnverifiableInstall as exc:
        logger.debug("deploy: install not skippable (%s)", exc)
        return None
    if not specs:
        return None
    satisfied: List[str] = []
    for spec in specs:
        proof = _requirement_satisfied(spec)
        if proof is None:
            return None
        satisfied.append(proof)
    return "requirements already satisfied: " + ", ".join(satisfied)


class DeployRunner:
    """Executes allowlisted commands on behalf of agents.

    One instance per orchestrator. Holds no mutable execution state, so it is
    safe to share across the dispatch thread pool.
    """

    def __init__(
        self,
        project_path: str | Path,
        allowlist: Optional[Sequence[str]] = None,
        enabled: Optional[bool] = None,
        timeout: Optional[float] = None,
        max_output_bytes: Optional[int] = None,
        evidence_dir: Optional[str] = None,
        externally_managed: Optional[bool] = None,
    ) -> None:
        self.project_path = Path(project_path).expanduser().resolve()
        self._explicit_allowlist = tuple(allowlist) if allowlist is not None else None
        self._explicit_enabled = enabled
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._evidence_dir = evidence_dir
        #: PEP 668 override; ``None`` means detect from the target binary.
        self._explicit_externally_managed = externally_managed

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------

    @property
    def externally_managed(self) -> Optional[bool]:
        """PEP 668 override, or ``None`` when detected per command."""
        return self._explicit_externally_managed

    @property
    def allowlist(self) -> Sequence[str]:
        if self._explicit_allowlist is not None:
            return self._explicit_allowlist
        return config.deploy_allowlist()

    @property
    def enabled(self) -> bool:
        """True only when the channel is on *and* something is allowlisted."""
        if self._explicit_enabled is not None:
            return bool(self._explicit_enabled) and bool(self.allowlist)
        return config.deploy_enabled()

    @property
    def timeout(self) -> float:
        if self._timeout is not None:
            return float(self._timeout)
        return config.deploy_timeout()

    @property
    def max_output_bytes(self) -> int:
        if self._max_output_bytes is not None:
            return int(self._max_output_bytes)
        return config.deploy_max_output_bytes()

    @property
    def evidence_root(self) -> Path:
        relative = self._evidence_dir or config.deploy_evidence_dir()
        return self.project_path / relative

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_invocations(payload: Any) -> List[Dict[str, Any]]:
        """Normalise ``data.deploy`` into a list of invocation dicts.

        Accepts the documented list-of-objects shape and tolerates a single
        object or a shell-ish string for convenience. A string is split with
        ``shlex`` but still passed to ``subprocess`` as a list, so splitting is
        a convenience and never a shell invocation.
        """
        if payload is None:
            return []
        if isinstance(payload, str):
            try:
                parts = shlex.split(payload)
            except ValueError:
                return []
            if not parts:
                return []
            return [{"command": parts[0], "args": parts[1:]}]
        if isinstance(payload, dict):
            return [payload]
        if not isinstance(payload, (list, tuple)):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _validate_invocation(self, invocation: Dict[str, Any]) -> tuple[str, List[str], str]:
        """Validate one invocation, returning ``(command, args, declared_expect)``.

        Raises :class:`DeployError` for anything malformed or forbidden. The
        caller converts that into an ``executed=False`` record, so a bad request
        is a reportable outcome rather than an exception escaping into dispatch.
        """
        raw_command = invocation.get("command") or invocation.get("cmd")
        command = str(raw_command or "").strip()
        if not command:
            raise DeployError("deploy invocation has no 'command'")
        if any(ch in command for ch in ("\x00", "\n", "\r")):
            raise DeployError(f"deploy command contains control characters: {command!r}")

        raw_args = invocation.get("args", [])
        if isinstance(raw_args, str):
            try:
                args = shlex.split(raw_args)
            except ValueError as exc:
                raise DeployError(f"deploy args are not parseable: {exc}") from exc
        elif isinstance(raw_args, (list, tuple)):
            args = []
            for item in raw_args:
                if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                    args.append(str(item))
                else:
                    raise DeployError(
                        f"deploy args must be strings, got {type(item).__name__}"
                    )
        else:
            raise DeployError(
                f"deploy 'args' must be a list or string, got {type(raw_args).__name__}"
            )

        if any("\x00" in arg for arg in args):
            raise DeployError("deploy args contain a NUL byte")

        requested_cwd = str(invocation.get("cwd") or "").strip()
        cwd = self.project_path
        if requested_cwd and requested_cwd not in (".", "./"):
            # Forced containment: a build cannot be aimed outside the project.
            try:
                cwd = resolve_inside(
                    self.project_path, requested_cwd, context="deploy cwd"
                )
            except PathPolicyError as exc:
                raise DeployError(f"deploy cwd is outside the project: {exc}") from exc
            if not cwd.is_dir():
                raise DeployError(f"deploy cwd does not exist: {requested_cwd}")

        declared_expect = str(invocation.get("expect") or "").strip().upper()

        resolved = _resolve_executable(command, self.allowlist, search_path=self.project_path)
        if resolved is None:
            raise DeployError(
                f"executable '{os.path.basename(command)}' is not on the deploy allowlist "
                f"({', '.join(self.allowlist) if self.allowlist else 'allowlist is empty'}); "
                "set ORCHESTRATOR_DEPLOY_ALLOWLIST to permit it"
            )

        return resolved, args, declared_expect

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_one(self, invocation: Dict[str, Any]) -> DeployRecord:
        """Execute a single invocation and return its evidence record.

        Never raises for an expected failure mode — a refused executable, a
        timeout, a non-zero exit all come back as a record with ``executed``
        set honestly, which is what lets the DoD distinguish "ran" from
        "claimed".
        """
        declared_command = str(invocation.get("command") or invocation.get("cmd") or "").strip()
        declared_args_raw = invocation.get("args", [])
        declared_args = (
            [str(item) for item in declared_args_raw]
            if isinstance(declared_args_raw, (list, tuple))
            else []
        )

        if not self.enabled:
            return DeployRecord(
                command=declared_command or "<none>",
                args=declared_args,
                cwd=str(self.project_path),
                executed=False,
                status=STATUS_REFUSED,
                reason=(
                    "deployment channel is disabled: set ORCHESTRATOR_DEPLOY_ENABLED=1 and "
                    "ORCHESTRATOR_DEPLOY_ALLOWLIST to allow execution"
                ),
            )

        try:
            executable, args, declared_expect = self._validate_invocation(invocation)
        except DeployError as exc:
            return DeployRecord(
                command=declared_command or "<none>",
                args=declared_args,
                cwd=str(self.project_path),
                executed=False,
                status=STATUS_REFUSED,
                reason=str(exc),
            )

        command = os.path.basename(executable) or executable

        # PEP 668 + redundant installs. Skip runs first: a command with
        # nothing to do gets no flag and no process.
        if is_pip_install(executable, args):
            skip_reason = pip_requirements_satisfied(
                executable, args, project_path=self.project_path
            )
            if skip_reason:
                logger.info("deploy: skipping '%s' — %s", command, skip_reason)
                config.emit_telemetry(
                    config.TELEMETRY_EVENT_DEPLOY,
                    level="INFO",
                    command_name=command,
                    executed=False,
                    status=STATUS_SKIPPED,
                    argv=[executable, *args],
                )
                return DeployRecord(
                    command=command,
                    args=args,
                    cwd=str(self.project_path),
                    exit_code=None,
                    executed=False,
                    status=STATUS_SKIPPED,
                    reason=skip_reason,
                    declared_expect=declared_expect,
                    expect_matched=None,
                )
            managed = (
                self._explicit_externally_managed
                if self._explicit_externally_managed is not None
                else is_pep668_managed(executable)
            )
            if managed and not any(
                arg in ("--break-system-packages", "--no-break-system-packages")
                for arg in args
            ):
                logger.info(
                    "deploy: '%s' runs in an externally managed environment; "
                    "appending --break-system-packages",
                    command,
                )
                args = [*args, "--break-system-packages"]

        argv = [executable, *args]
        environment = _scrubbed_environment()
        limit = self.max_output_bytes

        logger.info(
            "deploy: running '%s' (cwd=%s, timeout=%ss)",
            " ".join(shlex.quote(part) for part in argv),
            self.project_path,
            self.timeout,
        )

        started = os.times()
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, shell=False, allowlisted
                argv,
                cwd=str(self.project_path),
                env=environment,
                capture_output=True,
                timeout=self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = self._elapsed_ms(started)
            partial_out = self._coerce_stream(exc.stdout)
            partial_err = self._coerce_stream(exc.stderr)
            stdout_text, stdout_trunc = _truncate(partial_out, limit)
            stderr_text, stderr_trunc = _truncate(partial_err, limit)
            logger.warning("deploy: '%s' timed out after %ss", command, self.timeout)
            return DeployRecord(
                command=command,
                args=args,
                cwd=str(self.project_path),
                exit_code=None,
                stdout_tail=stdout_text,
                stderr_tail=stderr_text,
                stdout_truncated=stdout_trunc,
                stderr_truncated=stderr_trunc,
                duration_ms=elapsed,
                executed=False,
                status=STATUS_TIMEOUT,
                reason=f"command exceeded the {self.timeout}s timeout and was terminated",
            )
        except (OSError, ValueError) as exc:
            logger.warning("deploy: '%s' could not be spawned: %s", command, exc)
            return DeployRecord(
                command=command,
                args=args,
                cwd=str(self.project_path),
                duration_ms=self._elapsed_ms(started),
                executed=False,
                status=STATUS_SPAWN_FAILED,
                reason=f"could not start process: {exc}",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("deploy: unexpected failure for '%s': %s", command, exc)
            return DeployRecord(
                command=command,
                args=args,
                cwd=str(self.project_path),
                duration_ms=self._elapsed_ms(started),
                executed=False,
                status=STATUS_ERROR,
                reason=f"unexpected runner error: {type(exc).__name__}: {exc}",
            )

        elapsed = self._elapsed_ms(started)
        stdout_text, stdout_trunc = _truncate(completed.stdout or b"", limit)
        stderr_text, stderr_trunc = _truncate(completed.stderr or b"", limit)

        expect_matched: Optional[bool] = None
        if declared_expect:
            expect_matched = self._match_expect(declared_expect, completed.returncode)

        record = DeployRecord(
            command=command,
            args=args,
            cwd=str(self.project_path),
            exit_code=int(completed.returncode),
            stdout_tail=stdout_text,
            stderr_tail=stderr_text,
            stdout_truncated=stdout_trunc,
            stderr_truncated=stderr_trunc,
            duration_ms=elapsed,
            executed=True,
            status=STATUS_EXECUTED,
            reason="",
            declared_expect=declared_expect,
            expect_matched=expect_matched,
        )
        # GAP-MED-01: one structured event per real invocation. Only the shape
        # and the outcome are recorded — never stdout/stderr, which routinely
        # contain build paths, environment echoes, and occasionally credentials.
        config.emit_telemetry(
            config.TELEMETRY_EVENT_DEPLOY,
            level="INFO",
            command_name=command,
            executed=True,
            exit_code=record.exit_code,
            duration_ms=elapsed,
            expect_matched=expect_matched,
            argv=record.argv,
        )
        logger.info(
            "deploy: '%s' exited %s in %dms",
            command,
            record.exit_code,
            elapsed,
        )
        return record

    @staticmethod
    def _elapsed_ms(started: Any) -> int:
        """Elapsed wall-clock ms since ``started`` (an ``os.times()`` snapshot)."""
        elapsed = os.times()
        children = max(0.0, elapsed.children_user - started.children_user)
        children_sys = max(0.0, elapsed.children_system - started.children_system)
        user = elapsed.user - started.user
        system = elapsed.system - started.system
        total = (children + children_sys + user + system) * 1000.0
        return int(max(0.0, total))

    @staticmethod
    def _coerce_stream(value: Any) -> bytes:
        """Normalise a captured stream (bytes or str, possibly None) to bytes."""
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8", errors="replace")
        return str(value).encode("utf-8", errors="replace")

    @staticmethod
    def _match_expect(declared: str, exit_code: int) -> bool:
        """Compare the agent's declared expectation against the real exit code."""
        if declared in ("PASS", "SUCCESS", "0", "OK"):
            return exit_code == 0
        if declared in ("FAIL", "FAILURE", "NONZERO", "NON_ZERO", "1"):
            return exit_code != 0
        return exit_code == 0

    def run_payload(self, payload: Any) -> List[DeployRecord]:
        """Execute every invocation in an agent's ``data.deploy`` payload."""
        records: List[DeployRecord] = []
        for invocation in self._coerce_invocations(payload):
            records.append(self.run_one(invocation))
        return records

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    def write_evidence(self, task_id: str, record: DeployRecord, index: int = 0) -> Optional[str]:
        """Persist a transcript under ``docs/evidence/<task>/``.

        Returns the project-relative path, or ``None`` when the write fails —
        evidence is valuable but must never take down a dispatch.
        """
        safe_task = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in str(task_id)
        )
        directory = self.evidence_root / safe_task
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("deploy: cannot create evidence dir %s: %s", directory, exc)
            return None
        # The command may be a path ("./scripts/test.sh"); only the basename
        # becomes a filename, and separators are flattened, so the evidence
        # path is always a single well-formed file inside `directory`.
        safe_command = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in os.path.basename(record.command or "command")
        ).lstrip(".") or "command"
        path = directory / f"{index:02d}-{safe_command}.json"
        payload = record.to_dict()
        payload["task_id"] = str(task_id)
        payload["transcript"] = record.render()
        payload["recorded_at"] = _utc_now()
        try:
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("deploy: cannot write evidence %s: %s", path, exc)
            return None
        return str(path.relative_to(self.project_path))

    @staticmethod
    def has_ground_truth(records: Sequence[DeployRecord]) -> bool:
        """True when at least one record proves something actually executed.

        Note this is *not* the same as success: a real non-zero exit is ground
        truth (the build genuinely failed), which is exactly the information a
        fabricated report destroys.
        """
        return any(record.executed for record in records)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


__all__ = [
    "DeployError",
    "DeployRecord",
    "DeployRunner",
    "ENV_ALLOWLIST",
    "STATUS_EXECUTED",
    "STATUS_REFUSED",
    "STATUS_TIMEOUT",
    "STATUS_SPAWN_FAILED",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "is_pep668_managed",
    "is_pip_install",
    "pep668_marker_candidates",
    "pip_requirements_satisfied",
    "pip_targets_running_environment",
]
