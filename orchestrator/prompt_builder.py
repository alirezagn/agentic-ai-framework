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
# Inlined expected outputs are pre-budgeted at load (32K/40K) — render must
# not re-truncate them or the model again edits files blind.
EXPECTED_RENDER_LIMIT = 44_000

#: Canonical output contract appended to every agent's system prompt.
#:
#: GAP-CRIT-09 / HIGH-13. The previous 11 ``framework/AGENT_PROMPTS/*.md`` files
#: each documented a top-level ``"documents": [{"name", "content"}]`` array.
#: Nothing reads that key: :meth:`BaseAgent._materialize_artifacts` reads
#: ``data.documents`` as a **dict** mapping path to body. A model following those
#: files emitted a shape the runtime silently discarded — which is the single
#: most common reason a task delivered nothing while the agent reported success.
#:
#: So the contract lives here, in code, next to the parser that enforces it, and
#: the markdown files are generated from it (see
#: ``tests/test_final_critical_gaps.py``). One source, cannot drift.
OUTPUT_FORMAT_INSTRUCTIONS = (
    "Respond with exactly ONE fenced ```json block and no other prose. "
    "The JSON object must follow this contract:\n"
    "{\n"
    '  "agent_id": "<your agent id>",\n'
    '  "task_id": "<the task id>",\n'
    '  "status": "completed | failed | blocked",\n'
    '  "summary": "<one factual sentence with measurable results>",\n'
    '  "artifacts": ["<file names you produced>"],\n'
    '  "errors": ["<specific blockers, empty list if none>"],\n'
    '  "warnings": ["<risks or follow-ups, empty list if none>"],\n'
    '  "data": { <the keys below> }\n'
    "}\n"
    "\n"
    "data keys — the ONLY channels that deliver a file or evidence "
    "(all nested under \"data\"):\n"
    '- data.documents — a DICT mapping the expected output path to its full body: '
    "{\"<expected output path>\": \"<full file body>\"}. Key it by the exact "
    "expected_outputs string. Use it for a file that does not exist yet, or is "
    "short. Never an array.\n"
    '- data.edits — for patching an existing file: {"<existing path>": '
    "{\"search\": \"<exact current text, once>\", "
    '"replace": "<new text>"}} — the search snippet must match exactly once. '
    "Pass a LIST of {search, replace} for disjoint changes in one file.\n"
    '- data.deploy: [{"command": "<allowlisted binary or ./project/script.sh>", '
    '"args": ["<arg>"], "cwd": "<optional, project-relative>", '
    '"expect": "PASS" | "FAIL"}] — to have something ACTUALLY RUN. The '
    "runtime executes it and returns the real exit code as "
    "data.deploy_results; you never decide whether it ran.\n"
    '- data.acceptance_results: [{"name": "<criterion>", "status": "PASS" | "FAIL", '
    '"detail": "<evidence>"}] — REQUIRED for every criterion you checked. A '
    "FAIL entry blocks completion, so report it honestly.\n"
    '- data.test_status: "PASS" | "FAIL" | "NOT RUN" — use NOT RUN whenever you '
    "could not execute a verification. Inventing a result is a contract "
    "violation; NOT RUN is a legitimate outcome.\n"
    '- data.findings / data.corrections — review findings and the tasks '
    "they imply.\n"
    '- data.review_status: "PASS" | "PASS WITH ACTIONS" | "FAIL" — review '
    "agents only.\n"
    "\n"
    "There is NO top-level \"documents\" key. File content delivered only as "
    "prose in `summary` does NOT deliver the file: the Definition of Done checks "
    "the file on disk.\n"
    "Never claim DONE for significant work; report measurable results only."
)

