"""Global configuration for the agentic-ai orchestrator runtime.

This module is the single source of truth for:

* system keys (logical name -> environment variable name),
* loop / retry thresholds enforced by the Supervisor,
* context compaction benchmarks,
* state file names and directory layout,
* task and health state vocabularies.

Nothing in this package should hard-code a threshold; every limit is read
from the constants and dataclasses defined here so that behaviour can be
tuned in exactly one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# System keys
# ---------------------------------------------------------------------------

class MissingSystemKeyError(RuntimeError):
    """Raised when a required system key is requested but not configured."""

    def __init__(self, key: str, env_var: str) -> None:
        super().__init__(
            f"System key '{key}' is not configured. Set the environment "
            f"variable {env_var} or provide it through configuration."
        )
        self.key = key
        self.env_var = env_var


@dataclass(frozen=True)
class SystemKey:
    """A single external credential/endpoint tracked by the orchestrator."""

    key: str
    env_var: str
    description: str
    required: bool = False

    def resolve(self, overrides: Optional[Mapping[str, str]] = None) -> Optional[str]:
        if overrides is not None and self.key in overrides:
            value = overrides.get(self.key)
            if value:
                return value
        env_value = os.environ.get(self.env_var)
        if env_value:
            return env_value
        if self.required:
            raise MissingSystemKeyError(self.key, self.env_var)
        return None


@dataclass(frozen=True)
class SystemKeys:
    """Registry of every external key the runtime can consume."""

    keys: Dict[str, SystemKey] = field(default_factory=dict)

    def register(self, key: SystemKey) -> "SystemKeys":
        self.keys[key.key] = key
        return self

    def get(self, key: str) -> SystemKey:
        if key not in self.keys:
            raise MissingSystemKeyError(key, key.upper())
        return self.keys[key]

    def resolve(self, key: str, overrides: Optional[Mapping[str, str]] = None) -> Optional[str]:
        return self.get(key).resolve(overrides)

    def names(self) -> List[str]:
        return sorted(self.keys)


def build_system_keys() -> SystemKeys:
    registry = SystemKeys()
    registry.register(
        SystemKey(
            key="anthropic_api_key",
            env_var="ANTHROPIC_API_KEY",
            description="API key used by specialist agents for model calls.",
            required=False,
        )
    )
    registry.register(
        SystemKey(
            key="ollama_base_url",
            env_var="OLLAMA_BASE_URL",
            description="Base URL of the local Ollama server used for chat agents.",
            required=False,
        )
    )
    registry.register(
        SystemKey(
            key="openrouter_api_key",
            env_var="OPENROUTER_API_KEY",
            description="API key for the OpenRouter fallback provider.",
            required=False,
        )
    )
    registry.register(
        SystemKey(
            key="checkpoint_signing_key",
            env_var="CHECKPOINT_SIGNING_KEY",
            description="Optional HMAC key used to sign checkpoint metadata.",
            required=False,
        )
    )
    return registry


SYSTEM_KEYS: SystemKeys = build_system_keys()


# ---------------------------------------------------------------------------
# .env file loading (CLI entry point only)
# ---------------------------------------------------------------------------

def load_env_file(path: Optional[os.PathLike] = None) -> int:
    """Load ``KEY=VALUE`` lines from a .env file into ``os.environ``.

    Uses setdefault semantics: variables already present in the environment
    always win. Blank lines, ``#`` comments and an optional ``export``
    prefix are supported; surrounding quotes on values are stripped.
    A missing file is not an error (returns 0).

    Returns the number of variables that were set.
    """
    env_path = Path(path) if path is not None else Path.cwd() / ".env"
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    loaded = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


def maybe_load_env_file() -> int:
    """Load ``./.env`` unless the pytest harness is currently running.

    Tests must stay offline-safe, so the CLI entry point skips .env
    injection whenever ``PYTEST_CURRENT_TEST`` is set (it propagates into
    subprocesses spawned by the suite as well).
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 0
    return load_env_file()


