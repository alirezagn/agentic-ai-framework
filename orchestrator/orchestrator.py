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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config
from .agents.base_agent import (
    AGENT_REGISTRY,
    AgentError,
    AgentOutput,
    BaseAgent,
    create_agent,
    delivery_problems,
    normalize_agent_name,
    preexisting_expected,
    widen_delivery_scope,
)
from .auto_plan import PLANNING_RULES, expand_implementation_stages
from .checkpoint_manager import CheckpointManager
from .context_monitor import tokens_for_output, utilization_for_output
from .deploy_runner import STATUS_SKIPPED, DeployRecord, DeployRunner
from .state_manager import (
    UNTRUSTED_TASK_FIELDS,
    StateError,
    StateManager,
    TaskNotFoundError,
    save_text_file,
    strip_untrusted_task_fields,
    utc_now_iso,
)
from .supervisor import HealthReport, LoopDetection, SupervisorAgent

logger = logging.getLogger(__name__)

# GAP-HIGH-01: per-attempt scratch state on a BaseAgent. These are rewritten at
# the top of every run() (see orchestrator.agents.base_agent.run), so they must
# never be carried from a pinned instance onto a detached one -- that would
# reintroduce the sharing this framework avoids by re-instantiating per dispatch.
_AGENT_SCRATCH_ATTRIBUTES = frozenset(
    {"_current_task_id", "last_payload_chars", "_delivery_snapshot"}
)


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


#: Suffixes on a task's ``expected_outputs`` that indicate a build/test
#: artifact, i.e. something that can only legitimately be produced by running
#: something. A task delivering one of these is asking for ground truth.
_TEST_LIKE_SUFFIXES = (
    ".log",
    ".bin",
    ".elf",
    ".map",
    ".o",
    ".obj",
    ".out",
    ".hex",
    ".test",
    ".xml",
    ".json",
    ".trx",
    ".junit",
    ".coverage",
    ".lcov",
)


#: The DoD problem that is the review gate itself rather than a defect in the
#: work. A task entering REVIEW always trips it (``review.status`` is not yet
#: PASS), so forwarding it as a "known failure" would make every review-required
#: task fail its own review — the guard would fire on the gate it was meant to
#: sit behind.
REVIEW_GATE_PROBLEM = "independent review not passed"


def _review_forwardable_problems(problems: Sequence[str]) -> List[str]:
    """Drop the review-gate line from a DoD result before forwarding it.

    Everything else — a missing expected output, an unmet acceptance check, an
    unevidenced execution claim — is a real defect the reviewer must weigh.
    """
    return [
        str(item)
        for item in problems
        if str(item).strip() and str(item).strip() != REVIEW_GATE_PROBLEM
    ]


def _recorded_dod_problems(task: Dict[str, Any]) -> List[str]:
    """Recover the DoD problems recorded against a task awaiting review.

    GAP-CRIT-04. The reviewer's only durable trace of a failed DoD is the task
    note, and that note holds the *claiming summary* whenever the task went to
    review rather than to FAILED. So the problems are also persisted as a
    structured list, and this reads it back.

    Reading the record rather than re-running the DoD matters: the task has
    since been persisted and reloaded, so re-deriving could produce a different
    answer than the one the task was failed on.
    """
    recorded = task.get("pending_dod_problems")
    if not isinstance(recorded, list):
        return []
    problems: List[str] = []
    for item in recorded:
        if not isinstance(item, str):
            # A non-string would stringify to "None"/"{}" and be shown to the
            # reviewer as a finding, which is worse than omitting it.
            continue
        text = item.strip()
        if text:
            problems.append(text)
    return problems


# DoD fallback ingestion: owner-scoped default acceptance criteria, used when
# planning (auto_plan, startup specs, requirements seeding) omitted
# ``acceptance_criteria``. Wording mirrors the seeded criteria vocabulary in
# state_manager, so a fallback criterion reads like one a human would write.
_DEFAULT_ACCEPTANCE_CRITERIA = [
    "Complete task analysis and produce structured markdown outputs",
]

_FALLBACK_ACCEPTANCE_CRITERIA: Dict[str, List[str]] = {
    "requirements_agent": [
        "Requirements and acceptance criteria captured in docs/REQUIREMENTS.md",
    ],
    "test_agent": [
        "Tests map to requirements and pass",
    ],
    "documentation_agent": [
        "Docs match shipped behavior",
    ],
}


def _fallback_acceptance_criteria(task: Dict[str, Any]) -> List[str]:
    """Measurable default criteria for a task with a missing/empty list."""
    owner = str(task.get("owner") or "").strip().lower()
    specific = _FALLBACK_ACCEPTANCE_CRITERIA.get(owner)
    if specific:
        return list(specific)
    return list(_DEFAULT_ACCEPTANCE_CRITERIA)


def _test_like_outputs(task: Dict[str, Any]) -> bool:
    """True when a task's expected outputs look like build/test artifacts.

    A third trigger for the evidence check, alongside a declared ``data.deploy``
    and a summary claiming execution. Recognising the artifact by shape catches
    the case where the agent neither declares nor narrates — it just quietly
    hands over a plausible-looking ``test_results.log``.
    """
    outputs = task.get("expected_outputs") or []
    if not isinstance(outputs, list):
        return False
    for item in outputs:
        name = str(item).strip().lower()
        if not name:
            continue
        if name.endswith(_TEST_LIKE_SUFFIXES):
            return True
        base = name.rsplit("/", 1)[-1]
        if base.startswith(("test_", "tests_")) or "test_result" in base:
            return True
    return False


_PROSE_NOT_RUN = re.compile(r"\bnot[\s-]?run\b", re.IGNORECASE)


def _reports_not_run(output: Optional["AgentOutput"]) -> bool:
    """True when the output states, in prose, that nothing was executed.

    ``data.test_status`` is the structured channel, but a model that writes
    "tests NOT RUN" in its summary has reported the honest negative just as
    clearly — and a repair round that fails to move it into the field turns a
    legitimate outcome into a failed task. Accepting the prose form costs
    nothing: a fabricated *pass* still needs a ground-truth record, while a
    fabricated "NOT RUN" only loses information. Honoured only when the same
    output does not also claim execution.
    """
    if output is None:
        return False
    text = " ".join(
        [str(output.summary)] + [str(item) for item in output.artifacts or []]
    )
    return bool(_PROSE_NOT_RUN.search(text))


#: How much of a failing run's captured output is quoted back to the repair
#: round: enough to name file, line and exception, small enough that the
#: repair note stays a note.
_FAILURE_TAIL_CHARS = 600


