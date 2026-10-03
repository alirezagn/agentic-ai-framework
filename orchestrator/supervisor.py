"""SupervisorAgent — independent health, loop and deadlock monitor.

The Supervisor observes the project; it never marks implementation work as
complete. It performs lookups over TASKS.yaml and PROJECT.yaml to catch:

* circular dependencies (deadlocks) in the task graph,
* repeating loops — same failing strategy after 3 attempts, 5 cycles without
  progress, or exhausted alternative strategies,
* context pressure against the compaction benchmarks (70% / 85%),
* blocked tasks that unnecessarily starve unrelated work.

It also runs the **validation circuit-breaker**: a task that keeps getting
rejected by the Definition of Done or by schema checks (as opposed to
crashing on code) is auto-waived after ``validation_waive_after`` consecutive
rejections, so a purely structural failure never parks the project in
``HUMAN_DECISION_REQUIRED`` while downstream tasks wait.

When limits are exceeded it emits escalations that move the project into
``HUMAN_DECISION_REQUIRED``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config
from .state_manager import StateError, StateManager, utc_now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation circuit-breaker: DoD/schema rejections vs code/runtime failures
# ---------------------------------------------------------------------------

# Text markers that identify a *structural* rejection: the Definition of Done,
# schema/shape validation, or JSON parse/repair rounds. These are the failures
# the circuit-breaker counts.
VALIDATION_FAILURE_MARKERS: Tuple[str, ...] = (
    "do unmet",
    "no acceptance criteria",
    "expected output",
    "acceptance check failed",
    "independent review not passed",
    "requirement not found",
    "output summary must not be empty",
    "output data must be a dictionary",
    "does not match executor",
    "does not match executed task",
    "not parseable",
    "must be a json object",
    "truncation-repair",
    "invalid json",
    "schema",
    "validation",
)

# Text markers that identify a *code/runtime* failure. These win over the
# validation markers and always reset the streak: a crashing agent must keep
# its retry budget and must never be silently waived.
RUNTIME_FAILURE_MARKERS: Tuple[str, ...] = (
    "traceback",
    "errno",
    "exit code",
    "non-zero exit",
    "signal",
    "timed out",
    "connection refused",
    "connection reset",
    "temporary failure",
    "backend down",
    "backend unavailable",
)


def is_validation_failure(error: Optional[str]) -> bool:
    """True when ``error`` is a DoD/schema rejection, not a code/runtime crash.

    The circuit-breaker only counts validation-class failures: two
    consecutive rejections waive the task, while a crashing or unreachable
    backend keeps escalating the normal (human) way.
    """
    text = str(error or "").casefold()
    if not text.strip():
        return False
    if any(marker in text for marker in RUNTIME_FAILURE_MARKERS):
        return False
    return any(marker in text for marker in VALIDATION_FAILURE_MARKERS)


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
    """An event that requires attention.

    ``blocking`` escalations move the project to HUMAN_DECISION_REQUIRED;
    non-blocking ones are advisories that classify as WARNING (B6).
    """

    task_id: str
    reason: str
    options: List[str] = field(default_factory=list)
    category: str = "general"
    blocking: bool = True
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "reason": self.reason,
            "options": list(self.options),
            "category": self.category,
            "blocking": self.blocking,
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
    stale_blocks: List[str] = field(default_factory=list)
    decision_conflicts: List[Dict[str, Any]] = field(default_factory=list)

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
            "stale_blocks": list(self.stale_blocks),
            "decision_conflicts": list(self.decision_conflicts),
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
        lines.append(f"STALE BLOCKS ({len(self.stale_blocks)}):")
        if self.stale_blocks:
            for task_id in self.stale_blocks:
                lines.append(
                    f"  - {task_id}: BLOCKED but every dependency is satisfied "
                    "(run refresh_ready_states)"
                )
        else:
            lines.append("  [None]")
        lines.append("")
        lines.append(f"DECISION CONFLICTS ({len(self.decision_conflicts)}):")
        if self.decision_conflicts:
            for conflict in self.decision_conflicts:
                lines.append(
                    f"  - {conflict.get('task_id')}: pending "
                    f"{', '.join(conflict.get('decision_ids') or [])}"
                )
        else:
            lines.append("  [None]")
        lines.append("")
        lines.append(f"ESCALATIONS ({len(self.escalations)}):")
        if self.escalations:
            for escalation in self.escalations:
                marker = "blocking" if escalation.blocking else "advisory"
                lines.append(
                    f"  - [{escalation.category}/{marker}] {escalation.task_id}: "
                    f"{escalation.reason}"
                )
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
            # A5: "same strategy" means attempts since the last strategy
            # change. Legacy projects without the field use attempt_count.
            since_raw = execution.get("attempts_since_change")
            if since_raw is None:
                since_change = attempts
            else:
                try:
                    since_change = int(since_raw or 0)
                except (TypeError, ValueError):
                    since_change = attempts

            if since_change >= self.thresholds.same_strategy_max_attempts:
                detections.append(
                    LoopDetection(
                        task_id=task_id,
                        kind=config.LOOP_KIND_SAME_STRATEGY,
                        count=since_change,
                        threshold=self.thresholds.same_strategy_max_attempts,
                        detail=(
                            f"same failing strategy attempted {since_change} times "
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
            try:
                stall = int(execution.get("evidence_stall_count", 0) or 0)
            except (TypeError, ValueError):
                stall = 0
            if stall >= self.thresholds.evidence_stall_max:
                detections.append(
                    LoopDetection(
                        task_id=task_id,
                        kind=config.LOOP_KIND_NO_NEW_EVIDENCE,
                        count=stall,
                        threshold=self.thresholds.evidence_stall_max,
                        detail=(
                            f"{stall} dispatches produced no new artifacts or "
                            f"decisions (limit {self.thresholds.evidence_stall_max})"
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

    def detect_stale_blocks(
        self, tasks: Optional[Sequence[Dict[str, Any]]] = None
    ) -> List[str]:
        """BLOCKED tasks whose dependencies are all satisfied (A3).

        A stale block means refresh_ready_states did not run (or a status was
        set manually); the graph says the task could move to READY.
        """
        task_list = list(tasks) if tasks is not None else self.state_manager.load_tasks()
        stale: List[str] = []
        for task in task_list:
            if not isinstance(task, dict):
                continue
            if str(task.get("status", "")) != config.TASK_BLOCKED:
                continue
            if self.state_manager.dependencies_satisfied(task, task_list):
                stale.append(str(task.get("id")))
        return sorted(stale)

    def detect_decision_conflicts(self) -> List[Dict[str, Any]]:
        """Tasks targeted by more than one pending PROPOSED_CHANGE (A3)."""
        pending = self.state_manager.pending_proposed_changes()
        by_task: Dict[str, List[str]] = {}
        for decision in pending:
            dec_id = str(decision.get("id") or "DEC-UNKNOWN")
            affected = decision.get("affected_tasks") or []
            if not affected:
                by_task.setdefault("UNSPECIFIED", []).append(dec_id)
            for task_id in affected:
                by_task.setdefault(str(task_id), []).append(dec_id)
        return [
            {"task_id": task_id, "decision_ids": sorted(ids)}
            for task_id, ids in sorted(by_task.items())
            if len(ids) > 1
        ]

    def build_escalations(
        self,
        loops: Sequence[LoopDetection],
        deadlocks: Sequence[List[str]],
        blocked_info: Dict[str, Any],
        context: Dict[str, Any],
        stale_blocks: Optional[Sequence[str]] = None,
        decision_conflicts: Optional[Sequence[Dict[str, Any]]] = None,
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
            elif loop.kind == config.LOOP_KIND_NO_NEW_EVIDENCE:
                reason = (
                    f"Task {loop.task_id} produced no new artifacts, decisions or "
                    f"requirement updates for {loop.count} dispatches "
                    f"(limit {loop.threshold})."
                )
                options = [
                    "Verify the agent is writing the expected evidence files",
                    "Change the strategy or split the task",
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
            escalations.append(
                Escalation(
                    task_id=loop.task_id,
                    reason=reason,
                    options=options,
                    category="loop",
                    blocking=True,
                )
            )

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
                    category="deadlock",
                    blocking=True,
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
                    category="context",
                    blocking=False,
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
                    category="starvation",
                    blocking=True,
                )
            )

        for conflict in decision_conflicts or []:
            task_id = str(conflict.get("task_id") or "UNKNOWN")
            ids = ", ".join(conflict.get("decision_ids") or [])
            escalations.append(
                Escalation(
                    task_id=task_id,
                    reason=(
                        f"Task {task_id} is targeted by multiple pending "
                        f"PROPOSED_CHANGE decisions ({ids}) — approving one may "
                        "invalidate another."
                    ),
                    options=[
                        "Reject the superseded change",
                        "Merge the changes into a single decision",
                        "Decide the order of application explicitly",
                    ],
                    category="decision_conflict",
                    blocking=True,
                )
            )

        for task_id in stale_blocks or []:
            escalations.append(
                Escalation(
                    task_id=str(task_id),
                    reason=(
                        f"Task {task_id} is BLOCKED but every dependency is "
                        "satisfied — the state is stale."
                    ),
                    options=[
                        "Run refresh_ready_states to promote the task",
                        "Re-check the dependency edge manually",
                    ],
                    category="stale_block",
                    blocking=False,
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
        recovering: bool = False,
    ) -> HealthState:
        # C2: an active recovery (fresh strategy change) with loop signals is
        # RECOVERY, not a human decision — checked before escalations so the
        # pre-recovery signals do not bounce the state straight back.
        if recovering and loops:
            return HealthState.RECOVERY
        blocking = [escalation for escalation in escalations if escalation.blocking]
        if blocking:
            return HealthState.HUMAN_DECISION_REQUIRED
        if escalations:
            return HealthState.WARNING
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
        stale_blocks = self.detect_stale_blocks(tasks)
        decision_conflicts = self.detect_decision_conflicts()
        escalations = self.build_escalations(
            loops,
            deadlocks,
            blocked_info,
            context,
            stale_blocks=stale_blocks,
            decision_conflicts=decision_conflicts,
        )
        recovering = any(
            str(task.get("status", "")) not in config.TERMINAL_TASK_STATUSES
            and bool((task.get("execution") or {}).get("recovering"))
            for task in tasks
            if isinstance(task, dict)
        )
        state = self.classify_state(
            loops, deadlocks, blocked_info, context, escalations, recovering=recovering
        )

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
        if stale_blocks:
            recommendations.append(
                f"{len(stale_blocks)} stale block(s): BLOCKED tasks whose dependencies "
                "are satisfied — run refresh_ready_states."
            )
        if decision_conflicts:
            recommendations.append(
                "Pending PROPOSED_CHANGE decisions overlap — resolve the conflict "
                "before approving either change."
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
            stale_blocks=stale_blocks,
            decision_conflicts=decision_conflicts,
        )

    def print_health_report(self) -> str:
        return self.check_health().render()

    # ------------------------------------------------------------------
    # Diagnosis (B5)
    # ------------------------------------------------------------------

    def diagnose(self, use_llm: bool = True) -> str:
        """Explain the current health state and what to do about it.

        Uses the configured LLM when a provider is available and reachable;
        any failure (no provider, network down, bad response) falls back to
        the deterministic rules-only diagnosis so this never raises.
        """
        report = self.check_health()
        lines = [
            f"State: {report.state}",
            f"Context: {report.context.get('utilization_percent', 0)}% "
            f"({report.context.get('band')})",
        ]
        if report.loops:
            lines.append("Loops:")
            lines.extend(
                f"  - {loop.task_id}: {loop.kind} {loop.count}/{loop.threshold} "
                f"({'exceeded' if loop.exceeded else 'watch'})"
                for loop in report.loops
            )
        if report.deadlocks:
            lines.append("Deadlocks:")
            lines.extend("  - " + " -> ".join(cycle) for cycle in report.deadlocks)
        if report.stale_blocks:
            lines.append(
                "Stale blocks: " + ", ".join(report.stale_blocks)
                + " (dependencies satisfied — refresh state)"
            )
        if report.decision_conflicts:
            lines.append(
                "Decision conflicts: "
                + "; ".join(
                    f"{c.get('task_id')} <- {', '.join(c.get('decision_ids') or [])}"
                    for c in report.decision_conflicts
                )
            )
        if report.escalations:
            lines.append("Escalations:")
            lines.extend(
                f"  - [{esc.category}] {esc.task_id}: {esc.reason}"
                for esc in report.escalations
            )
        if report.blocked_task_ids:
            lines.append("Blocked: " + ", ".join(report.blocked_task_ids))
        if report.failed_task_ids:
            lines.append("Failed: " + ", ".join(report.failed_task_ids))
        lines.extend(f"Recommendation: {item}" for item in report.recommendations)
        rules_diagnosis = "\n".join(lines)

        if not use_llm:
            return rules_diagnosis
        try:
            from .llm_client import LLMClient

            if not LLMClient.is_available():
                return rules_diagnosis
            client = LLMClient()
            result = client.complete(
                system=(
                    "You are the supervisor of an autonomous agent framework. "
                    "Give a short, concrete diagnosis (max 6 lines) of the project "
                    "health below and the single best next action. No preamble."
                ),
                messages=[{"role": "user", "content": rules_diagnosis}],
                max_tokens=400,
            )
            text = (result.text or "").strip()
            return text or rules_diagnosis
        except Exception:  # noqa: BLE001 — diagnosis must never raise
            logger.debug("LLM diagnosis failed; using rules-only output", exc_info=True)
            return rules_diagnosis

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
        # Reconcile the validation circuit-breaker first: a struck-out task
        # must be WAIVED before health is computed, otherwise its failures
        # still feed loop/starvation escalations.
        try:
            self.sweep_validation_circuit_breakers()
        except StateError:
            logger.debug("validation circuit-breaker sweep failed", exc_info=True)
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
                    f"[{escalation.category}] {escalation.task_id}: {escalation.reason}"
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

    # ------------------------------------------------------------------
    # Validation circuit-breaker (DoD/schema strikes -> auto-waive)
    # ------------------------------------------------------------------

    def apply_validation_circuit_breaker(self, task_id: str, error: str = "") -> bool:
        """Record one failure and auto-waive ``task_id`` when it strikes out.

        Counts *consecutive* validation-class failures
        (:func:`is_validation_failure`) in ``execution.validation_failure_streak``.
        Once the streak reaches ``thresholds.validation_waive_after``
        (default 2), the task flips to ``WAIVED``: dependents proceed without
        a human decision and a purely structural rejection can never park the
        project in ``HUMAN_DECISION_REQUIRED``. Code/runtime failures reset
        the streak to 0 and keep the normal escalation path.

        Returns True when this call waived the task.
        """
        task = self.state_manager.find_task(task_id)
        if task is None:
            return False
        execution = task.get("execution") or {}
        try:
            streak = int(execution.get("validation_failure_streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        validation = is_validation_failure(error)
        streak = streak + 1 if validation else 0
        try:
            self.state_manager.update_task_execution(
                task_id, set_values={"validation_failure_streak": streak}
            )
        except StateError:
            logger.debug(
                "could not persist validation streak for %s", task_id, exc_info=True
            )
            return False
        if not validation or streak < self.thresholds.validation_waive_after:
            return False
        if str(task.get("status", "")) == config.TASK_WAIVED:
            return False
        return self._waive_struck_out_task(task_id, streak)

    def sweep_validation_circuit_breakers(self) -> List[str]:
        """Waive any FAILED task whose validation streak already struck out.

        Idempotent safety net: dispatch applies the breaker inline, but a
        streak recorded by another path (or a crash between the two writes)
        must not leave a struck-out task parked in FAILED. Called from
        :meth:`sync_project_health`, so every health sync also reconciles it.
        """
        waived: List[str] = []
        threshold = self.thresholds.validation_waive_after
        for task in self.state_manager.load_tasks():
            if not isinstance(task, dict):
                continue
            if str(task.get("status", "")) != config.TASK_FAILED:
                continue
            execution = task.get("execution") or {}
            try:
                streak = int(execution.get("validation_failure_streak") or 0)
            except (TypeError, ValueError):
                continue
            if streak < threshold:
                continue
            task_id = str(task.get("id") or "")
            if task_id and self._waive_struck_out_task(task_id, streak):
                waived.append(task_id)
        return waived

    def _waive_struck_out_task(self, task_id: str, streak: int) -> bool:
        """Flip ``task_id`` to WAIVED, warn in CURRENT_STATE, unblock graph."""
        try:
            self.state_manager.update_task_status(task_id, config.TASK_WAIVED)
            self.state_manager.append_current_state(
                f"WARNING: {task_id} auto-waived after {streak} consecutive "
                "Definition-of-Done/schema rejections — dependents may proceed "
                "without a human decision"
            )
            # Promote TODO/BLOCKED dependents the graph now satisfies, so the
            # next dispatch never stalls behind a stale block on a waived task.
            self.state_manager.refresh_ready_states()
        except StateError:
            logger.warning(
                "validation circuit-breaker could not waive %s", task_id, exc_info=True
            )
            return False
        logger.warning(
            "validation circuit-breaker waived %s after %d consecutive rejections",
            task_id,
            streak,
        )
        return True


__all__ = [
    "SupervisorAgent",
    "SupervisorError",
    "HealthState",
    "HealthReport",
    "LoopDetection",
    "Escalation",
    "is_validation_failure",
]
