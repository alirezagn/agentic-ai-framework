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
#: Two different failures shaped it. In ``projects/sys_mon`` the agent
#: delivered correct code importing ``psutil`` and reported "you may need to
#: install psutil" **in prose** — no ``requirements.txt``, no ``data.deploy``
#: step, so the very next turn died at import with ``ModuleNotFoundError``.
#: A warning in ``summary`` installs nothing.
#:
#: In the ``projects/sys-usage`` rerun the opposite failure appeared: the
#: model asked ``data.deploy`` to ``pip install -r requirements.txt`` and the
#: runner executed it twice into a PEP 668 system interpreter
#: (``--break-system-packages``). Operator directive, recorded 2026-10-03:
#: **never install libraries or applications — declare them and document the
#: setup command.** :func:`orchestrator.deploy_runner.install_refusal_reason`
#: enforces it; install invocations come back ``executed: false`` with a
#: policy reason.
#:
#: So the contract has exactly two obligations, in this order:
#:
#: 1. **Declare** the dependency as a file the Definition of Done can see.
#: 2. **Document** the setup command for the human operator (README), and
#:    verify only against the environment as it is.
#:
#: The honest-failure clause stays load-bearing: a refusal (policy, disabled
#: channel, missing allowlist) is a legitimate finding — ``NOT RUN`` plus the
#: named packages beats a fabricated execution record, which this framework
#: treats as the more serious defect.
DEPENDENCY_AUTOMATION_INSTRUCTIONS = (
    "Dependency contract — declare and document, never install:\n"
    "- The moment your code imports a package that is not in the Python standard "
    "library, you OWN the declaration. Mentioning it in `summary` or `warnings` "
    "installs nothing.\n"
    "- STEP 1 — DECLARE: create or update `requirements.txt` in the PROJECT ROOT, "
    "one requirement per line, pinned with `>=` (e.g. `psutil>=5.9`). Deliver it "
    "like any other file: `data.documents[\"requirements.txt\"]` when missing, "
    "`data.edits[\"requirements.txt\"]` when it already exists. Add the path to "
    "the task's `expected_outputs` so the Definition of Done verifies it is on "
    "disk. Read-only imports you did not introduce (already declared) need no "
    "new entry.\n"
    "- STEP 2 — DOCUMENT: add the setup command to the README's installation "
    "section — `pip install -r requirements.txt` — for the HUMAN operator to "
    "run. The framework NEVER installs libraries or applications: an install "
    "invocation you put in `data.deploy` (pip, apt, npm, cargo, ...) is "
    "refused by the runner with a policy reason, and a refused entry proves "
    "nothing about your code.\n"
    "- STEP 3 — VERIFY: request the tests against the environment as it is. "
    "If a step fails with `ModuleNotFoundError`, the package is simply not "
    "installed on this machine: that is a missing STEP 1 or STEP 2, not an "
    "environment bug — report `data.test_status = \"NOT RUN\"` and state in "
    "`warnings` exactly which packages are missing and the setup command that "
    "provides them.\n"
    "- Refusals are findings too: when an invocation comes back "
    "`executed: false` (policy refusal, channel disabled, command not on the "
    "allowlist), keep `requirements.txt` as your deliverable and report "
    "`NOT RUN` with the record's reason. Never report a dependency as "
    "installed — you have no executed record that could ever prove it.\n"
    "- Prefer the standard library when it genuinely suffices, and say so in "
    "`summary`; a dependency you do not need is not a dependency to manage."
)