# ---------------------------------------------------------------------------
# Loop thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopThresholds:
    """Limits enforced by the Supervisor when agents repeat themselves."""

    same_strategy_max_attempts: int = 3
    no_progress_max_cycles: int = 5
    max_alternative_strategies: int = 2
    max_retries: int = 3
    identical_output_max_repeats: int = 3
    evidence_stall_max: int = 3

    def as_dict(self) -> Dict[str, int]:
        return {
            "same_strategy_max_attempts": self.same_strategy_max_attempts,
            "no_progress_max_cycles": self.no_progress_max_cycles,
            "max_alternative_strategies": self.max_alternative_strategies,
            "max_retries": self.max_retries,
            "identical_output_max_repeats": self.identical_output_max_repeats,
            "evidence_stall_max": self.evidence_stall_max,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, int]]) -> "LoopThresholds":
        if not data:
            return cls()
        return cls(
            same_strategy_max_attempts=int(
                data.get("same_strategy_max_attempts", cls.same_strategy_max_attempts)
            ),
            no_progress_max_cycles=int(
                data.get("no_progress_max_cycles", cls.no_progress_max_cycles)
            ),
            max_alternative_strategies=int(
                data.get("max_alternative_strategies", cls.max_alternative_strategies)
            ),
            max_retries=int(data.get("max_retries", cls.max_retries)),
            identical_output_max_repeats=int(
                data.get("identical_output_max_repeats", cls.identical_output_max_repeats)
            ),
            evidence_stall_max=int(
                data.get("evidence_stall_max", cls.evidence_stall_max)
            ),
        )


LOOP_THRESHOLDS: LoopThresholds = LoopThresholds()


# ---------------------------------------------------------------------------
# Context compaction benchmarks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionThresholds:
    """Context utilisation bands, in percent.

    Healthy    : utilisation < warning_percent
    Warning    : warning_percent <= utilisation < compaction_percent
    Compaction : compaction_percent <= utilisation < critical_percent
    Critical   : utilisation >= critical_percent
    """

    warning_percent: int = 60
    compaction_percent: int = 70
    compaction_high_percent: int = 80
    critical_percent: int = 85

    def classify(self, utilization_percent: float) -> str:
        if utilization_percent >= self.critical_percent:
            return "CRITICAL"
        if utilization_percent >= self.compaction_high_percent:
            return "COMPACT_HIGH"
        if utilization_percent >= self.compaction_percent:
            return "COMPACT"
        if utilization_percent >= self.warning_percent:
            return "WARNING"
        return "HEALTHY"

    def compaction_required(self, utilization_percent: float) -> bool:
        return utilization_percent >= self.compaction_percent

    def critical(self, utilization_percent: float) -> bool:
        return utilization_percent >= self.critical_percent

    def as_dict(self) -> Dict[str, int]:
        return {
            "warning_percent": self.warning_percent,
            "compaction_percent": self.compaction_percent,
            "compaction_high_percent": self.compaction_high_percent,
            "critical_percent": self.critical_percent,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, int]]) -> "CompactionThresholds":
        if not data:
            return cls()
        return cls(
            warning_percent=int(data.get("warning_percent", cls.warning_percent)),
            compaction_percent=int(data.get("compaction_percent", cls.compaction_percent)),
            compaction_high_percent=int(
                data.get("compaction_high_percent", cls.compaction_high_percent)
            ),
            critical_percent=int(data.get("critical_percent", cls.critical_percent)),
        )


COMPACTION_THRESHOLDS: CompactionThresholds = CompactionThresholds()

# When PROJECT_MEMORY.md exceeds this size, context compaction collapses the
# middle into a summary marker (framework/00:80-90 "discard irrelevant
# working context"). ~15k tokens.
MEMORY_COMPACT_MAX_CHARS: int = 60000


# ---------------------------------------------------------------------------
# State file layout
# ---------------------------------------------------------------------------

PROJECT_FILE = "PROJECT.yaml"
TASKS_FILE = "TASKS.yaml"
MEMORY_FILE = "PROJECT_MEMORY.md"
CURRENT_STATE_FILE = "CURRENT_STATE.md"
DECISIONS_FILE = "DECISIONS.md"
RISKS_FILE = "RISKS.md"
CHANGELOG_FILE = "CHANGELOG.md"

STATE_FILES: Tuple[str, ...] = (
    PROJECT_FILE,
    TASKS_FILE,
    MEMORY_FILE,
    CURRENT_STATE_FILE,
    DECISIONS_FILE,
    RISKS_FILE,
    CHANGELOG_FILE,
)

