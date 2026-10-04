"""Command line interface for the orchestrator runtime.

Examples::

    python -m orchestrator.cli init my-project
    python -m orchestrator.cli --project projects/kid-robot-face status
    python -m orchestrator.cli --project projects/kid-robot-face tasks
    python -m orchestrator.cli --project projects/kid-robot-face run
    python -m orchestrator.cli --project projects/kid-robot-face run --task TASK-002
    python -m orchestrator.cli --project projects/kid-robot-face health
    python -m orchestrator.cli --project projects/kid-robot-face checkpoint save cp-001 --notes "phase done"
    python -m orchestrator.cli --project projects/kid-robot-face checkpoint list
    python -m orchestrator.cli --project projects/kid-robot-face checkpoint restore cp-001
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .agents.base_agent import agent_names
from .checkpoint_manager import CheckpointError
from .path_policy import PathPolicyError, validate_relative_name
from .orchestrator import (
    LoopLimitExceededError,
    MasterOrchestrator,
    MissingAgentError,
    OrchestratorError,
    TaskRunResult,
)
from .state_manager import (
    StateError,
    StateFileMissingError,
    StateManager,
    TaskNotFoundError,
    atomic_write_text,
    save_yaml_file,
    utc_now_iso,
)
from .llm_client import LLMClient, LLMError
from .supervisor import HealthReport

logger = logging.getLogger(__name__)


def _setup_logging(verbosity: int = 0, quiet: bool = False) -> None:
    """Configure root logging from CLI flags and ORCHESTRATOR_LOG_LEVEL."""
    if quiet:
        level = logging.ERROR
    elif verbosity >= 1:
        level = logging.DEBUG
    else:
        level_name = os.environ.get("ORCHESTRATOR_LOG_LEVEL", config.DEFAULT_LOG_LEVEL)
        level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.basicConfig(format=config.LOG_FORMAT, level=level, force=True)


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--project",
        dest="project",
        default=argparse.SUPPRESS,
        help="Path to the project directory (e.g. projects/kid-robot-face)",
    )

    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Agentic AI Framework — orchestrator runtime",
    )
    parser.add_argument(
        "--project",
        dest="project",
        default=None,
        help="Path to the project directory (e.g. projects/kid-robot-face)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="orchestrator 3.0.0",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Debug-level logging (repeatable)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Log errors only",
    )

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser(
        "init", help="Scaffold a new project under projects/"
    )
    init_parser.add_argument(
        "name",
        help=(
            "Project name (created at <dest>/<name>, default dest: projects). "
            "When --dest already ends with this name, it is used directly "
            "instead of nesting a second copy"
        ),
    )
    init_parser.add_argument(
        "--dest",
        dest="dest",
        default="projects",
        help=(
            "Parent directory for the new project (default: projects). If its "
            "final component equals the project name, it is used as the "
            "project directory itself"
        ),
    )
    init_parser.add_argument(
        "--goal",
        dest="goal",
        default=None,
        help="One-sentence project goal written into PROJECT_MEMORY.md",
    )
    init_parser.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="Overwrite state files if the project directory already exists",
    )
    init_parser.add_argument(
        "--no-plan",
        dest="no_plan",
        action="store_true",
        help="Skip goal-driven LLM planning (write starter skeleton tasks instead)",
    )
    init_parser.set_defaults(handler=cmd_init)

    tasks_parser = subparsers.add_parser(
        "tasks", parents=[shared], help="Show the task dependency graph"
    )
    tasks_parser.set_defaults(handler=cmd_tasks)

    status_parser = subparsers.add_parser(
        "status", parents=[shared], help="Load the project and print its status"
    )
    status_parser.set_defaults(handler=cmd_status)

    run_parser = subparsers.add_parser(
        "run", parents=[shared], help="Dispatch READY tasks (or one specific task)"
    )
    run_parser.add_argument(
        "--task",
        dest="task",
        default=None,
        help="Run only this task id (e.g. TASK-002)",
    )
    run_parser.add_argument(
        "--max-tasks",
        dest="max_tasks",
        type=int,
        default=25,
        help="Maximum number of tasks dispatched in one cycle (default: 25)",
    )
    run_parser.add_argument(
        "--max-concurrent",
        dest="max_concurrent",
        type=int,
        default=1,
        help="Number of agent executions to overlap in one cycle (default: 1)",
    )
    run_parser.add_argument(
        "--all",
        dest="all",
        action="store_true",
        help=(
            "Keep dispatching waves until every task is terminal: exit 0 when "
            "finished, 3 when blocked/stalled/loop-limited or nothing can reach "
            "READY, 4 when a human decision is required"
        ),
    )
    run_parser.set_defaults(handler=cmd_run)

    plan_parser = subparsers.add_parser(
        "plan",
        parents=[shared],
        help="Generate the task graph from the goal (planning agent)",
    )
    plan_parser.add_argument(
        "--goal",
        dest="goal",
        default=None,
        help="One-sentence goal to plan from (default: PROJECT_MEMORY.md)",
    )
    plan_parser.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="Regenerate even if tasks exist (graph replaced only after a successful plan)",
    )
    plan_parser.add_argument(
        "--max-tasks",
        dest="max_tasks",
        type=int,
        default=8,
        help="Maximum tasks the planning agent may emit (default: 8)",
    )
    plan_parser.set_defaults(handler=cmd_plan)

    health_parser = subparsers.add_parser(
        "health", parents=[shared], help="Run the supervisor health check"
    )
    health_parser.add_argument(
        "--diagnose",
        dest="diagnose",
        action="store_true",
        help="Also print a supervisor diagnosis (LLM when configured, rules-only otherwise)",
    )
    health_parser.set_defaults(handler=cmd_health)

    phase_parser = subparsers.add_parser(
        "phase",
        parents=[shared],
        help="Show or set the lifecycle phase (REQUIREMENTS..MAINTENANCE)",
    )
    phase_parser.add_argument(
        "action",
        choices=["show", "set"],
        help="show the current phase, or set it explicitly",
    )
    phase_parser.add_argument(
        "value",
        nargs="?",
        default=None,
        help="Phase name for 'set' (e.g. IMPLEMENTATION)",
    )
    phase_parser.set_defaults(handler=cmd_phase)

    waive_parser = subparsers.add_parser(
        "waive",
        parents=[shared],
        help="Drop a blocking dependency edge (human unblock for deadlocks)",
    )
    waive_parser.add_argument("task", help="Task to unblock (e.g. TASK-004)")
    waive_parser.add_argument(
        "--dep",
        dest="dep",
        required=True,
        help="Dependency id to remove (e.g. TASK-003)",
    )
    waive_parser.add_argument(
        "--reason",
        nargs="?",
        dest="reason",
        default="",
        help="Reason recorded in CHANGELOG.md",
    )
    waive_parser.set_defaults(handler=cmd_waive)

    retry_parser = subparsers.add_parser(
        "retry",
        parents=[shared],
        help="Reset a stalled/loop-limited task so it can dispatch again",
    )
    retry_parser.add_argument("task", help="Task to retry (e.g. TASK-003)")
    retry_parser.add_argument(
        "--reason",
        nargs="?",
        dest="reason",
        default="",
        help="Reason recorded in CHANGELOG.md",
    )
    retry_parser.set_defaults(handler=cmd_retry)

    reopen_parser = subparsers.add_parser(
        "reopen",
        parents=[shared],
        help="Consciously overturn a finished (DONE/CANCELLED) task back to READY",
    )
    reopen_parser.add_argument("task", help="Task to reopen (e.g. TASK-003)")
    reopen_parser.add_argument(
        "--reason",
        nargs="?",
        dest="reason",
        default="",
        help="Why the finished task is being overturned (required; feeds the next prompt)",
    )
    reopen_parser.set_defaults(handler=cmd_reopen)

    agents_parser = subparsers.add_parser(
        "agents", parents=[shared], help="List registered specialist agents"
    )
    agents_parser.set_defaults(handler=cmd_agents)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint", parents=[shared], help="Checkpoint save/list/restore operations"
    )
    checkpoint_parser.add_argument(
        "action",
        choices=["save", "list", "restore"],
        help="Checkpoint action to perform",
    )
    checkpoint_parser.add_argument(
        "--checkpoint",
        dest="checkpoint",
        default=None,
        help="Checkpoint id (required for save and restore)",
    )
    checkpoint_parser.add_argument(
        "--checkpoint-id",
        dest="checkpoint_id",
        default=None,
        help=argparse.SUPPRESS,
    )
    checkpoint_parser.add_argument(
        "--notes",
        dest="notes",
        default="",
        help="Recovery notes stored with the checkpoint",
    )
    checkpoint_parser.set_defaults(handler=cmd_checkpoint)

    return parser


def _resolve_project(args: argparse.Namespace) -> str:
    project = getattr(args, "project", None)
    if not project:
        print(
            "error: --project is required (e.g. --project projects/kid-robot-face)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return project


def _echo_goal(goal: str) -> None:
    """Show the effective goal (init/plan) and flag placeholder text.

    A copied placeholder (``…``) persists as the project goal and steers the
    planner off-target, so the goal must be visible at the moment of use.
    """
    text = " ".join(str(goal or "").split())
    display = (text[:157] + "...") if len(text) > 160 else text
    print(f"Goal: {display or '(none)'}")
    lowered = text.lower()
    if not text or lowered in {"…", "...", "<your goal>"} or lowered.startswith("tbd"):
        print(
            "warning: goal looks like a placeholder — the plan will not match "
            'your intent; pass --goal "your one-sentence goal"',
            file=sys.stderr,
        )


def resolve_init_target(dest: Optional[str], name: str) -> Path:
    """Directory ``init`` will scaffold ``name`` into.

    ``init`` joins ``<dest>/<name>``, which is right when ``--dest`` is a
    *parent* directory but wrong when the user has already named the output
    folder themselves::

        init my_app --dest /tmp      ->  /tmp/my_app          (parent given)
        init my_app --dest /tmp/my_app -> /tmp/my_app         (folder given)

    The second form used to produce ``/tmp/my_app/my_app``. That is not a
    cosmetic difference: the project then lives one level below where the user
    pointed, so the path they pass to every later ``--project`` is wrong, and the
    duplicated directory is what they see when they go looking for it.

    So when the last component of ``--dest`` already *is* the project name, the
    destination is used as-is. Compared on the final component only, which keeps
    a parent that merely happens to share the suffix (``--dest /srv/my_appiles``)
    nesting correctly.

    The match is exact. A case-differing name (``--dest /tmp/My_App`` with
    ``init my_app``) still nests, deliberately: guessing at case-insensitive
    filesystems would silently reinterpret the user's path, whereas an extra
    level is visible and reversible.
    """
    parent = Path(dest) if dest else Path("projects")
    if parent.name and parent.name == name:
        return parent
    return parent / name


def cmd_init(args: argparse.Namespace) -> int:
    name = str(getattr(args, "name", None) or "").strip()
    if not name:
        print("error: project name is required", file=sys.stderr)
        return 2
    # The name becomes a directory component, so it must be a safe *relative*
    # path, not merely non-empty. Without this check `init "../../tmp/evil"
    # --dest /some/where` resolves outside --dest and scaffolds a complete
    # project wherever the traversal lands; it was previously blocked only by
    # accident, when the destination happened to exist and tripped the --force
    # guard.
    try:
        name = validate_relative_name(name, context="project name")
    except PathPolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    target = resolve_init_target(getattr(args, "dest", None), name)
    force = bool(getattr(args, "force", False))
    if target.exists() and not force:
        print(
            f"ERROR: {target} already exists (use --force to overwrite state files)",
            file=sys.stderr,
        )
        return 2

    goal = getattr(args, "goal", None) or "TBD — define the one-sentence project goal."
    _echo_goal(goal)
    now = utc_now_iso()
    target.mkdir(parents=True, exist_ok=True)

    save_yaml_file(
        target / config.PROJECT_FILE,
        {
            "project": {
                "id": name,
                "name": name,
                "version": "0.1.0",
                "status": "REQUIREMENTS",
            },
            "phase": {"current": "REQUIREMENTS"},
            "progress": {"requirements": 0, "implementation": 0},
            "health": {
                "status": "HEALTHY",
                "blocked_tasks": 0,
                "failed_tasks": 0,
                "loop_detected": False,
                "deadlock_detected": False,
            },
            "context": {
                "utilization_percent": 0,
                "compaction_threshold": config.COMPACTION_THRESHOLDS.compaction_percent,
                "critical_threshold": config.COMPACTION_THRESHOLDS.critical_percent,
            },
            "agents": {"orchestrator": "ACTIVE", "supervisor": "ACTIVE"},
            "next_tasks": [],
            "blockers": [],
            "human_decisions": [],
            "constraints": {"schedule": "TBD", "team": "TBD", "standards": "TBD"},
            "budget": {"hours": None, "tokens": None},
            "resources": {"hardware": [], "services": [], "people": []},
            "last_checkpoint": {
                "id": "none",
                "date": now[:10],
                "phase": "REQUIREMENTS",
            },
            "updated_at": now,
        },
    )
    save_yaml_file(
        target / config.TASKS_FILE,
        {
            "tasks": [],
            "parallel_groups": [],
            "critical_path": {"path": []},
            "summary": {"total": 0},
        },
    )
    atomic_write_text(
        target / config.CURRENT_STATE_FILE,
        (
            f"# CURRENT_STATE — {name}\n\n"
            f"- Phase: REQUIREMENTS\n"
            f"- Health: HEALTHY\n"
            f"- Updated: {now}\n\n"
            "Freshly initialized project; no work executed yet.\n"
        ),
    )
    atomic_write_text(
        target / config.DECISIONS_FILE,
        f"# DECISIONS — {name}\n\nNo decisions recorded yet.\n",
    )
    atomic_write_text(
        target / config.RISKS_FILE,
        f"# RISKS — {name}\n\nNo risks recorded yet.\n",
    )
    atomic_write_text(
        target / config.CHANGELOG_FILE,
        f"# CHANGELOG — {name}\n\n- {now} project initialized (orchestrator init)\n",
    )
    atomic_write_text(
        target / "docs" / "README.md",
        f"# {name} — artifacts\n\nMaterialized agent artifacts land in this folder.\n",
    )

    # G3: generate the task graph — LLM plan when a backend is configured,
    # deterministic starter skeleton otherwise (--no-plan forces the latter).
    seeded: List[str] = []
    no_plan = bool(getattr(args, "no_plan", False))
    if not no_plan and LLMClient.is_available():
        print("Generating task graph with the planning agent (--no-plan to skip)...")
        try:
            seeded = MasterOrchestrator(target).build_plan(goal=goal)
            print(f"  generated {len(seeded)} tasks: {', '.join(seeded)}")
        except Exception as exc:  # noqa: BLE001 - never fail init on LLM issues
            print(
                f"warning: planning agent failed, writing starter tasks: {exc}",
                file=sys.stderr,
            )
            seeded = []
    if not seeded:
        try:
            seeded = StateManager(target).seed_starter_tasks(goal)
        except StateError as exc:
            print(f"warning: starter tasks not written: {exc}", file=sys.stderr)
            seeded = []
        if seeded:
            print(f"  starter task graph: {len(seeded)} tasks (edit {target}/TASKS.yaml)")

    if seeded:
        status_line = (
            f"Initialized {now}. Task graph generated: {len(seeded)} tasks "
            f"({seeded[0]}..{seeded[-1]})."
        )
        next_steps = (
            "- Review/edit the generated task graph: TASKS.yaml\n"
            "- Create REQUIREMENTS.md with REQ-001.. entries\n"
            "- Follow project-templates/NEW_PROJECT_CHECKLIST.md\n"
        )
    else:
        status_line = f"Initialized {now}. No tasks defined yet — run `plan`."
        next_steps = (
            "- Generate the task graph: `orchestrator plan --project .`\n"
            "- Create REQUIREMENTS.md with REQ-001.. entries\n"
            "- Follow project-templates/NEW_PROJECT_CHECKLIST.md\n"
        )
    atomic_write_text(
        target / config.MEMORY_FILE,
        (
            f"# PROJECT_MEMORY — {name}\n\n"
            "## Goal\n\n"
            f"{goal}\n\n"
            "## Status\n\n"
            f"{status_line}\n\n"
            "## Key Decisions\n\n"
            "- (none yet)\n\n"
            "## Next Steps\n\n"
            f"{next_steps}"
        ),
    )

    problems = StateManager(target).validate()
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1

    # A7: baseline checkpoint so a fresh project can always be restored.
    try:
        orchestrator = MasterOrchestrator(target)
        # G11a: a leftover baseline from an earlier init of the same name
        # (e.g. `init --force` over a reused workspace) would freeze stale
        # state — refresh it so cp-000-init always matches this init.
        orchestrator.checkpoints.delete_checkpoint("cp-000-init")
        orchestrator.create_checkpoint(
            "cp-000-init", notes="Project initialized (orchestrator init)"
        )
    except (CheckpointError, StateError, OrchestratorError) as exc:
        print(f"warning: init checkpoint not created: {exc}", file=sys.stderr)

    print(f"Initialized project '{name}' at {target}/")
    print("Next steps:")
    if seeded:
        print(f"  1. Review the task graph: {target}/TASKS.yaml")
        print(f"  2. Refine the goal and write REQUIREMENTS.md (REQ-001..) in {target}/")
    else:
        print(f"  1. Generate the task graph: orchestrator plan --project {target}")
        print(f"  2. Write the goal and REQUIREMENTS.md in {target}/")
    print("  3. Full checklist: project-templates/NEW_PROJECT_CHECKLIST.md")
    print(f"  4. orchestrator --project {target} tasks")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    project = _resolve_project(args)
    state = StateManager(project)
    tasks = state.load_tasks()
    document = state.load_tasks_document()
    if not tasks:
        print("No tasks defined.")
        return 0

    project_doc = state.load_project()
    project_block = project_doc.get("project") or {}
    ready_ids = {task.get("id") for task in state.get_ready_tasks()}
    counts = state.summary_counts()

    print("=" * 78)
    print(
        f"TASK DEPENDENCY GRAPH — {project_block.get('name', Path(project).name)}"
    )
    print("=" * 78)
    header = f"{'ID':<10} {'STATUS':<8} {'OWNER':<24} {'PRI':<9} {'DEPS':<18} READY"
    print(header)
    print("-" * len(header))
    for task in tasks:
        deps = ", ".join(task.get("dependencies") or []) or "-"
        if len(deps) > 16:
            deps = deps[:15] + "…"
        ready = "yes" if task.get("id") in ready_ids else ""
        print(
            f"{str(task.get('id', '?')):<10} "
            f"{str(task.get('status', '?')):<8} "
            f"{str(task.get('owner', '?')):<24} "
            f"{str(task.get('priority', '?')):<9} "
            f"{deps:<18} {ready}"
        )
    print("-" * len(header))
    summary_parts = [
        f"{status}={counts.get(status, 0)}" for status in config.TASK_STATUSES
    ]
    print(f"TOTAL {counts.get('TOTAL', 0)}  ({', '.join(summary_parts)})")
    if ready_ids:
        print(f"Ready now: {', '.join(sorted(str(i) for i in ready_ids))}")
    else:
        print("Ready now: (none)")
    critical_path = document.get("critical_path") or {}
    raw_path = critical_path.get("path") or ""
    if isinstance(raw_path, (list, tuple)):
        path_text = " -> ".join(str(item) for item in raw_path)
    else:
        path_text = str(raw_path)
    if path_text:
        print(f"Critical path: {path_text}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    orchestrator = MasterOrchestrator(_resolve_project(args))
    print(orchestrator.print_status())
    # GAP-HIGH-04: `status` is the command an operator runs when a run stalls
    # and no error was printed, so structural problems belong here. The deep
    # validate() finds the cause (orphan, cycle, unknown owner, unknown
    # dependency) that health heuristics cannot see. Reported, not fatal: a
    # human may be mid-edit, and `status` should show state and exit 0.
    problems = orchestrator.state.validate()
    if problems:
        print(f"Structural problems ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
    orchestrator.sync_health()
    health = orchestrator.health()
    print(f"Health: {health.state}")
    if health.recommendations:
        for recommendation in health.recommendations:
            print(f"  - {recommendation}")
    return 0


def _maybe_generate_graph(orchestrator: MasterOrchestrator) -> None:
    """G5: an empty graph is generated on the fly when a backend exists."""
    if orchestrator.state.load_tasks():
        return
    if LLMClient.is_available():
        print("Task graph is empty — generating it with the planning agent...")
        try:
            created = orchestrator.build_plan()
            print(f"  generated {len(created)} tasks: {', '.join(created)}")
        except (StateError, OrchestratorError, LLMError) as exc:
            print(f"  auto-plan failed: {exc}")
    else:
        print(
            "Task graph is empty — run `orchestrator plan` or edit TASKS.yaml "
            "(LLM backend not configured)."
        )


def _print_run_results(results: List[TaskRunResult]) -> int:
    """Print one wave of dispatch results; return the exit code they imply."""
    exit_code = 0
    for result in results:
        marker = "OK" if result.succeeded else "FAIL"
        print(f"[{marker}] {result.task_id} ({result.agent_id}): "
              f"{result.previous_status} -> {result.new_status}")
        print(f"       {result.output.summary}")
        if result.output.warnings:
            for warning in result.output.warnings:
                print(f"       warning: {warning}")
        if result.loop is not None:
            print(f"       LOOP: {result.loop.kind} {result.loop.count}/{result.loop.threshold}")
            exit_code = max(exit_code, 3)
        if result.checkpoint_id:
            print(f"       checkpoint: {result.checkpoint_id}")
        if not result.succeeded and result.output.errors:
            for error in result.output.errors[:3]:
                print(f"       error: {error}")

    retryable = [r.task_id for r in results if not r.succeeded]
    if retryable:
        print(
            "hint: fix the task inputs/strategy, then run "
            f"`orchestrator retry {retryable[0]}` to reset counters and re-dispatch"
        )
    return exit_code


def _all_tasks_terminal(orchestrator: MasterOrchestrator) -> bool:
    """True when the graph holds tasks and none of them is still in progress.

    Counts are used instead of ``phase.current`` because the phase is stored
    and forward-only: after a reopen or a re-plan it can read RELEASE while
    real work remains.
    """
    counts = orchestrator.state.summary_counts()
    if counts.get("TOTAL", 0) <= 0:
        return False
    active = sum(
        count
        for status, count in counts.items()
        if status != "TOTAL" and status not in config.TERMINAL_TASK_STATUSES
    )
    return active == 0


def _wave_summary(counts: Dict[str, Any]) -> str:
    order = (
        config.TASK_IN_PROGRESS,
        config.TASK_READY,
        config.TASK_REVIEW,
        config.TASK_TODO,
        config.TASK_BLOCKED,
        config.TASK_WAITING,
        config.TASK_FAILED,
        config.TASK_DONE,
        config.TASK_DONE_WITH_LIMITATION,
        config.TASK_CANCELLED,
        config.TASK_WAIVED,
    )
    parts = [
        f"{status} {counts.get(status, 0)}"
        for status in order
        if counts.get(status, 0)
    ]
    return " | ".join(parts) or "no tasks"


def _print_health_reasons(report: HealthReport) -> None:
    """Print *why* a run stopped: blocking escalations first, then hints.

    Health can be HUMAN_DECISION_REQUIRED because of a pending decision, a
    starvation escalation, or exhausted retries — naming one cause when the
    operator sees another sends them looking in the wrong file.
    """
    for escalation in report.escalations:
        if not escalation.blocking:
            continue
        print(f"  escalation: {escalation.reason}")
        for option in escalation.options:
            print(f"    - {option}")
    for recommendation in report.recommendations:
        print(f"  - {recommendation}")


def _run_all(
    orchestrator: MasterOrchestrator, max_tasks: int, max_concurrent: int
) -> int:
    """``run --all``: dispatch wave after wave until every task is terminal.

    Exit codes mirror a single ``run``: ``0`` everything finished, ``3``
    blocked/stalled/loop limit/nothing left that can reach READY, ``4`` a
    human decision is required. One wave is one ``run_cycle`` — the same unit
    a hand-written ``while`` loop around the CLI would run — so partial
    progress is already persisted when the loop stops.
    """
    _maybe_generate_graph(orchestrator)
    wave = 0
    while True:
        if _all_tasks_terminal(orchestrator):
            print()
            print("SUCCESS: every task is terminal (DONE / CANCELLED).")
            print(f"  {_wave_summary(orchestrator.state.summary_counts())}")
            return 0

        report = orchestrator.sync_health()
        if report.state == config.HEALTH_HUMAN_DECISION_REQUIRED:
            # "human decision" is not always a pending DEC-NNN: a blocking
            # escalation (starvation, deadlock, exhausted retries) sets the
            # same state, so print *why* it stopped instead of naming one
            # cause the operator may not have.
            print(
                "STOPPED: a human decision is required — resolve what is listed "
                "below, then rerun `run --all` "
                "(details: `orchestrator health --diagnose`)."
            )
            _print_health_reasons(report)
            print(f"Health: {report.state}")
            return 4
        if report.state in (config.HEALTH_STALLED, config.HEALTH_BLOCKED):
            print(
                f"STOPPED: health {report.state} — nothing can proceed "
                "(details: `orchestrator health --diagnose`)."
            )
            _print_health_reasons(report)
            return 3

        wave += 1
        results = orchestrator.run_cycle(
            max_tasks=max_tasks, max_concurrent=max_concurrent
        )
        if not results:
            # Nothing READY and not finished: the remaining tasks have no path
            # to READY. Stopping here is what keeps --all from spinning.
            print("No READY tasks available.")
            print(
                "not finished — remaining tasks cannot reach READY; "
                "inspect them with `orchestrator status`."
            )
            _print_health_reasons(report)
            print(f"Health: {report.state} (details: `orchestrator health --diagnose`)")
            return 3

        wave_exit = _print_run_results(results)
        print(
            f"wave {wave}: {len(results)} result(s) — "
            f"{_wave_summary(orchestrator.state.summary_counts())}"
        )
        if wave_exit:
            print(
                "STOPPED: a loop limit was hit — fix the task inputs, then run "
                "`orchestrator retry <task> --reason \"...\"` and rerun `run --all`."
            )
            return wave_exit


def cmd_run(args: argparse.Namespace) -> int:
    orchestrator = MasterOrchestrator(_resolve_project(args))
    task_id = getattr(args, "task", None)
    max_tasks = int(getattr(args, "max_tasks", 25) or 25)
    max_concurrent = max(1, int(getattr(args, "max_concurrent", 1) or 1))
    exit_code = 0

    if task_id and getattr(args, "all", False):
        print("ERROR: --all and --task are mutually exclusive.")
        return 2

    if getattr(args, "all", False):
        return _run_all(orchestrator, max_tasks=max_tasks, max_concurrent=max_concurrent)

    if task_id:
        try:
            result = orchestrator.run_task(task_id)
        except LoopLimitExceededError as exc:
            print(f"LOOP LIMIT: {exc}")
            print(
                "hint: fix the task inputs/strategy, then run "
                f"`orchestrator retry {task_id}` to reset its loop counters"
            )
            return 3
        except TaskNotFoundError as exc:
            print(f"ERROR: {exc}")
            return 2
        results = [result]
    else:
        _maybe_generate_graph(orchestrator)
        results = orchestrator.run_cycle(max_tasks=max_tasks, max_concurrent=max_concurrent)
        if not results:
            # Nothing to dispatch: give a one-line health summary instead of
            # dumping the full report on every idle cycle of a run loop.
            print("No READY tasks available.")
            report = orchestrator.sync_health()
            detail = (
                " (details: `orchestrator health --diagnose`)"
                if report.state != config.HEALTH_HEALTHY
                else ""
            )
            print(f"Health: {report.state}{detail}")
            if report.state == config.HEALTH_HUMAN_DECISION_REQUIRED:
                return 4
            if report.state in (config.HEALTH_STALLED, config.HEALTH_BLOCKED):
                return 3
            return 0

    exit_code = max(exit_code, _print_run_results(results))

    report = orchestrator.sync_health()
    print()
    print(report.render())
    if report.state == config.HEALTH_HUMAN_DECISION_REQUIRED:
        exit_code = max(exit_code, 4)
    return exit_code


def cmd_plan(args: argparse.Namespace) -> int:
    """G4: generate the task graph (LLM plan, starter skeleton without a backend)."""
    project = _resolve_project(args)
    orchestrator = MasterOrchestrator(project)
    effective_goal = (getattr(args, "goal", None) or "").strip()
    if not effective_goal:
        try:
            stored = orchestrator.state.load_memory().strip()
        except (StateError, FileNotFoundError):
            stored = ""
        match = re.search(r"^##\s+Goal\s*\n+(.*?)(?=^##\s|\Z)", stored, re.S | re.M)
        effective_goal = match.group(1).strip() if match else stored
    _echo_goal(effective_goal)
    if not LLMClient.is_available():
        goal = getattr(args, "goal", None) or ""
        try:
            created = orchestrator.state.seed_starter_tasks(goal)
        except StateError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if created:
            print(f"Starter task graph written ({len(created)} tasks): {', '.join(created)}")
            print(
                "LLM backend not configured — edit "
                f"{project}/TASKS.yaml to refine the goal-derived tasks."
            )
            return 0
        print(
            "ERROR: LLM backend not configured and the graph already has tasks; "
            "edit TASKS.yaml directly.",
            file=sys.stderr,
        )
        return 2
    try:
        created = orchestrator.build_plan(
            goal=getattr(args, "goal", None),
            max_tasks=int(getattr(args, "max_tasks", 8) or 8),
            force=bool(getattr(args, "force", False)),
        )
    except StateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OrchestratorError, LLMError) as exc:
        print(f"ERROR: planning failed: {exc}", file=sys.stderr)
        return 2
    print(f"Generated {len(created)} tasks: {', '.join(created)}")
    print(f"Edit {project}/TASKS.yaml to adjust.")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    orchestrator = MasterOrchestrator(_resolve_project(args))
    report = orchestrator.sync_health()
    print(report.render())
    if bool(getattr(args, "diagnose", False)):
        print()
        print("DIAGNOSIS")
        print("-" * 40)
        print(orchestrator.supervisor.diagnose())
    if report.state == config.HEALTH_HUMAN_DECISION_REQUIRED:
        return 4
    if report.state in (config.HEALTH_STALLED, config.HEALTH_BLOCKED):
        return 3
    return 0


def cmd_phase(args: argparse.Namespace) -> int:
    project = _resolve_project(args)
    state = StateManager(project)
    action = getattr(args, "action", "show")
    value = getattr(args, "value", None)

    if action == "show" or not value:
        current = str(
            (state.load_project().get("phase") or {}).get("current")
            or config.PHASE_REQUIREMENTS
        )
        derived = state.derive_phase()
        print(f"Current phase: {current}")
        print(f"Derived phase: {derived}  (from task graph)")
        print(f"Known phases:  {', '.join(config.PHASES)}")
        return 0

    phase = str(value).strip().upper()
    previous = str(
        (state.load_project().get("phase") or {}).get("current")
        or config.PHASE_REQUIREMENTS
    )
    state.set_phase(phase)  # validates against config.PHASES
    state.append_changelog(f"Phase set to {phase} (was {previous})")
    print(f"Phase: {previous} -> {phase}")

    if config.phase_index(phase) > config.phase_index(previous):
        orchestrator = MasterOrchestrator(project)
        checkpoint_id = f"cp-phase-{phase.lower()}"
        existing = {
            str(entry.get("id")) for entry in orchestrator.checkpoints.list_checkpoints()
        }
        if checkpoint_id not in existing:
            orchestrator.create_checkpoint(
                checkpoint_id, notes=f"Phase set to {phase}"
            )
            print(f"Checkpoint: {checkpoint_id}")
    return 0


def cmd_waive(args: argparse.Namespace) -> int:
    project = _resolve_project(args)
    state = StateManager(project)
    task_id = str(getattr(args, "task", "") or "")
    dep_id = str(getattr(args, "dep", "") or "")
    reason = str(getattr(args, "reason", "") or "")
    task = state.relax_dependency(task_id, dep_id)
    entry = f"Dependency {dep_id} waived on {task_id}"
    if reason:
        entry += f": {reason}"
    state.append_changelog(entry)
    print(f"{entry} (task is now {task.get('status')})")
    return 0


def _recovery_feedback(task: dict, reason: str) -> str:
    """Feedback that must reach the next dispatch prompt after a recovery.

    The human reason (or, absent one, the failure that caused the recovery)
    is stored as ``execution.retry_reason`` — without it the model is blind
    to why previous attempts failed and repeats them.
    """
    prev_error = str(((task.get("execution") or {}).get("last_error")) or "")
    feedback = str(reason or "").strip()
    if feedback and prev_error and prev_error not in feedback:
        feedback = f"{feedback}\nprevious failure: {prev_error}"
    elif not feedback:
        feedback = prev_error
    return feedback


def cmd_retry(args: argparse.Namespace) -> int:
    """Human recovery: clear a task's loop counters so dispatch accepts it again."""
    project = _resolve_project(args)
    state = StateManager(project)
    task_id = str(getattr(args, "task", "") or "").strip()
    reason = str(getattr(args, "reason", "") or "")
    try:
        task = state.get_task(task_id)
    except TaskNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2
    status = str(task.get("status") or "")
    if status in config.TERMINAL_TASK_STATUSES:
        print(
            f"ERROR: {task_id} is {status}; retry applies to active tasks only — "
            f'for a finished task use: orchestrator reopen {task_id} --reason "..."'
        )
        return 2
    feedback = _recovery_feedback(task, reason)
    state.update_task_execution(
        task_id,
        set_values={
            "attempts_since_change": 0,
            "strategy_changes": 0,
            "no_progress_cycles": 0,
            "evidence_stall_count": 0,
            "repeated_output_count": 0,
            "last_output_hash": None,
            "last_error": None,
            "retry_reason": feedback or None,
            "recovering": False,
            "validation_failure_streak": 0,
        },
    )
    if status in (config.TASK_READY, config.TASK_FAILED, config.TASK_IN_PROGRESS):
        state.update_task_status(task_id, config.TASK_READY)
    state.refresh_ready_states()
    final = str(state.get_task(task_id).get("status") or "")
    entry = f"retry {task_id} (loop counters reset)"
    if reason:
        entry += f": {reason}"
    state.append_changelog(entry)
    if final == config.TASK_READY:
        print(f"{entry} — status READY; `orchestrator run` will dispatch it")
    else:
        print(f"{entry} — status {final} (promotes to READY when dependencies finish)")
    return 0