#: Cross-component data contract for agents that define or consume data shapes.
#:
#: The failure this prevents is a *shape* mismatch across a task boundary, and
#: it is invisible until two separately-written components meet. Observed in
#: ``projects/sys_mon``: the metrics module returns nested mappings
#: (``{"ram": {"percent_used": 50}}``) while the view layer wants flat scalars
#: (``{"ram": 50.0}``). Each agent is individually correct, both test green in
#: isolation, and the integration point explodes with a ``TclError`` or a
#: ``TypeError`` that names neither the producer nor the consumer.
#:
#: Nothing in the payload prevents it, because a task carries *intent*, not a
#: type. The gap was never "the agent wrote a dict" -- it was that no artifact
#: anywhere obliged anyone to write down what the dict looks like.
#:
#: So the obligation is to make the shape explicit and checkable:
#:
#: 1. **Define** the schema where the components are specified, so a consumer is
#:    written against a declaration rather than against a sample.
#: 2. **Convert at the boundary** where producer and consumer disagree, in one
#:    named place, instead of letting both guess.
#: 3. **Prove it** with an integration test that exercises the real crossing.
#:
#: The error-path clause is included because it is the most common way a
#: contract breaks unnoticed: ``{"error": "psutil not installed"}`` is a flat
#: ``str`` value where the success path is a nested mapping, so the *failure*
#: path is the one shape nobody tested against the consumer.
DATA_CONTRACT_INSTRUCTIONS = (
    "Data contract — components that exchange values must agree on their shape:\n"
    "- A task states INTENT, not a type. Nothing in this payload pins the shape of "
    "a dict you return, so two independently written components will disagree "
    "unless you make the shape explicit. Writing one component at a time and "
    "hoping the shapes line up is what produces integration failures where one "
    "agent returns nested mappings {\"ram\": {\"percent_used\": 50}} and the next "
    "expects flat scalars {\"ram\": 50.0} -- the producer is not wrong and the "
    "consumer is not wrong, and the crash (TypeError, TclError, a KeyError three "
    "modules away) blames neither.\n"
    "- STEP 1 — DEFINE THE SCHEMA. When requirements_agent or architecture_agent "
    "creates task specs, or any task introduces a value crossing a module "
    "boundary, state the exact type for each field in docs/ARCHITECTURE.md, using "
    "a TypedDict or Pydantic model so it is executable rather than prose: the "
    "concrete container (dict or dataclass), the key names, and each field\'s "
    "type and whether it is optional. \"Return metrics\" is not a schema; "
    "`class Metrics(TypedDict): ram: Dict[str, float]` is. Nested-versus-flat is "
    "exactly the decision a type declaration forces you to make.\n"
    "- STEP 2 — CONVERT AT THE BOUNDARY. software_agent must cast or reshape "
    "explicitly when feeding metrics to a view layer, progress bar, formatter or "
    "serialiser: one named function, called at the crossing, that takes the "
    "producer shape and returns the consumer shape. Never let a component index "
    "a nested value and hand a bare float to code that expects a mapping, or the "
    "reverse. Convert where the two shapes meet; do not scatter try/except and "
    "coerce across every call site.\n"
    "- STEP 3 — KEEP THE ERROR PATH IN THE SCHEMA. An error result must satisfy "
    "the same declared shape as a success (for example an `error: Optional[str]` "
    "field alongside the normal fields, or a documented Union), never a different "
    "shape such as a bare {\"error\": str}. The failure path is the one no "
    "consumer is written against and the one that breaks first.\n"
    "- STEP 4 — WIRE AN ENTRYPOINT. Where a task produces runnable components, "
    "software_agent must deliver a main.py at the PROJECT ROOT that imports and "
    "runs them end to end, including the conversion wrappers from STEP 2, so the "
    "crossing is actually exercised rather than only assembled.\n"
    "- STEP 5 — TEST THE CROSSING, NOT JUST THE PARTS. Unit tests on each "
    "component prove nothing about the seam. Add a test that feeds real producer "
    "output into the real consumer and asserts the delivered shape, including one "
    "case for the error path. Per-component green plus a broken integration is the "
    "specific outcome this contract exists to prevent.\n"
    "- If a task's inputs do not pin a shape and you cannot infer one, choose and "
    "declare the shape yourself in docs/ARCHITECTURE.md rather than leaving it "
    "implicit -- an invented but declared shape is recoverable; an undeclared one "
    "is what the next agent has to guess against.\n"
    "- VERIFY SIGNATURES BEFORE IMPLEMENTING. Before writing a call into another "
    "module, read that module and match its signature verbatim: name, "
    "capitalisation, parameters in order, keyword-only arguments, return type. Do "
    "not reconstruct it from domain vocabulary, and do not write the call site "
    "first and then fit the definition to it. A signature you have not read is an "
    "assumption, and assumptions at the seam are what break the integration."
)


