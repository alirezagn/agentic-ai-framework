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
import sys
from pathlib import Path
from typing import List, Optional

from . import config
from .agents.base_agent import agent_names
from .checkpoint_manager import CheckpointError
from .orchestrator import (
    LoopLimitExceededError,
    MasterOrchestrator,
    MissingAgentError,
    OrchestratorError,
)
from .state_manager import (
    StateError,
    StateManager,
    TaskNotFoundError,
    atomic_write_text,
    save_yaml_file,
    utc_now_iso,
)

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
        version="orchestrator 2.0.0",
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
        help="Project name (created at <dest>/<name>, default dest: projects)",
    )
    init_parser.add_argument(
        "--dest",
        dest="dest",
        default="projects",
        help="Parent directory for the new project (default: projects)",
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
    run_parser.set_defaults(handler=cmd_run)

    health_parser = subparsers.add_parser(
        "health", parents=[shared], help="Run the supervisor health check"
    )
    health_parser.set_defaults(handler=cmd_health)

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


def cmd_init(args: argparse.Namespace) -> int:
    name = getattr(args, "name", None)
    if not name:
        print("error: project name is required", file=sys.stderr)
        return 2
    dest = getattr(args, "dest", None) or "projects"
    target = Path(dest) / name
    force = bool(getattr(args, "force", False))
    if target.exists() and not force:
        print(
            f"ERROR: {target} already exists (use --force to overwrite state files)",
            file=sys.stderr,
        )
        return 2

    goal = getattr(args, "goal", None) or "TBD — define the one-sentence project goal."
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
        target / config.MEMORY_FILE,
        (
            f"# PROJECT_MEMORY — {name}\n\n"
            "## Goal\n\n"
            f"{goal}\n\n"
            "## Status\n\n"
            f"Initialized {now}. No tasks defined yet.\n\n"
            "## Key Decisions\n\n"
            "- (none yet)\n\n"
            "## Next Steps\n\n"
            "- Follow project-templates/NEW_PROJECT_CHECKLIST.md\n"
            "- Create REQUIREMENTS.md with REQ-001.. entries\n"
            "- Add initial tasks to TASKS.yaml\n"
        ),
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

    problems = StateManager(target).validate()
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1

    print(f"Initialized project '{name}' at {target}/")
    print("Next steps:")
    print(f"  1. Write the one-sentence goal: {target}/{config.MEMORY_FILE}")
    print("  2. Create REQUIREMENTS.md and initial TASKS.yaml tasks")
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
    orchestrator.sync_health()
    health = orchestrator.health()
    print(f"Health: {health.state}")
    if health.recommendations:
        for recommendation in health.recommendations:
            print(f"  - {recommendation}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    orchestrator = MasterOrchestrator(_resolve_project(args))
    task_id = getattr(args, "task", None)
    max_tasks = int(getattr(args, "max_tasks", 25) or 25)
    max_concurrent = max(1, int(getattr(args, "max_concurrent", 1) or 1))
    exit_code = 0

    if task_id:
        try:
            result = orchestrator.run_task(task_id)
        except LoopLimitExceededError as exc:
            print(f"LOOP LIMIT: {exc}")
            return 3
        except TaskNotFoundError as exc:
            print(f"ERROR: {exc}")
            return 2
        results = [result]
    else:
        results = orchestrator.run_cycle(max_tasks=max_tasks, max_concurrent=max_concurrent)
        if not results:
            print("No READY tasks available.")

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

    report = orchestrator.sync_health()
    print()
    print(report.render())
    if report.state == config.HEALTH_HUMAN_DECISION_REQUIRED:
        exit_code = max(exit_code, 4)
    return exit_code


def cmd_health(args: argparse.Namespace) -> int:
    orchestrator = MasterOrchestrator(_resolve_project(args))
    report = orchestrator.sync_health()
    print(report.render())
    if report.state == config.HEALTH_HUMAN_DECISION_REQUIRED:
        return 4
    if report.state in (config.HEALTH_STALLED, config.HEALTH_BLOCKED):
        return 3
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
    try:
        return int(handler(args))
    except (StateError, OrchestratorError, MissingAgentError, CheckpointError) as exc:
        print(f"ERROR: {exc}")
        return 2
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
