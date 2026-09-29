"""MasterOrchestrator — the execution loop that links every component.

Responsibilities:

* load state through :class:`StateManager`,
* dispatch READY tasks to registered specialist agents,
* record execution counters and apply structured agent output to TASKS.yaml,
* enforce the supervisor's loop limits (max 3 retries with one strategy),
* run supervisor health checks after every cycle and persist them,
* auto-checkpoint when the compaction threshold is reached,
* resume from checkpoints without needing chat history.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from .agents.base_agent import (
    AGENT_REGISTRY,
    AgentError,
    AgentOutput,
    BaseAgent,
    create_agent,
    normalize_agent_name,
)
from .checkpoint_manager import CheckpointManager
from .context_monitor import utilization_for_output
from .state_manager import (
    StateError,
    StateManager,
    TaskNotFoundError,
    save_text_file,
    utc_now_iso,
)
from .supervisor import HealthReport, LoopDetection, SupervisorAgent

logger = logging.getLogger(__name__)


class OrchestratorError(RuntimeError):
    """Base error for orchestration failures."""


class LoopLimitExceededError(OrchestratorError):
    """A task exceeded its retry limits and must not be dispatched again."""

    def __init__(self, message: str, loop: Optional[LoopDetection] = None) -> None:
        super().__init__(message)
        self.loop = loop


class MissingAgentError(OrchestratorError):
    """No agent is registered for the task's owner."""


@dataclass
class TaskRunResult:
    """Outcome of a single task dispatch."""

    task_id: str
    agent_id: str
    output: AgentOutput
    previous_status: str
    new_status: str
    loop: Optional[LoopDetection] = None
    checkpoint_id: Optional[str] = None
    dispatched_at: str = field(default_factory=utc_now_iso)

    @property
    def succeeded(self) -> bool:
        return self.output.status == config.AGENT_STATUS_COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "succeeded": self.succeeded,
            "summary": self.output.summary,
            "checkpoint_id": self.checkpoint_id,
            "loop": self.loop.to_dict() if self.loop else None,
            "dispatched_at": self.dispatched_at,
        }


