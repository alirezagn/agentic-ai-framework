"""StateManager — atomic read/update/save routines for project state files.

The project directory is the single source of truth. Every write goes through
an atomic replace (write to a temporary file in the same directory, fsync,
then ``os.replace``) so a crash can never leave a half-written YAML or
Markdown file behind.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import yaml

try:  # POSIX advisory locking; absent on Windows.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

from . import config
from .context_monitor import context_window_tokens, utilization_from_tokens
from .config import (
    CHANGELOG_FILE,
    CURRENT_STATE_FILE,
    DECISIONS_FILE,
    MEMORY_FILE,
    PROJECT_FILE,
    RISKS_FILE,
    SATISFIED_DEPENDENCY_STATUSES,
    TASKS_FILE,
    priority_rank,
)


class StateError(RuntimeError):
    """Base error for state management failures."""


class StateFileMissingError(StateError):
    """A required state file does not exist on disk."""


class StateCorruptedError(StateError):
    """A state file exists but cannot be parsed."""


class TaskNotFoundError(StateError):
    """The requested task id is not present in TASKS.yaml."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def derive_initial_status(dependencies: Sequence[Any]) -> str:
    """Status a freshly created task starts in, derived from its dependencies.

    GAP-CRIT-05. A task's status on creation is a *fact about the graph*, not
    something an author or a model may assert:

    * no dependencies → ``TODO`` (pending). It has never run, so it cannot be
      IN_PROGRESS or any terminal state. ``refresh_ready_states`` promotes it
      to ``READY`` on the next pass.
    * one or more dependencies → ``BLOCKED``. Those dependencies cannot be
      satisfied yet by definition, so ``TODO`` would be a lie the ready-set
      scan has to correct anyway.

    Deliberately *not* derivable to ``READY``: an empty graph with no
    dependency edges would then dispatch work the moment it was ingested,
    which is the same "assert your way past the gate" problem one step later.
    ``refresh_ready_states()`` owns the TODO/BLOCKED → READY promotion, so the
    transition stays in exactly one place.

    Accepts any iterable of anything (``Sequence[Any]``) because callers hold
    dependency lists that have been normalised to strings at varying points in
    the ingestion path; only emptiness matters here.
    """
    return config.TASK_BLOCKED if list(dependencies) else config.TASK_TODO


#: Fields a model must never be able to set on a task it creates. ``status`` is
#: derived by :func:`derive_initial_status`; ``execution`` holds loop/attempt
#: counters that gate dispatch; ``review.status`` is written by the review flow.
UNTRUSTED_TASK_FIELDS: Tuple[str, ...] = ("status", "execution", "review")


def strip_untrusted_task_fields(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``spec`` with runtime-state fields removed.

    Defence in depth for GAP-CRIT-05, applied at the ingestion boundary before
    :meth:`StateManager.append_task` sees the payload. ``append_task`` derives
    the status itself, so this is not the only thing standing between a planner
    and a self-approved task — it exists so the sanitisation is explicit and
    observable at the boundary where untrusted data enters, rather than implied
    somewhere downstream.

    ``review`` is handled rather than blanket-dropped: ``review.required`` is
    legitimate planning input (it is in the planner's whitelist), while
    ``review.status`` is review-flow output. So the key is rebuilt with only the
    permitted sub-keys.
    """
    if not isinstance(spec, dict):
        raise StateError("Task spec must be a mapping")
    cleaned: Dict[str, Any] = {
        key: value for key, value in spec.items() if key not in UNTRUSTED_TASK_FIELDS
    }
    review = spec.get("review")
    if isinstance(review, dict):
        permitted_review = {
            key: value for key, value in review.items() if key == "required"
        }
        if permitted_review:
            cleaned["review"] = permitted_review
    return cleaned


# Map of task owner -> PROJECT.yaml progress key (workstream).
OWNER_PROGRESS_KEY: Dict[str, str] = {
    "requirements_agent": "requirements",
    "research_agent": "research",
    "architecture_agent": "architecture",
    "planning_agent": "planning",
    "software_agent": "implementation",
    "hardware_agent": "implementation",
    "test_agent": "testing",
    "documentation_agent": "documentation",
}


def _index_tasks(tasks: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map task id -> task, coercing ids to ``str`` so lookups always match.

    ``dependency_ids`` yields strings, so a YAML-parsed numeric id would
    otherwise never match and every dependent task would look permanently
    blocked. Built once per call site (GAP-HIGH-03) instead of per task.
    """
    return {str(task.get("id")): task for task in tasks}


def _dependencies_satisfied_with(
    task: Dict[str, Any],
    tasks: Sequence[Dict[str, Any]],
    index: Dict[str, Dict[str, Any]],
) -> bool:
    """Dependency check against a pre-built index (see :func:`_index_tasks`)."""
    for dep_id in _dependency_ids_of(task):
        dep = index.get(dep_id)
        if dep is None:
            return False
        if str(dep.get("status")) not in SATISFIED_DEPENDENCY_STATUSES:
            return False
    return True


def _dependency_ids_of(task: Dict[str, Any]) -> List[str]:
    """Normalised dependency ids for ``task`` (``dependency_ids`` as a function).

    The method form defers to this so the fast path and the public API cannot
    drift apart.
    """
    raw = task.get("dependencies") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item) for item in raw if str(item).strip()]