#: Dependency-management contract for any agent that introduces an import.
#:
#: The failure this prevents is specific and was observed in
#: ``projects/sys_mon``: the agent delivered correct code importing ``psutil``,
#: wrote the imports, and reported "you may need to install psutil" **in prose**.
#: No ``requirements.txt`` was created and no ``data.deploy`` step was requested,
#: so the very next turn — the test run — died at import with
#: ``ModuleNotFoundError``, and the orchestrator's own suite could not even be
#: collected. A warning in ``summary`` installs nothing.
#:
#: Two obligations, and the ordering between them is the point:
#:
#: 1. **Declare** the dependency as a file the Definition of Done can see.
#: 2. **Install** it through ``data.deploy``, ordered *before* the test or
#:    verification step that needs it.
#:
#: Declaring without installing is what happens today; installing without
#: declaring is unreproducible. Both, in that order, is the deliverable.
#:
#: The honest-failure clause is load-bearing and mirrors the existing deploy
#: contract: the execution channel is opt-in and closed by default
#: (:data:`config.DEPLOY_ENABLED` is False and :data:`config.DEPLOY_ALLOWLIST` is
#: empty), so ``pip`` is normally **not** allowlisted. An instruction that
#: merely said "always install" would push the model to claim an install it could
#: not perform — trading a missing file for a fabricated execution record, which
#: this framework treats as the more serious defect.
DEPENDENCY_AUTOMATION_INSTRUCTIONS = (
    "Dependency contract — third-party imports must be automated, not announced:\n"
    "- The moment your code imports a package that is not in the Python standard "
    "library, you OWN the install. Mentioning it in `summary` or `warnings` "
    "installs nothing.\n"
    "- STEP 1 — DECLARE: create or update `requirements.txt` in the PROJECT ROOT, "
    "one requirement per line, pinned with `>=` (e.g. `psutil>=5.9`). Deliver it "
    "like any other file: `data.documents[\"requirements.txt\"]` when missing, "
    "`data.edits[\"requirements.txt\"]` when it already exists. Add the path to "
    "the task's `expected_outputs` so the Definition of Done verifies it is on "
    "disk. Read-only imports you did not introduce (already declared) need no "
    "new entry.\n"
    "- STEP 2 — INSTALL: emit the install as an explicit `data.deploy` entry — "
    '{"command": "pip", "args": ["install", "-r", "requirements.txt"], '
    '"expect": "PASS", "rationale": "install declared dependencies"}. '
    "ORDER MATTERS: `data.deploy` runs in list order, so the install entry must "
    "come BEFORE any pytest, unittest or verification-script entry, or those run "
    "against an environment that lacks the package.\n"
    "- STEP 3 — VERIFY: only after the install has returned `executed: true`, "
    "request the tests. If a test step fails with `ModuleNotFoundError`, that is "
    "a missing STEP 1 or STEP 2 on your side, not an environment problem to report.\n"
    "- `pip` is allowlisted only when the operator enabled execution. If the "
    "install comes back `executed: false` (channel disabled, command refused, or "
    "no network), that is a legitimate finding: keep `requirements.txt` as your "
    "deliverable, set `data.test_status = \"NOT RUN\"`, and state in `warnings` "
    "exactly which packages could not be installed and why. Never report a "
    "dependency as installed without a matching executed record.\n"
    "- Prefer the standard library when it genuinely suffices, and say so in "
    "`summary`; a dependency you do not need is not a dependency to manage."
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
    # After the agent's own rules so it reads as a standing contract, not as a
    # suggestion that a later rule can talk the model out of.
    parts.append(DEPENDENCY_AUTOMATION_INSTRUCTIONS)
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
            # Files the agent must EDIT need near-full bodies for exact
            # search snippets (already budgeted at load time) — never
            # middle-truncate them like passive reference files.
            limit = (
                EXPECTED_RENDER_LIMIT
                if str(name).startswith("expected_output:")
                else context_file_limit
            )
            blocks.append(f"## {name}\n{truncate_middle(text, limit)}")
        sections.append("# Task context files\n" + "\n\n".join(blocks))

    thresholds = payload.get("thresholds") or {}
    if thresholds:
        sections.append("# Thresholds you must respect\n" + _render_json(thresholds))

    # GAP-CRIT-04: a structural DoD failure is a pre-verdict, not task context.
    # Rendering it as its own section guarantees the reviewer sees what the
    # Definition of Done already rejected, instead of approving an artifact the
    # framework knows was never delivered.
    pending_problems = payload.get("pending_dod_problems") or []
    if pending_problems:
        rendered = "\n".join(f"- {item}" for item in pending_problems)
        sections.append(
            "# UNMET DEFINITION OF DONE (already recorded — not your own finding)\n"
            "The orchestrator ran its structural checks on this task and they FAILED. "
            "A PASS verdict is invalid while any item below stands; either every one "
            "is genuinely resolved (and you can show why) or return PASS WITH ACTIONS "
            "or FAIL naming them. Do not treat a claimed summary as evidence.\n\n"
            f"{rendered}"
        )

    # Restated here, not only in the system message: the user message is the one
    # carrying the task and the delivery manifest, so an agent that reads for
    # "what must I deliver" sees the dependency obligation in the same place.
    sections.append("# Required output\n" + OUTPUT_FORMAT_INSTRUCTIONS)
    sections.append(
        "# Dependency obligation\n" + DEPENDENCY_AUTOMATION_INSTRUCTIONS
    )
    return "\n\n".join(sections)


def _render_json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


__all__ = [
    "PromptBuilderError",
    "AGENT_SPEC_FILES",
    "OUTPUT_FORMAT_INSTRUCTIONS",
    "DEPENDENCY_AUTOMATION_INSTRUCTIONS",
    "DEFAULT_MEMORY_LIMIT",
    "DEFAULT_CONTEXT_FILE_LIMIT",
    "framework_specs_dir",
    "spec_path",
    "load_agent_spec",
    "truncate_middle",
    "render_system_prompt",
    "build_prompt",
]
