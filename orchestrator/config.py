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

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


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
            description=(
                "Optional HMAC key used to sign checkpoint metadata. Setting it ENABLES signing "
                "(every new checkpoint is written with signed=true plus a signature); it does not "
                "merely enable a check."
            ),
            required=False,
        )
    )
    registry.register(
        SystemKey(
            key="checkpoint_signing_key_id",
            env_var="ORCHESTRATOR_CHECKPOINT_KEY_ID",
            description=(
                "Identifier for the active checkpoint signing key. Bound into the HMAC so a "
                "rotated key cannot verify snapshots signed with a retired one, and so the "
                "verifier can tell which key a snapshot claims to need."
            ),
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

# GAP-MED-01: machine-readable error telemetry, emitted as single-line JSON so a
# log shipper can parse it without a grok pattern. The human-readable
# ``LOG_FORMAT`` above is unchanged — an operator tailing stderr should not have
# to decode JSON — so the two coexist: human output on the console, structured
# events on a dedicated channel.
#
# One JSON object per line, keys stable across releases. The logger is
# ``orchestrator.telemetry`` and the sink is added only when
# ORCHESTRATOR_TELEMETRY_FILE (or an ORCHESTRATOR_TELEMETRY_DIR) is configured,
# so the default run writes no extra file.
TELEMETRY_LOGGER_NAME = "orchestrator.telemetry"

#: Event names. Stable identifiers: dashboards and alerts key on these, so a
#: rename is a breaking change.
TELEMETRY_EVENT_ERROR = "command.error"
TELEMETRY_EVENT_COMMAND = "command.start"
TELEMETRY_EVENT_COMMAND_DONE = "command.done"
TELEMETRY_EVENT_DEPLOY = "deploy.result"
TELEMETRY_EVENT_CHECKPOINT = "checkpoint.created"
TELEMETRY_EVENT_COMPACTION = "context.compaction"

#: Fields always present in a telemetry record, so consumers can rely on them
#: without null-checking every access.
TELEMETRY_FIELDS = ("event", "ts", "level", "command", "exc_type", "message")

#: Never logged, in any channel. Secrets that live in the environment, a signed
#: checkpoint, and any value that looks like a credential must not reach a log
#: file, so a shipped telemetry sink cannot become a credential store.
TELEMETRY_REDACT_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "checkpoint_signing_key",
    "llm_api_key",
    "openrouter_api_key",
    "password",
    "secret",
    "signing_key",
    "token",
)

def telemetry_file() -> str:
    """Destination for JSON telemetry events, or ``""`` when disabled.

    Read dynamically so a test can point it at a ``tmp_path`` without reimporting
    ``config``. An empty result means the channel is off and no file is written.
    """
    explicit = os.environ.get("ORCHESTRATOR_TELEMETRY_FILE", "").strip()
    if explicit:
        return explicit
    directory = os.environ.get("ORCHESTRATOR_TELEMETRY_DIR", "").strip()
    if directory:
        return os.path.join(directory, "telemetry.jsonl")
    return ""


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively replace credential-looking values with ``"***"``.

    GAP-MED-01. Telemetry is written to disk, which makes it durable in a way
    stdout is not, so anything recorded there outlives the run. Redaction is by
    key name (and by a value-shape heuristic) rather than by call site, so a new
    field is safe by default instead of safe only if someone remembered.
    """
    if _depth > 6:
        return "***"
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in TELEMETRY_REDACT_KEYS):
                cleaned[str(key)] = "***"
            else:
                cleaned[str(key)] = redact(item, _depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str) and _looks_like_secret(value):
        return "***"
    return value


_SECRET_SHAPES = ("sk-", "ghp_", "gho_", "xoxb-", "xoxp-", "AKIA", "Bearer ")


def _looks_like_secret(value: str) -> bool:
    """Heuristic: is this string shaped like a credential?"""
    if len(value) < 16:
        return False
    return any(value.startswith(prefix) for prefix in _SECRET_SHAPES)

# A cycle with no status fingerprint change counts as "no progress".
MAX_CONSECUTIVE_IDLE_CYCLES = LOOP_THRESHOLDS.no_progress_max_cycles

# Checksum algorithm used for checkpoint integrity verification.
CHECKSUM_ALGORITHM = "sha256"

# ---------------------------------------------------------------------------
# Deployment / execution policy (GAP-CRIT-01)
# ---------------------------------------------------------------------------

# The runtime is offline by design. Executing a build, a test or a flash is the
# one capability that can change the world outside the project directory, so it
# is opt-in twice over: the channel must be *enabled*, and the executable must be
# on the allowlist. Defaults are therefore both closed.
#
# Set ORCHESTRATOR_DEPLOY_ENABLED=1 and list executables in
# ORCHESTRATOR_DEPLOY_ALLOWLIST (comma-separated, resolved via PATH lookup) to
# let agents run them. With the channel off, `data.deploy` is reported as
# executed=false, which is what forces an honest NOT RUN downstream.
DEPLOY_ENABLED = False

# Comma-separated executable names permitted to be spawned. Empty by default.
DEPLOY_ALLOWLIST: Tuple[str, ...] = ()

# Wall-clock ceiling for one invocation, in seconds. A compiler or a test suite
# legitimately needs minutes; a hung process must not hold a worker forever.
DEPLOY_TIMEOUT_SECONDS = 300

# Hard cap on captured stdout/stderr per stream, in bytes. Output is truncated
# with an explicit marker rather than silently, so an agent reading the tail
# knows it is not seeing everything.
DEPLOY_MAX_OUTPUT_BYTES = 64 * 1024

# Where evidence transcripts are written, relative to the project root.
DEPLOY_EVIDENCE_DIR = "docs/evidence"

# Status vocabulary an agent may report for a verification it could not run.
TEST_STATUS_NOT_RUN = "NOT RUN"

# Substrings that mark an output as *claiming* an executed verification. Used
# by the DoD to decide when ground truth is mandatory. Deliberately narrow: a
# false positive forces a real check (annoying), a false negative lets a
# fabrication through (fatal), so the list favours recall.
DEPLOY_CLAIM_PATTERNS: Tuple[str, ...] = (
    "executed",
    "ran the test",
    "ran the tests",
    "test run",
    "tests passed",
    "test passed",
    "all tests",
    "42/42",
    "build succeeded",
    "build passed",
    "compiled successfully",
    "compilation succeeded",
    "ctest",
    "pytest",
    "idf.py build",
    "idf.py flash",
    "flashed",
    "flash succeeded",
    "coverage",
    "test report",
    "verification log",
    "exit code",
)


def _env_flag_deploy(name: str, default: bool = False) -> bool:
    """Read the deploy enable flag. Truthy: 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def deploy_enabled() -> bool:
    """True when agents may execute allowlisted commands.

    Read dynamically so a test (or an operator) can toggle it without a code
    change. Always False unless *both* the flag is set and the allowlist is
    non-empty: enabling the channel with nothing allowlisted would be a flag
    that appears to work and does nothing.
    """
    if not _env_flag_deploy("ORCHESTRATOR_DEPLOY_ENABLED", DEPLOY_ENABLED):
        return False
    return bool(deploy_allowlist())


def deploy_allowlist() -> Tuple[str, ...]:
    """Executables permitted to be spawned, from the environment.

    Read dynamically (see :func:`_env_flag_deploy`). Entries are normalised to
    a bare basename so ``/usr/bin/gcc`` and ``gcc`` are the same permission,
    and a path traversal attempt cannot smuggle a different binary through.
    """
    raw = os.environ.get("ORCHESTRATOR_DEPLOY_ALLOWLIST", "")
    if not raw.strip():
        return DEPLOY_ALLOWLIST
    names: List[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        names.append(os.path.basename(candidate))
    return tuple(dict.fromkeys(names))


def deploy_timeout() -> float:
    """Per-invocation timeout in seconds, clamped to a sane range."""
    raw = os.environ.get("ORCHESTRATOR_DEPLOY_TIMEOUT")
    if raw:
        try:
            value = float(raw)
            if 1.0 <= value <= 3600.0:
                return value
        except ValueError:
            pass
    return float(DEPLOY_TIMEOUT_SECONDS)


def deploy_max_output_bytes() -> int:
    """Byte cap per captured stream, clamped so a value of 0 cannot pass."""
    raw = os.environ.get("ORCHESTRATOR_DEPLOY_MAX_OUTPUT")
    if raw:
        try:
            value = int(raw)
            if 1024 <= value <= 8 * 1024 * 1024:
                return value
        except ValueError:
            pass
    return int(DEPLOY_MAX_OUTPUT_BYTES)


def deploy_evidence_dir() -> str:
    """Project-relative directory for evidence transcripts."""
    return os.environ.get("ORCHESTRATOR_DEPLOY_EVIDENCE_DIR", "").strip() or DEPLOY_EVIDENCE_DIR


def claims_execution(text: str) -> bool:
    """True when ``text`` claims a verification was actually executed.

    Used to decide when the DoD must demand a ground-truth record. Recall is
    favoured over precision: a false positive merely forces a real check.
    """
    if not text:
        return False
    lowered = str(text).lower()
    return any(pattern in lowered for pattern in DEPLOY_CLAIM_PATTERNS)


# ---------------------------------------------------------------------------
# Checkpoint signing / integrity policy
# ---------------------------------------------------------------------------

# Algorithm used for the checkpoint HMAC. Changing this invalidates every
# previously signed snapshot, so it is a named constant rather than a literal
# so the coupling is at least visible in one place.
CHECKPOINT_SIGNATURE_ALGORITHM = "HMAC-SHA256"

# Key id recorded in metadata when no explicit id is configured. Signing with a
# named key (rather than an anonymous one) is what makes rotation possible: a
# snapshot records which key it needs, so a retired key can be identified
# instead of silently producing a confusing mismatch.
DEFAULT_CHECKPOINT_KEY_ID = "default"

# Version of the metadata signing envelope. Bump when the MAC's input string
# changes shape, so an old snapshot is reported as "signed with an unknown
# scheme" rather than as tampered.
CHECKPOINT_SIGNATURE_VERSION = 1

# Escape hatch: when true, a snapshot whose provenance cannot be positively
# established (never signed, or signed under a key that is not configured) is
# accepted on checksums alone with a loud warning. Defaults to FALSE, because
# the point of persisting `signed` explicitly is that "unsigned" must never be
# reachable by deleting a field. Operators set CHECKPOINT_ALLOW_UNSIGNED=1 to
# restore the pre-2.0.1 behaviour for a snapshot store they cannot re-sign.
ALLOW_UNSIGNED = False


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean-ish environment variable.

    Truthy: ``1``, ``true``, ``yes``, ``on`` (case-insensitive). Anything else
    falsy. Deliberately evaluated on every call rather than captured at import
    so tests can toggle behaviour with ``monkeypatch.setenv``.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def allow_unsigned_checkpoints() -> bool:
    """True when a snapshot store may fall back to checksum-only verification.

    Read dynamically (see :func:`_env_flag`) so it is testable and so an
    operator can set it without a code change.
    """
    return _env_flag("CHECKPOINT_ALLOW_UNSIGNED", ALLOW_UNSIGNED)


# ---------------------------------------------------------------------------
# Structured error telemetry (GAP-MED-01)
# ---------------------------------------------------------------------------


def get_telemetry_logger() -> Optional["logging.Logger"]:
    """Return the JSON telemetry logger, or ``None`` when the channel is off.

    Configured lazily on first use so importing ``config`` never creates a file.
    Writes append-only JSON Lines, which survives the append-only state files
    this repository already relies on.
    """
    destination = telemetry_file()
    if not destination:
        return None
    logger = logging.getLogger(TELEMETRY_LOGGER_NAME)
    if getattr(logger, "_orchestrator_configured", False):
        return logger
    try:
        parent = os.path.dirname(os.path.abspath(destination))
        if parent:
            os.makedirs(parent, exist_ok=True)
        handler = logging.FileHandler(destination, encoding="utf-8")
    except OSError:
        # Telemetry must never be the reason a run fails.
        return None
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Structured events go to the file, never to stderr: they are machine
    # output and would only confuse an operator reading the human log.
    logger.propagate = False
    logger._orchestrator_configured = True  # type: ignore[attr-defined]
    return logger


def emit_telemetry(
    event: str,
    *,
    level: str = "INFO",
    exc_type: str = "",
    message: str = "",
    **fields: Any,
) -> None:
    """Record one structured event. A no-op when telemetry is not configured.

    Never raises: telemetry is observability, and a logging failure must not
    escalate into an operational one.
    """
    logger = get_telemetry_logger()
    if logger is None:
        return
    try:
        record: Dict[str, Any] = {
            "event": str(event),
            "ts": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": str(level).upper(),
            "command": str(fields.pop("command", "") or ""),
            "exc_type": str(exc_type or ""),
            "message": _telemetry_text(message),
        }
        record.update(redact(fields))
        logger.info(json.dumps(record, sort_keys=True, default=str))
    except Exception:  # noqa: BLE001 - telemetry must not break the run
        return


def _telemetry_text(message: Any, limit: int = 500) -> str:
    """Bounded, single-line rendering of a message for one-line JSON."""
    text = " ".join(str(message or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."

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
    "DEPLOY_ENABLED",
    "DEPLOY_ALLOWLIST",
    "DEPLOY_TIMEOUT_SECONDS",
    "DEPLOY_MAX_OUTPUT_BYTES",
    "DEPLOY_EVIDENCE_DIR",
    "DEPLOY_CLAIM_PATTERNS",
    "TEST_STATUS_NOT_RUN",
    "deploy_enabled",
    "deploy_allowlist",
    "deploy_timeout",
    "deploy_max_output_bytes",
    "deploy_evidence_dir",
    "claims_execution",
    "CHECKPOINT_SIGNATURE_ALGORITHM",
    "DEFAULT_CHECKPOINT_KEY_ID",
    "CHECKPOINT_SIGNATURE_VERSION",
    "ALLOW_UNSIGNED",
    "allow_unsigned_checkpoints",
    "is_terminal_status",
    "priority_rank",
]