def cmd_reopen(args: argparse.Namespace) -> int:
    """Consciously overturn a terminal task (DONE/CANCELLED) back to READY.

    Deliberately separate from ``retry``: re-running finished work must be an
    explicit, reasoned act (no sed on TASKS.yaml), and the reason feeds the
    next attempt's prompt like ``retry --reason`` does.
    """
    project = _resolve_project(args)
    state = StateManager(project)
    task_id = str(getattr(args, "task", "") or "").strip()
    reason = str(getattr(args, "reason", "") or "").strip()
    try:
        task = state.get_task(task_id)
    except TaskNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2
    status = str(task.get("status") or "")
    if status not in config.TERMINAL_TASK_STATUSES:
        print(
            f"ERROR: {task_id} is {status}; reopen applies to terminal tasks only "
            f"({', '.join(config.TERMINAL_TASK_STATUSES)}) — use retry for "
            "failed/active tasks"
        )
        return 2
    if not reason:
        print(
            f"ERROR: reopen {task_id} requires --reason "
            "(record why the finished task is being overturned)"
        )
        return 2
    state.update_task_execution(
        task_id,
        set_values={
            "attempts_since_change": 0,
            "strategy_changes": 0,
            "no_progress_cycles": 0,
            "evidence_stall_count": 0,
            "repeated_output_count": 0,
            "last_output_hash": None,
            "last_error": None,
            "retry_reason": _recovery_feedback(task, reason) or None,
            "recovering": False,
            "validation_failure_streak": 0,
        },
    )
    state.update_task_status(task_id, config.TASK_READY)
    state.refresh_ready_states()
    entry = f"reopen {task_id} from {status}: {reason}"
    state.append_changelog(entry)
    final = str(state.get_task(task_id).get("status") or "")
    if final == config.TASK_READY:
        print(f"{entry} — status READY; `orchestrator run` will dispatch it")
    else:
        print(f"{entry} — status {final} (promotes to READY when dependencies finish)")
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    _resolve_project(args)
    names = agent_names()
    if not names:
        print("No agents registered.")
        return 0
    print(f"Registered agents ({len(names)}):")
    for name in names:
        print(f"  - {name}")
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    project = _resolve_project(args)
    orchestrator = MasterOrchestrator(project)
    action = getattr(args, "action", None)
    checkpoint_id = getattr(args, "checkpoint", None) or getattr(args, "checkpoint_id", None)

    if action == "list":
        print(orchestrator.checkpoints.print_checkpoint_list())
        return 0

    if not checkpoint_id:
        print("ERROR: --checkpoint <id> is required for save and restore")
        return 2

    if action == "save":
        notes = getattr(args, "notes", "") or ""
        orchestrator.create_checkpoint(checkpoint_id, notes=notes)
        print(f"Checkpoint '{checkpoint_id}' saved for project '{orchestrator.state.project_path.name}'.")
        return 0

    if action == "restore":
        try:
            orchestrator.resume_from_checkpoint(checkpoint_id)
        except CheckpointError as exc:
            print(f"ERROR: {exc}")
            return 2
        print(f"Checkpoint '{checkpoint_id}' restored; state reloaded.")
        print(orchestrator.print_status())
        return 0

    print(f"ERROR: unknown checkpoint action '{action}'")
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    config.maybe_load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(
        verbosity=int(getattr(args, "verbose", 0) or 0),
        quiet=bool(getattr(args, "quiet", False)),
    )
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    command_name = type(args).__name__ or "unknown"
    config.emit_telemetry(
        config.TELEMETRY_EVENT_COMMAND, command=command_name, level="INFO"
    )
    try:
        exit_code = int(handler(args))
    except StateFileMissingError as exc:
        print(f"ERROR: {exc}")
        # Common cause: the workspace was wiped (rm -rf + rsync) without
        # re-running init — print the exact recovery command.
        marker = "State file not found: "
        message = str(exc)
        if marker in message:
            project_dir = Path(message.split(marker, 1)[1].strip()).parent
            print(
                f"hint: '{project_dir}' has no orchestrator state; re-initialize with:\n"
                f"      orchestrator init {project_dir.name} "
                f"--dest {project_dir.parent} --force --goal \"<your goal>\""
            )
        _telemetry_error(command_name, exc)
        return 2
    except (StateError, OrchestratorError, MissingAgentError, CheckpointError) as exc:
        logger.error("command failed: %s", exc)
        print(f"ERROR: {exc}")
        _telemetry_error(command_name, exc)
        return 2
    except FileNotFoundError as exc:
        logger.error("file not found: %s", exc)
        print(f"ERROR: {exc}")
        _telemetry_error(command_name, exc)
        return 2
    except KeyboardInterrupt:
        # Exit 130 is the conventional shell code for SIGINT, and is distinct
        # from 2 (state error) and 3 (loop limit) so a script can tell "the
        # operator stopped this" from "the orchestrator gave up".
        logger.warning("interrupted by operator")
        print("\nERROR: interrupted", file=sys.stderr)
        # `exc` is not bound in this handler (only in the ones below), and
        # referencing it turned every Ctrl-C into a NameError instead of 130.
        _telemetry_error(command_name, KeyboardInterrupt(), level="WARNING")
        return 130
    except Exception as exc:
        # GAP-MED-01. Last-resort handler. Filesystem conditions (ENOSPC, EACCES,
        # EISDIR) and provider exceptions outside the modelled types used to
        # escape as an interpreter traceback with exit code 1 — the code the
        # guide reserves for "no command". An operator cannot act on a
        # traceback, so it is logged in full and summarised on stderr.
        #
        # The traceback goes to the log, not to stdout: the operator gets an
        # actionable line, and `-v` (or ORCHESTRATOR_LOG_LEVEL=DEBUG) keeps the
        # frames available for diagnosis.
        logger.exception("unhandled %s in %s", type(exc).__name__, command_name)
        print(
            f"ERROR: {type(exc).__name__}: {exc}\n"
            f"hint: this is an unhandled internal error; re-run with -v for the "
            f"traceback, or report it with the state files",
            file=sys.stderr,
        )
        _telemetry_error(command_name, exc, unhandled=True)
        return 2
    config.emit_telemetry(
        config.TELEMETRY_EVENT_COMMAND_DONE,
        command=command_name,
        level="INFO",
        exit_code=exit_code,
    )
    return exit_code


def _telemetry_error(
    command_name: str,
    exc: BaseException,
    level: str = "ERROR",
    unhandled: bool = False,
) -> None:
    """Record a command failure as one structured event (GAP-MED-01).

    Split out so every failure path records the same shape, and so a failure
    cannot be added without also being made observable.
    """
    config.emit_telemetry(
        config.TELEMETRY_EVENT_ERROR,
        command=command_name,
        level=level,
        exc_type=type(exc).__name__,
        message=str(exc),
        unhandled=bool(unhandled),
    )


if __name__ == "__main__":
    sys.exit(main())
