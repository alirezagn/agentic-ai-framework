"""SupervisorAgent — independent health, loop and deadlock monitor.

The Supervisor observes the project; it never marks implementation work as
complete. It performs lookups over TASKS.yaml and PROJECT.yaml to catch:

* circular dependencies (deadlocks) in the task graph,
* repeating loops — same failing strategy after 3 attempts, 5 cycles without
  progress, or exhausted alternative strategies,
* context pressure against the compaction benchmarks (70% / 85%),
* blocked tasks that unnecessarily starve unrelated work.

When limits are exceeded it emits escalations that move the project into
``HUMAN_DECISION_REQUIRED``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import config
from .state_manager import StateError, StateManager, utc_now_iso

logger = logging.getLogger(__name__)


class SupervisorError(RuntimeError):
    """Raised for unrecoverable supervisor configuration/state problems."""


@dataclass
class HealthState:
    """String enum wrapper: compare ``report.state == HealthState.HEALTHY``."""

    value: str

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HealthState):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)


HealthState.HEALTHY = HealthState(config.HEALTH_HEALTHY)          # type: ignore[attr-defined]
HealthState.WARNING = HealthState(config.HEALTH_WARNING)          # type: ignore[attr-defined]
HealthState.STALLED = HealthState(config.HEALTH_STALLED)          # type: ignore[attr-defined]
HealthState.BLOCKED = HealthState(config.HEALTH_BLOCKED)          # type: ignore[attr-defined]
HealthState.RECOVERY = HealthState(config.HEALTH_RECOVERY)        # type: ignore[attr-defined]
HealthState.HUMAN_DECISION_REQUIRED = HealthState(                # type: ignore[attr-defined]
    config.HEALTH_HUMAN_DECISION_REQUIRED
)


@dataclass
class LoopDetection:
    """A single detected repetition problem."""

    task_id: str
    kind: str
    count: int
    threshold: int
    detail: str

    @property
    def exceeded(self) -> bool:
        return self.count >= self.threshold

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "count": self.count,
            "threshold": self.threshold,
            "detail": self.detail,
            "exceeded": self.exceeded,
        }


@dataclass
class Escalation:
    """An event that requires a human decision."""

    task_id: str
    reason: str
    options: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "reason": self.reason,
            "options": list(self.options),
            "created_at": self.created_at,
        }


@dataclass
class HealthReport:
    """Complete result of one health check."""

    state: HealthState
    context: Dict[str, Any]
    loops: List[LoopDetection]
    deadlocks: List[List[str]]
    blocked_task_ids: List[str]
    failed_task_ids: List[str]
    ready_task_ids: List[str]
    escalations: List[Escalation]
    recommendations: List[str]
    checked_at: str = field(default_factory=utc_now_iso)

    @property
    def loop_detected(self) -> bool:
        return any(loop.exceeded for loop in self.loops)

    @property
    def deadlock_detected(self) -> bool:
        return bool(self.deadlocks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": str(self.state),
            "context": self.context,
            "loops": [loop.to_dict() for loop in self.loops],
            "deadlocks": self.deadlocks,
            "blocked_tasks": self.blocked_task_ids,
            "failed_tasks": self.failed_task_ids,
            "ready_tasks": self.ready_task_ids,
            "escalations": [escalation.to_dict() for escalation in self.escalations],
            "recommendations": self.recommendations,
            "checked_at": self.checked_at,
            "loop_detected": self.loop_detected,
            "deadlock_detected": self.deadlock_detected,
        }

    def render(self) -> str:
        lines = [
            "=" * 60,
            "SUPERVISOR HEALTH REPORT",
            "=" * 60,
            f"Status: {self.state}",
            f"Time:   {self.checked_at}",
            "",
            f"CONTEXT USAGE: {self.context.get('utilization_percent', 0)}%",
            f"  band={self.context.get('band')} "
            f"(compaction at {self.context.get('compaction_threshold')}%, "
            f"critical at {self.context.get('critical_threshold')}%)",
            "",
            "TASK HEALTH:",
            f"  Ready:    {len(self.ready_task_ids)}",
            f"  Blocked:  {len(self.blocked_task_ids)}",
            f"  Failed:   {len(self.failed_task_ids)}",
            "",
            f"DETECTED LOOPS ({len(self.loops)}):",
        ]
        if self.loops:
            for loop in self.loops:
                marker = "EXCEEDED" if loop.exceeded else "watch"
                lines.append(
                    f"  [{marker}] {loop.task_id}: {loop.kind} "
                    f"({loop.count}/{loop.threshold}) — {loop.detail}"
                )
        else:
            lines.append("  [None]")
        lines.append("")
        lines.append(f"DEADLOCKS ({len(self.deadlocks)}):")
        if self.deadlocks:
            for cycle in self.deadlocks:
                lines.append("  " + " -> ".join(cycle))
        else:
            lines.append("  [None]")
        lines.append("")
        lines.append(f"ESCALATIONS ({len(self.escalations)}):")
        if self.escalations:
            for escalation in self.escalations:
                lines.append(f"  - {escalation.task_id}: {escalation.reason}")
                for option in escalation.options:
                    lines.append(f"      * {option}")
        else:
            lines.append("  [None]")
        lines.append("")
        lines.append(f"RECOMMENDATIONS ({len(self.recommendations)}):")
        if self.recommendations:
            for recommendation in self.recommendations:
                lines.append(f"  - {recommendation}")
        else:
            lines.append("  [None]")
        lines.append("=" * 60)
        return "\n".join(lines)


class SupervisorAgent:
    """Health monitoring engine — observes, protects and escalates."""

    def __init__(
        self,
        state_manager: Optional[StateManager] = None,
        project_path: Optional[str] = None,
        thresholds: Optional[config.LoopThresholds] = None,
        compaction: Optional[config.CompactionThresholds] = None,
    ) -> None:
        if state_manager is not None:
            self.state_manager = state_manager
        elif project_path is not None:
            self.state_manager = StateManager(project_path)
        else:
            raise SupervisorError("SupervisorAgent requires a state_manager or project_path")
        self.thresholds = thresholds or config.loop_thresholds()
        self.compaction = compaction or config.compaction_thresholds()

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_context_usage(self) -> Dict[str, Any]:
        utilization = self.state_manager.get_context_utilization()
        compaction_at = self.state_manager.get_compaction_threshold()
        critical_at = self.state_manager.get_critical_threshold()
        effective = self.compaction
        if compaction_at != config.COMPACTION_THRESHOLDS.compaction_percent:
            effective = config.CompactionThresholds(
                warning_percent=min(effective.warning_percent, compaction_at - 10),
                compaction_percent=compaction_at,
                compaction_high_percent=max(compaction_at + 10, effective.compaction_high_percent),
                critical_percent=critical_at,
            )
        band = effective.classify(utilization)
        return {
            "utilization_percent": utilization,
            "band": band,
            "compaction_threshold": compaction_at,
            "critical_threshold": critical_at,
            "compaction_required": effective.compaction_required(utilization),
            "critical": effective.critical(utilization),
        }

    def detect_loops(self, tasks: Optional[Sequence[Dict[str, Any]]] = None) -> List[LoopDetection]:
        """Find repeating loops against the configured thresholds."""
        task_list = list(tasks) if tasks is not None else self.state_manager.load_tasks()
        detections: List[LoopDetection] = []

        for task in task_list:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id", "UNKNOWN"))
            status = str(task.get("status", ""))
            if status in config.TERMINAL_TASK_STATUSES:
                continue
            execution = task.get("execution") or {}
            try:
                attempts = int(execution.get("attempt_count", 0) or 0)
                no_progress = int(execution.get("no_progress_cycles", 0) or 0)
                strategy_changes = int(execution.get("strategy_changes", 0) or 0)
            except (TypeError, ValueError):
                attempts, no_progress, strategy_changes = 0, 0, 0

            if attempts >= self.thresholds.same_strategy_max_attempts:
                detections.append(
                    LoopDetection(
                        task_id=task_id,
                        kind=config.LOOP_KIND_SAME_STRATEGY,
                        count=attempts,
                        threshold=self.thresholds.same_strategy_max_attempts,
                        detail=(
                            f"same failing strategy attempted {attempts} times "
                            f"(limit {self.thresholds.same_strategy_max_attempts})"
                        ),
                    )
                )
            if no_progress >= self.thresholds.no_progress_max_cycles:
                detections.append(
                    LoopDetection(
                        task_id=task_id,
                        kind=config.LOOP_KIND_NO_PROGRESS,
                        count=no_progress,
                        threshold=self.thresholds.no_progress_max_cycles,
                        detail=(
                            f"{no_progress} cycles without meaningful progress "
                            f"(limit {self.thresholds.no_progress_max_cycles})"
                        ),
                    )
                )
            if strategy_changes >= self.thresholds.max_alternative_strategies and attempts >= self.thresholds.same_strategy_max_attempts:
                detections.append(
                    LoopDetection(
                        task_id=task_id,
                        kind=config.LOOP_KIND_ALTERNATIVES_EXHAUSTED,
                        count=strategy_changes,
                        threshold=self.thresholds.max_alternative_strategies,
                        detail=(
                            f"{strategy_changes} alternative strategies exhausted "
                            f"(limit {self.thresholds.max_alternative_strategies})"
                        ),
                    )
                )
            try:
                repeats = int(execution.get("repeated_output_count", 0) or 0)
            except (TypeError, ValueError):
                repeats = 0
            if repeats >= self.thresholds.identical_output_max_repeats:
                detections.append(
                    LoopDetection(
                        task_id=task_id,
                        kind=config.LOOP_KIND_REPEATED_OUTPUT,
                        count=repeats,
                        threshold=self.thresholds.identical_output_max_repeats,
                        detail=(
                            f"{repeats} substantially identical outputs in a row "
                            f"(limit {self.thresholds.identical_output_max_repeats})"
                        ),
                    )
                )
        return detections

    def detect_circular_dependencies(
        self, tasks: Optional[Sequence[Dict[str, Any]]] = None
    ) -> List[List[str]]:
        """Depth-first search over TASKS.yaml; returns every cycle found."""
        task_list = list(tasks) if tasks is not None else self.state_manager.load_tasks()
        graph: Dict[str, List[str]] = {}
        for task in task_list:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id"))
            deps = task.get("dependencies") or []
            graph[task_id] = [str(dep) for dep in deps] if isinstance(deps, list) else []

        cycles: List[List[str]] = []
        seen_cycles: List[frozenset] = []
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {node: WHITE for node in graph}
        stack: List[str] = []

        def visit(node: str) -> None:
            color[node] = GRAY
            stack.append(node)
            for neighbour in graph.get(node, []):
                if neighbour not in graph:
                    continue
                state = color.get(neighbour, WHITE)
                if state == GRAY:
                    if neighbour in stack:
                        index = stack.index(neighbour)
                        cycle = stack[index:] + [neighbour]
                        key = frozenset(cycle)
                        if key not in seen_cycles:
                            seen_cycles.append(key)
                            cycles.append(cycle)
                elif state == WHITE:
                    visit(neighbour)
            stack.pop()
            color[node] = BLACK

        for node in sorted(graph):
            if color.get(node, WHITE) == WHITE:
                visit(node)
        return cycles

    def analyze_blocked(
        self, tasks: Optional[Sequence[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        task_list = list(tasks) if tasks is not None else self.state_manager.load_tasks()
        blocked_ids: List[str] = []
        ready_ids: List[str] = []
        failed_ids: List[str] = []
        for task in task_list:
            if not isinstance(task, dict):
                continue
            status = str(task.get("status", ""))
            task_id = str(task.get("id"))
            if status == config.TASK_FAILED:
                failed_ids.append(task_id)
                continue
            if status in config.TERMINAL_TASK_STATUSES:
                continue
            deps_ok = self.state_manager.dependencies_satisfied(task, task_list)
            if status == config.TASK_BLOCKED or (
                status in (config.TASK_TODO, config.TASK_READY) and not deps_ok
            ):
                blocked_ids.append(task_id)
            elif status == config.TASK_READY and deps_ok:
                ready_ids.append(task_id)
        return {
            "blocked_task_ids": sorted(blocked_ids),
            "ready_task_ids": sorted(ready_ids),
            "failed_task_ids": sorted(failed_ids),
            "independent_work_available": bool(ready_ids),
        }

    def build_escalations(
        self,
        loops: Sequence[LoopDetection],
        deadlocks: Sequence[List[str]],
        blocked_info: Dict[str, Any],
        context: Dict[str, Any],
    ) -> List[Escalation]:
        escalations: List[Escalation] = []
        for loop in loops:
            if not loop.exceeded:
                continue
            if loop.kind == config.LOOP_KIND_SAME_STRATEGY:
                reason = (
                    f"Task {loop.task_id} hit the retry limit "
                    f"({loop.count}/{loop.threshold} attempts with the same strategy)."
                )
                options = [
                    "Switch to a materially different strategy",
                    "Split the task into smaller prerequisite tasks",
                    "Escalate to a human for a decision",
                ]
            elif loop.kind == config.LOOP_KIND_NO_PROGRESS:
                reason = (
                    f"Task {loop.task_id} made no meaningful progress for "
                    f"{loop.count} cycles (limit {loop.threshold})."
                )
                options = [
                    "Diagnose root cause and re-scope the task",
                    "Cancel the task and replan",
                    "Escalate to a human for a decision",
                ]
            else:
                reason = (
                    f"Task {loop.task_id} exhausted all alternative strategies "
                    f"({loop.count}/{loop.threshold})."
                )
                options = [
                    "Request human guidance on approach",
                    "Accept a documented limitation",
                    "Redistribute the work to another agent",
                ]
            escalations.append(Escalation(task_id=loop.task_id, reason=reason, options=options))

        for cycle in deadlocks:
            escalations.append(
                Escalation(
                    task_id=cycle[0] if cycle else "UNKNOWN",
                    reason="Circular dependency detected: " + " -> ".join(cycle),
                    options=[
                        "Break the cycle by creating a prerequisite task",
                        "Split one of the tasks",
                        "Relax a nonessential dependency",
                    ],
                )
            )

        if context.get("critical"):
            escalations.append(
                Escalation(
                    task_id="CONTEXT",
                    reason=(
                        f"Context utilisation {context.get('utilization_percent')}% is at or above "
                        f"the critical threshold ({context.get('critical_threshold')}%)."
                    ),
                    options=[
                        "Compact context immediately",
                        "Save a checkpoint and discard working history",
                    ],
                )
            )

        if (
            blocked_info.get("blocked_task_ids")
            and not blocked_info.get("independent_work_available")
            and not loops
            and not deadlocks
        ):
            escalations.append(
                Escalation(
                    task_id=",".join(blocked_info["blocked_task_ids"]),
                    reason="All tasks are blocked and no independent ready work exists.",
                    options=[
                        "Resolve the critical dependency",
                        "Waive a nonessential dependency",
                        "Ask a human for the missing input",
                    ],
                )
            )
        return escalations

    def classify_state(
        self,
        loops: Sequence[LoopDetection],
        deadlocks: Sequence[List[str]],
        blocked_info: Dict[str, Any],
        context: Dict[str, Any],
        escalations: Sequence[Escalation],
    ) -> HealthState:
        if escalations:
            return HealthState.HUMAN_DECISION_REQUIRED
        if loops:
            return HealthState.STALLED
        if deadlocks:
            return HealthState.BLOCKED
        if context.get("critical") or (
            context.get("compaction_required") and not blocked_info.get("independent_work_available")
        ):
            return HealthState.WARNING
        if blocked_info.get("blocked_task_ids") and not blocked_info.get("independent_work_available"):
            return HealthState.BLOCKED
        if context.get("compaction_required") or blocked_info.get("failed_task_ids"):
            return HealthState.WARNING
        return HealthState.HEALTHY

    # ------------------------------------------------------------------
    # Full check
    # ------------------------------------------------------------------

    def check_health(self) -> HealthReport:
        context = self.check_context_usage()
        tasks = self.state_manager.load_tasks()
        loops = self.detect_loops(tasks)
        deadlocks = self.detect_circular_dependencies(tasks)
        blocked_info = self.analyze_blocked(tasks)
        escalations = self.build_escalations(loops, deadlocks, blocked_info, context)
        state = self.classify_state(loops, deadlocks, blocked_info, context, escalations)

        recommendations: List[str] = []
        if context.get("compaction_required"):
            recommendations.append(
                f"Context at {context['utilization_percent']}% — save a checkpoint and compact "
                f"(threshold {context['compaction_threshold']}%)."
            )
        if loops:
            recommendations.append(
                "Stop the repeated strategy, preserve state, and pick a materially different approach."
            )
        if deadlocks:
            recommendations.append(
                "Break the dependency cycle before starting any task inside it."
            )
        if blocked_info.get("blocked_task_ids") and blocked_info.get("independent_work_available"):
            recommendations.append(
                f"{len(blocked_info['blocked_task_ids'])} blocked task(s) detected; "
                "independent READY tasks can continue."
            )
        if blocked_info.get("failed_task_ids"):
            recommendations.append(
                f"{len(blocked_info['failed_task_ids'])} failed task(s) — failure risks "
                "are tracked in RISKS.md; re-plan before re-dispatching."
            )
        if not recommendations:
            recommendations.append("All systems healthy — continue execution.")

        return HealthReport(
            state=state,
            context=context,
            loops=loops,
            deadlocks=deadlocks,
            blocked_task_ids=blocked_info["blocked_task_ids"],
            failed_task_ids=blocked_info["failed_task_ids"],
            ready_task_ids=blocked_info["ready_task_ids"],
            escalations=escalations,
            recommendations=recommendations,
        )

    def print_health_report(self) -> str:
        return self.check_health().render()

    # ------------------------------------------------------------------
    # State synchronisation helpers
    # ------------------------------------------------------------------

    def ensure_failure_risk(self, task_id: str, error: str = "") -> Optional[str]:
        """Record a RISKS.md entry for a failing task (deduplicated).

        Returns the risk id when a NEW entry was written, ``None`` when a
        risk for this task already exists.
        """
        for risk in self.state_manager.list_risks():
            if re.search(
                rf"\bTask {re.escape(task_id)}\b",
                str(risk.get("title", "")),
                re.IGNORECASE,
            ):
                return None
        record = self.state_manager.append_risk(
            title=f"Task {task_id} keeps failing",
            probability="MEDIUM",
            impact="HIGH",
            description=(
                f"Dispatch of {task_id} failed"
                + (f": {error}" if error else ".")
                + " The task may stay failed until re-planned."
            ),
            mitigation=(
                "Diagnose the recorded error, pick a materially different strategy, "
                "or escalate to a human decision before re-dispatch."
            ),
            related_tasks=[task_id],
            owner="supervisor_agent",
        )
        risk_id = str(record["id"])
        logger.warning("risk %s recorded for failing task %s", risk_id, task_id)
        return risk_id

    def sync_project_health(self) -> HealthReport:
        """Persist counters derived from a health check into PROJECT.yaml.

        Also persists escalations into ``human_decisions`` (append/dedupe, so
        a resumed session sees them) and refreshes the live ``blockers`` list.
        """
        report = self.check_health()
        try:
            self.state_manager.update_health(
                status=str(report.state),
                blocked_tasks=len(report.blocked_task_ids),
                failed_tasks=len(report.failed_task_ids),
                loop_detected=report.loop_detected,
                deadlock_detected=report.deadlock_detected,
            )
            for escalation in report.escalations:
                self.state_manager.add_human_decision(
                    f"{escalation.task_id}: {escalation.reason}"
                )
            # Keep RISKS.md current: every failed task gets a (deduped) entry.
            for task_id in report.failed_task_ids:
                task = self.state_manager.find_task(str(task_id)) or {}
                execution = task.get("execution") or {}
                self.ensure_failure_risk(
                    str(task_id), str(execution.get("last_error") or "")
                )
            blockers = [
                f"{task_id}: blocked by unfinished dependencies"
                for task_id in report.blocked_task_ids
            ]
            blockers.extend(
                f"deadlock: {' -> '.join(cycle)}" for cycle in report.deadlocks
            )
            self.state_manager.set_blockers(blockers)
        except StateError:
            raise
        logger.debug(
            "health synced: %s (blocked=%d, failed=%d)",
            report.state,
            len(report.blocked_task_ids),
            len(report.failed_task_ids),
        )
        return report

    def should_block_dispatch(self, task_id: str) -> Optional[LoopDetection]:
        """Return the exceeded loop that forbids re-dispatching ``task_id``."""
        for loop in self.detect_loops():
            if loop.task_id == task_id and loop.exceeded:
                return loop
        return None

    def record_attempt(
        self,
        task_id: str,
        progressed: bool = False,
        strategy_changed: bool = False,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a task's execution counters after one dispatch attempt."""
        attempt_delta = 1
        no_progress_delta = 0 if progressed else 1
        return self.state_manager.update_task_execution(
            task_id,
            attempt_delta=attempt_delta,
            no_progress_delta=no_progress_delta,
            strategy_changed=strategy_changed,
            error=error,
            reset_error=progressed,
        )


__all__ = [
    "SupervisorAgent",
    "SupervisorError",
    "HealthState",
    "HealthReport",
    "LoopDetection",
    "Escalation",
]