#: Per-module interface specification injected into software_agent prompts only.
#:
#: DATA_CONTRACT_INSTRUCTIONS states the general obligation to everyone.
#: DATA_CONTRACT_SPEC is the concrete, fill-in-the-blanks form for the one agent
#: that writes the crossing, and it is injected only for that agent: a
#: requirements agent has no modules to declare, and a contract it cannot use is
#: prompt budget spent on text the model must ignore.
#:
#: Two shapes are offered deliberately, because the failure in
#: ``projects/sys_mon_gui`` was a producer returning a nested mapping that a
#: widget consumer indexed as a float. Either declare the true shape and convert,
#: or declare a scalar accessor -- but make the choice explicit, in the module
#: that owns the boundary.
#: Obligation for agents that build runnable applications, especially GUIs.
#:
#: The failure this prevents is *low fidelity*, not a crash: a delivered Tkinter
#: app that runs, but is a default-styled single progress bar on a grey
#: background with no telemetry, no chart axes, and nothing to interact with. It
#: satisfies the letter of a task like "add a progress bar" and is not the
#: dashboard that was asked for. Every one of those omissions is invisible to a
#: test that only checks the app starts.
#:
#: It is paired with a data contract because the two failures share a cause: an
#: agent optimises for the smallest thing that satisfies the sentence in front of
#: it. See :data:`DATA_CONTRACT_INSTRUCTIONS` for the shape half.
UI_CONTRACT_INSTRUCTIONS = (
    "UI fidelity contract — a dashboard task means a composed interface:\n"
    "- Shipping a lone default-styled progress bar understates the task and reads "
    "as a placeholder, whatever the task text literally asks for. An agent "
    "optimising for the smallest widget that satisfies the sentence in front of it "
    "is the cause, and every omission it produces is invisible to a test that only "
    "checks the app starts.\n"
    "- For any GUI task the delivered interface MUST include the elements listed in "
    "the UI fidelity spec: composed layout, dark-mode palette, status indicator, "
    "metric telemetry cards, an axis-labelled canvas chart with a legend, and at "
    "least one interactive control that actually works. They are required scope, "
    "not decoration -- so do not trade them away to fit an output limit; deliver the "
    "modules across several tasks instead of shipping one thin file.\n"
    "- Fidelity is checkable, so check it. Run the app through data.deploy and "
    "confirm the interface renders; a data.test_status of PASS with no executed "
    "record for the entrypoint is a fabrication."
)


#: Concrete UI requirements, injected into software_agent prompts only.
#:
#: Kept separate from :data:`UI_CONTRACT_INSTRUCTIONS` because the palette and
#: widget list are only meaningful to the agent writing the view layer, while the
#: obligation above applies to everyone. Same split as
#: :data:`DATA_CONTRACT_SPEC`.
UI_FIDELITY_SPEC = (
    "UI fidelity spec — a dashboard means all of the following, not a subset:\n"
    "- LAYOUT: a composed structure, not one widget on a root window. A header, a "
    "row of metric cards, and a chart region, laid out with pack/grid so the "
    "window is resizable without overlap or clipping.\n"
    "- DARK MODE PALETTE: use the concrete values rather than inventing them. "
    "Window/canvas background `#0f172a`, card and chart surface `#1e293b`, bar "
    "track `#334155`, primary text `#f8fafc`, secondary/axis text `#94a3b8`, and "
    "distinct accents per series (e.g. `#38bdf8` CPU, `#a855f7` RAM, `#34d399` "
    "disk). Consistent colour is what makes a set of numbers read as one system.\n"
    "- STATUS INDICATOR: show live state (normal/degraded/error) with a visible "
    "colour or label change. A dashboard that looks identical when the data has "
    "stopped arriving is misleading, not merely plain.\n"
    "- METRIC TELEMETRY CARDS: one card per metric, each with a label, the current "
    "value formatted to a fixed precision (e.g. `12.3%`), and a filled bar.\n"
    "- CUSTOM CANVAS CHART: plot history on a Canvas rather than relying on a "
    "single bar — with gridlines, AXES AND LABELS, a visible legend for each "
    "series, and a time axis. An unlabelled line on a blank canvas is not a chart "
    "a reader can interpret.\n"
    "- INTERACTIVE CONTROLS: at least one real control the user can operate — a "
    "slider (interval or history length), a start/pause or refresh button, or a "
    "selector — wired to actually change behaviour. Decorative controls that do "
    "nothing are worse than none.\n"
    "- GUARD THE UPDATE LOOP: the periodic callback must tolerate a short, missing "
    "or malformed reading and keep the UI responsive, and every widget call must "
    "use a real option name (`padx`/`pady`, never `px`/`py`) — an invalid option "
    "raises TclError at construction, before anything is visible."
)