def _status_breakdown_of(tasks: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Count tasks per status from an in-memory list (GAP-HIGH-03)."""
    breakdown: Dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status", "UNKNOWN"))
        breakdown[status] = breakdown.get(status, 0) + 1
    return breakdown


def derive_agent_status(owned: Sequence[Dict[str, Any]]) -> str:
    """Derive a PROJECT.yaml ``agents`` entry from the tasks an owner holds."""
    statuses = [str(task.get("status", "")) for task in owned]
    if not statuses:
        return "IDLE"
    if config.TASK_IN_PROGRESS in statuses:
        return "ACTIVE"
    if config.TASK_READY in statuses:
        return "READY"
    if config.TASK_BLOCKED in statuses:
        return "BLOCKED"
    if all(status in config.TERMINAL_TASK_STATUSES for status in statuses):
        return "COMPLETED"
    return "READY"


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically, under an advisory file lock.

    Sequence, in order:

    1. Serialize happens *before* any file is touched (the caller does this, and
       :func:`atomic_dump_yaml` enforces it), so a YAML error cannot leave a
       truncated state file behind.
    2. A sibling lock file is taken with :func:`_file_lock`. It is a **separate
       file**, not the target, because ``os.replace`` swaps the inode: locking
       the target would release the moment the rename lands and would not
       exclude a second writer that is mid-replace.
    3. Write to a temp file in the *same directory* (so ``os.replace`` is a
       same-filesystem rename, which is atomic), ``fsync`` the file, then
       ``os.replace`` it over the target.
    4. ``fsync`` the parent directory so the rename itself is durable. Without
       this a power loss can leave the directory entry pointing at the old
       inode even though the file contents were fsynced — the file "reverts",
       which for a state store means silently losing a task transition.

    The lock is advisory and best-effort: platforms without :mod:`fcntl`
       (Windows) fall back to no locking, because a hard failure there would
       break the runtime outright. Callers must therefore still assume a
       last-writer-wins outcome; the lock narrows the window, it does not
       close it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            _fsync_directory(path.parent)
        except BaseException:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise


def _safe_error_text(exc: BaseException, limit: int = 300) -> str:
    """Render an exception's message without letting rendering itself fail.

    PyYAML raises ``RepresenterError("<exception str() failed>")`` when the
    offending value's ``__repr__`` raises. Building the new error message then
    calls ``str()`` on that exception again, which re-enters the failing
    ``__repr__`` and lets the *original* error escape the handler — replacing a
    clear "cannot serialize" with an unrelated traceback, which is exactly the
    confusing failure this path exists to prevent.

    So: type name always, message only when it renders safely.
    """
    try:
        text = str(exc)
    except Exception:
        return "<message unavailable>"
    if not text:
        return "<no message>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


@contextmanager
def _document_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Hold the advisory lock for a state document across a read-modify-write.

    GAP-CRIT-06. A read-modify-write is only atomic if the *read* is inside the
    same critical section as the *write*. :func:`_file_lock` protects a single
    write; this wraps the whole cycle, which is what id allocation needs:

        with _document_lock(self.decisions_md):
            content = read()      # see every other writer's entry
            new_id = allocate(content)
            write(content + entry)

    Without this, two processes both read the file, both see no ``DEC-004``,
    both allocate ``DEC-004``, and the second ``os.replace`` silently discards
    the first decision.
    """
    with _file_lock(path, timeout=timeout):
        yield


def _append_text_locked(path: Path, block: str) -> str:
    """Append ``block`` to ``path`` atomically, returning the resulting text.

    GAP-CRIT-06. Uses ``O_APPEND`` under the document lock rather than
    read-concatenate-rewrite. Two reasons:

    * **Correctness.** The read no longer races the write, so concurrent
      appenders cannot lose each other's entries. A plain read-then-write of an
      append-only log is a lost-update bug by construction.
    * **Cost.** The old shape re-read and rewrote the whole file per entry, so
      a long session was O(n²) in bytes written. Appending is O(len(block)).
      These files are never edited in the middle, only extended.

    The trailing-newline fixup that the old code performed is kept, but done on
    the *file* (a one-byte read) rather than on the whole document. Single
    ``write()`` of a bounded block under ``O_APPEND`` is atomic on POSIX for
    regular files, so two processes cannot interleave a partial block.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        prefix = ""
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as probe:
                probe.seek(-1, os.SEEK_END)
                if probe.read(1) != b"\n":
                    prefix = "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(prefix + block)
            handle.flush()
            os.fsync(handle.fileno())
    return load_text_file(path)


def _fsync_directory(directory: Path) -> None:
    """``fsync`` a directory so a rename into it is durable.

    Best-effort: some filesystems refuse ``O_DIRECTORY`` fsync, and failing to
    persist a directory entry is not a reason to lose the write that already
    succeeded.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def _file_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Hold an advisory exclusive lock for ``path`` via a sibling ``.lock`` file.

    The lock is taken on ``<name>.lock`` rather than ``<name>`` itself because
    :func:`os.replace` installs a new inode: a lock held on the old inode is
    released by the rename, so a concurrent writer could slip in during exactly
    the window the lock was meant to cover.

    Two details keep this cheap and correct:

    * **Cached handle.** The ``.lock`` file is opened once per process and
      reused, and an in-process :class:`threading.Lock` guards it. Without the
      in-process guard, two threads in the same process each hold their own
      file descriptor for the same file, so ``flock`` treats them as distinct
      holders and the second one spins for the full timeout — which is how a
      20-second test appeared where a 20-millisecond one was expected.
    * **Re-entrancy.** Held as a per-path counter, so a nested
      :func:`_document_lock` on the same document (e.g. ``append_decision``
      taking the changelog lock while holding the decisions lock) is free
      instead of self-deadlocking.

    Yields immediately when :mod:`fcntl` is unavailable, or when the lock cannot
    be acquired within ``timeout`` — a stuck lock must not wedge the
    orchestrator, and every write path remains individually atomic regardless.
    """
    if fcntl is None:
        yield
        return
    lock_path = path.with_name(f".{path.name}.lock")
    state = _lock_state(lock_path)
    with state.thread_lock:
        depth = state.depth
        if depth == 0:
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                if state.handle is None or state.handle.closed:
                    state.handle = open(lock_path, "a+", encoding="utf-8")
            except OSError:
                yield
                return
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(state.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        logger.warning(
                            "proceeding without lock for %s after %.1fs",
                            path,
                            timeout,
                        )
                        break
                    time.sleep(0.005)
        state.depth = depth + 1
        try:
            yield
        finally:
            state.depth = depth
            if depth == 0 and state.handle is not None and not state.handle.closed:
                try:
                    fcntl.flock(state.handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


class _LockState:
    """Per-lock-file process state: one fd, one in-process mutex, one depth."""

    __slots__ = ("handle", "thread_lock", "depth")

    def __init__(self) -> None:
        self.handle: Optional[Any] = None
        # Reentrant by necessity: _document_lock holds this lock across the
        # critical section, and the write inside that section calls
        # atomic_write_text -> _file_lock again for the same document. A plain
        # Lock self-deadlocks there. Only the owning thread can be inside, so the
        # shared `depth` counter is safe without being per-thread.
        self.thread_lock = threading.RLock()
        self.depth = 0


#: Keyed by resolved lock path. Process-wide; the orchestrator is single-process
#: per project, so this stays small.
_LOCK_STATES: Dict[str, _LockState] = {}


def _lock_state(lock_path: Path) -> _LockState:
    key = str(lock_path)
    state = _LOCK_STATES.get(key)
    if state is None:
        state = _LockState()
        _LOCK_STATES[key] = state
    return state


def atomic_dump_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Serialize ``data`` to YAML and write it atomically under a file lock.

    GAP-HIGH-01/HIGH-02. This is the only supported way to persist a YAML state
    document. Two properties matter and neither is provided by a bare
    ``open(path, "w")``:

    * **Serialize first.** ``yaml.safe_dump`` runs to completion (and is
      converted to :class:`StateCorruptedError` on failure) before a single byte
      reaches the filesystem. A direct write truncates the file and then fails
      mid-dump, leaving unparseable YAML where the only copy of the task graph
      used to be.
    * **Atomic swap.** The bytes land in a temp file and are renamed over the
      target, so a reader never observes a partially written document and a
      crash cannot leave a half-written one.
    """
    try:
        text = yaml.safe_dump(
            data, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
    except BaseException as exc:
        # Any serialisation failure — an unserialisable value, a recursive
        # structure, or a value whose __repr__ raises — must abort here, while
        # the target file is still untouched. Catching broadly is deliberate:
        # the invariant being protected ("serialize before touching disk") is
        # more important than classifying the error precisely, and
        # StateCorruptedError is a StateError so every existing handler works.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise StateCorruptedError(
            f"Cannot serialize data for {path}: {type(exc).__name__}: "
            f"{_safe_error_text(exc)}"
        ) from exc
    atomic_write_text(Path(path), text)


def load_yaml_file(path: Path) -> Dict[str, Any]:
    """Load a YAML document, raising typed errors on failure."""
    path = Path(path)
    if not path.exists():
        raise StateFileMissingError(f"State file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise StateCorruptedError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise StateCorruptedError(f"Expected a mapping at the top of {path}")
    return data


def save_yaml_file(path: Path, data: Dict[str, Any]) -> None:
    """Serialize ``data`` to YAML and write it atomically under a file lock.

    Thin alias for :func:`atomic_dump_yaml`, kept as the historical public name
    so existing callers and tests need no change.
    """
    atomic_dump_yaml(Path(path), data)


def load_text_file(path: Path) -> str:
    """Load a text file, returning '' when it does not exist."""
    path = Path(path)
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def save_text_file(path: Path, content: str) -> None:
    atomic_write_text(Path(path), content)


class StateManager:
    """Load, mutate and persist the seven canonical project state files."""

    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path).expanduser().resolve()
        if not self.project_path.exists():
            raise StateError(f"Project path does not exist: {project_path}")
        if not self.project_path.is_dir():
            raise StateError(f"Project path is not a directory: {project_path}")

        self.project_yaml = self.project_path / PROJECT_FILE
        self.tasks_yaml = self.project_path / TASKS_FILE
        self.memory_md = self.project_path / MEMORY_FILE
        self.current_state_md = self.project_path / CURRENT_STATE_FILE
        self.decisions_md = self.project_path / DECISIONS_FILE
        self.risks_md = self.project_path / RISKS_FILE
        self.changelog_md = self.project_path / CHANGELOG_FILE

    # ------------------------------------------------------------------
    # Project (PROJECT.yaml)
    # ------------------------------------------------------------------

    def load_project(self) -> Dict[str, Any]:
        return load_yaml_file(self.project_yaml)

    def save_project(self, data: Dict[str, Any]) -> None:
        data = dict(data)
        data["updated_at"] = utc_now_iso()
        save_yaml_file(self.project_yaml, data)

    def get_context_utilization(self) -> float:
        project = self.load_project()
        context = project.get("context") or {}
        try:
            return float(context.get("utilization_percent", 0))
        except (TypeError, ValueError):
            return 0.0

    def get_compaction_threshold(self) -> int:
        project = self.load_project()
        context = project.get("context") or {}
        try:
            return int(context.get("compaction_threshold", config.COMPACTION_THRESHOLDS.compaction_percent))
        except (TypeError, ValueError):
            return config.COMPACTION_THRESHOLDS.compaction_percent

    def get_critical_threshold(self) -> int:
        project = self.load_project()
        context = project.get("context") or {}
        try:
            return int(context.get("critical_threshold", config.COMPACTION_THRESHOLDS.critical_percent))
        except (TypeError, ValueError):
            return config.COMPACTION_THRESHOLDS.critical_percent

    def set_context_utilization(self, utilization_percent: float) -> Dict[str, Any]:
        project = self.load_project()
        context = dict(project.get("context") or {})
        pct = int(round(utilization_percent))
        context["utilization_percent"] = pct
        context["cumulative_tokens"] = int(round(context_window_tokens() * pct / 100.0))
        project["context"] = context
        self.save_project(project)
        return project

    def add_context_tokens(self, tokens: int) -> Dict[str, Any]:
        """Accumulate real token usage for the session (B2: never resets except on compaction)."""
        project = self.load_project()
        context = dict(project.get("context") or {})
        try:
            cumulative = int(context.get("cumulative_tokens", 0) or 0)
        except (TypeError, ValueError):
            cumulative = 0
        cumulative = max(0, cumulative + max(0, int(tokens)))
        context["cumulative_tokens"] = cumulative
        context["utilization_percent"] = int(
            round(utilization_from_tokens(cumulative))
        )
        project["context"] = context
        self.save_project(project)
        return project

    def reset_context_tokens(self) -> Dict[str, Any]:
        """Reset cumulative usage after a context compaction event."""
        project = self.load_project()
        context = dict(project.get("context") or {})
        context["cumulative_tokens"] = 0
        context["utilization_percent"] = 0
        context["compaction_required"] = False
        project["context"] = context
        self.save_project(project)
        return project

    def update_health(
        self,
        status: Optional[str] = None,
        blocked_tasks: Optional[int] = None,
        failed_tasks: Optional[int] = None,
        loop_detected: Optional[bool] = None,
        deadlock_detected: Optional[bool] = None,
    ) -> Dict[str, Any]:
        project = self.load_project()
        health = dict(project.get("health") or {})
        if status is not None:
            health["status"] = status
        if blocked_tasks is not None:
            health["blocked_tasks"] = int(blocked_tasks)
        if failed_tasks is not None:
            health["failed_tasks"] = int(failed_tasks)
        if loop_detected is not None:
            health["loop_detected"] = bool(loop_detected)
        if deadlock_detected is not None:
            health["deadlock_detected"] = bool(deadlock_detected)
        project["health"] = health
        self.save_project(project)
        return project

    def get_health(self) -> Dict[str, Any]:
        project = self.load_project()
        return dict(project.get("health") or {})

    def set_phase(self, phase: str) -> Dict[str, Any]:
        if phase not in config.PHASES:
            raise StateError(
                f"Invalid phase '{phase}'. Allowed: {', '.join(config.PHASES)}"
            )
        project = self.load_project()
        phase_block = dict(project.get("phase") or {})
        phase_block["current"] = phase
        project["phase"] = phase_block
        nested = dict(project.get("project") or {})
        nested["status"] = phase
        project["project"] = nested
        self.save_project(project)
        return project

    def record_checkpoint(self, checkpoint_id: str, phase: Optional[str] = None) -> Dict[str, Any]:
        project = self.load_project()
        phase_block = project.get("phase") or {}
        project["last_checkpoint"] = {
            "id": checkpoint_id,
            "date": datetime.now(timezone.utc).date().isoformat(),
            "phase": phase if phase is not None else phase_block.get("current", "UNKNOWN"),
        }
        self.save_project(project)
        return project

    def add_human_decision(self, decision_text: str) -> Dict[str, Any]:
        project = self.load_project()
        decisions = list(project.get("human_decisions") or [])
        if decision_text not in decisions:
            decisions.append(decision_text)
        project["human_decisions"] = decisions
        self.save_project(project)
        return project

    def set_blockers(self, blockers: Sequence[str]) -> Dict[str, Any]:
        """Replace the live blocker list in PROJECT.yaml (current state view)."""
        project = self.load_project()
        project["blockers"] = [str(item) for item in blockers]
        self.save_project(project)
        return project

    # ------------------------------------------------------------------
    # Tasks (TASKS.yaml)
    # ------------------------------------------------------------------

    def load_tasks_document(self) -> Dict[str, Any]:
        return load_yaml_file(self.tasks_yaml)

    def save_tasks_document(self, document: Dict[str, Any]) -> None:
        save_yaml_file(self.tasks_yaml, document)

    def load_tasks(self) -> List[Dict[str, Any]]:
        document = self.load_tasks_document()
        return self._tasks_from_document(document)

    def _tasks_from_document(self, document: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract and validate the task list from an already-parsed document.

        GAP-HIGH-03. Lets a caller that has just parsed ``TASKS.yaml`` reuse
        that parse instead of triggering another ``yaml.safe_load``.
        """
        tasks = document.get("tasks")
        if tasks is None:
            return []
        if not isinstance(tasks, list):
            raise StateCorruptedError(f"'tasks' must be a list in {self.tasks_yaml}")
        return tasks

    def get_task(self, task_id: str) -> Dict[str, Any]:
        for task in self.load_tasks():
            if task.get("id") == task_id:
                return task
        raise TaskNotFoundError(f"No task with id '{task_id}' in {self.tasks_yaml}")

    def find_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        for task in self.load_tasks():
            if task.get("id") == task_id:
                return task
        return None

    def dependency_ids(self, task: Dict[str, Any]) -> List[str]:
        """Normalised dependency ids for ``task``.

        Delegates to :func:`_dependency_ids_of` so this public method and the
        fast dependency check used by :meth:`get_ready_tasks` cannot drift.
        """
        return _dependency_ids_of(task)

    def dependencies_satisfied(
        self,
        task: Dict[str, Any],
        tasks: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> bool:
        """True when every dependency of ``task`` is in a satisfied status.

        ``tasks`` may be supplied to avoid re-reading ``TASKS.yaml``. The id
        index is built once per call rather than per dependency (GAP-HIGH-03).
        """
        if tasks is None:
            tasks = self.load_tasks()
        return _dependencies_satisfied_with(task, tasks, _index_tasks(tasks))

    def get_ready_tasks(
        self, tasks: Optional[Sequence[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """READY tasks plus WAITING tasks still eligible for dispatch.

        WAITING (parked by a pending PROPOSED_CHANGE) is dispatched too so the
        decision gate can refuse it explicitly; the gate — not the status — is
        what blocks the work.

        ``tasks`` may be supplied when the caller already holds the parsed list
        (GAP-HIGH-03). The id index is also built once and passed down, since
        :meth:`dependencies_satisfied` otherwise rebuilds it per task.
        """
        if tasks is None:
            tasks = self.load_tasks()
        index = _index_tasks(tasks)
        ready = [
            task
            for task in tasks
            if task.get("status") in (config.TASK_READY, config.TASK_WAITING)
            and _dependencies_satisfied_with(task, tasks, index)
        ]
        ready.sort(key=lambda task: (priority_rank(task.get("priority")), str(task.get("id"))))
        return ready

    def get_blocked_tasks(self) -> List[Dict[str, Any]]:
        tasks = self.load_tasks()
        return [
            task
            for task in tasks
            if task.get("status") == config.TASK_BLOCKED
            or (
                task.get("status") in (config.TASK_TODO, config.TASK_READY)
                and not self.dependencies_satisfied(task, tasks)
            )
        ]

    def get_next_ready_task(self) -> Optional[Dict[str, Any]]:
        ready = self.get_ready_tasks()
        return ready[0] if ready else None

    def refresh_ready_states(self) -> List[str]:
        """Promote TODO/BLOCKED tasks whose dependencies are satisfied to READY.

        Returns the list of task ids whose status changed.
        """
        document = self.load_tasks_document()
        tasks = self._tasks_from_document(document)
        index = _index_tasks(tasks)
        changed: List[str] = []
        for task in tasks:
            status = task.get("status")
            if status not in (config.TASK_TODO, config.TASK_BLOCKED):
                continue
            if _dependencies_satisfied_with(task, tasks, index):
                task["status"] = config.TASK_READY
                changed.append(str(task.get("id")))
        if changed:
            document["tasks"] = tasks
            self.save_tasks_document(document)
        self.recompute_derived_state()
        return changed

    # ------------------------------------------------------------------
    # Derived state
    # ------------------------------------------------------------------

    def derive_phase(self, tasks: Optional[Sequence[Dict[str, Any]]] = None) -> str:
        """Derive the project phase from the task graph (A1).

        Forward-only: the first phase (in lifecycle order) owning a
        non-terminal task wins; phases with no matching tasks are skipped.
        Phases with no owner (INTEGRATION, RELEASE, ...) are crossed only
        once every owned group is terminal. Returns RELEASE when every task
        is terminal, REQUIREMENTS when there are no tasks. Never returns
        MAINTENANCE (entered manually only).
        """
        task_list = list(tasks) if tasks is not None else self.load_tasks()
        if not task_list:
            return config.PHASE_REQUIREMENTS
        terminal = set(config.TERMINAL_TASK_STATUSES)
        if all(str(task.get("status")) in terminal for task in task_list):
            return config.PHASE_RELEASE
        mapped_owners = {
            owner
            for owners in config.PHASE_OWNERS.values()
            for owner in owners
        }
        unknown_owners = {
            str(task.get("owner", ""))
            for task in task_list
            if str(task.get("owner", "")) and str(task.get("owner", "")) not in mapped_owners
        }
        for phase in config.PHASES:
            if phase in (config.PHASE_RELEASE, config.PHASE_MAINTENANCE):
                continue
            owners = set(config.PHASE_OWNERS.get(phase, ()))
            if phase == config.PHASE_IMPLEMENTATION:
                owners |= unknown_owners
            if not owners:
                continue
            owned = [task for task in task_list if str(task.get("owner", "")) in owners]
            if not owned:
                continue
            if any(str(task.get("status")) not in terminal for task in owned):
                return phase
        # Every mapped group finished but unknown-owner tasks remain.
        return config.PHASE_REQUIREMENTS if unknown_owners else config.PHASE_RELEASE

    def recompute_derived_state(self) -> Dict[str, Any]:
        """Recompute derived state so status files reflect actual task state.

        TASKS.yaml: ``summary.total_tasks``, ``summary.status_breakdown``,
        ``parallel_groups`` (pruned to tasks that exist), and
        ``critical_path.path`` (longest dependency chain).

        PROJECT.yaml: ``progress`` (per-workstream completion %), ``agents``
        (owner activity status), ``next_tasks`` (currently READY ids), and the
        forward-only derived phase.

        GAP-HIGH-03 — this runs after *every* status mutation, so its disk I/O
        dominated the write path. It previously parsed ``TASKS.yaml`` **four
        times** per call: once here, once more via ``load_tasks_document``,
        again through ``status_breakdown()``, and once more through
        ``get_ready_tasks()`` (plus O(n²) scans inside the two dependency
        helpers). At 400 tasks a single mutation cost ~6 s, almost all of it
        ``yaml.safe_load``/``safe_dump``.

        Now ``TASKS.yaml`` is parsed **once** and the in-memory list is threaded
        through the derivations that used to re-read it. ``status_breakdown``
        and ``get_ready_tasks`` accept an optional pre-loaded task list, and the
        per-owner/per-key group scans are bucketed in a single pass instead of
        re-filtering the whole list per key. Behaviour is unchanged; only the
        number of disk round-trips is.

        Planning-only fields (durations, estimates, group metadata) are
        preserved. Files are still only written when a derived value changed.
        """
        document = self.load_tasks_document()
        tasks = self._tasks_from_document(document)
        task_ids = {str(task.get("id")) for task in tasks}
        tasks_changed = False

        breakdown = _status_breakdown_of(tasks)
        summary = dict(document.get("summary") or {})
        new_summary = dict(summary)
        new_summary["total_tasks"] = len(tasks)
        new_summary["status_breakdown"] = dict(breakdown)
        if new_summary != summary:
            document["summary"] = new_summary
            tasks_changed = True

        groups = document.get("parallel_groups")
        if isinstance(groups, list):
            pruned: List[Any] = []
            for group in groups:
                if not isinstance(group, dict):
                    pruned.append(group)
                    continue
                before = [str(item) for item in (group.get("tasks") or [])]
                after = [item for item in before if item in task_ids]
                if after:
                    if after != before:
                        group["tasks"] = after
                        tasks_changed = True
                    pruned.append(group)
                else:
                    tasks_changed = True
            if pruned != groups:
                document["parallel_groups"] = pruned
                tasks_changed = True

        if tasks:
            chain = self._longest_dependency_chain(tasks)
            path_str = " -> ".join(chain)
            critical = dict(document.get("critical_path") or {})
            if str(critical.get("path") or "") != path_str:
                critical["path"] = path_str
                document["critical_path"] = critical
                tasks_changed = True

        if tasks_changed:
            self.save_tasks_document(document)

        project = self.load_project()
        project_changed = False

        # Single bucketing pass: previously each progress key and each owner
        # re-filtered the entire task list, making this block O(keys x tasks).
        by_progress_key: Dict[str, List[Dict[str, Any]]] = {}
        by_owner: Dict[str, List[Dict[str, Any]]] = {}
        for task in tasks:
            owner = str(task.get("owner", ""))
            if owner:
                by_owner.setdefault(owner, []).append(task)
            key = OWNER_PROGRESS_KEY.get(owner)
            if key:
                by_progress_key.setdefault(key, []).append(task)

        progress = dict(project.get("progress") or {})
        progress_keys = set(progress) | set(by_progress_key)
        new_progress = dict(progress)
        for key in sorted(progress_keys):
            owned = by_progress_key.get(key)
            if not owned:
                continue
            done = [
                task
                for task in owned
                if str(task.get("status")) in config.TERMINAL_TASK_STATUSES
            ]
            pct = int(round(100.0 * len(done) / len(owned)))
            if new_progress.get(key) != pct:
                new_progress[key] = pct
        if new_progress != progress:
            project["progress"] = new_progress
            project_changed = True

        agents = dict(project.get("agents") or {})
        new_agents = dict(agents)
        for owner in sorted(by_owner):
            status = derive_agent_status(by_owner[owner])
            if new_agents.get(owner) != status:
                new_agents[owner] = status
        if new_agents != agents:
            project["agents"] = new_agents
            project_changed = True

        next_tasks = [str(task.get("id")) for task in self.get_ready_tasks(tasks=tasks)]
        if (project.get("next_tasks") or []) != next_tasks:
            project["next_tasks"] = next_tasks
            project_changed = True

        # A1: forward-only lifecycle advance derived from the task graph.
        phase_block = dict(project.get("phase") or {})
        current_phase = str(phase_block.get("current") or config.PHASE_REQUIREMENTS)
        derived_phase = self.derive_phase(tasks)
        if config.phase_index(derived_phase) > config.phase_index(current_phase):
            phase_block["current"] = derived_phase
            project["phase"] = phase_block
            nested = dict(project.get("project") or {})
            nested["status"] = derived_phase
            project["project"] = nested
            project_changed = True

        if project_changed:
            self.save_project(project)

        return {
            "summary": new_summary,
            "progress": new_progress,
            "agents": new_agents,
            "next_tasks": next_tasks,
            "phase": str((project.get("phase") or {}).get("current") or config.PHASE_REQUIREMENTS),
        }

    @staticmethod
    def _longest_dependency_chain(tasks: List[Dict[str, Any]]) -> List[str]:
        """Longest dependency chain through the task graph (critical path)."""
        deps: Dict[str, List[str]] = {}
        order: List[str] = []
        for task in tasks:
            task_id = str(task.get("id"))
            deps[task_id] = [str(item) for item in (task.get("dependencies") or [])]
            order.append(task_id)

        memo: Dict[str, List[str]] = {}

        def chain(task_id: str, stack: tuple = ()) -> List[str]:
            if task_id in memo:
                return memo[task_id]
            if task_id in stack:
                return [task_id]
            best = [task_id]
            for dep in deps[task_id]:
                if dep not in deps:
                    continue
                sub = chain(dep, stack + (task_id,))
                if len(sub) + 1 > len(best):
                    best = sub + [task_id]
            memo[task_id] = best
            return best

        best_overall: List[str] = []
        for task_id in order:
            candidate = chain(task_id)
            if len(candidate) > len(best_overall):
                best_overall = candidate
        return best_overall

    def update_task_status(
        self,
        task_id: str,
        status: str,
        note: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        if status not in config.TASK_STATUSES:
            raise StateError(
                f"Invalid task status '{status}'. Allowed: {', '.join(config.TASK_STATUSES)}"
            )
        document = self.load_tasks_document()
        tasks = document.get("tasks") or []
        target: Optional[Dict[str, Any]] = None
        for task in tasks:
            if task.get("id") == task_id:
                target = task
                break
        if target is None:
            raise TaskNotFoundError(f"No task with id '{task_id}' in {self.tasks_yaml}")
        target["status"] = status
        if status in config.TERMINAL_TASK_STATUSES:
            execution = dict(target.get("execution") or {})
            execution["recovering"] = False
            target["execution"] = execution
        if note is not None:
            target["notes"] = note
        if error is not None:
            execution = dict(target.get("execution") or {})
            execution["last_error"] = str(error)[: config.ERROR_SNIPPET_LENGTH]
            target["execution"] = execution
        document["tasks"] = tasks
        self.save_tasks_document(document)
        return dict(target)

    def update_task_execution(
        self,
        task_id: str,
        attempt_delta: int = 0,
        no_progress_delta: int = 0,
        strategy_changed: bool = False,
        error: Optional[str] = None,
        reset_error: bool = False,
        set_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        document = self.load_tasks_document()
        tasks = document.get("tasks") or []
        target: Optional[Dict[str, Any]] = None
        for task in tasks:
            if task.get("id") == task_id:
                target = task
                break
        if target is None:
            raise TaskNotFoundError(f"No task with id '{task_id}' in {self.tasks_yaml}")

        execution = dict(target.get("execution") or {})
        attempt_count = int(execution.get("attempt_count", 0) or 0)
        no_progress_cycles = int(execution.get("no_progress_cycles", 0) or 0)
        strategy_changes = int(execution.get("strategy_changes", 0) or 0)
        since_change = int(execution.get("attempts_since_change", 0) or 0)

        execution["attempt_count"] = max(0, attempt_count + int(attempt_delta))
        execution["no_progress_cycles"] = max(0, no_progress_cycles + int(no_progress_delta))
        if strategy_changed:
            execution["strategy_changes"] = strategy_changes + 1
            execution["attempts_since_change"] = 0
            execution["recovering"] = True
        else:
            execution["attempts_since_change"] = max(0, since_change + int(attempt_delta))
            # Recovery ends when the new strategy also burns the retry budget.
            if (
                execution.get("recovering")
                and execution["attempts_since_change"]
                >= config.loop_thresholds().same_strategy_max_attempts
            ):
                execution["recovering"] = False
        if reset_error:
            execution["last_error"] = None
            execution["retry_reason"] = None
        elif error is not None:
            execution["last_error"] = str(error)[: config.ERROR_SNIPPET_LENGTH]
        if set_values:
            execution.update(dict(set_values))
        target["execution"] = execution

        document["tasks"] = tasks
        self.save_tasks_document(document)
        return dict(target)

    @staticmethod
    def _next_task_id(tasks: Sequence[Dict[str, Any]]) -> str:
        highest = 0
        for task in tasks:
            match = re.match(r"TASK-(\d+)$", str(task.get("id", "")))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"TASK-{highest + 1:03d}"

    def append_task(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Append a goal-derived task to TASKS.yaml (A2: goal -> task graph).

        ``spec`` is the normalized payload (title/owner/dependencies/...);
        missing ``id`` is auto-assigned as ``TASK-NNN``. Unknown owners or
        dependencies and duplicate ids raise StateError.

        GAP-CRIT-05 — a newly created task's status is **derived, never
        supplied**. ``status``, ``execution`` and ``review.status`` in the
        incoming spec are discarded and the status is computed from the
        dependency list (see :func:`derive_initial_status`). A planner that
        replies ``{"status": "DONE"}`` therefore cannot self-approve a task and
        bypass execution, review and the Definition of Done; nor can it
        pre-load ``execution.attempt_count`` to trip the loop gate on a task
        that has never run. Pass ``status`` only via the trusted seeding path
        (:meth:`seed_starter_tasks`), which passes the same derived value.
        """
        if not isinstance(spec, dict):
            raise StateError("Task spec must be a mapping")
        document = self.load_tasks_document()
        tasks = list(document.get("tasks") or [])
        existing_ids = {str(task.get("id")) for task in tasks}

        # GAP-MED-02: refuse to add a task that would *join* a dependency cycle,
        # before writing. A cycle is unfixable by waiting (every task in it
        # waits for the others), so it must never reach the file where it would
        # silently strand the graph.
        #
        # Scoped to cycles the new task participates in. A cycle that already
        # exists elsewhere in the graph must not block unrelated new work — it
        # is reported by validate() and fixed with `waive`. A blanket rejection
        # would make the graph unappendable until an unrelated defect was
        # cleared, which is a worse failure than the one being prevented.
        new_id = str((spec or {}).get("id") or "").strip() or "__new__"
        probe: Dict[str, Any] = dict(spec) if isinstance(spec, dict) else {}
        probe["id"] = new_id
        probe["dependencies"] = _dependency_ids_of(probe)
        prospective = [
            {**item, "id": str(item.get("id")), "dependencies": _dependency_ids_of(item)}
            for item in tasks
            if isinstance(item, dict) and item.get("id")
        ]
        prospective.append(probe)
        cycles = self.find_dependency_cycles(prospective)
        joined = [cycle for cycle in cycles if new_id in cycle]
        if joined:
            raise StateError(
                "Task spec would create a dependency cycle: "
                + "; ".join(" -> ".join(cycle + [cycle[0]]) for cycle in joined)
            )

        task = dict(spec)
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            task_id = self._next_task_id(tasks)
        if task_id in existing_ids:
            raise StateError(f"Task id '{task_id}' already exists")
        task["id"] = task_id

        title = str(task.get("title") or "").strip()
        if not title:
            raise StateError(f"Task '{task_id}' needs a title")
        owner = str(task.get("owner") or "").strip()
        if not owner:
            raise StateError(f"Task '{task_id}' needs an owner")

        from .agents.base_agent import agent_names  # local import: avoid cycle

        if owner not in agent_names():
            raise StateError(
                f"Task '{task_id}' has unknown owner '{owner}'. "
                f"Registered: {', '.join(sorted(agent_names()))}"
            )

        deps = [str(item) for item in (task.get("dependencies") or [])]
        known = existing_ids | {task_id}
        unknown = [dep for dep in deps if dep not in known]
        if unknown:
            raise StateError(
                f"Task '{task_id}' references unknown dependencies: {', '.join(unknown)}"
            )
        task["dependencies"] = deps

        # Derived status — the model never gets a say. Anything the spec
        # supplied is removed first so a stale key cannot survive by alias.
        task.pop("status", None)
        task["status"] = derive_initial_status(deps)

        task["priority"] = str(task.get("priority") or "MEDIUM")
        task["expected_outputs"] = list(task.get("expected_outputs") or [])
        task["acceptance_criteria"] = list(task.get("acceptance_criteria") or [])
        task.setdefault("notes", "")
        # Fresh execution counters: a spec cannot pre-load attempt/loop counts.
        task["execution"] = {}
        execution = dict(task.get("execution") or {})
        execution.setdefault("attempt_count", 0)
        execution.setdefault("no_progress_cycles", 0)
        execution.setdefault("strategy_changes", 0)
        execution.setdefault("attempts_since_change", 0)
        execution.setdefault("recovering", False)
        execution.setdefault("last_error", None)
        execution.setdefault("retry_reason", None)
        task["execution"] = execution
        review = dict(task.get("review") or {})
        review.setdefault("required", False)
        # Reset rather than setdefault: review.status is written by the review
        # flow, never by a planner, so an injected value must not survive. A task
        # that has never run cannot be past review.
        review["status"] = "NOT_STARTED"
        task["review"] = review

        tasks.append(task)
        document["tasks"] = tasks
        self.save_tasks_document(document)
        self.recompute_derived_state()
        return dict(task)

    def seed_starter_tasks(self, goal: str = "") -> List[str]:
        """Write a generic starter task graph when TASKS.yaml is empty (G1).

        Deterministic fallback for goal-driven planning: five sequential
        tasks covering requirements -> architecture -> implementation ->
        testing -> documentation. Returns the created task ids, or an
        empty list when tasks already exist (idempotent).
        """
        if self.load_tasks():
            return []
        label = str(goal or "").strip() or "the project goal"
        specs: List[Dict[str, Any]] = [
            {
                "title": f"Capture requirements for: {label}",
                "owner": "requirements_agent",
                "status": config.TASK_TODO,
                "priority": "CRITICAL",
                "dependencies": [],
                "expected_outputs": ["REQUIREMENTS.md"],
                "acceptance_criteria": [
                    "REQUIREMENTS.md exists with REQ-001.. entries "
                    "and measurable acceptance criteria"
                ],
                "notes": (
                    "Starter task generated by `orchestrator init` — "
                    "edit freely in TASKS.yaml."
                ),
            },
            {
                "title": "Define system architecture and interfaces",
                "owner": "architecture_agent",
                "status": config.TASK_BLOCKED,
                "priority": "CRITICAL",
                "dependencies": [],  # filled from the previous id below
                "expected_outputs": ["ARCHITECTURE.md"],
                "acceptance_criteria": [
                    "ARCHITECTURE.md covers every requirement with an REQ id"
                ],
            },
            {
                "title": "Implement the planned work",
                "owner": "software_agent",
                "status": config.TASK_BLOCKED,
                "priority": "HIGH",
                "dependencies": [],
                "expected_outputs": ["IMPLEMENTATION.md"],
                "acceptance_criteria": [
                    "Implementation follows the approved architecture"
                ],
            },
            {
                "title": "Test and validate the implementation",
                "owner": "test_agent",
                "status": config.TASK_BLOCKED,
                "priority": "HIGH",
                "dependencies": [],
                "expected_outputs": ["TEST_REPORT.md"],
                "acceptance_criteria": ["Tests map to requirements and pass"],
            },
            {
                "title": "Update documentation and prepare release notes",
                "owner": "documentation_agent",
                "status": config.TASK_BLOCKED,
                "priority": "MEDIUM",
                "dependencies": [],  # depends on implementation (index 2)
                "expected_outputs": ["RELEASE_NOTES.md"],
                "acceptance_criteria": ["Docs match shipped behavior"],
            },
        ]
        # Chain: 0 <- 1 <- 2 <- {3, 4}
        created: List[str] = []
        for index, spec in enumerate(specs):
            if index == 1:
                spec["dependencies"] = [created[0]]
            elif index == 2:
                spec["dependencies"] = [created[1]]
            elif index in (3, 4):
                spec["dependencies"] = [created[2]]
            task = self.append_task(spec)
            created.append(str(task.get("id")))
        self.refresh_ready_states()
        return created

    def relax_dependency(self, task_id: str, dependency_id: str) -> Dict[str, Any]:
        """Drop one blocking dependency edge (A6: human unblocks a deadlock)."""
        document = self.load_tasks_document()
        tasks = document.get("tasks") or []
        target: Optional[Dict[str, Any]] = None
        for task in tasks:
            if task.get("id") == task_id:
                target = task
                break
        if target is None:
            raise TaskNotFoundError(f"No task with id '{task_id}' in {self.tasks_yaml}")
        deps = [str(item) for item in (target.get("dependencies") or [])]
        if dependency_id not in deps:
            raise StateError(
                f"Task '{task_id}' does not depend on '{dependency_id}'"
            )
        target["dependencies"] = [dep for dep in deps if dep != dependency_id]
        document["tasks"] = tasks
        self.save_tasks_document(document)
        self.recompute_derived_state()
        self.refresh_ready_states()
        refreshed = self.find_task(task_id)
        return dict(refreshed or target)

    def set_review_status(self, task_id: str, review_status: str) -> Dict[str, Any]:
        document = self.load_tasks_document()
        tasks = document.get("tasks") or []
        target: Optional[Dict[str, Any]] = None
        for task in tasks:
            if task.get("id") == task_id:
                target = task
                break
        if target is None:
            raise TaskNotFoundError(f"No task with id '{task_id}' in {self.tasks_yaml}")
        review = dict(target.get("review") or {})
        review["status"] = review_status
        target["review"] = review
        document["tasks"] = tasks
        self.save_tasks_document(document)
        return dict(target)

    def status_breakdown(self, tasks: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, int]:
        """Count tasks per status.

        ``tasks`` may be supplied when the caller already holds the parsed list
        (GAP-HIGH-03), avoiding a redundant ``TASKS.yaml`` parse.
        """
        if tasks is None:
            tasks = self.load_tasks()
        return _status_breakdown_of(tasks)

    def summary_counts(self) -> Dict[str, int]:
        breakdown = self.status_breakdown()
        breakdown["TOTAL"] = sum(breakdown.values())
        return breakdown

    # ------------------------------------------------------------------
    # Markdown state files
    # ------------------------------------------------------------------

    def load_memory(self) -> str:
        return load_text_file(self.memory_md)

    def save_memory(self, content: str) -> None:
        save_text_file(self.memory_md, content)

    def compact_memory(self, max_chars: int = config.MEMORY_COMPACT_MAX_CHARS) -> bool:
        """Fold the middle of PROJECT_MEMORY.md when it outgrows the budget (B1).

        Keeps the head (context/goals/recent decisions) and the tail (open
        issues/next steps), replaces the middle with a marker listing the
        dropped section headings. Returns True when a rewrite happened.
        """
        text = self.load_memory()
        if len(text) <= max_chars:
            return False
        head_len = int(max_chars * 0.6)
        tail_len = int(max_chars * 0.25)
        head = text[:head_len]
        tail = text[-tail_len:]
        dropped = text[head_len : len(text) - tail_len]
        headings = [
            match.lstrip("# ").strip()
            for match in re.findall(r"^#{1,3} .+$", dropped, re.M)
        ]
        marker = (
            f"\n\n<!-- compacted {utc_now_iso()}: {len(dropped)} chars folded"
            + (f"; sections: {', '.join(headings[:12])}" if headings else "")
            + " -->\n\n"
        )
        self.save_memory(head + marker + tail)
        return True

    def load_current_state(self) -> str:
        return load_text_file(self.current_state_md)

    def save_current_state(self, content: str) -> None:
        save_text_file(self.current_state_md, content)

    def append_current_state(self, message: str) -> str:
        """Append one update line to CURRENT_STATE.md.

        Appends in place under the document lock (GAP-CRIT-06) rather than
        rewriting the whole file, so concurrent writers cannot lose an entry.
        """
        entry = f"**Update ({utc_now_iso()}):** {message}\n"
        return _append_text_locked(self.current_state_md, entry)

    def load_decisions(self) -> str:
        return load_text_file(self.decisions_md)

    def save_decisions(self, content: str) -> None:
        save_text_file(self.decisions_md, content)

    # ------------------------------------------------------------------
    # Decision control (PROPOSED_CHANGE gate) — framework/00:132-157
    # ------------------------------------------------------------------

    _DEC_SECTION_HEADER_RE = re.compile(r"^#{2,3}\s*(DEC-\d+)\s*[:\-—]\s*(.*?)\s*$")
    _DEC_FIELD_RE = re.compile(r"^\*\*([A-Za-z][A-Za-z ]+):\*\*\s*(.*?)\s*$")
    _DEC_TABLE_ROW_RE = re.compile(r"^\|\s*(DEC-\d+)\s*\|([^|]*)\|([^|]*)\|(.*?)\|\s*$")

    @staticmethod
    def _split_affected_ids(value: str) -> List[str]:
        tokens = re.split(r"[,;]+", value or "")
        ids = [token.strip() for token in tokens if token.strip()]
        return [token for token in ids if token.lower() not in ("none", "(none)", "-")]

    def _parse_decisions(self, content: str) -> List[Dict[str, Any]]:
        """Parse DECISIONS.md entries (rich sections and table rows)."""
        decisions: List[Dict[str, Any]] = []
        lines = content.splitlines()
        index = 0
        while index < len(lines):
            header = self._DEC_SECTION_HEADER_RE.match(lines[index])
            if header:
                fields: Dict[str, str] = {}
                end = index + 1
                while end < len(lines) and not re.match(r"^##\s", lines[end]):
                    field = self._DEC_FIELD_RE.match(lines[end])
                    if field:
                        fields[field.group(1).strip()] = field.group(2).strip()
                    end += 1
                decisions.append(
                    {
                        "id": header.group(1),
                        "title": header.group(2).strip(),
                        "status": str(fields.get("Status", "")).upper(),
                        "reason": fields.get("Reason", ""),
                        "affected_tasks": self._split_affected_ids(
                            fields.get("Affected Tasks", "")
                        ),
                        "format": "section",
                        "line": index,
                        "end": end,
                    }
                )
                index = end
                continue
            row = self._DEC_TABLE_ROW_RE.match(lines[index])
            if row:
                decisions.append(
                    {
                        "id": row.group(1),
                        "title": row.group(2).strip(),
                        "status": row.group(3).strip().upper(),
                        "reason": row.group(4).strip(),
                        "affected_tasks": [],
                        "format": "table",
                        "line": index,
                        "end": index + 1,
                    }
                )
            index += 1
        return decisions

    def list_decisions(self) -> List[Dict[str, Any]]:
        """All decisions recorded in DECISIONS.md, in file order."""
        return self._parse_decisions(self.load_decisions())

    def pending_proposed_changes(self) -> List[Dict[str, Any]]:
        """PROPOSED_CHANGE entries still awaiting a human decision."""
        return [item for item in self.list_decisions() if item.get("status") == "PROPOSED_CHANGE"]

    def append_decision(
        self,
        title: str,
        status: str = "PROPOSED_CHANGE",
        reason: str = "",
        alternatives: str = "",
        impact: str = "",
        affected_tasks: Optional[Sequence[str]] = None,
        risks: str = "",
        recommendation: str = "",
        proposed_by: str = "",
    ) -> Dict[str, Any]:
        """Record a new decision entry in DECISIONS.md (+ CHANGELOG.md).

        The ``PROPOSED_CHANGE`` status is the gate from GETTING_STARTED
        Pattern 3: while pending, dispatch of ``affected_tasks`` is blocked
        until a human calls :meth:`resolve_decision`.
        """
        def one_line(value: Any) -> str:
            return " ".join(str(value or "").split())

        affected = [
            str(item).strip()
            for item in (affected_tasks or [])
            if str(item).strip()
        ]

        def render(dec_id: str) -> str:
            return (
                "\n---\n\n"
                f"## {dec_id}: {one_line(title)}\n\n"
                f"**Date:** {utc_now_iso()}  \n"
                f"**Status:** {one_line(status)}  \n"
                f"**Proposed by:** {one_line(proposed_by)}  \n"
                f"**Reason:** {one_line(reason)}  \n"
                f"**Alternatives:** {one_line(alternatives)}  \n"
                f"**Impact:** {one_line(impact)}  \n"
                f"**Affected Tasks:** {one_line(', '.join(affected))}  \n"
                f"**Risks:** {one_line(risks)}  \n"
                f"**Recommendation:** {one_line(recommendation)}\n"
            )

        # GAP-CRIT-06: the read, the id allocation and the write form ONE
        # critical section. Splitting them lets two processes both read a file
        # containing no DEC-004, both allocate DEC-004, and the second write
        # silently discards the first decision. The section body is rendered
        # inside the lock so the id and the text can never disagree.
        with _document_lock(self.decisions_md):
            content = self.load_decisions()
            numbers = [
                int(match.group(1)) for match in re.finditer(r"DEC-(\d+)", content)
            ]
            dec_id = f"DEC-{((max(numbers) + 1) if numbers else 1):03d}"
            section = render(dec_id)
            if content and not content.endswith("\n"):
                content += "\n"
            self.save_decisions(content + section)
        self.append_changelog(
            f"Decision {dec_id} recorded ({one_line(status)}): {one_line(title)}"
        )
        return {
            "id": dec_id,
            "title": one_line(title),
            "status": one_line(status),
            "reason": one_line(reason),
            "affected_tasks": affected,
        }

    def resolve_decision(self, dec_id: str, approved: bool = True) -> Dict[str, Any]:
        """Approve or reject a recorded decision (updates DECISIONS.md).

        The parse and the rewrite share one document lock (GAP-CRIT-06) so a
        decision appended concurrently cannot shift the line offsets this
        method is about to rewrite.
        """
        with _document_lock(self.decisions_md):
            return self._resolve_decision_locked(dec_id, approved)

    def _resolve_decision_locked(self, dec_id: str, approved: bool) -> Dict[str, Any]:
        content = self.load_decisions()
        entries = self._parse_decisions(content)
        target = next((item for item in entries if item["id"] == dec_id), None)
        if target is None:
            raise StateError(f"No decision '{dec_id}' in {self.decisions_md}")
        new_status = "APPROVED" if approved else "REJECTED"

        lines = content.splitlines()
        start, end = int(target["line"]), int(target["end"])
        if target["format"] == "section":
            replaced = False
            for line_index in range(start + 1, end):
                field = self._DEC_FIELD_RE.match(lines[line_index])
                if field and field.group(1).strip() == "Status":
                    lines[line_index] = f"**Status:** {new_status}  "
                    replaced = True
                    break
            if not replaced:
                raise StateError(f"Decision '{dec_id}' has no Status field to update")
        else:
            cells = lines[start].split("|")
            if len(cells) < 5:
                raise StateError(f"Decision '{dec_id}' table row is malformed")
            cells[3] = f" {new_status} "
            lines[start] = "|".join(cells)

        updated = "\n".join(lines)
        if content.endswith("\n"):
            updated += "\n"
        self.save_decisions(updated)
        self.append_changelog(f"Decision {dec_id} {new_status}")
        refreshed = next(
            item for item in self._parse_decisions(self.load_decisions())
            if item["id"] == dec_id
        )
        return refreshed

    # ------------------------------------------------------------------
    # Risk register (RISKS.md) — QUICK_REFERENCE: Supervisor owns this file
    # ------------------------------------------------------------------

    _RISK_SECTION_HEADER_RE = re.compile(r"^#{2,3}\s*(RISK-\d+)\s*[:\-—]\s*(.*?)\s*$")
    _RISK_TABLE_ROW_RE = re.compile(
        r"^\|\s*(RISK-\d+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|(.*?)\|\s*$"
    )

    def _parse_risks(self, content: str) -> List[Dict[str, Any]]:
        """Parse RISKS.md entries (rich sections and register tables)."""
        risks: List[Dict[str, Any]] = []
        lines = content.splitlines()
        index = 0
        while index < len(lines):
            header = self._RISK_SECTION_HEADER_RE.match(lines[index])
            if header:
                fields: Dict[str, str] = {}
                end = index + 1
                while end < len(lines) and not re.match(r"^#{2,3}\s", lines[end]):
                    field = self._DEC_FIELD_RE.match(lines[end])
                    if field:
                        fields[field.group(1).strip()] = field.group(2).strip()
                    end += 1
                risks.append(
                    {
                        "id": header.group(1),
                        "title": header.group(2).strip(),
                        "probability": str(fields.get("Probability", "")).upper(),
                        "impact": str(fields.get("Impact", "")).upper(),
                        "status": str(fields.get("Status", "OPEN")).upper() or "OPEN",
                        "related_tasks": self._split_affected_ids(
                            fields.get("Related Tasks", "")
                        ),
                        "format": "section",
                        "line": index,
                        "end": end,
                    }
                )
                index = end
                continue
            row = self._RISK_TABLE_ROW_RE.match(lines[index])
            if row:
                status = "OPEN"
                if index + 1 < len(lines):
                    following = self._DEC_FIELD_RE.match(lines[index + 1])
                    if following and following.group(1).strip() == "Status":
                        status = following.group(2).strip().upper() or "OPEN"
                risks.append(
                    {
                        "id": row.group(1),
                        "title": row.group(2).strip(),
                        "probability": row.group(3).strip().upper(),
                        "impact": row.group(4).strip().upper(),
                        "status": status,
                        "related_tasks": [],
                        "format": "table",
                        "line": index,
                        "end": index + 1,
                    }
                )
            index += 1
        return risks

    def list_risks(self) -> List[Dict[str, Any]]:
        """All risks recorded in RISKS.md, in file order."""
        return self._parse_risks(self.load_risks())

    def append_risk(
        self,
        title: str,
        probability: str = "MEDIUM",
        impact: str = "MEDIUM",
        description: str = "",
        mitigation: str = "",
        related_tasks: Optional[Sequence[str]] = None,
        owner: str = "supervisor_agent",
        status: str = "OPEN",
    ) -> Dict[str, Any]:
        """Record a new risk entry in RISKS.md (+ CHANGELOG.md)."""
        def one_line(value: Any) -> str:
            return " ".join(str(value or "").split())

        related = [
            str(item).strip() for item in (related_tasks or []) if str(item).strip()
        ]

        def render(risk_id: str) -> str:
            return (
                "\n---\n\n"
                f"### {risk_id}: {one_line(title)}\n\n"
                f"**Date:** {utc_now_iso()}  \n"
                f"**Status:** {one_line(status)}  \n"
                f"**Probability:** {one_line(probability)}  \n"
                f"**Impact:** {one_line(impact)}  \n"
                f"**Related Tasks:** {one_line(', '.join(related))}  \n"
                f"**Owner:** {one_line(owner)}  \n\n"
                "**Description:**  \n"
                f"{one_line(description) or '(none recorded)'}\n\n"
                "**Mitigation:**  \n"
                f"{one_line(mitigation) or '(none recorded)'}\n"
            )

        # GAP-CRIT-06: read + id allocation + write in one critical section, so
        # two failing tasks recorded concurrently cannot both become RISK-002
        # with one of them silently dropped.
        with _document_lock(self.risks_md):
            content = self.load_risks()
            numbers = [
                int(match.group(1)) for match in re.finditer(r"RISK-(\d+)", content)
            ]
            risk_id = f"RISK-{((max(numbers) + 1) if numbers else 1):03d}"
            section = render(risk_id)
            if content and not content.endswith("\n"):
                content += "\n"
            self.save_risks(content + section)
        self.append_changelog(
            f"Risk {risk_id} recorded ({one_line(status)}): {one_line(title)}"
        )
        return {
            "id": risk_id,
            "title": one_line(title),
            "status": one_line(status),
            "probability": one_line(probability),
            "impact": one_line(impact),
            "related_tasks": related,
        }

    def update_risk_status(self, risk_id: str, status: str) -> Dict[str, Any]:
        """Change an entry's status (OPEN / MITIGATED / CLOSED / REALIZED).

        Parsed and rewritten under one document lock (GAP-CRIT-06) for the same
        offset-stability reason as :meth:`resolve_decision`.
        """
        with _document_lock(self.risks_md):
            return self._update_risk_status_locked(risk_id, status)

    def _update_risk_status_locked(self, risk_id: str, status: str) -> Dict[str, Any]:
        content = self.load_risks()
        entries = self._parse_risks(content)
        target = next((item for item in entries if item["id"] == risk_id), None)
        if target is None:
            raise StateError(f"No risk '{risk_id}' in {self.risks_md}")
        new_status = str(status).upper()

        lines = content.splitlines()
        start, end = int(target["line"]), int(target["end"])
        if target["format"] == "section":
            replaced = False
            for line_index in range(start + 1, end):
                field = self._DEC_FIELD_RE.match(lines[line_index])
                if field and field.group(1).strip() == "Status":
                    lines[line_index] = f"**Status:** {new_status}  "
                    replaced = True
                    break
            if not replaced:
                raise StateError(f"Risk '{risk_id}' has no Status field to update")
        else:
            # Table rows have no Status column: add a Status line after the row.
            status_line = f"**Status:** {new_status}  "
            if start + 1 >= len(lines):
                lines.append(status_line)
            elif lines[start + 1].strip().startswith("**Status:**"):
                lines[start + 1] = status_line
            else:
                lines.insert(start + 1, status_line)

        updated = "\n".join(lines)
        if content.endswith("\n"):
            updated += "\n"
        self.save_risks(updated)
        self.append_changelog(f"Risk {risk_id} {new_status}")
        refreshed = next(
            item for item in self._parse_risks(self.load_risks())
            if item["id"] == risk_id
        )
        return refreshed

    def load_risks(self) -> str:
        return load_text_file(self.risks_md)

    def save_risks(self, content: str) -> None:
        save_text_file(self.risks_md, content)

    def load_changelog(self) -> str:
        return load_text_file(self.changelog_md)

    def save_changelog(self, content: str) -> None:
        save_text_file(self.changelog_md, content)

    def append_changelog(self, entry: str) -> str:
        """Append one dated section to CHANGELOG.md.

        Appends in place under the document lock (GAP-CRIT-06).
        """
        block = f"## {utc_now_iso()} — {entry}\n"
        return _append_text_locked(self.changelog_md, block)

    def append_memory_section(self, heading: str, body: str) -> str:
        """Append a section to PROJECT_MEMORY.md.

        ``append_changelog`` is deliberately *not* called here: the compaction
        path calls this while already holding the memory document lock, and
        taking the changelog lock inside it would invert the lock order against
        :meth:`append_decision`.
        """
        block = f"## {heading}\n\n{body.strip()}\n"
        return _append_text_locked(self.memory_md, block)

    def extract_requirement_ids(self) -> List[str]:
        """Collect REQ-* ids referenced anywhere in PROJECT_MEMORY.md."""
        memory = self.load_memory()
        seen: List[str] = []
        for match in re.finditer(r"REQ-\d{3,}", memory):
            req_id = match.group(0)
            if req_id not in seen:
                seen.append(req_id)
        return seen

    # ------------------------------------------------------------------
    # Snapshots / helpers
    # ------------------------------------------------------------------

    def state_file_paths(self) -> List[Path]:
        return [self.project_path / name for name in config.STATE_FILES if (self.project_path / name).exists()]

    def save_state_snapshot(self) -> Dict[str, Any]:
        return {
            "timestamp": utc_now_iso(),
            "project": self.load_project(),
            "tasks": self.load_tasks(),
            "memory": self.load_memory(),
            "current_state": self.load_current_state(),
        }

    def task_index(self) -> Dict[str, Dict[str, Any]]:
        return {str(task.get("id")): task for task in self.load_tasks()}

    def print_status(self) -> str:
        project = self.load_project()
        project_block = project.get("project") or {}
        phase_block = project.get("phase") or {}
        health = project.get("health") or {}
        context = project.get("context") or {}
        counts = self.summary_counts()
        ready = self.get_ready_tasks()

        lines = [
            "=" * 60,
            f"PROJECT: {project_block.get('name', 'unknown')} (v{project_block.get('version', '?')})",
            "=" * 60,
            f"Phase:   {phase_block.get('current', 'UNKNOWN')}",
            f"Status:  {project_block.get('status', 'UNKNOWN')}",
            f"Health:  {health.get('status', 'UNKNOWN')}",
            f"Context: {context.get('utilization_percent', 0)}% "
            f"(compaction at {context.get('compaction_threshold', 70)}%)",
            "",
            "TASK SUMMARY:",
        ]
        for status in config.TASK_STATUSES:
            count = counts.get(status, 0)
            if count:
                lines.append(f"  {status:<12} {count} tasks")
        lines.append(f"  {'TOTAL':<12} {counts.get('TOTAL', 0)} tasks")
        lines.append("")
        lines.append(f"READY TASKS ({len(ready)}):")
        if ready:
            for task in ready:
                lines.append(f"  - {task.get('id')} [{task.get('priority', '?')}] {task.get('title', '')}")
        else:
            lines.append("  [None]")
        lines.append("=" * 60)
        return "\n".join(lines)

    @staticmethod
    def find_dependency_cycles(
        tasks: Sequence[Dict[str, Any]],
    ) -> List[List[str]]:
        """Return every dependency cycle in ``tasks`` (GAP-MED-02).

        Iterative three-colour DFS. Reports each cycle once, as a node-ordered
        path, so an error message can name the exact chain to break.

        Iterative rather than recursive on purpose: a plan can produce a graph
        thousands of tasks deep, and the recursive form would hit Python's
        recursion limit and surface as ``RecursionError`` — turning a structural
        problem into an opaque crash inside a status mutation.

        A self-dependency (``TASK-001 -> TASK-001``) is reported as a
        single-node cycle, which is the shape that is easiest to miss and
        permanently blocks a task.
        """
        graph: Dict[str, List[str]] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id"))
            if not task_id or task_id == "None":
                continue
            raw = task.get("dependencies") or []
            graph[task_id] = [str(item) for item in raw] if isinstance(raw, list) else []

        WHITE, GRAY, BLACK = 0, 1, 2
        colour: Dict[str, int] = {node: WHITE for node in graph}
        cycles: List[List[str]] = []
        seen: set = set()

        for start in graph:
            if colour.get(start, WHITE) != WHITE:
                continue
            # Each frame is (node, iterator over its remaining dependencies).
            stack: List[List[Any]] = [[start, iter(graph.get(start, []))]]
            path: List[str] = [start]
            colour[start] = GRAY
            while stack:
                frame = stack[-1]
                node = frame[0]
                advanced = False
                for neighbour in frame[1]:
                    if neighbour not in graph:
                        # Unknown dependency: reported separately by validate().
                        continue
                    state = colour.get(neighbour, WHITE)
                    if state == WHITE:
                        colour[neighbour] = GRAY
                        stack.append([neighbour, iter(graph.get(neighbour, []))])
                        path.append(neighbour)
                        advanced = True
                        break
                    if state == GRAY:
                        # Back edge: everything from `neighbour` onward is a cycle.
                        if neighbour in path:
                            cycle = path[path.index(neighbour):]
                        else:
                            cycle = [neighbour]
                        key = frozenset(cycle)
                        if key not in seen:
                            seen.add(key)
                            cycles.append(list(cycle))
                if not advanced:
                    colour[node] = BLACK
                    stack.pop()
                    if path and path[-1] == node:
                        path.pop()
        return cycles

    def _cycle_problems(self, tasks: Sequence[Dict[str, Any]]) -> List[str]:
        """Human-readable cycle problems for a task list."""
        return [
            "TASKS.yaml: dependency cycle "
            + " -> ".join(cycle + [cycle[0]])
            + f" ({len(cycle)} task(s)); break one edge with `waive`"
            for cycle in self.find_dependency_cycles(tasks)
        ]

    def validate(self) -> List[str]:
        """Return a list of structural problems found in the state files."""
        problems: List[str] = []
        try:
            self.load_project()
        except StateError as exc:
            problems.append(f"PROJECT.yaml: {exc}")
        try:
            document = self.load_tasks_document()
        except StateError as exc:
            problems.append(f"TASKS.yaml: {exc}")
            return problems

        tasks = document.get("tasks") or []
        known_ids = {task.get("id") for task in tasks if isinstance(task, dict)}
        seen_ids: set = set()
        for task in tasks:
            if not isinstance(task, dict):
                problems.append("TASKS.yaml: every entry under 'tasks' must be a mapping")
                continue
            task_id = task.get("id")
            if not task_id:
                problems.append("TASKS.yaml: task without an 'id'")
                continue
            if task_id in seen_ids:
                problems.append(f"TASKS.yaml: duplicate task id '{task_id}'")
            seen_ids.add(task_id)
            status = task.get("status")
            if status not in config.TASK_STATUSES:
                problems.append(f"TASKS.yaml: task '{task_id}' has invalid status '{status}'")
            # GAP-MED-02: a cycle is a structural defect, not a health warning.
            # validate() is the check operators are told to run, so it has to
            # see cycles — previously only a separate `health` call could.
            raw_dependencies = task.get("dependencies")
            dependency_list = (
                raw_dependencies if isinstance(raw_dependencies, list) else []
            )
            for dep in dependency_list:
                if dep not in known_ids:
                    problems.append(
                        f"TASKS.yaml: task '{task_id}' depends on unknown task '{dep}'"
                    )
                if str(dep) == str(task_id):
                    problems.append(
                        f"TASKS.yaml: task '{task_id}' depends on itself (self-cycle); "
                        "break it with `waive`"
                    )
            if raw_dependencies is not None and not isinstance(raw_dependencies, list):
                problems.append(
                    f"TASKS.yaml: task '{task_id}' 'dependencies' must be a list, "
                    f"got {type(raw_dependencies).__name__} (silently treated as empty)"
                )

        problems.extend(self._cycle_problems([t for t in tasks if isinstance(t, dict)]))
        return problems


__all__ = [
    "StateManager",
    "StateError",
    "StateFileMissingError",
    "StateCorruptedError",
    "TaskNotFoundError",
    "UNTRUSTED_TASK_FIELDS",
    "derive_initial_status",
    "strip_untrusted_task_fields",
    "atomic_write_text",
    "load_yaml_file",
    "save_yaml_file",
    "load_text_file",
    "save_text_file",
    "utc_now_iso",
]