YAML_STATE_FILES: Tuple[str, ...] = (PROJECT_FILE, TASKS_FILE)
MARKDOWN_STATE_FILES: Tuple[str, ...] = (
    MEMORY_FILE,
    CURRENT_STATE_FILE,
    DECISIONS_FILE,
    RISKS_FILE,
    CHANGELOG_FILE,
)

DEFAULT_CHECKPOINTS_DIR = "checkpoints"


def default_checkpoints_dir() -> str:
    """Root directory holding per-project checkpoint folders.

    Overridable via ``ORCHESTRATOR_CHECKPOINTS_DIR`` so tests (and sandboxes)
    never write checkpoints into the working tree.
    """
    raw = os.environ.get("ORCHESTRATOR_CHECKPOINTS_DIR")
    if raw and str(raw).strip():
        return str(raw).strip()
    return DEFAULT_CHECKPOINTS_DIR
CHECKPOINT_INDEX_FILE = "index.json"
CHECKPOINT_METADATA_FILE = "metadata.json"
CHECKPOINT_BACKUP_DIR = "backups"

DEFAULT_PROJECTS_DIR = "projects"
DEFAULT_ORCHESTRATOR_DIR = "orchestrator"
DEFAULT_AGENTS_DIR = "agents"
DEFAULT_LOGS_DIR = "logs"

FRAMEWORK_SPECS_DIR = "framework"


# ---------------------------------------------------------------------------
# Task vocabularies
# ---------------------------------------------------------------------------

TASK_TODO = "TODO"
TASK_READY = "READY"
TASK_IN_PROGRESS = "IN_PROGRESS"
TASK_WAITING = "WAITING"
TASK_BLOCKED = "BLOCKED"
TASK_REVIEW = "REVIEW"
TASK_FAILED = "FAILED"
TASK_DONE = "DONE"
TASK_DONE_WITH_LIMITATION = "DONE WITH ACCEPTED LIMITATION"
TASK_CANCELLED = "CANCELLED"

TASK_STATUSES: Tuple[str, ...] = (
    TASK_TODO,
    TASK_READY,
    TASK_IN_PROGRESS,
    TASK_WAITING,
    TASK_BLOCKED,
    TASK_REVIEW,
    TASK_FAILED,
    TASK_DONE,
    TASK_DONE_WITH_LIMITATION,
    TASK_CANCELLED,
)

# Statuses that never need to be dispatched again.
TERMINAL_TASK_STATUSES: Tuple[str, ...] = (TASK_DONE, TASK_DONE_WITH_LIMITATION, TASK_CANCELLED)

# Statuses whose dependencies are satisfied from the graph point of view.
SATISFIED_DEPENDENCY_STATUSES: Tuple[str, ...] = (TASK_DONE, TASK_DONE_WITH_LIMITATION, TASK_CANCELLED)

# ---------------------------------------------------------------------------
# Project lifecycle phases (framework/00_MASTER_ORCHESTRATOR.md:203-229)
# ---------------------------------------------------------------------------

PHASE_REQUIREMENTS = "REQUIREMENTS"
PHASE_RESEARCH = "RESEARCH"
PHASE_ARCHITECTURE = "ARCHITECTURE"
PHASE_PLANNING = "PLANNING"
PHASE_IMPLEMENTATION = "IMPLEMENTATION"
PHASE_INTEGRATION = "INTEGRATION"
PHASE_TESTING = "TESTING"
PHASE_VALIDATION = "VALIDATION"
PHASE_RELEASE = "RELEASE"
PHASE_MAINTENANCE = "MAINTENANCE"

PHASES: Tuple[str, ...] = (
    PHASE_REQUIREMENTS,
    PHASE_RESEARCH,
    PHASE_ARCHITECTURE,
    PHASE_PLANNING,
    PHASE_IMPLEMENTATION,
    PHASE_INTEGRATION,
    PHASE_TESTING,
    PHASE_VALIDATION,
    PHASE_RELEASE,
    PHASE_MAINTENANCE,
)

# Which agent owners gate each phase. Phases with no owner-matched tasks are
# auto-skipped; MAINTENANCE is only ever entered manually.
PHASE_OWNERS: Dict[str, Tuple[str, ...]] = {
    PHASE_REQUIREMENTS: ("requirements_agent",),
    PHASE_RESEARCH: ("research_agent",),
    PHASE_ARCHITECTURE: ("architecture_agent",),
    PHASE_PLANNING: ("planning_agent",),
    PHASE_IMPLEMENTATION: ("hardware_agent", "software_agent", "firmware_agent"),
    PHASE_INTEGRATION: (),
    PHASE_TESTING: ("test_agent",),
    PHASE_VALIDATION: (),
    PHASE_RELEASE: (),
    PHASE_MAINTENANCE: (),
}