DATA_CONTRACT_SPEC = (
    "Module interface spec — declare the cross-module surface EXACTLY, for "
    "every module you create or consume:\n"
    "For each public method or function that crosses a module boundary, state:\n"
    "  1. NAME, verbatim, including capitalisation, exactly as defined in the "
    "file you read.\n"
    "  2. PARAMETERS: every positional argument in order, with types; then "
    "keyword-only arguments by name. Never invent a parameter.\n"
    "  3. RETURN TYPE, at one of two levels of precision:\n"
    "     (a) a TypedDict / Pydantic model, when the payload is structured "
    "(---\n"
    "         class RamStats(TypedDict):\n"
    "             total_gb: float\n"
    "             available_gb: float\n"
    "             percent_used: float\n"
    "         class Metrics(TypedDict):\n"
    "             cpu_percent: float\n"
    "             ram: RamStats\n"
    "             disk: RamStats\n"
    "     ), or\n"
    "     (b) a SCALAR return value (float/int/str), when the consumer only "
    "needs one number --\n"
    "         def ram_percent_used() -> float: ...\n"
    "     Choosing (b) at the producer, or an explicit "
    "`ram[\"percent_used\"]` accessor, is what prevents a widget receiving a "
    "dict where it needs a float.\n"
    "  4. RAISES: what it throws when the dependency is unavailable, and what "
    "the caller must do about it.\n"
    "Non-negotiable while implementing:\n"
    "- Read the producer file before you call it. Match the signature you find; "
    "do not reconstruct it from the domain vocabulary.\n"
    "- Never index a nested mapping as though it were a scalar, and never hand a "
    "scalar to code that indexes keys. Convert in one named function at the "
    "crossing.\n"
    "- No defensive `.get(a, .get(b, 0.0))` chains. An uncertain key is a "
    "reason to re-read the file or report a finding, not to guess: a fallback "
    "renders as 0.0 and hides the mismatch permanently.\n"
    "- Keep main.py at the project root importing every module, so the declared "
    "interfaces are actually exercised together.\n"
    "- The error path returns the SAME declared shape (an `error: Optional[str]` "
    "field or a documented Union), never a bare {\"error\": str} that matches "
    "nothing else."
)

#: Agents whose prompts receive :data:`DATA_CONTRACT_SPEC` in addition to the
#: global contract. software_agent is the agent that writes and crosses module
#: boundaries; firmware_agent is deliberately excluded for now -- it shares the
#: 07_SOFTWARE_FIRMWARE spec file, so adding it here is a one-word change once
#: firmware tasks actually carry module boundaries.
DATA_CONTRACT_SPEC_AGENTS = frozenset({"software_agent"})

#: Agents receiving :data:`UI_FIDELITY_SPEC` in addition to the shared
#: :data:`UI_CONTRACT_INSTRUCTIONS`.
UI_FIDELITY_SPEC_AGENTS = frozenset({"software_agent"})


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
    parts.append(DATA_CONTRACT_INSTRUCTIONS)
    parts.append(UI_CONTRACT_INSTRUCTIONS)
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
    sections.append("# Data contract obligation\n" + DATA_CONTRACT_INSTRUCTIONS)
    sections.append("# Application contract obligation\n" + UI_CONTRACT_INSTRUCTIONS)
    # Per-agent interface spec. Keyed off payload["agent_id"], which build_payload
    # sets from self.AGENT_ID, so the strictest form lands on the agent that
    # actually writes module boundaries -- and on nobody else.
    # Not base_agent.normalize_agent_name: that module imports this one, so
    # importing it back would be circular. It is exactly strip().lower().
    agent_id = str(payload.get("agent_id") or "").strip().lower()
    if agent_id in DATA_CONTRACT_SPEC_AGENTS:
        sections.append(
            f"# Module interface spec ({agent_id})\n" + DATA_CONTRACT_SPEC
        )
    # The visual checklist only means something to the agent writing the view
    # layer; every agent already receives the obligation itself via
    # AUTHORING_CONTRACT, so nothing is lost by scoping the detail.
    if agent_id in UI_FIDELITY_SPEC_AGENTS:
        sections.append(f"# UI fidelity spec ({agent_id})\n" + UI_FIDELITY_SPEC)
    return "\n\n".join(sections)


def _render_json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


__all__ = [
    "PromptBuilderError",
    "AGENT_SPEC_FILES",
    "OUTPUT_FORMAT_INSTRUCTIONS",
    "DEPENDENCY_AUTOMATION_INSTRUCTIONS",
    "DATA_CONTRACT_INSTRUCTIONS",
    "UI_CONTRACT_INSTRUCTIONS",
    "UI_FIDELITY_SPEC",
    "DATA_CONTRACT_SPEC",
    "DATA_CONTRACT_SPEC_AGENTS",
    "UI_FIDELITY_SPEC_AGENTS",
    "DEFAULT_MEMORY_LIMIT",
    "DEFAULT_CONTEXT_FILE_LIMIT",
    "framework_specs_dir",
    "spec_path",
    "load_agent_spec",
    "truncate_middle",
    "render_system_prompt",
    "build_prompt",
]
