"""Specialist agent implementations for the orchestrator runtime."""

from .base_agent import (
    AGENT_REGISTRY,
    AgentError,
    AgentOutput,
    AgentOutputError,
    BaseAgent,
    agent_names,
    create_agent,
    normalize_agent_name,
    register_agent,
)
from .llm_agent import LLMAgent
from .requirements_agent import RequirementsAgent, RequirementsParseError
from .specialists import (
    ArchitectureAgent,
    DocumentationAgent,
    HardwareAgent,
    PlanningAgent,
    ResearchAgent,
    ReviewAgent,
    SoftwareAgent,
    TestAgent,
)

__all__ = [
    "AGENT_REGISTRY",
    "AgentError",
    "AgentOutput",
    "AgentOutputError",
    "BaseAgent",
    "LLMAgent",
    "RequirementsAgent",
    "RequirementsParseError",
    "ResearchAgent",
    "ArchitectureAgent",
    "PlanningAgent",
    "HardwareAgent",
    "SoftwareAgent",
    "TestAgent",
    "ReviewAgent",
    "DocumentationAgent",
    "agent_names",
    "create_agent",
    "normalize_agent_name",
    "register_agent",
]