def _failure_excerpt(record: DeployRecord) -> str:
    """The captured output of a failing run, quoted into the problem text.

    An exit code alone leaves the repair round blind. Rerun-5's TASK-007
    failed with ``pytest exited 2`` while the actual error — ``NameError:
    name 'TypedDict' is not defined`` — sat in the record's captured tails
    and never reached the one repair round, which then spent itself guessing.
    The tail window of stderr (stdout when stderr is empty) is quoted,
    flattened to a single line and bounded so the note stays small: pytest's
    short summary names file and exception at the end of that window.
    """
    raw = (record.stderr_tail or "").strip() or (record.stdout_tail or "").strip()
    if not raw:
        return ""
    flat = " ".join(raw.split())
    if len(flat) > _FAILURE_TAIL_CHARS:
        flat = flat[-_FAILURE_TAIL_CHARS:]
    return f" — output: {flat}"


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
        # GAP-CRIT-01: the only component in the package that may spawn a
        # process. Injectable so tests can substitute a fake and stay hermetic.
        self.deploy_runner = DeployRunner(self.project_path)
        self._checkpoint_counter = 0
        self._idle_cycles = 0
        self._last_fingerprint: Optional[str] = None
        # Recent project-state fingerprints, used to detect A<->B oscillation.
        self._fingerprint_history: List[str] = []
        self.registered_agents: Dict[str, BaseAgent] = {}
        # GAP-HIGH-01: agents handed in via register_agent() are caller-owned
        # objects, so they cannot simply be re-created per dispatch. We record
        # their class instead, so a parallel dispatch can build an equivalent
        # *separate* instance rather than sharing one object's mutable scratch
        # state across threads. Keys mirror ``registered_agents``.
        self._pinned_agents: Dict[str, type] = {}
        # Serializes TASKS.yaml / PROJECT.yaml read-modify-write cycles so
        # parallel dispatch (run_cycle(max_concurrent>1)) cannot lose updates.
        self._state_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Agent registration
    # ------------------------------------------------------------------

    def register_agent(self, agent: BaseAgent) -> BaseAgent:
        """Register a caller-owned agent instance for ``agent.AGENT_ID``.

        GAP-HIGH-01. The instance is pinned so stateful agents keep their state
        across dispatches, but its *class* is recorded so a parallel dispatch can
        obtain an equivalent independent instance (see :meth:`resolve_agent`).
        An agent whose class cannot be re-instantiated without arguments is
        still shared, and that is recorded rather than silently pretended away.
        """
        key = normalize_agent_name(agent.AGENT_ID)
        with self._state_lock:
            self.registered_agents[key] = agent
            self._pinned_agents[key] = type(agent)
        return agent

    def _instantiate_like(self, instance: BaseAgent) -> Optional[BaseAgent]:
        """Build a fresh, independent instance equivalent to ``instance``.

        The constructor is called with the state manager alone, then the pinned
        instance's own attributes are carried over. Carrying them matters: a
        caller who registered a customized agent (injected callbacks, extra
        configuration) must get an agent that behaves the same, not a bare one.

        The per-attempt scratch attributes are excluded. They are rewritten at
        the top of every ``run()`` (``base_agent._current_task_id``,
        ``last_payload_chars``, ``_delivery_snapshot``), so copying a stale
        value would reintroduce the exact sharing this method exists to prevent.

        Returns ``None`` when the class cannot be constructed from the state
        manager alone; the caller then falls back to sharing, and
        :meth:`shared_agent_keys` reports it so the condition is visible rather
        than silently causing a race.
        """
        agent_class = type(instance)
        try:
            detached = agent_class(state_manager=self.state)
        except Exception:
            logger.warning(
                "agent %s cannot be re-instantiated for parallel dispatch; "
                "falling back to the shared instance",
                agent_class.__name__,
            )
            return None
        for name, value in vars(instance).items():
            if name in _AGENT_SCRATCH_ATTRIBUTES:
                continue
            try:
                setattr(detached, name, value)
            except Exception:  # read-only property; the fresh default stands
                continue
        return detached

    def shared_agent_keys(self) -> List[str]:
        """Agent ids currently resolved to a shared instance.

        Non-empty means at least one registered agent is not parallel-safe,
        because its class could not be re-instantiated. Surfaced by
        ``orchestrator status`` so the condition is visible before it causes a
        race.
        """
        with self._state_lock:
            shared: List[str] = []
            for key, instance in self.registered_agents.items():
                if self._instantiate_like(instance) is None:
                    shared.append(key)
            return sorted(shared)

    def register_agent_class(self, agent_class: type) -> BaseAgent:
        agent = agent_class(state_manager=self.state)
        return self.register_agent(agent)

    def resolve_agent(self, owner: str, fresh: bool = False) -> BaseAgent:
        """Resolve the agent for ``owner``.

        ``fresh=True`` returns an instance that no other in-flight dispatch is
        using. GAP-HIGH-01: previously a *pinned* agent was returned even for a
        fresh request, so two parallel tasks owned by the same registered agent
        shared one object's mutable scratch state — ``_current_task_id``,
        ``last_payload_chars`` and ``_delivery_snapshot`` — which let one task's
        output be attributed to another and one task's DoD read another's
        delivery snapshot.

        Falls back to the shared pinned instance when the class cannot be
        re-instantiated; :meth:`shared_agent_keys` reports that.
        """
        key = normalize_agent_name(owner)
        with self._state_lock:
            registered = self.registered_agents.get(key)
            if registered is not None:
                if not fresh:
                    return registered
                detached = self._instantiate_like(registered)
                return detached if detached is not None else registered
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

    def _persist_last_loop(self, task_id: str, loop: LoopDetection) -> None:
        """Record why a dispatch was refused (A5: visible after resume)."""
        if loop.task_id != task_id:
            # PROJECT-level detections (oscillation) are not task state.
            return
        try:
            self.state.update_task_execution(
                task_id, set_values={"last_loop": loop.to_dict()}
            )
        except StateError:
            logger.debug("could not persist last_loop for %s", task_id, exc_info=True)

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
                self._persist_last_loop(task_id, oscillation)
                raise LoopLimitExceededError(
                    f"Task '{task_id}' refused: project state is oscillating "
                    f"({oscillation.detail})",
                    loop=oscillation,
                )

            loop = self.supervisor.should_block_dispatch(task_id)
            if loop is not None:
                self._persist_last_loop(task_id, loop)
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
            # Phase 2 must see the post-update view: agents that inspect
            # task.status (e.g. the reviewer) otherwise read the stale READY
            # value and can refuse the work as "not executed".
            task = self.get_task(task_id)

        # --- phase 2: agent execution (unlocked) ------------------------
        # G22: task-start snapshot shared by this attempt's DoD checks.
        delivery_snapshot = preexisting_expected(self.state.project_path, task)
        logger.info("dispatching %s -> %s (owner: %s)", task_id, agent.AGENT_ID, owner)
        output = agent.run(task)
        output.task_id = task_id

        # GAP-CRIT-01: run anything the agent asked to execute, HERE — outside
        # the state lock. A build or test suite can run for minutes, and holding
        # `_state_lock` across it would stall every other worker's state turn.
        # This is the only ground truth in the system: the runner, not the model,
        # stamps `executed` and the exit code.
        deploy_records = self._run_requested_deploys(task_id, output)

        # --- phase 2b: Definition-of-Done evaluation and auto-repair --------
        # GAP-HIGH-03. The DoD check and the repair round are computed OUTSIDE
        # the state lock. `repair_delivery` is a full LLM round trip (up to
        # ORCHESTRATOR_LLM_TIMEOUT seconds, default 120); running it while
        # holding `_state_lock` blocked every other dispatch worker's state
        # turn for that duration, turning `--max-concurrent 4` into a queue.
        #
        # The evaluation is read-only, so moving it out of the lock is safe;
        # every *write* still happens in phase 3 under the lock.
        review_block = task.get("review") or {}
        dod_problems: List[str] = []
        if output.status == config.AGENT_STATUS_COMPLETED:
            dod_problems = self.definition_of_done(
                task, output,
                preexisting=delivery_snapshot,
                deploy_records=deploy_records,
            )
            if dod_problems:
                # One automatic repair round: feed the DoD problems back to the
                # agent instead of failing cold (symmetric to the truncation
                # repair on parse failures).
                logger.info(
                    "DoD reported %d problem(s) for %s; attempting auto-repair off-lock",
                    len(dod_problems),
                    task_id,
                )
                repaired = agent.repair_delivery(task, output, dod_problems)
                if repaired is not None:
                    repaired.task_id = task_id
                    logger.info("DoD auto-repair succeeded for %s", task_id)
                    original_output = output
                    output = repaired
                    # The repair must not narrow the checked scope: files the
                    # original delivery touched stay under the delivery/import
                    # contract even when the repair reply does not mention
                    # them (sys-usage TASK-005: the repair rewrote main.py and
                    # the undeclared typing_extensions import in processor.py
                    # would have dropped out of the re-evaluation — only the
                    # executed-verification re-run still caught it).
                    widen_delivery_scope(original_output, repaired)
                    # Ground truth must follow the repair: records from before
                    # it judge the pre-repair workspace (sys-usage TASK-006:
                    # a pytest exit 2 recorded at 16:48:12 was re-checked
                    # against a test file rewritten at 16:48:34, so the repair
                    # could never clear the evidence gate). Re-run whatever
                    # the repaired output declares — falling back to the
                    # original declaration when the repair dropped it — and
                    # evaluate the DoD against the fresh records.
                    repaired_data = (
                        repaired.data if isinstance(repaired.data, dict) else {}
                    )
                    rerun = repaired if repaired_data.get("deploy") else original_output
                    logger.info(
                        "re-running deploy evidence for %s after auto-repair", task_id
                    )
                    deploy_records = self._run_requested_deploys(task_id, rerun)
                    dod_problems = self.definition_of_done(
                        task, output,
                        preexisting=delivery_snapshot,
                        deploy_records=deploy_records,
                    )
                else:
                    logger.info("DoD auto-repair produced no fix for %s", task_id)

        # --- phase 3: apply results (locked) ----------------------------
        with self._state_lock:
            self._update_context_utilization(agent, output)
            self._record_proposed_change_if_any(task, output, agent.AGENT_ID)

            checkpoint_id: Optional[str] = None
            new_status: str

            # DoD fallback ingestion: persist owner-scoped default criteria
            # when planning omitted them (locked phase — phase 2b stays
            # read-only by design).
            self._ingest_fallback_criteria(task)

            # Findings harvesting: a turn flagged non-compliant by DoD, or a
            # schema-rejected (FAILED) turn, may still carry real analysis.
            # Persist it before the failure bookkeeping so the model's work
            # survives the rejected dispatch.
            if dod_problems or output.status == config.AGENT_STATUS_FAILED:
                try:
                    harvested = agent.harvest_findings(task, output)
                except Exception:  # noqa: BLE001 — harvesting never fails a dispatch
                    logger.debug(
                        "findings harvest failed for %s", task_id, exc_info=True
                    )
                    harvested = []
                if harvested:
                    output.warnings = list(output.warnings) + [
                        f"findings harvested: {', '.join(harvested)}"
                    ]

            if output.status == config.AGENT_STATUS_COMPLETED:
                if review_block.get("required"):
                    new_status = config.TASK_REVIEW
                    self.state.set_review_status(task_id, "READY")
                    # GAP-CRIT-04: the DoD problems travel with the task into
                    # review, and are persisted so a review resumed in a later
                    # cycle can still recover them. A review that cannot see them
                    # can approve work the DoD already rejected.
                    if dod_problems:
                        forwarded = _review_forwardable_problems(dod_problems)
                        if forwarded:
                            self._persist_dod_problems(task_id, forwarded)
                        else:
                            self._clear_dod_problems(task_id)
                        output.warnings = list(output.warnings) + [
                            f"definition_of_done reported {len(forwarded)} structural "
                            "problem(s); the reviewer must not return PASS while "
                            "these stand"
                        ]
                    else:
                        self._clear_dod_problems(task_id)
                    self.state.update_task_status(
                        task_id, new_status, note=output.summary, error=None
                    )
                    self.state.update_task_execution(task_id, reset_error=True)
                elif dod_problems:
                    # Definition of Done is not met: the task may not become DONE.
                    # Record the rejection (not the claiming summary) as the note
                    # so the next attempt sees honest context.
                    new_status = config.TASK_FAILED
                    error_text = "DoD unmet: " + "; ".join(dod_problems)
                    self.state.update_task_status(
                        task_id, new_status, note=error_text, error=error_text
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
                    self.state.append_current_state(
                        f"{task_id} ({agent.AGENT_ID}) -> {new_status}: {output.summary}"
                    )
                # B7: evidence of progress cancels the pre-execution no-progress
                # bump recorded in phase 1 (failed attempts that still produced
                # artifacts count as progress too).
                progressed = new_status in (
                    config.TASK_REVIEW,
                    config.TASK_DONE,
                    config.TASK_DONE_WITH_LIMITATION,
                ) or bool(output.artifacts)
                if progressed:
                    self.state.update_task_execution(task_id, no_progress_delta=-1)
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
                    if self.auto_checkpoint:
                        # Best-effort: the task is already FAILED with its own
                        # error text. Failing it again with a checkpoint
                        # collision would hide the real cause behind
                        # bookkeeping (the exact `cp-risk-RISK-001 already
                        # exists` failure that stopped a whole run).
                        risk_checkpoint = self._safe_auto_checkpoint(
                            lambda: self.create_checkpoint(
                                f"cp-risk-{risk_id}",
                                notes=f"Failure risk {risk_id} recorded for {task_id}",
                            ),
                            "risk",
                        )
                        if risk_checkpoint:
                            output.warnings = list(output.warnings) + [
                                f"checkpoint: {risk_checkpoint}"
                            ]

                # Validation circuit-breaker: count this rejection and, at
                # two consecutive DoD/schema failures, auto-waive the task so
                # dependents proceed without a human decision (code/runtime
                # failures reset the streak and escalate the normal way).
                if self.supervisor.apply_validation_circuit_breaker(
                    task_id, error_text
                ):
                    output.warnings = list(output.warnings) + [
                        f"{task_id} auto-waived by the supervisor after repeated "
                        "DoD/schema rejections; dependents may proceed"
                    ]

            self._record_loop_signals(task_id, task, output)
            self._ingest_output_tasks(output)
            if new_status in (
                config.TASK_REVIEW,
                config.TASK_DONE,
                config.TASK_DONE_WITH_LIMITATION,
            ):
                self.state.update_task_execution(task_id, set_values={"last_loop": None})

            after = self.get_task(task_id)
            previous_phase = str(
                (self.state.load_project().get("phase") or {}).get("current")
                or config.PHASE_REQUIREMENTS
            )
            self.state.recompute_derived_state()
            # Side effect first, then pick the *reported* id: compaction >
            # milestone > phase (every check must run — no short-circuit).
            phase_checkpoint = self._safe_auto_checkpoint(
                lambda: self._checkpoint_phase_advance(previous_phase), "phase"
            )
            self._record_fingerprint()
            loop_now = None
            for detection in self.supervisor.detect_loops([after]):
                if detection.task_id == task_id and detection.exceeded:
                    loop_now = detection
                    break

            # GAP-HIGH-02: evaluate EVERY trigger, then pick a winner.
            #
            # This used to be `a or b or c`, which short-circuits: on a
            # dispatch that both crossed the compaction threshold and completed
            # the last task of a milestone, `check_milestones()` never ran. If
            # that was the final dispatch, `cp-milestone-complete` was never
            # written — the one moment the snapshot matters most.
            #
            # Each trigger is a *decision* (should a checkpoint be taken), not
            # merely a value, so evaluating all of them is correct; only one
            # checkpoint is then created, because a compaction and a milestone
            # completing in the same instant are the same event.
            auto_checkpoint_id = self._safe_auto_checkpoint(
                lambda: self._maybe_auto_checkpoint(after), "auto"
            )
            milestone_checkpoint_id = self._safe_auto_checkpoint(
                self.check_milestones, "milestone"
            )
            checkpoint_id = (
                auto_checkpoint_id
                or milestone_checkpoint_id
                or phase_checkpoint
            )
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

    def _run_requested_deploys(
        self,
        task_id: str,
        output: AgentOutput,
    ) -> List[DeployRecord]:
        """Execute whatever the agent asked to run, off the state lock.

        GAP-CRIT-01. An agent may propose *what* to run; it never decides
        *whether* it ran. Every invocation goes through
        :class:`~orchestrator.deploy_runner.DeployRunner`, which stamps
        ``executed`` and the real exit code itself.

        Records are attached to ``output.data["deploy_results"]`` so the agent's
        own edit session and the DoD see identical evidence, and each executed
        transcript is persisted under ``docs/evidence/<task>/``.

        Returns an empty list when the agent requested nothing — that is the
        common case and must stay cheap.
        """
        if output is None:
            return []
        data = output.data if isinstance(output.data, dict) else {}
        requested = data.get("deploy")
        if not requested:
            return []

        records: List[DeployRecord] = []
        try:
            records = list(self.deploy_runner.run_payload(requested))
        except Exception as exc:  # pragma: no cover - runner is defensive already
            logger.warning("deploy runner failed for %s: %s", task_id, exc)
            return []

        for index, record in enumerate(records):
            if not record.executed:
                logger.info(
                    "deploy not executed for %s (%s): %s",
                    task_id,
                    record.status,
                    record.reason,
                )
                continue
            artifact = self.deploy_runner.write_evidence(task_id, record, index)
            if artifact:
                record.artifact_path = artifact

        # Attach to the output so the DoD, a repair turn and any later reviewer
        # all read the same records rather than re-deriving them.
        data["deploy_results"] = [record.to_dict() for record in records]
        output.data = data
        return records

    def _ingest_output_tasks(self, output: AgentOutput) -> List[str]:
        """Append goal-derived tasks carried in ``data.tasks`` (A2).

        This is the untrusted boundary: ``data.tasks`` is model-authored. GAP-CRIT-05
        — every spec is sanitized by :func:`strip_untrusted_task_fields` before
        ``append_task`` sees it, and ``append_task`` derives the status from the
        dependency list regardless. So a planner replying
        ``{"status": "DONE"}`` or ``{"execution": {"attempt_count": 999}}``
        gets a task that is genuinely pending (or BLOCKED when it has
        dependencies) with zeroed counters.

        Anything stripped is reported as an output warning rather than silently
        discarded, so a misbehaving model is visible in the dispatch log
        instead of quietly corrected.
        """
        if output.status != config.AGENT_STATUS_COMPLETED:
            return []
        data = output.data if isinstance(output.data, dict) else {}
        raw = data.get("tasks")
        if not isinstance(raw, list) or not raw:
            return []
        created: List[str] = []
        warnings: List[str] = []
        for spec in raw:
            if not isinstance(spec, dict):
                continue
            rejected = sorted(set(UNTRUSTED_TASK_FIELDS) & set(spec))
            try:
                cleaned = strip_untrusted_task_fields(spec)
                task = self.state.append_task(cleaned)
            except StateError as exc:
                warnings.append(f"task ingest skipped: {exc}")
                continue
            created.append(str(task.get("id")))
            if rejected:
                warnings.append(
                    f"task {task.get('id')}: ignored model-supplied "
                    f"{', '.join(rejected)} (status is derived from dependencies)"
                )
        if created:
            self.state.append_current_state(
                f"Tasks added from agent output: {', '.join(created)}"
            )
        if warnings:
            output.warnings = list(output.warnings) + warnings
        return created

    # ------------------------------------------------------------------
    # Goal -> task graph (A2 / G2)
    # ------------------------------------------------------------------

    def build_plan(
        self,
        goal: Optional[str] = None,
        max_tasks: int = 8,
        force: bool = False,
    ) -> List[str]:
        """Generate the task graph from the goal via the planning agent.

        Runs the planning agent on a synthetic planning task (no artifact
        materialization), normalizes ``data.tasks`` and ingests it through
        :meth:`_ingest_output_tasks`. An existing graph is only replaced
        after the agent produced a usable plan, so a failed or unavailable
        LLM never destroys existing tasks.
        """
        max_tasks = max(1, int(max_tasks or 8))
        existing = self.state.load_tasks()
        if existing and not force:
            raise StateError(
                f"graph already has {len(existing)} task(s); "
                "edit TASKS.yaml or pass force=True to regenerate"
            )
        goal_text = str(goal or "").strip()
        if not goal_text:
            try:
                goal_text = self.state.load_memory().strip()
            except StateError:
                goal_text = ""
        agent = self.resolve_agent("planning_agent")
        contract = (
            "Reply ONLY with a single JSON object of the form:\n"
            '{"status":"completed","summary":"<one line>","data":{"tasks":['
            '{"title":"...","owner":"requirements_agent","priority":"CRITICAL",'
            '"dependencies":[0],"expected_outputs":["docs/X.md"],'
            '"acceptance_criteria":["..."],"input_files":["docs/PRD.md"],'
            '"notes":"<one line of execution guidance>"}]}}\n'
            "Rules: 3..8 tasks covering the goal; owner must be one of: "
            "requirements_agent, research_agent, architecture_agent, "
            "planning_agent, hardware_agent, software_agent, firmware_agent, "
            "test_agent, review_agent, documentation_agent; dependencies are "
            "0-based indices into this tasks array (earlier entries only); "
            "never include id, status, agent_id or task_id fields.\n"
            "input_files (optional): for each task pick 1..6 REAL files listed "
            "below (or known project paths) that the task must read — "
            "nonexistent paths are dropped and, when nothing valid remains, "
            "core docs are attached automatically. notes (optional): short "
            "execution guidance for the task owner. If a task reads a file "
            "that another task in this plan will produce (its "
            "expected_outputs), declare that task in dependencies — the "
            "review/synthesis task must always run after the tasks whose "
            "outputs it reads.\n\n"
            f"{PLANNING_RULES}"
        )
        plan_task: Dict[str, Any] = {
            "id": "PLAN-001",
            "title": "Plan the initial task graph for the project goal",
            "owner": "planning_agent",
            "status": config.TASK_IN_PROGRESS,
            "priority": "CRITICAL",
            "dependencies": [],
            "expected_outputs": [],
            "acceptance_criteria": [
                "Task graph covers the goal end to end with valid owners, "
                "priorities and an acyclic dependency graph"
            ],
            "notes": (
                f"Goal:\n{goal_text}\n\n{contract}\n\n{self._plan_source_index()}"
                if goal_text
                else f"{contract}\n\n{self._plan_source_index()}"
            ),
            "input_files": self._plan_context_files(),
        }
        output = agent.run(plan_task, materialize=False)
        if output.status != config.AGENT_STATUS_COMPLETED:
            reason = "; ".join(output.errors) or output.summary or "unknown error"
            raise StateError(f"planning agent failed: {reason}")
        data = output.data if isinstance(output.data, dict) else {}
        raw = data.get("tasks")
        if not isinstance(raw, list) or not raw:
            raise StateError(
                "planning agent returned no tasks (expected JSON with "
                f"data.tasks; summary={output.summary[:120]!r}, "
                f"data keys={sorted(str(key) for key in data)})"
            )
        specs = self._normalize_plan_specs(raw[:max_tasks])
        if not specs:
            raise StateError("planning output contained no usable task specs")
        # Architecture rule: a GUI or multi-module goal may not ship as one
        # "implement everything" task. The contract asks the model for the
        # stage chain; this is the deterministic pass that enforces it when
        # the plan arrives anyway as a single implementation task.
        specs = expand_implementation_stages(specs, goal=goal_text)
        prepared: List[Dict[str, Any]] = []
        # G11: remember the model's REQUESTED inputs per task — existence
        # filtering below drops exactly the not-yet-created artifacts (the
        # report) that a producer task will generate, and wiring runs on
        # these requested paths, not the filtered ones.
        requested_inputs: Dict[str, List[str]] = {}
        seen: set = set()
        for spec in specs:
            owner = str(spec.get("owner") or "").strip()
            resolved_owner = self._resolve_plan_owner(owner)
            if resolved_owner is None:
                output.warnings = list(output.warnings) + [
                    f"plan ingest skipped '{spec.get('title')}': "
                    "task has no owner"
                ]
                continue
            if resolved_owner != owner:
                output.warnings = list(output.warnings) + [
                    f"owner '{owner}' mapped to '{resolved_owner}' "
                    f"for task '{spec.get('title')}'"
                ]
            spec["owner"] = resolved_owner
            # G9: agents receive project files through input_files; a plan
            # that omits them starves the executor of context (research/
            # review agents then refuse with "missing input data").
            requested_inputs[str(spec.get("id"))] = [
                str(item) for item in (spec.get("input_files") or [])
            ]
            validated = self._validated_input_files(spec.get("input_files"))
            if validated:
                spec["input_files"] = validated
            else:
                spec["input_files"] = list(self._plan_context_files(limit=4))
            # Only backward references can exist by append time; unknown or
            # forward dependencies are dropped instead of skipping the task.
            spec["dependencies"] = [
                dep for dep in list(spec.get("dependencies") or []) if dep in seen
            ]
            prepared.append(spec)
            seen.add(str(spec.get("id")))
        if not prepared:
            raise StateError("planning output contained no valid task specs")
        # G11: order review/synthesis tasks after the tasks whose outputs
        # they requested (models routinely leave those dependencies empty).
        prepared = self._wire_plan_producer_deps(prepared, requested_inputs)
        with self._state_lock:
            if existing:
                document = self.state.load_tasks_document()
                document["tasks"] = []
                self.state.save_tasks_document(document)
            output.data = dict(data)
            output.data["tasks"] = prepared
            created = self._ingest_output_tasks(output)
        if not created:
            raise StateError("no tasks could be ingested from planning output")
        self.state.refresh_ready_states()
        # GAP-HIGH-04: validate the graph that was just written, here, while the
        # plan that produced it is still the thing being worked on. Until now
        # the only caller of validate() was `init`, which cannot run again once
        # a graph exists, so an ingested graph's problems surfaced much later as
        # an unrelated runtime failure.
        #
        # Reported, not raised. append_task already rejects the defects it knows
        # (unknown owner/dependency, duplicate id, missing title, cycle) and each
        # rejection surfaces as an output warning, so a blocker reaching this
        # point is either pre-existing (a PROJECT.yaml typo, which is not the
        # plan's fault and must not stop planning) or one append_task cannot
        # express. Failing here would make a plan unbuildable because of an
        # unrelated project defect.
        self._report_graph_problems(output, "planning output")
        return created

    def _report_graph_problems(
        self, output: AgentOutput, source: str
    ) -> List[str]:
        """Run the deep graph validation and surface anything it finds.

        GAP-HIGH-04. Returns the problems and attaches them to the dispatch
        record as warnings, so the findings appear in the run log next to the
        output that caused them and in ``orchestrator status``.
        """
        problems = self.state.graph_blockers()
        if not problems:
            return []
        logger.warning(
            "%s left a structurally invalid task graph: %s",
            source,
            "; ".join(problems),
        )
        output.warnings = list(output.warnings) + [
            f"graph validation: {problem}" for problem in problems
        ]
        return problems

    # Keyword buckets checked in order; first hit wins. Weak models invent
    # domain owners ("qa_agent", "kernel_dev_agent") — map them instead of
    # dropping otherwise-good planning output.
    _PLAN_OWNER_KEYWORDS: Sequence[Tuple[frozenset, str]] = (
        (frozenset({"requirement", "requirements", "req"}), "requirements_agent"),
        (frozenset({"research", "investigation"}), "research_agent"),
        (frozenset({"architecture", "architect", "design"}), "architecture_agent"),
        (
            frozenset({"plan", "planning", "planner", "roadmap", "milestone"}),
            "planning_agent",
        ),
        (frozenset({"documentation", "docs", "doc"}), "documentation_agent"),
        (frozenset({"review", "reviewer"}), "review_agent"),
        (
            frozenset({"hardware", "pcb", "pinout", "schematic", "bom"}),
            "hardware_agent",
        ),
        (frozenset({"firmware", "driver", "hal", "boot", "partition"}), "firmware_agent"),
        (
            frozenset({
                "test", "tests", "qa", "verify", "verification", "validate",
                "validation", "audit", "quality",
            }),
            "test_agent",
        ),
        (
            frozenset({
                "software", "kernel", "fs", "filesystem", "app", "application",
                "frontend", "backend", "web", "api", "code", "dev", "develop",
                "implement", "integration", "cli",
            }),
            "software_agent",
        ),
    )

    @staticmethod
    def _resolve_plan_owner(owner: str) -> Optional[str]:
        """Map a model-chosen owner onto a registered agent id (G2).

        Exact matches win; otherwise the owner name is tokenized and
        matched against keyword buckets (``qa_agent`` -> ``test_agent``).
        Unknown but non-empty owners fall back to ``software_agent``; only
        a missing owner causes the spec to be dropped.
        """
        from .agents.base_agent import agent_names  # local import: avoid cycle

        key = str(owner or "").strip().lower()
        if not key:
            return None
        canonical = {name.lower(): name for name in agent_names()}
        if key in canonical:
            return canonical[key]
        tokens = set(re.findall(r"[a-z]+", key))
        tokens.discard("agent")
        for keywords, agent_id in MasterOrchestrator._PLAN_OWNER_KEYWORDS:
            if tokens & keywords:
                return agent_id
        return canonical.get("software_agent", "software_agent")

    @staticmethod
    def _normalize_plan_specs(raw: Sequence[Any]) -> List[Dict[str, Any]]:
        """Turn raw planning-agent task entries into ingestible specs (G2).

        Repairs the common LLM mistakes: missing ids (assigned ``TASK-NNN``
        in array order) and dependencies expressed as 0-based array indices,
        titles or stray ids. Only whitelisted task fields survive, so a
        model cannot inject ``status``/``execution`` state.
        """
        allowed = (
            "title",
            "owner",
            "priority",
            "dependencies",
            "expected_outputs",
            "acceptance_criteria",
            "notes",
            "input_files",
        )
        entries = [dict(item) for item in raw if isinstance(item, dict)]
        lookup: Dict[str, int] = {}
        for index, entry in enumerate(entries):
            model_id = str(entry.get("id") or "").strip().lower()
            entry["id"] = f"TASK-{index + 1:03d}"
            for key in list(entry):
                if key not in allowed and key != "id":
                    entry.pop(key, None)
            title = str(entry.get("title") or "").strip().lower()
            if model_id and model_id not in lookup:
                lookup[model_id] = index
            if entry["id"].lower() not in lookup:
                lookup[entry["id"].lower()] = index
            if title and title not in lookup:
                lookup[title] = index
        specs: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not str(entry.get("title") or "").strip():
                continue
            resolved: List[str] = []
            for dep in entry.get("dependencies") or []:
                dep_index: Optional[int] = None
                if isinstance(dep, bool):
                    continue
                if isinstance(dep, int):
                    dep_index = dep
                else:
                    dep_key = str(dep).strip()
                    if dep_key.isdigit():
                        dep_index = int(dep_key)
                    else:
                        dep_index = lookup.get(dep_key.lower())
                if dep_index is None or not (0 <= dep_index < len(entries)):
                    continue
                dep_id = f"TASK-{dep_index + 1:03d}"
                if dep_index != index and dep_id not in resolved:
                    resolved.append(dep_id)
            entry["dependencies"] = resolved
            for key in ("title", "owner", "priority"):
                if key in entry:
                    entry[key] = str(entry[key]).strip()
            entry["expected_outputs"] = [
                str(item) for item in (entry.get("expected_outputs") or [])
                if str(item).strip()
            ]
            entry["acceptance_criteria"] = [
                str(item) for item in (entry.get("acceptance_criteria") or [])
                if str(item).strip()
            ]
            entry["input_files"] = [
                str(item).strip()
                for item in (entry.get("input_files") or [])
                if str(item).strip()
            ][:12]
            if "notes" in entry:
                entry["notes"] = str(entry["notes"]).strip()[:600]
            # GAP-CRIT-05: strip runtime-state fields here too, not only at the
            # ingestion call. The whitelist above already excludes them for the
            # keys it names, but producer wiring re-reads the model's original
            # input_files and merges back into these entries, so this is the
            # point where a merged-back key could reappear.
            entry = strip_untrusted_task_fields(entry)
            specs.append(entry)
        return specs

    def _wire_plan_producer_deps(
        self,
        specs: List[Dict[str, Any]],
        requested: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        """G11: an input another planned task will produce implies ordering.

        Models routinely give the final review/synthesis task
        ``dependencies: []`` while listing the report it must review as an
        ``input_file`` — the reviewer then dispatches before the report
        exists (or silently reads a stale copy from a previous run).
        ``requested`` carries the model's *unfiltered* input paths (the
        existence filter in ``build_plan`` drops artifacts that no task has
        produced yet — precisely the ones wiring is about).

        For each requested artifact with a producer: the artifact is restored
        to the consumer's ``input_files`` (it will exist at execution time),
        and the producer edge is added unless it would create a cycle. The
        list is then topologically re-sorted because ``append_task`` rejects
        forward references. Raw model forward-deps are NOT honored here —
        they are still dropped by the caller's backward-only filter.
        """
        if len(specs) < 2:
            return specs
        producers: Dict[str, List[str]] = {}
        for spec in specs:
            for artifact in spec.get("expected_outputs") or []:
                producers.setdefault(str(artifact), []).append(str(spec.get("id")))

        deps_map: Dict[str, List[str]] = {
            str(spec.get("id")): [str(dep) for dep in (spec.get("dependencies") or [])]
            for spec in specs
        }

        def depends_on(start: str, target: str) -> bool:
            stack = [start]
            seen: set = set()
            while stack:
                current = stack.pop()
                if current == target:
                    return True
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(deps_map.get(current, []))
            return False

        for spec in specs:
            consumer = str(spec.get("id"))
            deps = deps_map[consumer]
            for artifact in requested.get(consumer, []):
                owners = [p for p in producers.get(str(artifact), []) if p != consumer]
                if not owners:
                    continue  # no planned producer: leave G9 filtering alone
                inputs = list(spec.get("input_files") or [])
                if artifact not in inputs and len(inputs) < 12:
                    inputs.append(str(artifact))
                    spec["input_files"] = inputs
                for producer in owners:
                    if producer in deps:
                        continue
                    if depends_on(producer, consumer):
                        continue  # edge would create a cycle
                    deps.append(producer)
            spec["dependencies"] = list(deps)
        return self._topo_sorted_plan(specs)

    @staticmethod
    def _topo_sorted_plan(specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Stable topological order; drops duplicate/unknown/self deps."""
        by_id = {str(spec.get("id")): spec for spec in specs}
        valid = set(by_id)
        deps: Dict[str, List[str]] = {}
        for spec in specs:
            sid = str(spec.get("id"))
            cleaned: List[str] = []
            for dep in spec.get("dependencies") or []:
                dep_id = str(dep)
                if dep_id in valid and dep_id != sid and dep_id not in cleaned:
                    cleaned.append(dep_id)
            spec["dependencies"] = cleaned
            deps[sid] = cleaned
        ordered: List[str] = []
        placed: set = set()
        remaining = [str(spec.get("id")) for spec in specs]
        while remaining:
            batch = [sid for sid in remaining if all(d in placed for d in deps[sid])]
            if not batch:  # cycle (should not happen): keep original order
                ordered.extend(remaining)
                break
            for sid in batch:
                ordered.append(sid)
                placed.add(sid)
                remaining.remove(sid)
        return [by_id[sid] for sid in ordered]

    def _plan_context_files(self, limit: int = 6) -> List[str]:
        """Existing docs fed to the planning agent as task context (G2)."""
        root = Path(self.project_path)
        state_files = {
            config.MEMORY_FILE,
            config.CURRENT_STATE_FILE,
            config.DECISIONS_FILE,
            config.RISKS_FILE,
            config.CHANGELOG_FILE,
            # Project agent-instruction files fight the planning contract
            # (they teach the model a different output shape).
            "AGENTS.md",
        }
        preferred = [
            "README.md",
            "PRD.md",
            "ROADMAP.md",
            "docs/README.md",
            "docs/PRD.md",
            "docs/ARCHITECTURE.md",
            "docs/REQUIREMENTS.md",
            "docs/PLAN.md",
        ]
        picked: List[str] = []
        for rel in preferred:
            if len(picked) >= limit:
                break
            if (root / rel).is_file():
                picked.append(rel)
        if len(picked) < limit:
            candidates = sorted(root.glob("*.md"))
            docs_dir = root / "docs"
            if docs_dir.is_dir():
                candidates += sorted(docs_dir.glob("*.md"))
            for path in candidates:
                if len(picked) >= limit:
                    break
                rel = str(path.relative_to(root))
                if rel in picked or rel in state_files:
                    continue
                picked.append(rel)
        return picked

    def _validated_input_files(self, files: Any, limit: int = 12) -> List[str]:
        """Keep only existing plain project files from a model-provided list.

        Rejects absolute paths, ``..`` traversal and non-files so a model
        cannot point agents outside the project (G9).
        """
        root = Path(self.project_path).resolve()
        validated: List[str] = []
        if not isinstance(files, (list, tuple)):
            return validated
        for item in files:
            rel = str(item).strip()
            if not rel or len(validated) >= limit:
                continue
            candidate = Path(rel)
            if candidate.is_absolute() or ".." in candidate.parts:
                continue
            resolved = (root / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file():
                validated.append(rel)
        return validated

    def _plan_source_index(self, limit: int = 60) -> str:
        """Compact list of real source paths for the planning contract.

        Lets the model cite genuine files (``kernel/kernel.c``) in task
        ``input_files`` — it never sees the tree otherwise, and research/audit
        tasks refuse when no source files reach their payload.
        """
        root = Path(self.project_path)
        skip = {
            "build", "build_host_test", "managed_components", "node_modules",
            ".git", ".pio", "dist", "venv", "__pycache__", "checkpoints",
        }
        found: List[str] = []
        for pattern in ("*.c", "*.h", "*.cpp", "*.ino", "*.py"):
            for path in sorted(root.rglob(pattern)):
                if any(part in skip for part in path.parts):
                    continue
                found.append(str(path.relative_to(root)))
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        if not found:
            return ""
        return (
            "Available source files (real paths for input_files): "
            + ", ".join(found)
        )

    def _checkpoint_phase_advance(self, previous_phase: str) -> Optional[str]:
        """Checkpoint when the derived lifecycle phase moves forward (A7)."""
        if not self.auto_checkpoint:
            return None
        current = str(
            (self.state.load_project().get("phase") or {}).get("current")
            or config.PHASE_REQUIREMENTS
        )
        if config.phase_index(current) <= config.phase_index(previous_phase):
            return None
        checkpoint_id = f"cp-phase-{current.lower()}"
        existing = {
            str(entry.get("id")) for entry in self.checkpoints.list_checkpoints()
        }
        if checkpoint_id in existing:
            return None
        return self.create_checkpoint(
            checkpoint_id, notes=f"Phase advanced to {current}"
        )

    def _record_loop_signals(
        self, task_id: str, task: Dict[str, Any], output: AgentOutput
    ) -> None:
        """Persist strategy-change, repeated-output and evidence signals.

        ``data.strategy_changed`` from an agent advances the alternative
        strategy counter; consecutive identical outputs increment
        ``repeated_output_count``; dispatches that add no new artifacts,
        documents or decisions increment ``evidence_stall_count`` (A4 —
        ``no_new_evidence``). All counters feed
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

        # A4: did this dispatch leave new evidence behind?
        data = output.data if isinstance(output.data, dict) else {}
        known_artifacts = {
            str(item) for item in (execution.get("known_artifacts") or [])
        }
        fresh_artifacts = {
            str(item) for item in (output.artifacts or []) if str(item) not in known_artifacts
        }
        documents = data.get("documents") if isinstance(data.get("documents"), dict) else {}
        proposed = isinstance(data.get("proposed_change"), dict)
        has_new_evidence = bool(fresh_artifacts) or bool(documents) or proposed
        try:
            previous_stall = int(execution.get("evidence_stall_count", 0) or 0)
        except (TypeError, ValueError):
            previous_stall = 0
        stall = 0 if has_new_evidence else previous_stall + 1

        set_values: Dict[str, Any] = {
            "last_output_hash": fingerprint,
            "repeated_output_count": repeats,
            "evidence_stall_count": stall,
            "known_artifacts": sorted(
                known_artifacts | {str(item) for item in (output.artifacts or [])}
            ),
        }
        self.state.update_task_execution(
            task_id,
            strategy_changed=strategy_changed,
            set_values=set_values,
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
        """Persist the measured context cost of one agent execution (B2).

        Tokens are accumulated into ``context.cumulative_tokens`` so the
        utilisation reflects the whole session; it only resets when a
        compaction event fires. The value written here is what
        :meth:`SupervisorAgent.check_context_usage` acts on (70%/85%).
        """
        data = output.data if isinstance(output.data, dict) else {}
        chars = getattr(agent, "last_payload_chars", 0)
        tokens = tokens_for_output(data, chars)
        try:
            self.state.add_context_tokens(tokens)
        except StateError:
            pass
        return utilization_for_output(data, chars)

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
        self._set_affected_waiting(affected)
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

    def _set_affected_waiting(self, affected: Sequence[str]) -> None:
        """Park non-terminal tasks affected by a pending change in WAITING (C1).

        REVIEW tasks keep their status (the review queue keys off it); the
        decision gate still refuses them at dispatch time.
        """
        for task_id in affected:
            task = self.state.find_task(str(task_id))
            if task is None:
                continue
            status = str(task.get("status"))
            if status in config.TERMINAL_TASK_STATUSES or status in (
                config.TASK_REVIEW,
                config.TASK_WAITING,
            ):
                continue
            try:
                self.state.update_task_status(str(task_id), config.TASK_WAITING)
            except StateError:
                continue

    def _clear_waiting_after_decision(self, affected: Sequence[str]) -> None:
        """Return WAITING tasks to READY/BLOCKED once the decision resolved (C1)."""
        tasks = self.state.load_tasks()
        by_id = {str(task.get("id")): task for task in tasks}
        for task_id in affected:
            task = by_id.get(str(task_id))
            if task is None or str(task.get("status")) != config.TASK_WAITING:
                continue
            target = (
                config.TASK_READY
                if self.state.dependencies_satisfied(task, tasks)
                else config.TASK_BLOCKED
            )
            try:
                self.state.update_task_status(str(task_id), target)
            except StateError:
                continue

    def approve_decision(self, dec_id: str, approved: bool = True) -> Dict[str, Any]:
        """Human approval/rejection of a recorded decision (opens the gate)."""
        record = self.state.resolve_decision(dec_id, approved=approved)
        self._clear_waiting_after_decision(record.get("affected_tasks") or [])
        verb = "approved" if approved else "rejected"
        self.state.append_current_state(f"{dec_id} {verb} by human; affected tasks un-gated.")
        return record

    def _evidence_problems(
        self,
        task: Dict[str, Any],
        output: Optional[AgentOutput],
        deploy_records: Optional[Sequence[DeployRecord]],
    ) -> List[str]:
        """GAP-CRIT-01: require ground truth for any claimed verification.

        This is the check that closes the fabrication hole. Three conditions
        force it, because all three are ways a task can appear verified without
        anything having run:

        1. the output declares ``data.deploy`` — the agent asked to execute;
        2. the agent's summary or artifacts *claim* an executed verification
           (matched by :func:`config.claims_execution`), e.g. "42/42 tests
           passed", "build succeeded", "flashed";
        3. the task's expected outputs look like build/test artifacts.

        When it fires, one of two things must be true:

        * a record with ``executed=True`` exists — then the real exit code is
          authoritative. A non-zero exit is ground truth too: the build
          genuinely failed, which is precisely the fact a fabricated report
          erases. It blocks DONE and routes to repair.
        * or the agent reports ``NOT RUN`` — an honest negative, which is
          allowed to proceed on its other merits. "I could not run this" is a
          legitimate outcome; inventing a pass is not.

        The asymmetry is deliberate: refusing an unevidenced claim is always
        correct, while forcing NOT RUN on a task that legitimately had nothing
        to run would be noise.
        """
        problems: List[str] = []
        records = list(deploy_records or [])

        # ``output`` is optional: definition_of_done(task) is a valid call (the
        # review path and several tests do it), in which case there is no claim
        # to check and only the expected-output shape can still trigger this.
        data: Dict[str, Any] = {}
        claims = False
        if output is not None:
            data = output.data if isinstance(output.data, dict) else {}
            claims = config.claims_execution(output.summary)
            for artifact in output.artifacts or []:
                if config.claims_execution(str(artifact)):
                    claims = True
                    break
        declares_deploy = bool(data.get("deploy"))
        test_like_outputs = _test_like_outputs(task)

        if not (declares_deploy or claims or test_like_outputs):
            return problems

        ground_truth = [record for record in records if record.executed]
        if not ground_truth:
            skipped = [record for record in records if record.status == STATUS_SKIPPED]
            if (
                records
                and len(skipped) == len(records)
                and not claims
                and not test_like_outputs
            ):
                # Every requested command was a redundant install already
                # satisfied in the target environment: there was genuinely
                # nothing to run, and nothing was invented. A claim of a
                # passing run is never backed by a skip, so `claims` still
                # falls through to the refusal below.
                return problems
            reported = str(data.get("test_status") or "").strip().upper()
            if reported == config.TEST_STATUS_NOT_RUN:
                # Honest negative. Nothing ran, and the agent said so.
                return problems
            if not claims and _reports_not_run(output):
                # Same honest negative, stated in prose instead of in the
                # field (a 12B model rarely moves it across on its own, and a
                # repair round that cannot either would fail a legitimate task).
                return problems
            if declares_deploy:
                reasons = "; ".join(
                    record.reason for record in records if record.reason
                ) or "the deployment channel produced no executed record"
                problems.append(
                    "task declared data.deploy but nothing was executed "
                    f"({reasons}); report test_status={config.TEST_STATUS_NOT_RUN} "
                    "or enable the deployment channel"
                )
            else:
                problems.append(
                    "output claims an executed verification with no ground-truth record "
                    f"(channel enabled: {self.deploy_runner.enabled}); report "
                    f"test_status={config.TEST_STATUS_NOT_RUN} or provide real execution output"
                )
            return problems

        failing = [record for record in ground_truth if record.exit_code != 0]
        if failing:
            detail = ", ".join(
                f"{record.command} exited {record.exit_code}"
                f"{_failure_excerpt(record)}"
                for record in failing
            )
            problems.append(f"executed verification failed: {detail}")

        mismatched = [
            record
            for record in ground_truth
            if record.expect_matched is False
        ]
        if mismatched:
            detail = ", ".join(
                f"{record.command} expected {record.declared_expect}"
                for record in mismatched
            )
            problems.append(
                f"declared expectation did not match reality: {detail}"
            )

        if claims and all(record.succeeded for record in ground_truth):
            # The claim is backed by real passing runs — nothing to add.
            return problems
        return problems

    def _ingest_fallback_criteria(self, task: Dict[str, Any]) -> None:
        """Persist default acceptance criteria when a task has none.

        DoD fallback ingestion: ``auto_plan``/startup specs/requirements
        seeding can omit ``acceptance_criteria``. The same defaults the DoD
        evaluates against are written back to TASKS.yaml here (locked phase),
        so a reviewer or a resumed session sees measurable criteria instead
        of an empty list. Best-effort: bookkeeping never fails a dispatch.
        """
        criteria = task.get("acceptance_criteria") or []
        if isinstance(criteria, list) and any(str(item).strip() for item in criteria):
            return
        fallback = _fallback_acceptance_criteria(task)
        task_id = str(task.get("id") or "")
        if not task_id or not fallback:
            return
        try:
            self.state.set_acceptance_criteria(task_id, fallback)
        except StateError:
            logger.debug(
                "could not persist fallback criteria for %s", task_id, exc_info=True
            )
            return
        logger.info(
            "ingested %d fallback acceptance criteria for %s",
            len(fallback),
            task_id,
        )

    def definition_of_done(
        self,
        task: Dict[str, Any],
        output: Optional[AgentOutput] = None,
        preexisting: Optional[Any] = None,
        deploy_records: Optional[Sequence[DeployRecord]] = None,
    ) -> List[str]:
        """Structural Definition-of-Done checks (empty list == satisfied).

        Mirrors the DoD criteria that can be verified without an LLM:
        measurable acceptance criteria exist (a missing list falls back to
        owner-scoped defaults instead of failing), every expected output was
        materialized, an independent review passed when required, and — when
        the task claims or requires an executed verification — ground truth
        exists for it (:meth:`_evidence_problems`).
        """
        problems: List[str] = []
        criteria = task.get("acceptance_criteria") or []
        if isinstance(criteria, list):
            usable = [item for item in criteria if str(item).strip()]
        else:
            usable = []
        if not usable:
            # DoD fallback ingestion: a task whose planning omitted
            # acceptance_criteria is evaluated against owner-scoped defaults
            # (persisted to TASKS.yaml by _ingest_fallback_criteria) instead
            # of being rejected with "no acceptance criteria defined".
            usable = _fallback_acceptance_criteria(task)
        logger.debug(
            "DoD for %s evaluates %d acceptance criterion(a)",
            task.get("id"),
            len(usable),
        )

        problems.extend(
            delivery_problems(
                self.state.project_path, task, output, preexisting=preexisting
            )
        )

        # B3: requirement traceability — every declared REQ id must exist.
        declared = task.get("requirement_ids")
        if isinstance(declared, list) and declared:
            docs_dir = self.state.project_path / "docs"
            req_doc = docs_dir / "REQUIREMENTS.md"
            body = ""
            if req_doc.exists():
                try:
                    body = req_doc.read_text(encoding="utf-8")
                except OSError:
                    body = ""
            if not body.strip():
                problems.append(
                    "requirement_ids declared but docs/REQUIREMENTS.md is missing"
                )
            else:
                for raw_req in declared:
                    req_id = str(raw_req).strip()
                    if req_id and req_id not in body:
                        problems.append(f"requirement not found in REQUIREMENTS.md: {req_id}")

        # B3: acceptance evidence — a reported failing check blocks DONE.
        results = task.get("acceptance_results")
        if results is None and output is not None:
            data = output.data if isinstance(output.data, dict) else {}
            results = data.get("acceptance_results")
        if isinstance(results, list):
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("status", "")).upper() in ("FAIL", "FAILED"):
                    name = str(entry.get("name") or entry.get("criterion") or "criterion")
                    problems.append(f"acceptance check failed: {name}")

        review = task.get("review") or {}
        if review.get("required") and review.get("status") not in ("PASS", "PASS WITH ACTIONS"):
            problems.append("independent review not passed")

        problems.extend(
            self._evidence_problems(task, output, deploy_records)
        )
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
        if outcome is not None:
            outcome = str(outcome).strip().upper()
        else:
            outcome = "PASS" if passed else "FAIL"
        if review.get("required") and outcome == "PASS" and _recorded_dod_problems(task):
            # GAP-CRIT-04: a review that passed while known DoD problems stood
            # must not reach DONE. The reviewer is told about them; if it still
            # returns PASS, the structural failure wins, because a human
            # signature does not create a file that was never written.
            outstanding = _recorded_dod_problems(task)
            outcome = "FAIL"
            note = ((note + "; ") if note else "") + (
                "review PASSed but the Definition of Done was unmet: "
                + "; ".join(outstanding)
            )
        if outcome == "PASS":
            status = config.TASK_DONE
        elif outcome == "PASS WITH ACTIONS":
            status = config.TASK_DONE_WITH_LIMITATION
        elif outcome == "FAIL":
            status = config.TASK_FAILED
        else:
            raise OrchestratorError(f"Unknown review outcome '{outcome}'")
        review["status"] = outcome
        prospective = dict(task)
        prospective["review"] = review
        # GAP-CRIT-04: the recorded problems are re-checked here too, so a
        # review can never complete a task the DoD rejected — whether the
        # problems came from this attempt or a previous one.
        dod_problems = _review_forwardable_problems(
            self.definition_of_done(prospective)
        ) or _recorded_dod_problems(task)
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
                entry.pop("pending_dod_problems", None)
                if note:
                    entry["notes"] = note
                break
        self.state.save_tasks_document(document)
        self.state.append_current_state(
            f"{task_id} review {outcome.lower().replace(' ', '_')}"
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

    def _persist_dod_problems(self, task_id: str, problems: Sequence[str]) -> None:
        """Record DoD problems on the task so a later review can recover them.

        Written directly to the document because this is review-flow metadata,
        not agent-supplied state: it must not go through
        :meth:`StateManager.append_task`, which deliberately strips anything
        that looks like an input.
        """
        with self._state_lock:
            document = self.state.load_tasks_document()
            for entry in document.get("tasks") or []:
                if entry.get("id") == task_id:
                    entry["pending_dod_problems"] = [str(item) for item in problems]
                    break
            self.state.save_tasks_document(document)

    def _clear_dod_problems(self, task_id: str) -> None:
        """Drop stale DoD problems once a task passes the checks."""
        with self._state_lock:
            document = self.state.load_tasks_document()
            changed = False
            for entry in document.get("tasks") or []:
                if entry.get("id") == task_id and "pending_dod_problems" in entry:
                    entry.pop("pending_dod_problems")
                    changed = True
                    break
            if changed:
                self.state.save_tasks_document(document)

    def dispatch_review(
        self,
        task_id: str,
        dod_problems: Optional[Sequence[str]] = None,
    ) -> TaskRunResult:
        """Run the independent review for one REVIEW task.

        The reviewer is always the registered ``review_agent`` — never the
        task's own owner — so no artifact approves itself.

        GAP-CRIT-04. ``dod_problems`` carries the structural checks that already
        failed for this task, so the reviewer is told *what is structurally
        wrong* rather than having to rediscover it. Without it the review ran
        against a task the DoD had already rejected and could return ``PASS`` on
        work that was never delivered — the reviewer approves the artifact, the
        artifact does not exist.

        They are passed explicitly rather than re-derived, so the review sees
        the *same* findings the DoD recorded and cannot disagree with them by
        re-evaluating against a task that has since changed.
        """
        task = self.get_task(task_id)
        previous_status = str(task.get("status"))
        if previous_status != config.TASK_REVIEW:
            raise OrchestratorError(
                f"Task '{task_id}' is {previous_status}, not REVIEW; nothing to review"
            )

        pending_problems = [str(item) for item in (dod_problems or []) if str(item).strip()]

        loop = self.supervisor.should_block_dispatch(task_id)
        if loop is not None:
            self._persist_last_loop(task_id, loop)
            raise LoopLimitExceededError(
                f"Task '{task_id}' exceeded the {loop.kind} limit "
                f"({loop.count}/{loop.threshold}): {loop.detail}",
                loop=loop,
            )

        self._enforce_decision_gate(task_id)

        agent = self.resolve_agent("review_agent")
        logger.info("reviewing %s with %s", task_id, agent.AGENT_ID)
        self.state.set_review_status(task_id, "IN_PROGRESS")
        review_task = self.get_task(task_id)
        if pending_problems:
            # Surface on the task so it reaches the payload's task block, and in
            # a dedicated field the reviewer reads as a pre-verdict.
            review_task["pending_dod_problems"] = list(pending_problems)
        output = agent.run(review_task, materialize=False)
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
        previous_phase = str(
            (self.state.load_project().get("phase") or {}).get("current")
            or config.PHASE_REQUIREMENTS
        )
        self.state.recompute_derived_state()
        phase_checkpoint = self._checkpoint_phase_advance(previous_phase)
        self._record_fingerprint()
        # GAP-HIGH-02: same non-short-circuiting evaluation as the dispatch
        # path. A review completion is exactly when `cp-milestone-complete`
        # fires, so a compaction in the same instant must not swallow it.
        auto_checkpoint_id = self._maybe_auto_checkpoint(self.get_task(task_id))
        milestone_checkpoint_id = self.check_milestones()
        checkpoint_id = (
            auto_checkpoint_id
            or milestone_checkpoint_id
            or phase_checkpoint
        )
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
        """Dispatch every pending independent review; never raises.

        GAP-CRIT-04: the DoD problems recorded on the task are forwarded, so a
        review resumed in a later cycle still knows what the DoD rejected — the
        note is not enough, because the claiming summary is what it contains.
        """
        results: List[TaskRunResult] = []
        for task in self.pending_review_tasks():
            task_id = str(task.get("id"))
            try:
                results.append(
                    self.dispatch_review(
                        task_id,
                        dod_problems=_recorded_dod_problems(task),
                    )
                )
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
            result = self.dispatch_review(
                task_id,
                dod_problems=_recorded_dod_problems(self.get_task(task_id)),
            )
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
        """Checkpoint + memory note + memory fold at the compaction threshold.

        GAP-CRIT-06: the id is allocated by the checkpoint index under its own
        lock, replacing a per-instance counter plus a list-then-check probe. The
        old shape raced two processes into the same ``cp-auto-NNN`` (and, since
        the probe read a stale index, into an already-existing directory).
        """
        checkpoint_id = self.checkpoints._next_checkpoint_id("cp-auto")
        try:
            self._checkpoint_counter = int(str(checkpoint_id).rsplit("-", 1)[-1])
        except (TypeError, ValueError):
            self._checkpoint_counter += 1
        notes = f"Automatic checkpoint: {reason}"
        self.state.record_checkpoint(checkpoint_id)
        self.checkpoints.create_checkpoint(checkpoint_id, notes=notes)
        # GAP-MED-03: record the compaction BEFORE the fold. The fold rewrites
        # PROJECT_MEMORY.md, so a memory-section note about the event can be
        # partially discarded by the operation it describes. CHANGELOG.md is
        # append-only and the index row makes the event queryable.
        before = self.state.get_context_utilization()
        self.checkpoints.record_compaction_event(
            checkpoint_id,
            reason=reason,
            detail={
                "utilization_before_percent": round(float(before), 1),
                "compaction_threshold_percent": float(
                    self.state.get_compaction_threshold()
                ),
            },
        )
        self.state.append_current_state(
            f"Context compaction -> {checkpoint_id} (reason: {reason}; "
            f"utilization {before:.1f}% reset to a fresh budget)"
        )
        config.emit_telemetry(
            config.TELEMETRY_EVENT_COMPACTION,
            level="INFO",
            checkpoint_id=checkpoint_id,
            reason=reason,
            utilization_before_percent=round(float(before), 1),
        )
        self.state.append_changelog(
            f"Context compaction: {checkpoint_id} saved (reason: {reason}); "
            f"utilization {before:.1f}% reset to a fresh budget"
        )
        self.state.append_memory_section(
            "Context Compaction",
            (
                f"Checkpoint **{checkpoint_id}** saved at {utc_now_iso()}.\n\n"
                f"Reason: {reason}.\n\n"
                "Working context was discarded; resume from PROJECT_MEMORY.md and TASKS.yaml."
            ),
        )
        # B1: fold an oversized PROJECT_MEMORY.md; B2: fresh token budget.
        folded = self.state.compact_memory()
        self.state.reset_context_tokens()
        if folded:
            self.state.append_current_state(
                "PROJECT_MEMORY.md folded by compaction (head + tail kept)."
            )
        self.state.append_current_state(f"Context compacted; checkpoint {checkpoint_id} saved.")
        self.state.append_changelog(f"Auto checkpoint {checkpoint_id} ({reason})")
        return checkpoint_id

    def create_checkpoint(
        self, checkpoint_id: str, notes: str = "", *, overwrite: bool = True
    ) -> str:
        """Snapshot the project under ``checkpoint_id``.

        An existing id is **replaced** by default (``overwrite=True``): these
        are bookkeeping snapshots — ``cp-risk-*`` comes back whenever a fresh
        plan rewrites ``RISKS.md`` while the old directory is still on disk —
        and a name collision must never fail the task that triggered it. Pass
        ``overwrite=False`` to keep the strict "id must be new" behaviour.
        """
        self.state.record_checkpoint(checkpoint_id)
        self.checkpoints.create_checkpoint(
            checkpoint_id, notes=notes, overwrite=overwrite
        )
        self.state.append_changelog(f"Checkpoint {checkpoint_id} saved. {notes}".strip())
        logger.info("checkpoint '%s' saved", checkpoint_id)
        return checkpoint_id

    def _safe_auto_checkpoint(self, maker: Callable[[], Optional[str]], label: str) -> Optional[str]:
        """Run an auto-checkpoint trigger; a snapshot must never fail a task.

        Every caller is in the dispatch finalize path: the task's work is
        already done and persisted, so an I/O error while snapshotting may
        only cost the snapshot — not the dispatch, and not the wave.
        """
        try:
            return maker()
        except Exception as exc:  # noqa: BLE001 - bookkeeping is best-effort
            logger.warning("%s checkpoint skipped: %s", label, exc)
            return None

    def resume_from_checkpoint(self, checkpoint_id: str, isolated: bool = False) -> Path:
        if isolated:
            target = self.project_path / f".restore-{checkpoint_id}"
            restored = self.checkpoints.restore_checkpoint(checkpoint_id, target_path=target)
            return restored
        restored = self.checkpoints.restore_checkpoint(checkpoint_id)
        self.state = StateManager(self.project_path)
        self.supervisor = SupervisorAgent(state_manager=self.state)
        # Both registries are cleared together: leaving a pinned class behind
        # with no instance would let resolve_agent() fabricate an agent the
        # caller never registered.
        self.registered_agents = {}
        self._pinned_agents = {}
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