class MasterOrchestrator:
    """Master execution loop linking managers, agents and tracking loops."""

    def __init__(
        self,
        project_path: str | Path,
        checkpoints_root: Optional[str | Path] = None,
        auto_checkpoint: bool = True,
        agent_resolver: Optional[Callable[[str, StateManager], Optional[BaseAgent]]] = None,
    ) -> None:
        self.state = StateManager(project_path)
        self.project_path = self.state.project_path
        self.checkpoints = CheckpointManager(self.project_path, checkpoints_root=checkpoints_root)
        self.supervisor = SupervisorAgent(state_manager=self.state)
        self.auto_checkpoint = auto_checkpoint
        self.agent_resolver = agent_resolver
        self._checkpoint_counter = 0
        self._idle_cycles = 0
        self._last_fingerprint: Optional[str] = None
        # Recent project-state fingerprints, used to detect A<->B oscillation.
        self._fingerprint_history: List[str] = []
        self.registered_agents: Dict[str, BaseAgent] = {}
        # Agents handed in via register_agent() are singletons we cannot
        # re-create per thread; resolve_agent() always returns those.
        self._pinned_agents: set = set()
        # Serializes TASKS.yaml / PROJECT.yaml read-modify-write cycles so
        # parallel dispatch (run_cycle(max_concurrent>1)) cannot lose updates.
        self._state_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Agent registration
    # ------------------------------------------------------------------

    def register_agent(self, agent: BaseAgent) -> BaseAgent:
        key = normalize_agent_name(agent.AGENT_ID)
        with self._state_lock:
            self.registered_agents[key] = agent
            self._pinned_agents.add(key)
        return agent

    def register_agent_class(self, agent_class: type) -> BaseAgent:
        agent = agent_class(state_manager=self.state)
        return self.register_agent(agent)

    def resolve_agent(self, owner: str, fresh: bool = False) -> BaseAgent:
        """Resolve the agent for ``owner``.

        ``fresh=True`` bypasses the instance cache so concurrent dispatches
        never share one agent object (its execution scratch state is not
        thread-safe).
        """
        key = normalize_agent_name(owner)
        with self._state_lock:
            if key in self.registered_agents and (not fresh or key in self._pinned_agents):
                return self.registered_agents[key]
            if self.agent_resolver is not None:
                resolved = self.agent_resolver(owner, self.state)
                if resolved is not None:
                    if not fresh:
                        self.registered_agents[key] = resolved
                    return resolved
            if key in AGENT_REGISTRY:
                agent = create_agent(key, state_manager=self.state)
                if not fresh:
                    self.registered_agents[key] = agent
                return agent
            known = (
                ", ".join(sorted(set(list(self.registered_agents) + list(AGENT_REGISTRY))))
                or "[none]"
            )
        raise MissingAgentError(
            f"No agent registered for owner '{owner}'. Available agents: {known}"
        )

    # ------------------------------------------------------------------
    # Dispatch primitives
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Dict[str, Any]:
        task = self.state.find_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"No task with id '{task_id}' in {self.state.tasks_yaml}")
        return task

    def dispatch(
        self, task_id: str, strategy_changed: bool = False, fresh_agent: bool = False
    ) -> TaskRunResult:
        """Run one task through its agent, enforcing loop limits.

        State mutations happen under ``_state_lock``; only the (slow) agent
        execution runs outside the lock so ``run_cycle(max_concurrent>1)`` can
        overlap model calls safely.
        """
        # --- phase 1: validation + IN_PROGRESS (locked) -----------------
        with self._state_lock:
            task = self.get_task(task_id)
            previous_status = str(task.get("status"))

            oscillation = self._detect_oscillation()
            if oscillation is not None:
                raise LoopLimitExceededError(
                    f"Task '{task_id}' refused: project state is oscillating "
                    f"({oscillation.detail})",
                    loop=oscillation,
                )

            loop = self.supervisor.should_block_dispatch(task_id)
            if loop is not None:
                raise LoopLimitExceededError(
                    f"Task '{task_id}' exceeded the {loop.kind} limit "
                    f"({loop.count}/{loop.threshold}): {loop.detail}",
                    loop=loop,
                )

            if not self.state.dependencies_satisfied(task):
                raise OrchestratorError(
                    f"Task '{task_id}' still has unfinished dependencies: "
                    f"{', '.join(self.state.dependency_ids(task))}"
                )

            self._enforce_decision_gate(task_id)

            owner = str(task.get("owner", ""))
            agent = self.resolve_agent(owner, fresh=fresh_agent)

            self.state.update_task_status(task_id, config.TASK_IN_PROGRESS)
            self.supervisor.record_attempt(
                task_id, progressed=False, strategy_changed=strategy_changed
            )

        # --- phase 2: agent execution (unlocked) ------------------------
        logger.info("dispatching %s -> %s", task_id, agent.AGENT_ID)
        output = agent.run(task)
        output.task_id = task_id

        # --- phase 3: apply results (locked) ----------------------------
        with self._state_lock:
            self._update_context_utilization(agent, output)
            self._record_proposed_change_if_any(task, output, agent.AGENT_ID)

            checkpoint_id: Optional[str] = None
            new_status: str

            if output.status == config.AGENT_STATUS_COMPLETED:
                review_block = task.get("review") or {}
                dod_problems = self.definition_of_done(task, output)
                if review_block.get("required"):
                    new_status = config.TASK_REVIEW
                    self.state.set_review_status(task_id, "READY")
                    self.state.update_task_status(
                        task_id, new_status, note=output.summary, error=None
                    )
                    self.state.update_task_execution(task_id, reset_error=True)
                elif dod_problems:
                    # Definition of Done is not met: the task may not become DONE.
                    new_status = config.TASK_FAILED
                    error_text = "DoD unmet: " + "; ".join(dod_problems)
                    self.state.update_task_status(
                        task_id, new_status, note=output.summary, error=error_text
                    )
                    self.state.append_current_state(
                        f"{task_id} rejected by Definition of Done: {error_text}"
                    )
                else:
                    new_status = config.TASK_DONE
                    self.state.update_task_status(
                        task_id, new_status, note=output.summary, error=None
                    )
                    self.state.update_task_execution(task_id, reset_error=True)
                if new_status != config.TASK_FAILED:
                    self.state.update_task_execution(task_id, no_progress_delta=-1)
                    self.state.append_current_state(
                        f"{task_id} ({agent.AGENT_ID}) -> {new_status}: {output.summary}"
                    )
            elif output.status == config.AGENT_STATUS_BLOCKED:
                new_status = config.TASK_BLOCKED
                error_text = "; ".join(output.errors) or output.summary
                self.state.update_task_status(
                    task_id, new_status, note=output.summary, error=error_text
                )
                self.state.append_current_state(f"{task_id} blocked: {output.summary}")
            else:
                error_text = "; ".join(output.errors) or output.summary
                refreshed = self.get_task(task_id)
                execution = refreshed.get("execution") or {}
                attempts = int(execution.get("attempt_count", 0) or 0)
                new_status = config.TASK_FAILED
                self.state.update_task_status(
                    task_id, new_status, note=output.summary, error=error_text
                )
                self.state.append_current_state(
                    f"{task_id} failed on attempt {attempts}: {output.summary}"
                )

            if new_status == config.TASK_FAILED:
                risk_id = self.supervisor.ensure_failure_risk(task_id, error_text)
                if risk_id:
                    output.warnings = list(output.warnings) + [
                        f"{risk_id} recorded in RISKS.md for {task_id}"
                    ]

            self._record_loop_signals(task_id, task, output)

            after = self.get_task(task_id)
            self.state.recompute_derived_state()
            self._record_fingerprint()
            loop_now = None
            for detection in self.supervisor.detect_loops([after]):
                if detection.task_id == task_id and detection.exceeded:
                    loop_now = detection
                    break

            checkpoint_id = self._maybe_auto_checkpoint(after) or self.check_milestones()
            return TaskRunResult(
                task_id=task_id,
                agent_id=agent.AGENT_ID,
                output=output,
                previous_status=previous_status,
                new_status=str(after.get("status")),
                loop=loop_now,
                checkpoint_id=checkpoint_id,
            )

    @staticmethod
    def _output_fingerprint(output: AgentOutput) -> str:
        """Normalized hash of an agent output (status + summary + errors)."""
        parts = [str(output.status), str(output.summary)] + [
            str(item) for item in output.errors
        ]
        normalized = " ".join(" ".join(parts).lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _record_loop_signals(
        self, task_id: str, task: Dict[str, Any], output: AgentOutput
    ) -> None:
        """Persist strategy-change and repeated-output signals.

        ``data.strategy_changed`` from an agent advances the alternative
        strategy counter; consecutive identical outputs increment
        ``repeated_output_count``. Both counters feed
        :meth:`SupervisorAgent.detect_loops`.
        """
        execution = task.get("execution") or {}
        previous_hash = str(execution.get("last_output_hash") or "")
        try:
            previous_count = int(execution.get("repeated_output_count", 0) or 0)
        except (TypeError, ValueError):
            previous_count = 0
        fingerprint = self._output_fingerprint(output)
        repeats = (
            previous_count + 1
            if previous_hash and previous_hash == fingerprint
            else 1
        )
        strategy_changed = bool(
            isinstance(output.data, dict) and output.data.get("strategy_changed")
        )
        self.state.update_task_execution(
            task_id,
            strategy_changed=strategy_changed,
            set_values={
                "last_output_hash": fingerprint,
                "repeated_output_count": repeats,
            },
        )

    def _record_fingerprint(self) -> None:
        """Append the current status-only fingerprint to the rolling history."""
        self._fingerprint_history.append(self.state_fingerprint())
        self._fingerprint_history = self._fingerprint_history[-12:]

    def _detect_oscillation(self) -> Optional[LoopDetection]:
        """Detect the state alternating between two fingerprints (A<->B).

        Consecutive duplicates are collapsed first, so an already-gated
        project keeps reporting oscillation while the two-state pattern is
        the last thing recorded. Normal monotonic progress never matches.
        """
        compressed: List[str] = []
        for item in self._fingerprint_history:
            if not compressed or compressed[-1] != item:
                compressed.append(item)
        if len(compressed) < 4 or len(set(compressed)) != 2:
            return None
        recent = compressed[-4:]
        if not (
            recent[0] == recent[2]
            and recent[1] == recent[3]
            and recent[0] != recent[1]
        ):
            return None
        flips = len(compressed) - 1
        return LoopDetection(
            task_id="PROJECT",
            kind=config.LOOP_KIND_STATE_OSCILLATION,
            count=len(compressed),
            threshold=4,
            detail=(
                f"state alternating between two fingerprints "
                f"({flips} flips across {len(compressed)} state changes)"
            ),
        )

    def _update_context_utilization(self, agent: BaseAgent, output: AgentOutput) -> float:
        """Persist the measured context utilisation of one agent execution.

        Re-arms the 70% compaction / 85% critical path: the value written here
        is what :meth:`SupervisorAgent.check_context_usage` acts on.
        """
        data = output.data if isinstance(output.data, dict) else {}
        percent = utilization_for_output(data, getattr(agent, "last_payload_chars", 0))
        try:
            self.state.set_context_utilization(percent)
        except StateError:
            pass
        return percent

    # ------------------------------------------------------------------
    # Decision control (PROPOSED_CHANGE gate) — framework/00:132-157
    # ------------------------------------------------------------------

    def _pending_change_affecting(self, task_id: str) -> Optional[Dict[str, Any]]:
        for change in self.state.pending_proposed_changes():
            if task_id in (change.get("affected_tasks") or []):
                return change
        return None

    def _enforce_decision_gate(self, task_id: str) -> None:
        """Refuse to dispatch work affected by an unapproved change.

        Raises :class:`OrchestratorError` (never marks the task FAILED) and
        surfaces the pending decision in ``human_decisions`` so a resumed
        session sees why work stopped.
        """
        change = self._pending_change_affecting(task_id)
        if change is None:
            return
        title = str(change.get("title") or "")
        self.state.add_human_decision(
            f"{change['id']} must be approved before affected tasks can proceed"
        )
        raise OrchestratorError(
            f"Task '{task_id}' is affected by {change['id']} "
            f"(PROPOSED_CHANGE, pending human approval): {title}"
        )

    def _record_proposed_change_if_any(
        self, task: Dict[str, Any], output: AgentOutput, agent_id: str
    ) -> Optional[Dict[str, Any]]:
        """Persist ``data.proposed_change`` from an agent as a gated decision."""
        data = output.data if isinstance(output.data, dict) else {}
        change = data.get("proposed_change")
        if not isinstance(change, dict):
            return None
        title = str(
            change.get("title") or f"Change proposed while working on {task.get('id')}"
        )
        raw_affected = change.get("affected_tasks")
        affected: List[str] = []
        if isinstance(raw_affected, (list, tuple)):
            affected = [str(item).strip() for item in raw_affected if str(item).strip()]
        if not affected:
            affected = [str(task.get("id"))]
        record = self.state.append_decision(
            title=title,
            status="PROPOSED_CHANGE",
            reason=str(change.get("reason") or ""),
            alternatives=str(change.get("alternatives") or ""),
            impact=str(change.get("impact") or ""),
            affected_tasks=affected,
            risks=str(change.get("risks") or ""),
            recommendation=str(change.get("recommendation") or ""),
            proposed_by=agent_id,
        )
        self.state.add_human_decision(
            f"{record['id']} pending approval: {title} (affects {', '.join(affected)})"
        )
        self.state.append_current_state(
            f"Recorded {record['id']} (PROPOSED_CHANGE): {title}; "
            f"dispatch of {', '.join(affected)} gated until approval."
        )
        output.warnings = list(output.warnings) + [
            f"{record['id']} (PROPOSED_CHANGE) recorded; "
            f"affected tasks gated until approval"
        ]
        return record

    def approve_decision(self, dec_id: str, approved: bool = True) -> Dict[str, Any]:
        """Human approval/rejection of a recorded decision (opens the gate)."""
        record = self.state.resolve_decision(dec_id, approved=approved)
        verb = "approved" if approved else "rejected"
        self.state.append_current_state(f"{dec_id} {verb} by human; affected tasks un-gated.")
        return record

    def definition_of_done(
        self, task: Dict[str, Any], output: Optional[AgentOutput] = None
    ) -> List[str]:
        """Structural Definition-of-Done checks (empty list == satisfied).

        Mirrors README.md's DoD criteria that can be verified without an LLM:
        measurable acceptance criteria exist, every expected output was
        materialized under ``docs/``, and — when the task demands one — an
        independent review has passed.
        """
        problems: List[str] = []
        criteria = task.get("acceptance_criteria") or []
        if isinstance(criteria, list):
            usable = [item for item in criteria if str(item).strip()]
        else:
            usable = []
        if not usable:
            problems.append("no acceptance criteria defined")

        expected = task.get("expected_outputs") or []
        docs_dir = self.state.project_path / "docs"
        if isinstance(expected, list):
            for raw_name in expected:
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                filename = Path(raw_name.strip()).name
                if not (docs_dir / filename).exists():
                    problems.append(f"expected output not materialized: {filename}")

        review = task.get("review") or {}
        if review.get("required") and review.get("status") not in ("PASS", "PASS WITH ACTIONS"):
            problems.append("independent review not passed")
        return problems

    def complete_review(
        self,
        task_id: str,
        passed: bool = True,
        note: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> str:
        """Record an independent review outcome and finalize the task.

        ``outcome`` may be ``PASS`` (-> DONE), ``PASS WITH ACTIONS``
        (-> DONE WITH ACCEPTED LIMITATION) or ``FAIL`` (-> FAILED); it
        defaults from the boolean ``passed`` for backward compatibility.
        """
        task = self.get_task(task_id)
        review = dict(task.get("review") or {})
        if outcome is None:
            outcome = "PASS" if passed else "FAIL"
        normalized = str(outcome).strip().upper()
        if normalized == "PASS":
            status = config.TASK_DONE
        elif normalized == "PASS WITH ACTIONS":
            status = config.TASK_DONE_WITH_LIMITATION
        elif normalized == "FAIL":
            status = config.TASK_FAILED
        else:
            raise OrchestratorError(f"Unknown review outcome '{outcome}'")
        review["status"] = normalized
        prospective = dict(task)
        prospective["review"] = review
        dod_problems = self.definition_of_done(prospective)
        if dod_problems and status in (
            config.TASK_DONE,
            config.TASK_DONE_WITH_LIMITATION,
        ):
            status = config.TASK_FAILED
            note = ((note + "; ") if note else "") + "DoD unmet: " + "; ".join(dod_problems)
        document = self.state.load_tasks_document()
        for entry in document.get("tasks") or []:
            if entry.get("id") == task_id:
                entry["review"] = review
                entry["status"] = status
                if note:
                    entry["notes"] = note
                break
        self.state.save_tasks_document(document)
        self.state.append_current_state(
            f"{task_id} review {normalized.lower().replace(' ', '_')}"
        )
        return status

    # ------------------------------------------------------------------
    # Independent review dispatch
    # ------------------------------------------------------------------

    def pending_review_tasks(self) -> List[Dict[str, Any]]:
        """Tasks waiting for their independent review (READY or orphaned)."""
        pending: List[Dict[str, Any]] = []
        for task in self.state.load_tasks():
            if task.get("status") != config.TASK_REVIEW:
                continue
            review_status = str((task.get("review") or {}).get("status", ""))
            if review_status in ("READY", "IN_PROGRESS"):
                pending.append(task)
        return pending

    def dispatch_review(self, task_id: str) -> TaskRunResult:
        """Run the independent review for one REVIEW task.

        The reviewer is always the registered ``review_agent`` — never the
        task's own owner — so no artifact approves itself.
        """
        task = self.get_task(task_id)
        previous_status = str(task.get("status"))
        if previous_status != config.TASK_REVIEW:
            raise OrchestratorError(
                f"Task '{task_id}' is {previous_status}, not REVIEW; nothing to review"
            )

        loop = self.supervisor.should_block_dispatch(task_id)
        if loop is not None:
            raise LoopLimitExceededError(
                f"Task '{task_id}' exceeded the {loop.kind} limit "
                f"({loop.count}/{loop.threshold}): {loop.detail}"
            )

        self._enforce_decision_gate(task_id)

        agent = self.resolve_agent("review_agent")
        logger.info("reviewing %s with %s", task_id, agent.AGENT_ID)
        self.state.set_review_status(task_id, "IN_PROGRESS")
        output = agent.run(self.get_task(task_id), materialize=False)
        output.task_id = task_id
        self._update_context_utilization(agent, output)

        if output.status != config.AGENT_STATUS_COMPLETED:
            failure = "; ".join(output.errors) or output.summary
            self.state.set_review_status(task_id, "READY")
            self.supervisor.record_attempt(task_id, progressed=False, error=failure)
            self.state.append_current_state(f"{task_id} review attempt failed: {output.summary}")
            return TaskRunResult(
                task_id=task_id,
                agent_id=agent.AGENT_ID,
                output=output,
                previous_status=previous_status,
                new_status=config.TASK_REVIEW,
            )

        outcome = str(output.data.get("review_status", "") or "")
        if outcome not in ("PASS", "PASS WITH ACTIONS", "FAIL"):
            failure = f"review_agent returned no valid review_status (got '{outcome}')"
            self.state.set_review_status(task_id, "READY")
            self.supervisor.record_attempt(task_id, progressed=False, error=failure)
            output = AgentOutput(
                agent_id=agent.AGENT_ID,
                task_id=task_id,
                status=config.AGENT_STATUS_FAILED,
                summary=f"Invalid review result for {task_id}",
                errors=[failure],
            )
            return TaskRunResult(
                task_id=task_id,
                agent_id=agent.AGENT_ID,
                output=output,
                previous_status=previous_status,
                new_status=config.TASK_REVIEW,
            )

        new_status = self.complete_review(task_id, note=output.summary, outcome=outcome)
        self._write_review_report(task, output)

        followups: List[str] = []
        if outcome == "FAIL":
            followups = [item for item in output.data.get("corrections") or []]
        elif outcome == "PASS WITH ACTIONS":
            followups = [item for item in output.data.get("actions") or []]
            if not followups:
                followups = [item for item in output.data.get("corrections") or []]
        created = self._create_followup_tasks(task, followups)

        self.state.update_task_execution(task_id, no_progress_delta=-1)
        if created:
            output.warnings = list(output.warnings) + [
                f"follow-up tasks created: {', '.join(created)}"
            ]
        self._record_proposed_change_if_any(task, output, agent.AGENT_ID)
        self.state.recompute_derived_state()
        self._record_fingerprint()
        checkpoint_id = self._maybe_auto_checkpoint(
            self.get_task(task_id)
        ) or self.check_milestones()
        return TaskRunResult(
            task_id=task_id,
            agent_id=agent.AGENT_ID,
            output=output,
            previous_status=previous_status,
            new_status=new_status,
            checkpoint_id=checkpoint_id,
        )

    def _write_review_report(self, task: Dict[str, Any], output: AgentOutput) -> None:
        """Materialize REVIEW-<task>.md so the review leaves evidence on disk."""
        task_id = str(task.get("id", "TASK"))
        report_name = f"REVIEW-{task_id}.md"
        documents = output.data.get("documents") if isinstance(output.data, dict) else None
        content = None
        if isinstance(documents, dict):
            content = documents.get("REVIEW_REPORT.md") or documents.get(report_name)
        if not isinstance(content, str) or not content.strip():
            content = BaseAgent.render_artifact(report_name, task, output)
        try:
            docs_dir = self.state.project_path / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            save_text_file(docs_dir / report_name, content)
        except OSError:
            return
        output.artifacts = sorted(set(output.artifacts) | {f"docs/{report_name}"})

    def _create_followup_tasks(
        self, source_task: Dict[str, Any], items: List[Any]
    ) -> List[str]:
        """Append correction/action tasks to TASKS.yaml; returns new ids."""
        cleaned = [str(item).strip() for item in items if str(item).strip()][:10]
        if not cleaned:
            return []
        document = self.state.load_tasks_document()
        tasks = document.get("tasks") or []
        max_number = 0
        for entry in tasks:
            match = re.match(r"TASK-(\d+)", str(entry.get("id", "")))
            if match:
                max_number = max(max_number, int(match.group(1)))
        created: List[str] = []
        for item in cleaned:
            max_number += 1
            followup_id = f"TASK-{max_number:03d}"
            tasks.append(
                {
                    "id": followup_id,
                    "title": item[:160],
                    "owner": source_task.get("owner", ""),
                    "status": config.TASK_TODO,
                    "priority": source_task.get("priority", "MEDIUM"),
                    "dependencies": [],
                    "expected_outputs": [],
                    "acceptance_criteria": [item],
                    "input_files": list(source_task.get("input_files") or []),
                    "execution": {
                        "attempt_count": 0,
                        "no_progress_cycles": 0,
                        "strategy_changes": 0,
                        "last_error": None,
                    },
                    "review": {"required": False, "status": "NOT_STARTED"},
                    "notes": f"Follow-up from review of {source_task.get('id')}",
                }
            )
            created.append(followup_id)
        document["tasks"] = tasks
        self.state.save_tasks_document(document)
        self.state.refresh_ready_states()
        self.state.append_current_state(
            f"Follow-up tasks created from {source_task.get('id')}: {', '.join(created)}"
        )
        return created

    def _run_pending_reviews(self) -> List[TaskRunResult]:
        """Dispatch every pending independent review; never raises."""
        results: List[TaskRunResult] = []
        for task in self.pending_review_tasks():
            task_id = str(task.get("id"))
            try:
                results.append(self.dispatch_review(task_id))
            except LoopLimitExceededError as exc:
                loop = getattr(exc, "loop", None) or LoopDetection(
                    task_id=task_id,
                    kind=config.LOOP_KIND_SAME_STRATEGY,
                    count=config.loop_thresholds().same_strategy_max_attempts,
                    threshold=config.loop_thresholds().same_strategy_max_attempts,
                    detail=str(exc),
                )
                results.append(
                    TaskRunResult(
                        task_id=task_id,
                        agent_id="review_agent",
                        output=AgentOutput(
                            agent_id="review_agent",
                            task_id=task_id,
                            status=config.AGENT_STATUS_FAILED,
                            summary=str(exc),
                            errors=[str(exc)],
                        ),
                        previous_status=str(task.get("status")),
                        new_status=str(task.get("status")),
                        loop=loop,
                    )
                )
            except (OrchestratorError, StateError, AgentError) as exc:
                results.append(
                    TaskRunResult(
                        task_id=task_id,
                        agent_id="review_agent",
                        output=AgentOutput(
                            agent_id="review_agent",
                            task_id=task_id,
                            status=config.AGENT_STATUS_FAILED,
                            summary=str(exc),
                            errors=[str(exc)],
                        ),
                        previous_status=str(task.get("status")),
                        new_status=str(task.get("status")),
                    )
                )
        return results

    # ------------------------------------------------------------------
    # Cycles
    # ------------------------------------------------------------------

    def fingerprint(self) -> str:
        parts: List[str] = []
        for task in sorted(self.state.load_tasks(), key=lambda item: str(item.get("id"))):
            execution = task.get("execution") or {}
            parts.append(
                f"{task.get('id')}:{task.get('status')}:"
                f"{execution.get('attempt_count', 0)}:{execution.get('no_progress_cycles', 0)}"
            )
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
        return digest

    def state_fingerprint(self) -> str:
        """Status-only fingerprint used for oscillation detection.

        Excludes attempt counters (which change every dispatch) so a state
        that keeps returning to the same statuses hashes identically.
        """
        parts: List[str] = []
        for task in sorted(self.state.load_tasks(), key=lambda item: str(item.get("id"))):
            review = task.get("review") or {}
            parts.append(
                f"{task.get('id')}:{task.get('status')}:{review.get('status', '')}"
            )
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
        return digest

    def _dispatch_safe(
        self, task: Dict[str, Any], fresh_agent: bool = False
    ) -> Tuple[TaskRunResult, bool]:
        """Dispatch one task, converting known errors into failed results.

        Returns ``(result, raised)`` where ``raised`` marks a pre-execution
        failure (loop limit / dependency / missing agent) that must not
        consume the ``max_tasks`` quota.
        """
        task_id = str(task.get("id"))
        try:
            return self.dispatch(task_id, fresh_agent=fresh_agent), False
        except LoopLimitExceededError as exc:
            logger.warning("loop limit refused %s: %s", task_id, exc)
            loop = getattr(exc, "loop", None) or LoopDetection(
                task_id=task_id,
                kind=config.LOOP_KIND_SAME_STRATEGY,
                count=config.loop_thresholds().same_strategy_max_attempts,
                threshold=config.loop_thresholds().same_strategy_max_attempts,
                detail=str(exc),
            )
            return (
                TaskRunResult(
                    task_id=task_id,
                    agent_id=str(task.get("owner", "unknown")),
                    output=AgentOutput(
                        agent_id=str(task.get("owner", "unknown")),
                        task_id=task_id,
                        status=config.AGENT_STATUS_FAILED,
                        summary=str(exc),
                        errors=[str(exc)],
                    ),
                    previous_status=str(task.get("status")),
                    new_status=str(task.get("status")),
                    loop=loop,
                ),
                True,
            )
        except (OrchestratorError, StateError, AgentError) as exc:
            return (
                TaskRunResult(
                    task_id=task_id,
                    agent_id=str(task.get("owner", "unknown")),
                    output=AgentOutput(
                        agent_id=str(task.get("owner", "unknown")),
                        task_id=task_id,
                        status=config.AGENT_STATUS_FAILED,
                        summary=str(exc),
                        errors=[str(exc)],
                    ),
                    previous_status=str(task.get("status")),
                    new_status=str(task.get("status")),
                ),
                True,
            )

    def run_cycle(
        self, max_tasks: int = 25, max_concurrent: int = 1
    ) -> List[TaskRunResult]:
        """Run pending reviews, then dispatch every currently READY task (bounded).

        ``max_concurrent > 1`` overlaps agent executions with a thread pool;
        task-state mutations stay serialized through ``_state_lock`` and
        results are returned in READY order regardless of finish order.
        """
        results: List[TaskRunResult] = []
        with self._state_lock:
            self.state.refresh_ready_states()
            self._record_fingerprint()
        results.extend(self._run_pending_reviews())
        with self._state_lock:
            ready = self.state.get_ready_tasks()

        if max_concurrent > 1 and len(ready) > 1:
            batch = ready[:max_tasks]
            logger.debug(
                "running %d ready task(s), max_concurrent=%d", len(batch), max_concurrent
            )
            with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
                futures = [
                    pool.submit(self._dispatch_safe, task, True) for task in batch
                ]
                for future in futures:
                    result, _raised = future.result()
                    results.append(result)
        else:
            dispatched = 0
            for task in ready:
                if dispatched >= max_tasks:
                    break
                result, raised = self._dispatch_safe(task)
                results.append(result)
                if not raised:
                    dispatched += 1
        self._track_progress()
        return results

    def run_until_stalled(
        self, max_cycles: int = 10, max_concurrent: int = 1
    ) -> List[List[TaskRunResult]]:
        """Run cycles until no progress is made for the configured limit."""
        all_results: List[List[TaskRunResult]] = []
        limit = config.loop_thresholds().no_progress_max_cycles
        for _cycle in range(max_cycles):
            results = self.run_cycle(max_concurrent=max_concurrent)
            all_results.append(results)
            if not results:
                break
            if self._idle_cycles >= limit:
                break
        self.supervisor.sync_project_health()
        return all_results

    def run_task(self, task_id: str) -> TaskRunResult:
        task = self.get_task(task_id)
        review = task.get("review") or {}
        if task.get("status") == config.TASK_REVIEW and str(review.get("status", "")) in (
            "READY",
            "IN_PROGRESS",
        ):
            result = self.dispatch_review(task_id)
        else:
            result = self.dispatch(task_id)
        self.supervisor.sync_project_health()
        return result

    def _track_progress(self) -> None:
        fingerprint = self.fingerprint()
        if self._last_fingerprint is None:
            self._last_fingerprint = fingerprint
            return
        if fingerprint == self._last_fingerprint:
            self._idle_cycles += 1
        else:
            self._idle_cycles = 0
            self._last_fingerprint = fingerprint

    # ------------------------------------------------------------------
    # Context compaction / checkpointing
    # ------------------------------------------------------------------

    def _maybe_auto_checkpoint(self, task: Dict[str, Any]) -> Optional[str]:
        if not self.auto_checkpoint:
            return None
        context = self.supervisor.check_context_usage()
        if not context.get("compaction_required"):
            return None
        return self.compact_context(reason=f"Context at {context['utilization_percent']}%")

    @staticmethod
    def _milestone_slug(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
        return slug or "milestone"

    def check_milestones(self) -> Optional[str]:
        """Save a checkpoint for every newly completed milestone.

        Tasks may declare ``milestone: <name>``. Once every task carrying
        that milestone is in a terminal status, ``cp-milestone-<slug>`` is
        written exactly once (deduplicated against existing checkpoints).
        When the whole task graph is terminal, ``cp-milestone-complete`` is
        written as well. Returns the id of the first checkpoint created.
        """
        if not self.auto_checkpoint:
            return None
        tasks = self.state.load_tasks_document().get("tasks") or []
        if not tasks:
            return None
        terminal = config.TERMINAL_TASK_STATUSES
        existing = {str(entry.get("id")) for entry in self.checkpoints.list_checkpoints()}

        by_milestone: Dict[str, List[Dict[str, Any]]] = {}
        for task in tasks:
            name = str(task.get("milestone") or "").strip()
            if name:
                by_milestone.setdefault(name, []).append(task)

        pending: List[Tuple[str, str]] = []
        for name in sorted(by_milestone):
            group = by_milestone[name]
            if all(str(entry.get("status")) in terminal for entry in group):
                checkpoint_id = f"cp-milestone-{self._milestone_slug(name)}"
                if checkpoint_id not in existing:
                    pending.append(
                        (
                            checkpoint_id,
                            f"Milestone '{name}' completed: {len(group)}/{len(group)} "
                            "tasks terminal.",
                        )
                    )
        if all(str(entry.get("status")) in terminal for entry in tasks):
            if "cp-milestone-complete" not in existing:
                pending.append(
                    ("cp-milestone-complete", "All tasks reached a terminal status.")
                )

        created: Optional[str] = None
        for checkpoint_id, notes in pending:
            if checkpoint_id in existing:
                continue
            self.create_checkpoint(checkpoint_id, notes=notes)
            existing.add(checkpoint_id)
            if created is None:
                created = checkpoint_id
        return created

    def compact_context(self, reason: str = "context compaction") -> str:
        """Checkpoint + memory note at the compaction threshold."""
        self._checkpoint_counter += 1
        checkpoint_id = f"cp-auto-{self._checkpoint_counter:03d}"
        existing = {entry.get("id") for entry in self.checkpoints.list_checkpoints()}
        while checkpoint_id in existing:
            self._checkpoint_counter += 1
            checkpoint_id = f"cp-auto-{self._checkpoint_counter:03d}"
        notes = f"Automatic checkpoint: {reason}"
        self.state.record_checkpoint(checkpoint_id)
        self.checkpoints.create_checkpoint(checkpoint_id, notes=notes)
        self.state.append_memory_section(
            "Context Compaction",
            (
                f"Checkpoint **{checkpoint_id}** saved at {utc_now_iso()}.\n\n"
                f"Reason: {reason}.\n\n"
                "Working context was discarded; resume from PROJECT_MEMORY.md and TASKS.yaml."
            ),
        )
        self.state.append_current_state(f"Context compacted; checkpoint {checkpoint_id} saved.")
        self.state.append_changelog(f"Auto checkpoint {checkpoint_id} ({reason})")
        return checkpoint_id

    def create_checkpoint(self, checkpoint_id: str, notes: str = "") -> str:
        self.state.record_checkpoint(checkpoint_id)
        self.checkpoints.create_checkpoint(checkpoint_id, notes=notes)
        self.state.append_changelog(f"Checkpoint {checkpoint_id} saved. {notes}".strip())
        logger.info("checkpoint '%s' saved", checkpoint_id)
        return checkpoint_id

    def resume_from_checkpoint(self, checkpoint_id: str, isolated: bool = False) -> Path:
        if isolated:
            target = self.project_path / f".restore-{checkpoint_id}"
            restored = self.checkpoints.restore_checkpoint(checkpoint_id, target_path=target)
            return restored
        restored = self.checkpoints.restore_checkpoint(checkpoint_id)
        self.state = StateManager(self.project_path)
        self.supervisor = SupervisorAgent(state_manager=self.state)
        self.registered_agents = {}
        self._last_fingerprint = None
        self._fingerprint_history = []
        self._idle_cycles = 0
        logger.info("checkpoint '%s' restored (loop history cleared)", checkpoint_id)
        return restored

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def health(self) -> HealthReport:
        return self.supervisor.check_health()

    def sync_health(self) -> HealthReport:
        self.state.refresh_ready_states()
        return self.supervisor.sync_project_health()

    def status(self) -> Dict[str, Any]:
        project = self.state.load_project()
        report = self.health()
        return {
            "project": project.get("project", {}),
            "phase": (project.get("phase") or {}).get("current"),
            "health": str(report.state),
            "context": report.context,
            "tasks": self.state.summary_counts(),
            "ready": [str(task.get("id")) for task in self.state.get_ready_tasks()],
            "loops": [loop.to_dict() for loop in report.loops],
            "deadlocks": report.deadlocks,
            "checkpoints": [entry.get("id") for entry in self.checkpoints.list_checkpoints()],
            "registered_agents": sorted(self.registered_agents) or sorted(AGENT_REGISTRY),
        }

    def print_status(self) -> str:
        return self.state.print_status()


__all__ = [
    "MasterOrchestrator",
    "OrchestratorError",
    "LoopLimitExceededError",
    "MissingAgentError",
    "TaskRunResult",
]
