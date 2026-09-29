"""Prompt assembly — turns an agent payload into a single model prompt.

Implements the "Approach 2" pipeline from IMPLEMENTATION_ROADMAP.md:

1. load the agent's specification from ``framework/XX_*.md``,
2. build a prompt from system rules + spec + project memory + task + context,
3. hand it to :mod:`orchestrator.llm_client`,
4. parse the structured JSON answer with ``BaseAgent.extract_json_block``.

The framework directory is resolved relative to the repository root so the
loader works from any working directory; tests can point it anywhere with the
``specs_dir`` argument.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Union

from . import config

REPO_ROOT = Path(__file__).resolve().parent.parent

# agent id -> framework specification file
AGENT_SPEC_FILES: Dict[str, str] = {
    "master_orchestrator": "00_MASTER_ORCHESTRATOR.md",
    "supervisor_agent": "01_SUPERVISOR_AGENT.md",
    "requirements_agent": "02_REQUIREMENTS_AGENT.md",
    "research_agent": "03_RESEARCH_AGENT.md",
    "architecture_agent": "04_ARCHITECTURE_AGENT.md",
    "planning_agent": "05_PLANNING_AGENT.md",
    "hardware_agent": "06_HARDWARE_AGENT.md",
    "software_agent": "07_SOFTWARE_FIRMWARE_AGENT.md",
    "firmware_agent": "07_SOFTWARE_FIRMWARE_AGENT.md",
    "test_agent": "08_TEST_AGENT.md",
    "review_agent": "09_REVIEW_AGENT.md",
    "documentation_agent": "10_DOCUMENTATION_AGENT.md",
}

DEFAULT_MEMORY_LIMIT = 24000
DEFAULT_CONTEXT_FILE_LIMIT = 8000

OUTPUT_FORMAT_INSTRUCTIONS = (
    "Respond with exactly ONE fenced ```json block and no other prose. "
    "The JSON object must follow this contract:\n"
    "{\n"
    '  "agent_id": "<your agent id>",\n'
    '  "task_id": "<the task id>",\n'
    '  "status": "completed | failed | blocked",\n'
    '  "summary": "<one factual sentence with measurable results>",\n'
    '  "data": { "<structured results the spec asks for>" },\n'
    '  "artifacts": ["<file names you produced>"],\n'
    '  "errors": ["<specific blockers, empty list if none>"],\n'
    '  "warnings": ["<risks or follow-ups, empty list if none>"]\n'
    "}\n"
    "Never claim DONE for significant work; report measurable results only."
)


class PromptBuilderError(RuntimeError):
    """Raised when a prompt cannot be assembled from the supplied payload."""


def framework_specs_dir(specs_dir: Optional[Union[str, Path]] = None) -> Path:
    """Directory holding the framework/XX_*.md agent specifications."""
    if specs_dir is not None:
        return Path(specs_dir)
    return REPO_ROOT / config.FRAMEWORK_SPECS_DIR


def spec_path(
    agent_id: str, specs_dir: Optional[Union[str, Path]] = None
) -> Optional[Path]:
    """Return the framework spec file for an agent id, or None when missing."""
    name = AGENT_SPEC_FILES.get(str(agent_id).strip().lower())
    if name is None:
        return None
    candidate = framework_specs_dir(specs_dir) / name
    return candidate if candidate.exists() else None


def load_agent_spec(
    agent_id: str, specs_dir: Optional[Union[str, Path]] = None
) -> str:
    """Load the framework specification text for an agent ('' when missing)."""
    path = spec_path(agent_id, specs_dir)
    if path is None:
        return ""
    return path.read_text(encoding="utf-8")


def truncate_middle(text: str, limit: int) -> str:
    """Keep the head and tail of ``text`` when it exceeds ``limit`` chars."""
    if limit <= 0 or len(text) <= limit:
        return text
    keep_head = int(limit * 0.6)
    keep_tail = limit - keep_head - 24
    head = text[:keep_head]
    tail = text[-keep_tail:] if keep_tail > 0 else ""
    return f"{head}\n...[truncated {len(text) - limit} chars]...\n{tail}"


def render_system_prompt(system_rules: str, agent_spec: str) -> str:
    """Everything that goes into the ``system`` message of the model call."""
    parts = ["You are a specialist agent inside an agentic AI orchestrator."]
    if system_rules:
        parts.append("Operating rules:\n" + system_rules.strip())
    if agent_spec:
        parts.append("Your agent specification (framework contract):\n" + agent_spec.strip())
    parts.append(OUTPUT_FORMAT_INSTRUCTIONS)
    return "\n\n".join(parts)


def build_prompt(
    payload: Dict[str, object],
    agent_spec: str = "",
    memory_limit: int = DEFAULT_MEMORY_LIMIT,
    context_file_limit: int = DEFAULT_CONTEXT_FILE_LIMIT,
) -> str:
    """Assemble the single user message for one agent execution."""
    if not isinstance(payload, dict):
        raise PromptBuilderError(
            f"payload must be a dict, got {type(payload).__name__}"
        )
    task = payload.get("task") or {}
    if not isinstance(task, dict):
        raise PromptBuilderError("payload.task must be a dict")
    task_id = str(task.get("id", "") or "")
    if not task_id:
        raise PromptBuilderError("payload is missing task.id")

    sections = []
    rules = str(payload.get("system_rules") or "")
    if rules:
        sections.append("# Operating rules\n" + rules)
    if agent_spec:
        sections.append("# Agent specification\n" + agent_spec.strip())
    memory = str(payload.get("project_memory") or "")
    if memory:
        sections.append("# PROJECT_MEMORY.md\n" + truncate_middle(memory, memory_limit))
    sections.append("# Task (from TASKS.yaml)\n" + _render_json(task))

    context = payload.get("context") or {}
    if isinstance(context, dict) and context:
        blocks = []
        for name in sorted(context):
            value = context[name]
            text = value if isinstance(value, str) else str(value)
            blocks.append(f"## {name}\n{truncate_middle(text, context_file_limit)}")
        sections.append("# Task context files\n" + "\n\n".join(blocks))

    thresholds = payload.get("thresholds") or {}
    if thresholds:
        sections.append("# Thresholds you must respect\n" + _render_json(thresholds))

    sections.append("# Required output\n" + OUTPUT_FORMAT_INSTRUCTIONS)
    return "\n\n".join(sections)


def _render_json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


__all__ = [
    "PromptBuilderError",
    "AGENT_SPEC_FILES",
    "OUTPUT_FORMAT_INSTRUCTIONS",
    "DEFAULT_MEMORY_LIMIT",
    "DEFAULT_CONTEXT_FILE_LIMIT",
    "framework_specs_dir",
    "spec_path",
    "load_agent_spec",
    "truncate_middle",
    "render_system_prompt",
    "build_prompt",
]