def phase_index(phase: str) -> int:
    """Index of ``phase`` in :data:`PHASES` (0 for unknown values)."""
    try:
        return PHASES.index(str(phase))
    except ValueError:
        return 0

PRIORITY_ORDER: Dict[str, int] = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
}

DEFAULT_PRIORITY_RANK = 999


# ---------------------------------------------------------------------------
# Health vocabularies
# ---------------------------------------------------------------------------

HEALTH_HEALTHY = "HEALTHY"
HEALTH_WARNING = "WARNING"
HEALTH_STALLED = "STALLED"
HEALTH_BLOCKED = "BLOCKED"
HEALTH_RECOVERY = "RECOVERY"
HEALTH_HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"

HEALTH_STATES: Tuple[str, ...] = (
    HEALTH_HEALTHY,
    HEALTH_WARNING,
    HEALTH_STALLED,
    HEALTH_BLOCKED,
    HEALTH_RECOVERY,
    HEALTH_HUMAN_DECISION_REQUIRED,
)

# Loop finding kinds produced by the Supervisor.
LOOP_KIND_SAME_STRATEGY = "same_strategy"
LOOP_KIND_NO_PROGRESS = "no_progress"
LOOP_KIND_ALTERNATIVES_EXHAUSTED = "alternatives_exhausted"
LOOP_KIND_STATE_OSCILLATION = "state_oscillation"
LOOP_KIND_REPEATED_OUTPUT = "repeated_output"
LOOP_KIND_NO_NEW_EVIDENCE = "no_new_evidence"
LOOP_KINDS: Tuple[str, ...] = (
    LOOP_KIND_SAME_STRATEGY,
    LOOP_KIND_NO_PROGRESS,
    LOOP_KIND_ALTERNATIVES_EXHAUSTED,
    LOOP_KIND_STATE_OSCILLATION,
    LOOP_KIND_REPEATED_OUTPUT,
    LOOP_KIND_NO_NEW_EVIDENCE,
)

# Allowed result statuses an agent may return.
AGENT_STATUS_COMPLETED = "completed"
AGENT_STATUS_FAILED = "failed"
AGENT_STATUS_BLOCKED = "blocked"
AGENT_STATUSES: Tuple[str, ...] = (
    AGENT_STATUS_COMPLETED,
    AGENT_STATUS_FAILED,
    AGENT_STATUS_BLOCKED,
)

# Requirement vocabularies (see framework/02_REQUIREMENTS_AGENT.md).
REQUIREMENT_TYPES: Tuple[str, ...] = (
    "FUNCTIONAL",
    "NON-FUNCTIONAL",
    "CONSTRAINT",
    "INTERFACE",
)
REQUIREMENT_PRIORITIES: Tuple[str, ...] = ("MUST", "SHOULD", "COULD")
REQUIREMENT_STATUSES: Tuple[str, ...] = ("APPROVED", "PROPOSED", "REJECTED")

# Vague words that make an acceptance criterion unmeasurable.
VAGUE_ACCEPTANCE_TERMS: Tuple[str, ...] = (
    "fast",
    "quick",
    "good",
    "nice",
    "easy",
    "user friendly",
    "intuitive",
    "robust",
    "scalable",
    "appropriate",
)


# ---------------------------------------------------------------------------
# Misc runtime settings
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
DEFAULT_LOG_LEVEL = "INFO"

# A cycle with no status fingerprint change counts as "no progress".
MAX_CONSECUTIVE_IDLE_CYCLES = LOOP_THRESHOLDS.no_progress_max_cycles

# Checksum algorithm used for checkpoint integrity verification.
CHECKSUM_ALGORITHM = "sha256"

# Characters of an agent error retained inside TASKS.yaml.
ERROR_SNIPPET_LENGTH = 500


def loop_thresholds(overrides: Optional[Mapping[str, int]] = None) -> LoopThresholds:
    """Return the active loop thresholds, optionally merged with overrides."""
    if not overrides:
        return LOOP_THRESHOLDS
    merged = LOOP_THRESHOLDS.as_dict()
    merged.update({key: int(value) for key, value in overrides.items()})
    return LoopThresholds.from_dict(merged)


