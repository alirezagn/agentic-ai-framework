"""Shared pytest fixtures and project builders for the orchestrator test suites."""

from __future__ import annotations

import json
import socket
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


class FakeDeployRunner:
    """Offline stand-in for :class:`orchestrator.deploy_runner.DeployRunner`.

    The real runner spawns processes. The test suite must stay hermetic and
    100% offline, so every test that exercises the evidence path injects this
    instead and gets deterministic records.

    It records what was requested, so a test can assert *which* commands an
    agent asked to run without anything executing.

    Behaviour knobs for tests:

    ``executed``
        What ``run_one`` reports. ``False`` reproduces a disabled channel.
    ``exit_code``
        Exit code reported for executed invocations — set non-zero to model a
        genuinely failing build.
    ``refuse``
        When true, every invocation is refused as if the allowlist rejected it.
    ``captured_output``
        stdout text placed in the returned record.
    """

    def __init__(
        self,
        executed: bool = True,
        exit_code: Optional[int] = 0,
        refuse: bool = False,
        captured_output: str = "fake output",
    ) -> None:
        self.executed = executed
        self.exit_code = exit_code
        self.refuse = refuse
        self.captured_output = captured_output
        self.requests: List[Dict[str, Any]] = []
        self.evidence_written: List[str] = []

    @property
    def enabled(self) -> bool:
        return not self.refuse

    @property
    def allowlist(self) -> List[str]:
        return ["ctest", "pytest", "cmake"]

    def run_one(self, invocation: Dict[str, Any]) -> Any:
        from orchestrator.deploy_runner import (
            STATUS_EXECUTED,
            STATUS_REFUSED,
            DeployRecord,
        )

        self.requests.append(dict(invocation))
        command = str(invocation.get("command") or "")
        args = [str(item) for item in (invocation.get("args") or [])]
        if self.refuse:
            return DeployRecord(
                command=command,
                args=args,
                executed=False,
                status=STATUS_REFUSED,
                reason="fake runner refuses everything",
            )
        return DeployRecord(
            command=command,
            args=args,
            cwd="<fake>",
            exit_code=self.exit_code,
            stdout_tail=self.captured_output,
            duration_ms=1,
            executed=self.executed,
            status=STATUS_EXECUTED if self.executed else STATUS_REFUSED,
            reason="" if self.executed else "fake runner configured as not executed",
            declared_expect=str(invocation.get("expect") or "").strip().upper() or "",
            expect_matched=None,
        )

    def run_payload(self, payload: Any) -> List[Any]:
        records: List[Any] = []
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict):
                records.append(self.run_one(item))
        return records

    def write_evidence(self, task_id: str, record: Any, index: int = 0) -> Optional[str]:
        path = f"docs/evidence/{task_id}/{index:02d}-{record.command}.json"
        self.evidence_written.append(path)
        return path

    @staticmethod
    def has_ground_truth(records: Any) -> bool:
        return any(getattr(record, "executed", False) for record in records or [])


@pytest.fixture()
def fake_deploy_runner() -> FakeDeployRunner:
    """Reachable runner: every requested command really executed and passed."""
    return FakeDeployRunner(executed=True, exit_code=0)


@pytest.fixture()
def disabled_deploy_runner() -> FakeDeployRunner:
    """Disabled channel: nothing executes, so claims must become NOT RUN."""
    return FakeDeployRunner(executed=False, refuse=True)


@pytest.fixture()
def failing_deploy_runner() -> FakeDeployRunner:
    """Channel on, build genuinely fails with a non-zero exit."""
    return FakeDeployRunner(executed=True, exit_code=2, captured_output="2 tests failed")


@pytest.fixture(autouse=True)
def _redirect_default_checkpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep default-root checkpoints inside the test's tmp dir (gap E1).

    Any code that builds a CheckpointManager/checkpoints_root=None (CLI
    commands, MasterOrchestrator defaults) would otherwise write into the
    working tree's ``checkpoints/`` folder.
    """
    root = tmp_path / "default-checkpoints"
    monkeypatch.setenv("ORCHESTRATOR_CHECKPOINTS_DIR", str(root))
    return root

#: Loopback literals permitted during tests. The LLM client targets
#: ``127.0.0.1`` in a handful of tests that exercise the transport, so those
#: must keep working; everything else is refused.
ALLOWED_TEST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test attempts a real outbound connection.

    The suite must stay offline-safe: an inherited ``ANTHROPIC_API_KEY`` in the
    developer's shell would otherwise turn it into an unbilled, unbounded API
    consumer. A test that *wants* an LLM injects ``FakeLLMClient`` or a
    ``transport`` callable instead.

    Address-family handling matters here. ``socket.connect`` is called as
    ``connect(address)`` for ``AF_INET``/``AF_INET6`` (a tuple) but as
    ``connect(path)`` for ``AF_UNIX`` (a string). Unpacking ``args[0][0]``
    unconditionally raises ``TypeError`` on a Unix-domain socket, which is both
    the wrong exception and a confusing one. So the family is checked first and
    Unix sockets are allowed through: they are filesystem-local by definition
    and cannot reach the network.
    """
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any, /) -> Any:
        if self.family in (socket.AF_UNIX, getattr(socket, "AF_UNSPEC", None)):
            return original_connect(self, address)
        if not isinstance(address, (tuple, list)) or not address:
            raise RuntimeError(
                f"Unauthorized network egress: unrecognised address form {address!r} "
                f"for family {self.family!r} during test execution"
            )
        host = address[0]
        if host not in ALLOWED_TEST_HOSTS:
            raise RuntimeError(
                f"Unauthorized network egress attempt detected during test execution to: {host}"
            )
        return original_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

# ---------------------------------------------------------------------------
# Collection scope
# ---------------------------------------------------------------------------

# The materializer writes a copy of every delivered file under the project's
# docs/ mirror, including test modules. Those copies are documentation, not
# tests, and collecting them collides with the real module on basename
# ("import file mismatch"), which interrupts the entire suite at collection.
#
# Scoped to docs/ rather than a broad norecursedirs so a genuine test directory
# is never hidden by accident.
collect_ignore_glob = ["*/docs/*", "*/docs/**/*"]
