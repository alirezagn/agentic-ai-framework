"""Shared pytest fixtures and project builders for the orchestrator test suites."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from orchestrator.llm_client import LLMResult


class FakeLLMClient:
    """Offline stand-in for LLMClient used by agent tests."""

    def __init__(self, responses: List[Any]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def complete(self, system: str, messages: List[Dict[str, str]], **kwargs: Any) -> LLMResult:
        self.calls.append({"system": system, "messages": messages, **kwargs})
        if not self.responses:
            raise AssertionError("FakeLLMClient ran out of responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResult):
            return item
        return LLMResult(
            text=str(item), model="fake-model", provider="fake",
            usage={"input_tokens": 100, "output_tokens": 20},
        )


MEMORY_TEMPLATE = """# PROJECT_MEMORY — Test Project

## Status

Initialized test memory used by the orchestrator pipeline tests.

## Goal

Build a deterministic test fixture for state, checkpoint and loop handling.

## Key Requirements (Summary)

| REQ-ID | Title | Status |
|--------|-------|--------|
| REQ-001 | Voice Input Capture | APPROVED |
| REQ-002 | Speech-to-Text | APPROVED |
| REQ-003 | LLM Chat Response | PROPOSED |
| REQ-004 | OLED Face Display | APPROVED |

## Block Format Requirements

REQ-ID: REQ-005
Title: Response Latency
Type: NON-FUNCTIONAL
Priority: MUST
Description: The system must answer a spoken prompt within one second.
Rationale: Slow answers feel broken to a child.
Acceptance Criteria: Latency below 1 second; Packet loss below 1 percent
Dependencies: REQ-001
Related Risks: RISK-001
Related Tests: TEST-001
Status: APPROVED

REQ-ID: REQ-006
Title: Smooth Animations
Type: NON-FUNCTIONAL
Priority: SHOULD
Description: Face animations should transition smoothly between expressions.
Acceptance Criteria: Animations feel fast and smooth
Status: PROPOSED

## Assumptions

Assumption: A home WiFi network is always available for testing.

## Current Blockers

None.
"""

PROJECT_TEMPLATE: Dict[str, Any] = {
    "project": {
        "id": "PROJECT-TEST-001",
        "name": "test-project",
        "version": "0.1.0",
        "status": "REQUIREMENTS",
    },
    "phase": {"current": "REQUIREMENTS"},
    "progress": {"requirements": 50, "implementation": 0},
    "health": {
        "status": "HEALTHY",
        "blocked_tasks": 0,
        "failed_tasks": 0,
        "loop_detected": False,
        "deadlock_detected": False,
    },
    "context": {
        "utilization_percent": 35,
        "compaction_threshold": 70,
        "critical_threshold": 85,
    },
    "agents": {"orchestrator": "ACTIVE", "supervisor": "ACTIVE", "requirements_agent": "READY"},
    "next_tasks": ["TASK-002"],
    "blockers": [],
    "human_decisions": [],
    "last_checkpoint": {"id": "none", "date": "2026-09-30", "phase": "REQUIREMENTS"},
    "updated_at": "2026-09-30T00:00:00Z",
}


def _task(
    task_id: str,
    owner: str,
    status: str,
    dependencies: Optional[List[str]] = None,
    review_required: bool = False,
    attempt_count: int = 0,
    no_progress_cycles: int = 0,
    strategy_changes: int = 0,
    notes: str = "",
    priority: str = "HIGH",
    milestone: Optional[str] = None,
) -> Dict[str, Any]:
    task: Dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "owner": owner,
        "status": status,
        "priority": priority,
        "dependencies": list(dependencies or []),
        "expected_outputs": [f"{task_id}.md"],
        "acceptance_criteria": [f"{task_id} produces a verified artifact"],
        "input_files": ["PROJECT_MEMORY.md"],
        "execution": {
            "attempt_count": attempt_count,
            "no_progress_cycles": no_progress_cycles,
            "strategy_changes": strategy_changes,
            "last_error": None,
        },
        "review": {"required": review_required, "status": "NOT_STARTED"},
        "notes": notes,
    }
    if milestone is not None:
        task["milestone"] = milestone
    return task


def build_test_project(root: Path, context_utilization: int = 35) -> Path:
    """Create a fully-formed project fixture under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    project = json.loads(json.dumps(PROJECT_TEMPLATE))
    project["context"]["utilization_percent"] = context_utilization

    tasks = {
        "tasks": [
            _task("TASK-001", "requirements_agent", "DONE", [], notes="REQ-001 to REQ-004 captured"),
            _task(
                "TASK-002",
                "requirements_agent",
                "READY",
                ["TASK-001"],
                review_required=False,
                priority="CRITICAL",
                notes="Finalize acceptance criteria REQ-005",
            ),
            _task("TASK-003", "architecture_agent", "BLOCKED", ["TASK-002"]),
            _task("TASK-004", "hardware_agent", "TODO", ["TASK-003"]),
        ]
    }

    (root / "PROJECT.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    (root / "TASKS.yaml").write_text(
        yaml.safe_dump(tasks, sort_keys=False), encoding="utf-8"
    )
    (root / "PROJECT_MEMORY.md").write_text(MEMORY_TEMPLATE, encoding="utf-8")
    (root / "CURRENT_STATE.md").write_text(
        "# CURRENT_STATE — Test Project\n\nSnapshot for tests.\n", encoding="utf-8"
    )
    (root / "DECISIONS.md").write_text(
        "# DECISIONS — Test Project\n\n"
        "| DEC-ID | Decision | Status | Reason |\n"
        "|--------|----------|--------|--------|\n"
        "| DEC-001 | Use YAML for state | APPROVED | Human readable |\n",
        encoding="utf-8",
    )
    (root / "RISKS.md").write_text(
        "# RISKS — Test Project\n\n"
        "| RISK-ID | Risk | Probability | Impact | Mitigation |\n"
        "|---------|------|-------------|--------|------------|\n"
        "| RISK-001 | Latency above 1s | MEDIUM | Broken UX | Use tiny model |\n",
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "# CHANGELOG — Test Project\n\n## 2026-09-30 — initialized\n", encoding="utf-8"
    )
    return root


@pytest.fixture()
def test_project(tmp_path: Path) -> Path:
    return build_test_project(tmp_path / "test-project")


@pytest.fixture()
def checkpoints_root(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints"
