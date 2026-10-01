"""Agentic AI Framework - Orchestrator Runtime.

Public API::

    from orchestrator import (
        StateManager, CheckpointManager, SupervisorAgent,
        MasterOrchestrator, RequirementsAgent,
    )
"""

from . import config
from .agents import (
    AgentError,
    AgentOutput,
    AgentOutputError,
    BaseAgent,
    RequirementsAgent,
    create_agent,
    register_agent,
)
from .checkpoint_manager import (
    Checkpoint,
    CheckpointError,
    CheckpointIntegrityError,
    CheckpointManager,
    CheckpointNotFoundError,
)
from .orchestrator import (
    LoopLimitExceededError,
    MasterOrchestrator,
    MissingAgentError,
    OrchestratorError,
    TaskRunResult,
)
from .state_manager import (
    StateCorruptedError,
    StateError,
    StateFileMissingError,
    StateManager,
    TaskNotFoundError,
    derive_initial_status,
    strip_untrusted_task_fields,
)
from .supervisor import (
    Escalation,
    HealthReport,
    HealthState,
    LoopDetection,
    SupervisorAgent,
    SupervisorError,
)

__version__ = "2.0.0"
__all__ = [
    "config",
    "StateManager",
    "StateError",
    "StateFileMissingError",
    "StateCorruptedError",
    "TaskNotFoundError",
    "derive_initial_status",
    "strip_untrusted_task_fields",
    "Checkpoint",
    "CheckpointManager",
    "CheckpointError",
    "CheckpointNotFoundError",
    "CheckpointIntegrityError",
    "SupervisorAgent",
    "SupervisorError",
    "HealthState",
    "HealthReport",
    "LoopDetection",
    "Escalation",
    "MasterOrchestrator",
    "OrchestratorError",
    "LoopLimitExceededError",
    "MissingAgentError",
    "TaskRunResult",
    "BaseAgent",
    "AgentOutput",
    "AgentError",
    "AgentOutputError",
    "RequirementsAgent",
    "register_agent",
    "create_agent",
]