def compaction_thresholds(overrides: Optional[Mapping[str, int]] = None) -> CompactionThresholds:
    """Return the active compaction benchmarks, optionally merged with overrides."""
    if not overrides:
        return COMPACTION_THRESHOLDS
    merged = COMPACTION_THRESHOLDS.as_dict()
    merged.update({key: int(value) for key, value in overrides.items()})
    return CompactionThresholds.from_dict(merged)


def is_terminal_status(status: str) -> bool:
    return status in TERMINAL_TASK_STATUSES


def priority_rank(priority: Optional[str]) -> int:
    if not priority:
        return DEFAULT_PRIORITY_RANK
    return PRIORITY_ORDER.get(priority.upper(), DEFAULT_PRIORITY_RANK)


__all__ = [
    "MissingSystemKeyError",
    "SystemKey",
    "SystemKeys",
    "SYSTEM_KEYS",
    "build_system_keys",
    "LoopThresholds",
    "LOOP_THRESHOLDS",
    "loop_thresholds",
    "CompactionThresholds",
    "COMPACTION_THRESHOLDS",
    "compaction_thresholds",
    "PROJECT_FILE",
    "TASKS_FILE",
    "MEMORY_FILE",
    "CURRENT_STATE_FILE",
    "DECISIONS_FILE",
    "RISKS_FILE",
    "CHANGELOG_FILE",
    "STATE_FILES",
    "YAML_STATE_FILES",
    "MARKDOWN_STATE_FILES",
    "DEFAULT_CHECKPOINTS_DIR",
    "default_checkpoints_dir",
    "CHECKPOINT_INDEX_FILE",
    "CHECKPOINT_METADATA_FILE",
    "CHECKPOINT_BACKUP_DIR",
    "DEFAULT_PROJECTS_DIR",
    "FRAMEWORK_SPECS_DIR",
    "TASK_STATUSES",
    "TASK_TODO",
    "TASK_READY",
    "TASK_IN_PROGRESS",
    "TASK_WAITING",
    "TASK_BLOCKED",
    "TASK_REVIEW",
    "TASK_FAILED",
    "TASK_DONE",
    "TASK_DONE_WITH_LIMITATION",
    "TASK_CANCELLED",
    "TERMINAL_TASK_STATUSES",
    "SATISFIED_DEPENDENCY_STATUSES",
    "PHASES",
    "PHASE_OWNERS",
    "phase_index",
    "PHASE_REQUIREMENTS",
    "PHASE_RESEARCH",
    "PHASE_ARCHITECTURE",
    "PHASE_PLANNING",
    "PHASE_IMPLEMENTATION",
    "PHASE_INTEGRATION",
    "PHASE_TESTING",
    "PHASE_VALIDATION",
    "PHASE_RELEASE",
    "PHASE_MAINTENANCE",
    "MEMORY_COMPACT_MAX_CHARS",
    "PRIORITY_ORDER",
    "HEALTH_STATES",
    "HEALTH_HEALTHY",
    "HEALTH_WARNING",
    "HEALTH_STALLED",
    "HEALTH_BLOCKED",
    "HEALTH_RECOVERY",
    "HEALTH_HUMAN_DECISION_REQUIRED",
    "LOOP_KINDS",
    "LOOP_KIND_SAME_STRATEGY",
    "LOOP_KIND_NO_PROGRESS",
    "LOOP_KIND_ALTERNATIVES_EXHAUSTED",
    "LOOP_KIND_STATE_OSCILLATION",
    "LOOP_KIND_REPEATED_OUTPUT",
    "LOOP_KIND_NO_NEW_EVIDENCE",
    "AGENT_STATUSES",
    "AGENT_STATUS_COMPLETED",
    "AGENT_STATUS_FAILED",
    "AGENT_STATUS_BLOCKED",
    "REQUIREMENT_TYPES",
    "REQUIREMENT_PRIORITIES",
    "REQUIREMENT_STATUSES",
    "VAGUE_ACCEPTANCE_TERMS",
    "ERROR_SNIPPET_LENGTH",
    "CHECKSUM_ALGORITHM",
    "is_terminal_status",
    "priority_rank",
]
