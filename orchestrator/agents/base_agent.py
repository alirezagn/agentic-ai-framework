"""BaseAgent — the parent interface every specialist agent implements.

Payload contract (input)::

    {
      "system_rules": str,      # global operating rules for the agent
      "project_memory": str,    # PROJECT_MEMORY.md contents
      "task": dict,             # the TASKS.yaml entry being executed
      "context": dict,          # extra files / notes relevant to the task
      "thresholds": dict,       # loop + compaction thresholds
    }

Structural output contract (returned by :meth:`BaseAgent.run`) is an
:class:`AgentOutput` that is always serialisable, always carries the task id,
and never contains free-form-only results: every agent must populate
``status``, ``summary`` and a structured ``data`` dictionary.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
)

from .. import config
from ..context_monitor import payload_chars
from ..import_contract import import_contract_problems
from ..prompt_builder import (
    DATA_CONTRACT_INSTRUCTIONS,
    DEPENDENCY_AUTOMATION_INSTRUCTIONS,
    UI_CONTRACT_INSTRUCTIONS,
)
from ..state_manager import StateManager, load_text_file, utc_now_iso

import hmac
import hashlib

logger = logging.getLogger(__name__)


# JSON only allows \" \\ \/ \b \f \n \r \t \uXXXX — anything else (e.g. a
# literal backslash in a path, or a \u with non-hex digits) must be
# escaped before parsing.
_INVALID_JSON_ESCAPE = re.compile(r'\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})')
_CONTROL_ESCAPES = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}
# Existing expected outputs are inlined into the payload so the model can
# quote exact search snippets (NUM_CTX=16384 tokens ≈ 60 KB): per-file and
# total budgets keep the whole prompt inside the window.
_EXPECTED_CONTEXT_CAP = 32_000
_EXPECTED_TOTAL_CAP = 40_000
# Current bodies injected into edit-session feedback after apply errors.
_EDITS_FEEDBACK_CAP = 12_000

def verify_hmac_signature(payload: bytes, signature: str | None, secret_key: str | None) -> bool:
    """Fails safely if key or signature is missing."""
    if not secret_key or not signature:
        return False
        
    expected_sig = hmac.new(
        secret_key.encode('utf-8'), 
        payload, 
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected_sig, signature)

def shares_meaningful_line(left: Path, right: Path) -> bool:
    """True when both files plausibly contain the same content.

    Primary signal: a shared line of at least 12 characters (short brace/JSON
    lines cannot fake an overlap between a prose wrapper and source code).
    Files too short to contain any such line fall back to sharing ANY
    non-empty line, so tiny post-edit files still pass. Unreadable files
    return True (the existence check already covers them).
    """

    def lines(path: Path) -> set:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
        return {line.strip() for line in text.splitlines() if line.strip()}

    left_lines = lines(left)
    right_lines = lines(right)
    left_long = {line for line in left_lines if len(line) >= 12}
    right_long = {line for line in right_lines if len(line) >= 12}
    if left_long & right_long:
        return True
    if not left_long and not right_long:
        return bool(left_lines & right_lines)
    return False


def preexisting_expected(project_path: Path, task: Dict[str, Any]) -> Set[str]:
    """Snapshot taken once per dispatch: which expected outputs already exist.

    The set is threaded through every delivery check (edit session, materialize,
    DoD) so files the task itself creates later are never mistaken for
    pre-existing project files — otherwise re-delivering a file the session
    just created turns into a permanent rejection loop.
    """
    project = Path(project_path)
    snapshot: Set[str] = set()
    for raw in task.get("expected_outputs") or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        name = raw.strip()
        target = Path(name)
        if not target.is_absolute():
            target = project / name
        if target.is_file():
            snapshot.add(name)
    return snapshot


def delivery_problems(
    project_path: Path,
    task: Dict[str, Any],
    output: Optional["AgentOutput"] = None,
    preexisting: Optional[Collection[str]] = None,
) -> List[str]:
    """Single source of truth for delivery checks (DoD + edit sessions).

    For every ``expected_outputs`` entry:
    - the ``docs/`` mirror must exist;
    - when the expected path already exists in the project (and is not a
      ``docs/``-relative target), the mirror must share real content with it
      — a summary/prose JSON wrapper shares no line and is rejected;
    - carrying a file that existed when the task started through
      ``data.documents`` (which never modifies pre-existing real files) is
      rejected outright: such files are changed only with ``data.edits``.
    - a missing non-``docs/`` expected output is a problem: ``data.documents``
      creates missing expected files at their real path, so by delivery time
      the real file must exist — a ``docs/`` mirror alone is not a delivery.

    Independently of ``expected_outputs``, every file this task delivered is
    also run through :func:`orchestrator.import_contract.import_contract_problems`:
    an import that names a producer function the producer does not define, or
    a file that does not parse, blocks the delivery too — the static half of
    ``INTERFACE_ALIGNMENT_CONTRACT``, so a guessed interface fails here with
    the exact statement instead of failing in the operator's terminal.

    ``preexisting`` is the task-start snapshot (see
    :func:`preexisting_expected`); when ``None`` the current filesystem state
    is used as the snapshot.
    """
    problems: List[str] = []
    project = Path(project_path)
    data: Dict[str, Any] = {}
    if output is not None and isinstance(output.data, dict):
        # Re-read through the normalizer: outputs built outside from_dict
        # (tests, recovery shims) may still carry dotted/list channel shapes.
        data = normalize_delivery_data(output.data)
    documents = data.get("documents") if isinstance(data.get("documents"), dict) else {}
    edits = data.get("edits") if isinstance(data.get("edits"), dict) else {}
    applied = data.get("edits_applied") if isinstance(data.get("edits_applied"), list) else []
    delivered = set(documents) | set(edits) | set(applied)
    problems.extend(import_contract_problems(project, delivered))

    expected = task.get("expected_outputs") or []
    if not isinstance(expected, list):
        return problems
    docs_dir = project / "docs"
    preexisting_set = set(preexisting) if preexisting is not None else None

    def has_channel(name: str, filename: str) -> bool:
        return name in delivered or filename in delivered

    for raw_name in expected:
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name.strip()
        filename = Path(name).name
        mirror = docs_dir / filename
        if not mirror.exists():
            problems.append(f"expected output not materialized: {filename}")
            continue
        project_file = project / name
        if name.split("/")[0] == "docs" or mirror.resolve() == project_file.resolve():
            continue  # docs/ target: the mirror IS the deliverable
        try:
            inside = project_file.resolve().is_relative_to(project.resolve())
        except OSError:
            inside = False
        if not inside:
            continue
        if not project_file.is_file():
            # Dispatch-context checks (snapshot provided) require the real
            # file whenever the output delivered real content for it — a
            # docs/ mirror alone is not a delivery (the TASK-004 pattern).
            # Content hidden by a key-shape variant (dotted ``data.documents``,
            # list-form records) is folded by ``normalize_delivery_data``
            # before this runs, so those deliveries count as visible too.
            # Outputs with no delivered content (rendered report artifacts)
            # keep the legacy mirror-only acceptance.
            if (
                preexisting_set is not None
                and name not in preexisting_set
                and has_channel(name, filename)
            ):
                problems.append(
                    f"expected output missing from the project: {name} — "
                    "data.documents creates a missing expected file at its "
                    "real path; a docs/ mirror alone does not deliver it"
                )
            continue
        existed_before = (
            name in preexisting_set
            if preexisting_set is not None
            else True
        )
        if not existed_before:
            continue  # created by this task: documents may deliver/update it
        if has_channel(name, filename) and not (
            (name in edits or filename in edits or name in applied or filename in applied)
        ):
            problems.append(
                f"{name} exists in the project — update it with data.edits; "
                "data.documents never modifies files that existed when the "
                "task started"
            )
            continue
        if not shares_meaningful_line(project_file, mirror):
            problems.append(
                f"delivered docs/{filename} shares no line with existing "
                f"{name} — summary/prose metadata does not deliver the "
                "file; use data.edits (search/replace) or the full file "
                "content"
            )
    return problems


def _repair_json_candidate(candidate: str) -> Optional[Dict[str, Any]]:
    r"""Best-effort parse of LLM JSON with common escape mistakes.

    Fixes invalid backslash escapes (``docs\config.md``, broken ``\u`` forms)
    and bare control characters (raw newlines) inside strings. Returns
    ``None`` when the candidate cannot be salvaged.
    """
    repaired = _INVALID_JSON_ESCAPE.sub(r"\\\\", candidate)
    try:
        value = json.loads(repaired)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    out: List[str] = []
    in_string = False
    escaped = False
    for char in repaired:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            out.append(char)
            continue
        if in_string and ord(char) < 0x20:
            out.append(_CONTROL_ESCAPES.get(char, f"\\u{ord(char):04x}"))
            continue
        out.append(char)
    try:
        value = json.loads("".join(out))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


class AgentError(RuntimeError):
    """Base error raised inside agents."""


class AgentOutputError(AgentError):
    """The agent produced output that could not be parsed or validated."""


def _strip_data_prefix(key: Any) -> Tuple[str, bool]:
    """Normalise one payload key, reporting whether it was dotted.

    Models sometimes emit channel names literally — ``"data.documents"``
    inside ``data`` (or even at the payload top level) instead of nesting
    under the ``data`` object. Every reader looks up ``data["documents"]``,
    so a dotted key hides the delivery: the docs/ mirror gets rendered but
    the real file is never written and no delivery channel is visible.
    """
    text = str(key)
    dotted = False
    while text.startswith("data."):
        text = text[len("data.") :]
        dotted = True
    return text, dotted


def _record_path(record: Mapping[str, Any]) -> str:
    for field_name in ("path", "file", "filename", "target"):
        value = record.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _documents_from_records(records: Sequence[Any]) -> Dict[str, str]:
    """``data.documents`` as a list of ``{path, content}`` records → dict.

    The list form shows up in truncation recovery (``data.documents[0]``
    carries its own ``path`` field) and from models that mirror the JSON
    examples as arrays; materialization and the delivery checks only
    understand the keyed-dict shape, so the list must be folded or the
    content is invisible there too.
    """
    out: Dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        path = _record_path(record)
        if not path or path in out:
            continue
        content = record.get("content")
        if not isinstance(content, str):
            content = record.get("text") if isinstance(record.get("text"), str) else None
        if content is None:
            continue
        out[path] = content
    return out


def _edits_from_records(records: Sequence[Any]) -> Dict[str, Any]:
    """``data.edits`` as a list of step records → the keyed dict shape.

    Accepts ``{path, search, replace}`` step records (repeated paths become
    an ordered step list) and ``{path, steps: [...]}`` bundles, matching the
    shapes :meth:`BaseAgent._apply_edits` already understands once the
    records are keyed by path.
    """
    out: Dict[str, Any] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        path = _record_path(record)
        if not path:
            continue
        raw_spec = record.get("steps")
        if not isinstance(raw_spec, list) or not raw_spec:
            if "search" in record or "replace" in record:
                raw_spec = [
                    {
                        "search": record.get("search"),
                        "replace": record.get("replace"),
                    }
                ]
            elif isinstance(record.get("content"), str):
                raw_spec = [{"search": "", "replace": record["content"]}]
            else:
                continue
        steps = [step for step in raw_spec if isinstance(step, dict)]
        if not steps:
            continue
        if path not in out:
            out[path] = steps if len(steps) > 1 else steps[0]
        else:
            existing = out[path]
            existing_list = existing if isinstance(existing, list) else [existing]
            out[path] = existing_list + steps
    return out


def normalize_delivery_data(data: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Fold model key-shape variants into the channels every reader expects.

    Two silent-loss shapes produced by real model runs:

    * dotted keys — ``data = {"data.documents": {"src/x.py": "..."}}`` (or
      the flat payload-level form ``{"data.documents": ...}`` handled by
      :meth:`AgentOutput.from_dict`), which no reader ever sees;
    * ``documents``/``edits`` as lists of path records instead of the
      keyed dicts the contract shows.

    Properly-named keys always win over dotted duplicates; only missing
    entries are filled in from the dotted variant.
    """
    items = list((data or {}).items())
    plain: Dict[str, Any] = {}
    dotted: Dict[str, Any] = {}
    for raw_key, value in items:
        key, was_dotted = _strip_data_prefix(raw_key)
        if not key:
            continue
        if not was_dotted:
            plain[key] = value
            continue
        if key not in dotted:
            dotted[key] = value
            continue
        existing = dotted[key]
        if isinstance(existing, dict) and isinstance(value, dict):
            dotted[key] = {**value, **existing}
    merged = dict(plain)
    for key, value in dotted.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = {**value, **merged[key]}
    documents = merged.get("documents")
    if isinstance(documents, list):
        merged["documents"] = _documents_from_records(documents)
    edits = merged.get("edits")
    if isinstance(edits, list):
        merged["edits"] = _edits_from_records(edits)
    return merged


@dataclass
class AgentOutput:
    """Structural output returned by every agent execution."""

    agent_id: str
    task_id: str
    status: str
    summary: str
    data: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    produced_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.status not in config.AGENT_STATUSES:
            raise AgentOutputError(
                f"Invalid agent status '{self.status}'. Allowed: {', '.join(config.AGENT_STATUSES)}"
            )

    @property
    def succeeded(self) -> bool:
        return self.status == config.AGENT_STATUS_COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "artifacts": list(self.artifacts),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "produced_at": self.produced_at,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AgentOutput":
        if not isinstance(payload, dict):
            raise AgentOutputError("Agent output payload must be a dictionary")
        # Single choke point for model-shaped payloads: fold dotted
        # ``data.*`` keys (payload level and inside ``data``) and list-form
        # channels into the shapes materialization/DoD read. Without this a
        # delivery under a dotted key renders only the docs/ wrapper — the
        # real file is never written and the channel reads as empty, which
        # used to let the task pass as DONE with the expected output missing.
        data = dict(payload.get("data") or {})
        flat: Dict[str, Any] = {}
        for raw_key, value in payload.items():
            key, was_dotted = _strip_data_prefix(raw_key)
            if not was_dotted or not key:
                continue
            if key in data or key in flat:
                continue
            flat[key] = value
        if flat:
            flat.update(data)
            data = flat
        return cls(
            agent_id=str(payload.get("agent_id", "")),
            task_id=str(payload.get("task_id", "")),
            status=str(payload.get("status", "")),
            summary=str(payload.get("summary", "")),
            data=normalize_delivery_data(data),
            artifacts=[str(item) for item in (payload.get("artifacts") or [])],
            errors=[str(item) for item in (payload.get("errors") or [])],
            warnings=[str(item) for item in (payload.get("warnings") or [])],
            produced_at=str(payload.get("produced_at") or utc_now_iso()),
        )

#: Schema rules for the ``data.deploy`` channel (GAP-HIGH-04), appended to
#: :attr:`BaseAgent.AUTHORING_CONTRACT` so every agent receives them.
#:
#: The channel exists because the framework must be able to tell a real
#: verification from an invented one: the runner — not the model — stamps
#: ``executed`` and the exit code, and the Definition of Done requires a
#: matching record before a task may be marked DONE. Without these rules an
#: agent has no way to *ask* for execution, and its only option is to assert
#: results it never obtained.
DATA_DEPLOY_CONTRACT = (
    "Execution contract — how to request that something actually run:\n"
    "- To have a build, test or tool really executed, return a LIST of "
    'invocation objects in data.deploy. Each entry: {"command": "<binary or '
    './project/script.sh>", "args": ["<arg>", ...], "cwd": "<optional, '
    'project-relative>", "expect": "PASS" | "FAIL", "rationale": "<one line>"}.\n'
    "- The runner executes it OUTSIDE your session, with a scrubbed environment, "
    "a forced project-root working directory, a timeout, and a captured output "
    "cap. It returns the real exit code and output; you never decide whether it "
    "ran.\n"
    "- `command` must be on the operator's executable allowlist. Anything else is "
    'refused before the process is created. Check data.delivery_manifest and the '
    "task notes for the permitted set; a refused command is reported back to you "
    "with a reason.\n"
    "- `expect` is your prediction, and it is CHECKED against reality. A mismatch "
    "blocks completion just like a failure does — state what you honestly expect, "
    "not what would look best.\n"
    "- `args` is a list of plain arguments, never a shell string. Shell syntax "
    '("; rm -rf /", "&&", ">", pipes) is passed through as inert text and will '
    "not do what you intended.\n"
    "- Request execution EARLY, before writing your summary: results come back in "
    'data.deploy_results with {executed, exit_code, stdout_tail, stderr_tail}, '
    "and a transcript is saved under docs/evidence/<task>/.\n"
    "- If execution is unavailable or refused, that is a legitimate finding: set "
    'data.test_status = "NOT RUN" and say what you could not verify and why. An '
    "honest NOT RUN completes the task on its other merits.\n"
    "- NEVER report a result you did not obtain. Claiming a pass with no matching "
    "executed record is a contract violation: the task is rejected and the claim "
    "is recorded as the reason."
)


#: Mandatory entry point for any runnable application (GAP: unrunnable projects).
#:
#: Observed in ``projects/sys_mon_gui``: three modules, each internally correct
#: and each individually green under test, and no way to run them. The only
#: thing that made the application real was a ``main.py`` written by hand
#: afterwards, importing every module, adapting shapes and starting the loop.
#:
#: The gap is structural, not stylistic: nothing in a per-task deliverable list
#: says "and something has to tie these together", so a multi-module project
#: arrives as modules. Every task can pass its own Definition of Done and the
#: result still cannot be started.
ENTRYPOINT_CONTRACT = (
    "Entry point contract — a project with an application must be runnable:\n"
    "- If the task delivers runnable components (a GUI, a CLI, a service, a "
    "long-running loop), you MUST also deliver a main.py at the PROJECT ROOT. "
    "It is not optional polish: without it the delivered modules cannot be "
    "started, and a reviewer has no way to observe the thing you built.\n"
    "- main.py must import EVERY module the task delivers, instantiate their "
    "dependencies in the correct order, convert shapes at the crossing, and "
    "enter the run loop (root.mainloop(), the CLI dispatch, the serve loop).\n"
    "- Handle argv minimally but honestly (a --help path or an explicit "
    "entrypoint function), and keep all side effects inside a main() guarded by "
    "if __name__ == \"__main__\": so importing main.py in a test does not start a "
    "window or block the process.\n"
    "- Add main.py to expected_outputs so the Definition of Done checks it "
    "exists and actually runs, rather than being assumed.\n"
    "- Deliver it even when every module already exists and looks complete. "
    "That is precisely the state where a project is unrunnable."
)

#: Interface discipline when writing a consumer against an existing producer.
#:
#: The companion failure to a missing entry point: a consumer written without
#: reading the code it calls. In ``sys_mon_gui`` the adapter had to guess between
#: `ram["percent_used"]` and `ram["percent"]` and paper over it with
#: ``.get("percent_used", .get("percent", 0.0))`` -- a fallback chain that hides
#: the mismatch instead of resolving it, and that silently yields 0.0 forever if
#: the real key ever changes. Guessing is what turns a type error into a wrong
#: number displayed on screen.
INTERFACE_ALIGNMENT_CONTRACT = (
    "Interface alignment contract — read the producer before writing the "
    "consumer:\n"
    "- Before generating ANY module that imports another, you MUST read the "
    "existing file(s) it will call, in the payload context or on disk, and match "
    "what you call VERBATIM: the method name and its capitalisation, the "
    "positional argument order, the keyword names, and the exact structure of "
    "the return value.\n"
    "- Never call a method you have not seen defined in a file you have read, "
    "and never invent a parameter name. An assumed signature is a TypeError at "
    "best and wrong data at worst.\n"
    "- Match the producer's actual return SHAPE, including nesting and field "
    "names -- a nested \"ram\": {\"percent_used\": 50}} consumed as a flat float, "
    "or a flat metrics[\"ram\"] read as a mapping, is the error. Read the "
    "keys; do not infer them from the domain.\n"
    "- Do NOT wrap an uncertain call in a defensive fallback chain such as "
    "`.get(a, .get(b, 0.0))`. That converts a loud TypeError into a silent wrong "
    "value that renders as 0.0 forever. If a shape is genuinely ambiguous, read "
    "the file again or report it as a finding -- do not guess quietly.\n"
    "- When the producer and the consumer cannot both change, adapt once, in "
    "one named conversion function, and say in the code comment which producer "
    "field maps to which consumer field.\n"
    "- Every import you deliver is statically verified at delivery time: the "
    "checker parses your files and confirms each intra-project name exists in "
    "the module that exports it, and that the file parses at all. A guessed "
    "name fails the Definition of Done with the exact import statement and the "
    "names the producer really defines — that failure comes back to you as one "
    "repair round, so read the file instead of inferring from the domain."
)


class BaseAgent:
    AGENT_ID = "base_agent"

    # Non-negotiable directives applied to EVERY agent, ahead of and separately
    # from any subclass rules. Kept as its own block (rather than folded into
    # SYSTEM_RULES) because SYSTEM_RULES is overridden by every subclass, so
    # anything placed there is shadowed for all 10 registered agents — which
    # is exactly how rule 6 (data.acceptance_results) and rule 4 came to be
    # specified but never delivered. Only system_rules() reads this attribute,
    # so it is structurally unreachable by a subclass that replaces SYSTEM_RULES.
    GLOBAL_SYSTEM_RULES = (
        "CRITICAL SYSTEM DIRECTIVES (override nothing, apply to every task):\n"
        "1. NEVER fabricate execution results, test outcomes, or measured numbers. "
        "A status you cannot evidence must be reported as NOT RUN or UNKNOWN, never "
        "as PASS.\n"
        "2. A task is DONE only when its deliverable exists AND you can point to the "
        "evidence for it. Claiming DONE without verifiable ground truth is a "
        "contract violation, not a shortcut.\n"
        "3. Missing inputs, absent files, or unavailable hardware are FINDINGS to "
        "report as UNKNOWN/TBD — never a reason to refuse. The only valid block is "
        "a pending human decision in DECISIONS.md.\n"
        "4. Always report acceptance evidence: for every acceptance criterion you "
        "checked, emit data.acceptance_results = [{name, status: PASS|FAIL, detail}]. "
        "A non-passing entry blocks completion, so an honest FAIL is always better "
        "than an omitted field."
    )
    # Human-readable operating rules injected into every payload.
    SYSTEM_RULES = (
        "1. Project files are the source of truth; chat history is temporary.\n"
        "2. Do not silently change approved architecture or decisions.\n"
        "3. Stop repeating the same failing strategy after the configured retry limit.\n"
        "4. Report measurable results only; vague claims are rejected.\n"
        "5. Never mark your own significant work DONE without independent review."
        # The acceptance-evidence requirement (previously rule 6 here) now lives
        # in GLOBAL_SYSTEM_RULES, because this block is overridden by every
        # subclass and was therefore never reaching any model.
    )
    # Appended to every agent's rules. Prevents the fabricated-refusal class:
    # agents claiming "missing input file", "no physical hardware" or "no
    # datasheet" as reasons to return blocked/failed instead of completing.
    OFFLINE_EXECUTION_RULE = (
        "Execution mode — offline: the payload is your complete working set; there is "
        "no physical hardware, network access, or files beyond context. Missing "
        "evidence (datasheets, board access, absent or empty input files) is itself a "
        "FINDING: record it as UNKNOWN/TBD in data.warnings and CONTINUE to a "
        "completed output. Never return blocked or failed because an input is "
        "missing — the only valid block is a pending human decision recorded in "
        "DECISIONS.md."
    )

    AUTHORING_CONTRACT = (
        "Authoring contract — how to deliver file changes:\n"
        "- Every expected_outputs entry is a file this task must deliver. For a new "
        "or short file (up to ~150 lines), return its FULL content in "
        'data.documents["<expected output path>"] using the exact expected output '
        "string as the key.\n"
        "- To modify an existing large file, return a targeted edit instead: "
        'data.edits["<path>"] = {"search": "<exact text currently in the file>", '
        '"replace": "<replacement text>"} — the search snippet must appear exactly '
        "once (include a few surrounding lines to make it unique). For several "
        'disjoint changes in one file, pass a LIST of {search, replace} objects — '
        "they are applied in order. Use one mechanism per file.\n"
        "- Replying with a JSON summary only (no documents/edits content) does NOT "
        "deliver the file: the orchestrator wraps such output as-is and the task is "
        "not really done.\n"
        "- Never echo whole input files back — overrunning the output token limit "
        "truncates the JSON and fails the task; use the smallest unique "
        "data.edits snippet instead.\n"
        "- The payload's delivery_manifest entry states, per expected output, "
        "whether the file exists and which channel is required: EXISTS -> "
        "data.edits only; MISSING -> data.documents only. Follow it exactly.\n"
        "- data.documents writes a MISSING expected file to its real project "
        "path (and the docs/ mirror); it never modifies a file that existed "
        "when the task started — use data.edits for those.\n"
        "- Never invent executed results: report test or verification statuses as "
        "NOT RUN unless the payload contains real execution output.\n"
        + DATA_DEPLOY_CONTRACT
        # Single source of truth: the same constant the prompt templates embed
        # and that tests/test_dependency_automation.py asserts on. Restated
        # here because this contract is what every agent's system_rules()
        # actually delivers, and a dependency announced in prose rather than
        # installed is what leaves the next turn unable to run the tests.
        + DEPENDENCY_AUTOMATION_INSTRUCTIONS
        # Same reasoning for cross-component data shapes: an agent that returns
        # a nested mapping while its consumer expects a flat scalar is not
        # violating anything it was told, because nothing told it. This block is
        # what every agent's system_rules() delivers, so it is the only place
        # the obligation reaches a model at all.
        + DATA_CONTRACT_INSTRUCTIONS
        # Mandatory entry point and interface discipline. Both live here, in the
        # block every agent's system_rules() actually delivers, because a
        # deliverable-list-shaped task never asks for either one.
        + ENTRYPOINT_CONTRACT
        + INTERFACE_ALIGNMENT_CONTRACT
        # The third leg of the same problem: a runnable, schema-correct app that
        # is still a bare default widget understates the task invisibly. Kept as
        # a separate block so the entrypoint/interface rules above stay readable,
        # and so the concrete visual checklist can be injected per agent.
        + UI_CONTRACT_INSTRUCTIONS
    )

    def __init__(
        self,
        project_path: Optional[str | Path] = None,
        state_manager: Optional[StateManager] = None,
        extra_rules: Optional[List[str]] = None,
    ) -> None:
        if state_manager is not None:
            self.state_manager = state_manager
            self.project_path = state_manager.project_path
        elif project_path is not None:
            self.state_manager = StateManager(project_path)
            self.project_path = self.state_manager.project_path
        else:
            raise AgentError(f"Agent '{self.AGENT_ID}' requires a project_path or state_manager")
        self.extra_rules = list(extra_rules or [])
        self.last_payload_chars = 0

    # ------------------------------------------------------------------
    # Payload construction
    # ------------------------------------------------------------------

    def system_rules(self) -> str:
        """Compose the full rule set sent to the model.

        Order is significant: the global directives come first so a subclass
        rule cannot dilute them, then the agent's own rules, then the two
        shared contracts, then per-instance extras.

        ``GLOBAL_SYSTEM_RULES`` and ``OFFLINE_EXECUTION_RULE`` / ``AUTHORING_CONTRACT``
        are read from ``self`` but assigned only on this class. A subclass that
        overrides ``SYSTEM_RULES`` — which all ten registered agents do — extends
        rather than shadows them. Empty entries are dropped so a subclass with no
        rules of its own still receives the base directives.
        """
        rules = [
            self.GLOBAL_SYSTEM_RULES,
            self.SYSTEM_RULES,
            self.OFFLINE_EXECUTION_RULE,
            self.AUTHORING_CONTRACT,
        ]
        rules.extend(self.extra_rules)
        return "\n\n".join(rule.strip() for rule in rules if rule and rule.strip())

    def relevant_context(self, task: Dict[str, Any]) -> Dict[str, str]:
        """Load only the files the task explicitly asks for."""
        context: Dict[str, str] = {}
        input_files = task.get("input_files") or []
        for name in input_files:
            file_path = Path(name)
            if not file_path.is_absolute():
                file_path = self.project_path / name
            if file_path.exists() and file_path.is_file():
                context[str(name)] = load_text_file(file_path)
        notes = task.get("notes")
        if notes:
            context["task_notes"] = str(notes)
        # Feedback loop: why the previous attempt failed must be salient in
        # the prompt — retry --reason and DoD/agent errors land here.
        execution = task.get("execution") if isinstance(task.get("execution"), dict) else {}
        feedback = execution.get("retry_reason") or execution.get("last_error")
        if feedback:
            context["recovery_feedback_from_previous_attempt"] = str(feedback)
        # B4: always surface decisions gating this task and its REQ traceability.
        decisions = self._decisions_affecting(str(task.get("id") or ""))
        if decisions:
            context["decisions_affecting_task"] = decisions
        requirements = self._requirements_for(task)
        if requirements:
            context["requirements"] = requirements
        # G22: existing expected outputs the agent must edit or verify —
        # without their content the model writes blind search snippets and
        # `data.edits` fails with "search matched 0 time(s)" (TASK-003).
        budget = _EXPECTED_TOTAL_CAP
        for raw in task.get("expected_outputs") or []:
            if budget <= 0:
                break
            if not isinstance(raw, str) or not raw.strip():
                continue
            name = raw.strip()
            if name in context or f"expected_output:{name}" in context:
                continue
            target = Path(name)
            if not target.is_absolute():
                target = self.project_path / name
            if not target.is_file():
                continue  # missing files: the manifest already says how to create them
            try:
                body = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            limit = min(_EXPECTED_CONTEXT_CAP, budget)
            if len(body) > limit:
                content = body[:limit] + (
                    f"\n…[truncated at {limit} chars — quote an exact "
                    "snippet from the shown portion]"
                )
            else:
                content = body
            budget -= len(content)
            context[f"expected_output:{name}"] = content
        # G22: existence facts BEFORE generation — the model must never have
        # to guess the delivery channel (that guessing failed repeatedly).
        manifest = self._delivery_manifest(task)
        if manifest:
            context["delivery_manifest"] = manifest
        return context

    def _delivery_manifest(self, task: Dict[str, Any]) -> str:
        """Per-expected-output channel instruction, checked at payload build."""
        lines: List[str] = []
        project = self.project_path
        for raw in task.get("expected_outputs") or []:
            if not isinstance(raw, str) or not raw.strip():
                continue
            name = raw.strip()
            if name.split("/")[0] == "docs":
                lines.append(
                    f"- {name}: docs deliverable — write its full body with data.documents"
                )
                continue
            target = Path(name)
            if not target.is_absolute():
                target = project / name
            if target.is_file():
                lines.append(
                    f"- {name}: EXISTS — a real project file: update it with "
                    "data.edits (delivery via data.documents will be rejected)"
                )
            else:
                lines.append(
                    f"- {name}: MISSING — create it with data.documents "
                    "(full content under that exact key; it is written to "
                    "the real path)"
                )
        if not lines:
            return ""
        return (
            "Delivery manifest (filesystem checked now — follow exactly):\n"
            + "\n".join(lines)
        )

    def _decisions_affecting(self, task_id: str) -> str:
        """DECISIONS.md entries whose Affected Tasks include ``task_id``."""
        if not task_id:
            return ""
        try:
            content = self.state_manager.load_decisions()
            entries = self.state_manager.list_decisions()
        except Exception:  # noqa: BLE001 — context injection must never fail a run
            return ""
        lines = content.splitlines()
        chunks: List[str] = []
        for entry in entries:
            if task_id not in (entry.get("affected_tasks") or []):
                continue
            start = int(entry.get("line", 0))
            end = int(entry.get("end", start))
            if entry.get("format") == "section" and end > start:
                chunk = "\n".join(lines[start:end]).strip()
            elif 0 <= start < len(lines):
                chunk = lines[start].strip()
            else:
                continue
            if chunk:
                chunks.append(chunk)
        return "\n\n".join(chunks)

    def _requirements_for(self, task: Dict[str, Any]) -> str:
        """Lines from docs/REQUIREMENTS.md covering the task's declared REQ ids."""
        declared = task.get("requirement_ids")
        if not isinstance(declared, list) or not declared:
            return ""
        req_file = Path(self.project_path) / "docs" / "REQUIREMENTS.md"
        if not req_file.exists():
            return ""
        try:
            body = load_text_file(req_file)
        except OSError:
            return ""
        picked: List[str] = []
        for raw_id in declared:
            req_id = str(raw_id).strip()
            if not req_id:
                continue
            for line in body.splitlines():
                if req_id in line and line.strip() not in picked:
                    picked.append(line.strip())
        return "\n".join(picked)

    def build_payload(self, task: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(task, dict):
            raise AgentError(f"Task payload must be a dictionary, got {type(task).__name__}")
        task_id = task.get("id")
        if not task_id:
            raise AgentError("Task payload is missing an 'id'")

        memory = ""
        try:
            memory = self.state_manager.load_memory()
        except Exception:
            memory = ""

        return {
            "agent_id": self.AGENT_ID,
            "system_rules": self.system_rules(),
            "project_memory": memory,
            "task": dict(task),
            "context": self.relevant_context(task),
            "thresholds": {
                "loop": config.loop_thresholds().as_dict(),
                "compaction": config.compaction_thresholds().as_dict(),
            },
            # GAP-CRIT-04: a task flagged for review carries the structural
            # problems the Definition of Done already found. Hoisted out of
            # `task` so the reviewer reads them as a pre-verdict it must weigh,
            # rather than as another field of the work description.
            "pending_dod_problems": [
                str(item) for item in (task.get("pending_dod_problems") or [])
            ],
            "built_at": utc_now_iso(),
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        """Override in subclasses. Must return an AgentOutput."""
        raise NotImplementedError(
            f"Agent '{self.AGENT_ID}' must implement execute()"
        )

    def repair_delivery(
        self,
        task: Dict[str, Any],
        output: AgentOutput,
        problems: List[str],
    ) -> Optional[AgentOutput]:
        """One automatic re-attempt after a Definition-of-Done rejection.

        Deterministic agents have nothing to re-ask; LLM agents override this
        to send the DoD problems back to the model once. Returns None when no
        usable repair is available (callers keep the original failure).
        """
        return None

    def harvest_findings(
        self, task: Dict[str, Any], output: AgentOutput
    ) -> List[str]:
        """Persist analysis/findings from a non-compliant turn (best-effort).

        Called by the orchestrator when the Definition of Done rejected the
        turn or the output failed schema validation. The model may still have
        produced genuine research/audit findings inside ``data`` — they are
        appended (and de-duplicated) to ``docs/findings/<task_id>.md``, and
        risk-shaped entries in ``data["risks"]`` are mirrored into RISKS.md,
        so a rejected dispatch never discards the analysis it did produce.

        Returns the written locations (``docs/findings/...`` plus risk ids).
        The orchestrator wraps this call, so failures degrade to a log line.
        """
        from ..state_manager import save_text_file

        if not isinstance(output, AgentOutput):
            return []
        task_id = str(task.get("id") or output.task_id or "UNKNOWN").strip() or "UNKNOWN"
        data = output.data if isinstance(output.data, dict) else {}

        def finding_lines(value: Any) -> List[str]:
            """Render a string / list-of-(str|dict) finding into markdown lines."""
            if isinstance(value, str):
                text = value.strip()
                return [text] if text else []
            if isinstance(value, list):
                lines: List[str] = []
                for item in value:
                    if isinstance(item, str) and item.strip():
                        lines.append(f"- {item.strip()}")
                    elif isinstance(item, dict):
                        title = str(item.get("title") or item.get("name") or "").strip()
                        detail = str(
                            item.get("description") or item.get("detail") or ""
                        ).strip()
                        body = f"{title}: {detail}" if title and detail else title or detail
                        if body:
                            lines.append(f"- {body}")
                return lines
            return []

        written: List[str] = []
        sections: List[str] = []
        finding_lines_out = finding_lines(data.get("findings"))
        if finding_lines_out:
            sections.append("## Findings\n\n" + "\n".join(finding_lines_out))
        analysis_lines = finding_lines(data.get("analysis"))
        if analysis_lines:
            sections.append("## Analysis\n\n" + "\n".join(analysis_lines))
        if sections:
            section_text = "\n\n".join(sections)
            entry = (
                f"\n\n---\n\n### Turn {output.produced_at} "
                f"({output.agent_id}, status={output.status})\n\n"
                "Harvested after a Definition-of-Done/schema rejection.\n\n"
                + section_text
                + "\n"
            )[:80_000]
            target = self.docs_dir() / "findings" / f"{task_id}.md"
            existing = ""
            try:
                if target.exists():
                    existing = load_text_file(target)
            except OSError:
                existing = ""
            # Dedupe on the stable section text — the turn header carries a
            # fresh timestamp, so an identical re-rejection must not append.
            if section_text.strip() and section_text.strip() not in existing:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if existing:
                        save_text_file(target, existing + entry)
                    else:
                        save_text_file(target, f"# Findings — {task_id}\n{entry}")
                    written.append(f"docs/findings/{task_id}.md")
                except OSError:
                    logger.debug(
                        "could not write findings file for %s", task_id, exc_info=True
                    )

        risk_items = data.get("risks") if isinstance(data.get("risks"), list) else []
        if risk_items:
            try:
                known_titles = {
                    str(risk.get("title") or "").strip()
                    for risk in self.state_manager.list_risks()
                }
                for item in list(risk_items)[:5]:
                    if isinstance(item, dict):
                        title = str(
                            item.get("title") or item.get("risk") or ""
                        ).strip()
                        description = str(
                            item.get("description") or item.get("detail") or ""
                        ).strip()
                        probability = str(item.get("probability") or "MEDIUM").strip().upper()
                        impact = str(item.get("impact") or "MEDIUM").strip().upper()
                    elif isinstance(item, str):
                        title = item.strip()
                        description = ""
                        probability, impact = "MEDIUM", "MEDIUM"
                    else:
                        continue
                    if not title:
                        continue
                    full_title = f"{task_id}: {title}"
                    if full_title in known_titles:
                        continue
                    record = self.state_manager.append_risk(
                        title=full_title,
                        probability=probability or "MEDIUM",
                        impact=impact or "MEDIUM",
                        description=description
                        or f"Harvested from the rejected {task_id} turn.",
                        mitigation=(
                            "Verify before relying on this finding — the turn that "
                            "produced it did not pass the Definition of Done."
                        ),
                        related_tasks=[task_id],
                        owner=str(output.agent_id or self.AGENT_ID),
                    )
                    known_titles.add(full_title)
                    risk_id = str(record.get("id") or "").strip()
                    if risk_id:
                        written.append(risk_id)
            except Exception:  # noqa: BLE001 — risk mirroring is best-effort
                logger.debug(
                    "could not mirror harvested risks for %s", task_id, exc_info=True
                )
        return written

    def run(self, task: Dict[str, Any], materialize: bool = True) -> AgentOutput:
        """Build the payload, execute, and never let exceptions escape.

        ``materialize=False`` suppresses artifact emission — used by the
        independent review pass so a reviewer never overwrites the artifact
        produced by the task's own agent.
        """
        task_id = str(task.get("id", "UNKNOWN")) if isinstance(task, dict) else "UNKNOWN"
        self._current_task_id = task_id
        self.last_payload_chars = 0
        # G22: one task-start snapshot for all delivery checks of this attempt.
        try:
            self._delivery_snapshot = preexisting_expected(self.project_path, task)
        except Exception:  # noqa: BLE001 — snapshot must never break a run
            self._delivery_snapshot = set()
        try:
            payload = self.build_payload(task)
            self.last_payload_chars = payload_chars(payload)
            output = self.execute(payload)
            if not isinstance(output, AgentOutput):
                raise AgentOutputError(
                    f"Agent '{self.AGENT_ID}' returned {type(output).__name__}, expected AgentOutput"
                )
            problems = self.validate_output(output)
            if problems:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.extend(problems)
            if materialize and output.status == config.AGENT_STATUS_COMPLETED:
                self._apply_edits(task, output)
            if materialize and output.status == config.AGENT_STATUS_COMPLETED:
                self._materialize_artifacts(task, output)
            return output
        except Exception as exc:  # noqa: BLE001 - agents must never crash the orchestrator
            return AgentOutput(
                agent_id=self.AGENT_ID,
                task_id=task_id,
                status=config.AGENT_STATUS_FAILED,
                summary=f"Agent '{self.AGENT_ID}' raised {type(exc).__name__}: {exc}",
                errors=[
                    str(exc),
                    traceback.format_exc(limit=8),
                ],
            )

    # ------------------------------------------------------------------
    # Artifact emission
    # ------------------------------------------------------------------

    def docs_dir(self) -> Path:
        """Directory where task artifacts are materialized."""
        return self.project_path / "docs"

    @staticmethod
    def render_artifact(name: str, task: Dict[str, Any], output: "AgentOutput") -> str:
        """Deterministic Markdown document for one expected output."""
        import json as _json

        lines = [
            f"# {name}",
            "",
            f"- Task: `{task.get('id', '')}` — {task.get('title', '')}",
            f"- Owner: {task.get('owner', '')}",
            f"- Agent: {output.agent_id}",
            f"- Status: {output.status}",
            f"- Produced: {output.produced_at}",
            "",
            "## Summary",
            "",
            output.summary or "(no summary)",
        ]
        if output.data:
            lines += [
                "",
                "## Data",
                "",
                "```json",
                _json.dumps(output.data, indent=2, ensure_ascii=False, default=str),
                "```",
            ]
        if output.errors:
            lines += ["", "## Errors", ""] + [f"- {item}" for item in output.errors]
        if output.warnings:
            lines += ["", "## Warnings", ""] + [f"- {item}" for item in output.warnings]
        lines.append("")
        return "\n".join(lines)

    #: How many touched files one turn's feedback may quote — a wide
    #: delivery must not turn the turn budget into a prompt bomb.
    _TOUCHED_FILES_FEEDBACK_MAX = 8

    def _touched_files_current_content(
        self, output: AgentOutput, *, intro: str = "Copy the exact search "
        "snippet from the current content below"
    ) -> str:
        """Feedback block: on-disk body of every file this output touched.

        Covers the three channels through which a session turn changes a
        real file: ``data.edits`` targets, ``data.edits_applied`` edits and
        ``data.documents`` creations. Missing, absolute and ``..`` paths are
        skipped; each body is capped at ``_EDITS_FEEDBACK_CAP`` and the block
        at ``_TOUCHED_FILES_FEEDBACK_MAX`` files.

        Why this exists: edit-session turns are stateless — each turn is a
        fresh prompt with only the feedback string swapped in, and the base
        prompt is frozen at session start, when expected outputs were still
        MISSING (``relevant_context`` skips absent files). From turn 2 on the
        model therefore cannot see what it just delivered; without the bodies
        it must guess search snippets for files it cannot see and burns its
        turns. sys-usage TASK-003 failed all three turns unable to fix a
        broken import in a file it had itself written one turn earlier.
        """
        data = output.data if isinstance(output.data, dict) else {}
        ordered: List[str] = []
        seen = set()
        for key in ("edits", "edits_applied", "documents"):
            value = data.get(key)
            if isinstance(value, dict):
                items: Iterable[Any] = value.keys()
            elif isinstance(value, list):
                items = value
            else:
                continue
            for raw in items:
                rel = str(raw).strip()
                target = Path(rel)
                if not rel or target.is_absolute() or ".." in target.parts:
                    continue
                if rel in seen:
                    continue
                seen.add(rel)
                ordered.append(rel)
        quoted: List[str] = []
        omitted = 0
        for rel in ordered:
            real = self.project_path / rel
            if not real.is_file():
                continue
            if len(quoted) >= self._TOUCHED_FILES_FEEDBACK_MAX:
                omitted += 1
                continue
            try:
                body = real.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(body) > _EDITS_FEEDBACK_CAP:
                body = body[:_EDITS_FEEDBACK_CAP] + "\n…[truncated]"
            quoted.append(f"current content of {rel} (authoritative):\n{body}")
        if not quoted:
            return ""
        text = f"\n{intro}:\n" + "\n\n".join(quoted)
        if omitted:
            text += f"\n…[{omitted} more touched file(s) omitted]"
        return text

    def _apply_edits(self, task: Dict[str, Any], output: AgentOutput) -> None:
        """Apply ``data.edits`` search/replace patches to real project files.

        The post-edit content is mirrored into ``data.documents`` so the
        normal ``docs/`` materialization (and the DoD existence check) sees
        the actual file body instead of a fallback wrapper. Any problem
        (path escape, ambiguous search, unreadable file) fails the output with
        a precise error instead of silently delivering a stub.

        When there is nothing to patch, the edit is a full write rather than a
        failure. Two cases, because both are requests whose intent is
        unambiguous:

        * ``search`` is empty, absent or ``null`` — the caller is describing
          the whole file, so ``replace`` becomes the entire content. This also
          works on a file that *does* exist, which makes it a deliberate reset.
        * the target is missing or empty — there is no content to search in and
          nothing to overwrite, so the first step's ``replace`` is written as a
          creation. Subsequent steps then patch that content normally, so a
          later ambiguous search is still reported honestly instead of being
          papered over.

        A missing target used to raise ``FileNotFoundError`` and an empty
        ``search`` a ``ValueError``, either of which failed the whole dispatch
        for content the agent had in fact delivered.
        """
        # Fold dotted/list-form channels first: a list-form data.edits would
        # otherwise read as "no edits" and the delivered content dropped.
        output.data = normalize_delivery_data(output.data)
        edits = output.data.get("edits")
        if not isinstance(edits, dict) or not edits:
            return
        from ..state_manager import save_text_file

        documents = output.data.setdefault("documents", {})
        if not isinstance(documents, dict):
            documents = {}
            output.data["documents"] = documents
        project_root = self.project_path.resolve()
        for raw_name, spec in edits.items():
            rel = str(raw_name).strip()
            target = Path(rel)
            if not rel or target.is_absolute() or ".." in target.parts:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.append(f"edits path must be project-relative: {raw_name!r}")
                return
            resolved = project_root / target
            try:
                resolved = resolved.resolve()
                if not resolved.is_relative_to(project_root):
                    raise OSError(f"path escapes project: {rel}")
                steps = spec if isinstance(spec, list) else [spec]
                if not steps:
                    raise ValueError(f"edits[{rel!r}] is an empty list")
                parsed: List[Dict[str, str]] = []
                for index, step in enumerate(steps):
                    if not isinstance(step, dict):
                        raise ValueError(
                            f"edits[{rel!r}][{index}] must be an object with 'search' and 'replace'"
                        )
                    search = step.get("search")
                    replace = step.get("replace")
                    # An absent or null 'search' is a request to write the file
                    # rather than patch it, and is treated exactly like
                    # search="". Rejecting it outright used to fail the whole
                    # dispatch for a request with an obvious intent.
                    if search is None:
                        search = ""
                    if not isinstance(search, str):
                        raise ValueError(
                            f"edits[{rel!r}][{index}] needs a string 'search' "
                            "(empty means a full write) and a string 'replace'"
                        )
                    if not isinstance(replace, str):
                        raise ValueError(
                            f"edits[{rel!r}][{index}] needs a string 'replace'"
                        )
                    parsed.append({"search": search, "replace": replace})
                # A missing file reads as empty instead of raising: there is
                # nothing to patch, so the edit is a creation.
                existed = resolved.is_file()
                content = resolved.read_text(encoding="utf-8") if existed else ""
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.append(str(exc))
                return
            updated = content
            # True only while real content exists to patch. An empty or absent
            # file offers nothing to search in and has nothing to lose, so
            # writing over it is the honest reading of the edit -- previously a
            # "matched 0 times" failure that discarded the delivered content.
            patchable = bool(content)
            for index, step in enumerate(parsed):
                if not step["search"] or not patchable:
                    updated = step["replace"]
                    patchable = True
                    continue
                matches = updated.count(step["search"])
                if matches != 1:
                    output.status = config.AGENT_STATUS_FAILED
                    output.errors.append(
                        f"edits[{rel!r}][{index}] search matched {matches} time(s) "
                        "(need exactly 1)"
                    )
                    return
                updated = updated.replace(step["search"], step["replace"], 1)
            try:
                # A creation may name a path whose parent does not exist yet;
                # the atomic writer needs that directory to stage its temp file.
                if not resolved.parent.is_dir():
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                save_text_file(resolved, updated)
            except OSError as exc:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.append(f"edits[{rel!r}] write failed: {exc}")
                return
            # The old code failed this output outright; the whole point of the
            # graceful path is that delivered content is not discarded. Record
            # it instead, so a patch aimed at a file that was not there stays
            # visible (a mistyped path is the likely cause).
            if not existed or not content:
                output.warnings.append(
                    f"edits[{rel!r}] created {'missing' if not existed else 'empty'} "
                    f"file with {len(parsed)} step(s); the requested content was "
                    "written rather than patched into existing text"
                )
            documents.setdefault(rel, updated)
            output.artifacts.append(rel)

    def _materialize_artifacts(self, task: Dict[str, Any], output: AgentOutput) -> None:
        """Write every expected output of the task into ``docs/``."""
        from ..state_manager import save_text_file

        expected = task.get("expected_outputs") or []
        if not isinstance(expected, list) or not expected:
            return
        # Fold dotted/list-form channels so delivered content is written to
        # the real path instead of degrading into a rendered docs/ wrapper.
        output.data = normalize_delivery_data(output.data)
        documents = output.data.get("documents")
        documents = documents if isinstance(documents, dict) else {}
        docs_dir = self.docs_dir()
        snapshot = getattr(self, "_delivery_snapshot", None)
        if snapshot is None:
            snapshot = preexisting_expected(self.project_path, task)
        project_root = self.project_path.resolve()
        written: List[str] = []
        try:
            docs_dir.mkdir(parents=True, exist_ok=True)
            for raw_name in expected:
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                name = raw_name.strip()
                filename = Path(name).name
                if filename in ("", ".", ".."):
                    continue
                content = documents.get(name)
                if not isinstance(content, str) or not content.strip():
                    content = documents.get(filename)
                if not isinstance(content, str) or not content.strip():
                    # Models sometimes flatten file bodies onto data itself
                    # instead of nesting them under data.documents.
                    content = output.data.get(name)
                if not isinstance(content, str) or not content.strip():
                    content = output.data.get(filename)
                delivered = isinstance(content, str) and bool(content.strip())
                if not delivered:
                    content = self.render_artifact(filename, task, output)
                save_text_file(docs_dir / filename, content)
                written.append(f"docs/{filename}")
                # G22: documents is the create channel for files that were
                # missing at task start — write the real path too, but only
                # from actually-delivered content (never a rendered wrapper)
                # and never for files that pre-existed (data.edits owns them).
                if not delivered or name.split("/")[0] == "docs" or name in snapshot:
                    continue
                real_target = Path(name)
                if real_target.is_absolute() or ".." in real_target.parts:
                    continue
                try:
                    real_path = (project_root / real_target).resolve()
                    if not real_path.is_relative_to(project_root):
                        continue
                    # The atomic writer stages a temp file next to the
                    # target; a first delivery into a fresh subdirectory
                    # (src/ on a new project) needs the directory created
                    # first, exactly like _apply_edits does.
                    if not real_path.parent.is_dir():
                        real_path.parent.mkdir(parents=True, exist_ok=True)
                    save_text_file(real_path, content)
                except OSError:
                    continue
        except OSError as exc:
            output.status = config.AGENT_STATUS_FAILED
            output.errors.append(f"Artifact emission failed: {exc}")
            return
        if written:
            merged = set(output.artifacts)
            merged.update(written)
            output.artifacts = sorted(merged)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def extract_json_block(text: str) -> Dict[str, Any]:
        """Extract the first JSON object found in ``text``.

        Fenced ```json blocks are preferred; otherwise the first balanced
        ``{...}`` region is parsed.
        """
        if not text or not text.strip():
            raise AgentOutputError("Cannot parse structured output from empty text")

        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
        candidates: List[str] = []
        if fenced:
            candidates.append(fenced.group(1))

        start = text.find("{")
        first_start = start
        outer_closed = False
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start : index + 1])
                        if start == first_start:
                            outer_closed = True
                        break
            start = text.find("{", start + 1)
            if len(candidates) > 20:
                break

        if fenced is None and first_start != -1 and not outer_closed:
            # A truncated reply (output-token budget exhausted) leaves the
            # outer object open; inner fragments would parse as bogus dicts
            # and surface as confusing downstream validation errors.
            tail = text.rstrip()[-70:]
            raise AgentOutputError(
                "agent output looks truncated (outer JSON object never "
                f"closed; tail: ...{tail!r})"
            )

        errors: List[str] = []
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                # LLM replies often embed literal backslashes (paths like
                # docs\config.md) or raw newlines — repair instead of failing.
                salvaged = _repair_json_candidate(candidate)
                if salvaged is None:
                    errors.append(str(exc))
                    continue
                parsed = salvaged
            if isinstance(parsed, dict):
                return parsed
            errors.append("JSON block is not an object")
        raise AgentOutputError(
            "No parseable JSON object in agent output: " + ("; ".join(errors) or "no JSON found")
        )

    @staticmethod
    def parse_structured_output(text: str) -> Dict[str, Any]:
        return BaseAgent.extract_json_block(text)

    def validate_output(self, output: AgentOutput) -> List[str]:
        """Return structural problems (empty list means valid)."""
        problems: List[str] = []
        if output.agent_id and output.agent_id != self.AGENT_ID:
            problems.append(
                f"Output agent_id '{output.agent_id}' does not match executor '{self.AGENT_ID}'"
            )
        expected_task_id = str(getattr(self, "_current_task_id", "") or "")
        if expected_task_id and output.task_id and output.task_id != expected_task_id:
            problems.append(
                f"Output task_id '{output.task_id}' does not match executed task '{expected_task_id}'"
            )
        if not output.summary.strip():
            problems.append("Output summary must not be empty")
        if not isinstance(output.data, dict):
            problems.append("Output data must be a dictionary")
        return problems

    def completed(
        self,
        task_id: str,
        summary: str,
        data: Optional[Dict[str, Any]] = None,
        artifacts: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
    ) -> AgentOutput:
        return AgentOutput(
            agent_id=self.AGENT_ID,
            task_id=str(task_id),
            status=config.AGENT_STATUS_COMPLETED,
            summary=summary,
            data=dict(data or {}),
            artifacts=list(artifacts or []),
            warnings=list(warnings or []),
        )

    def failed(self, task_id: str, summary: str, errors: Optional[List[str]] = None) -> AgentOutput:
        return AgentOutput(
            agent_id=self.AGENT_ID,
            task_id=str(task_id),
            status=config.AGENT_STATUS_FAILED,
            summary=summary,
            errors=list(errors or []),
        )

    def blocked(self, task_id: str, summary: str, errors: Optional[List[str]] = None) -> AgentOutput:
        return AgentOutput(
            agent_id=self.AGENT_ID,
            task_id=str(task_id),
            status=config.AGENT_STATUS_BLOCKED,
            summary=summary,
            errors=list(errors or []),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"{type(self).__name__}(project_path={str(self.project_path)!r})"


# ---------------------------------------------------------------------------
# Agent registry / factory
# ---------------------------------------------------------------------------

AGENT_REGISTRY: Dict[str, Type[BaseAgent]] = {}


def register_agent(name: Optional[str] = None) -> Callable[[Type[BaseAgent]], Type[BaseAgent]]:
    """Class decorator that adds an agent class to the global registry."""

    def decorator(cls: Type[BaseAgent]) -> Type[BaseAgent]:
        key = (name or cls.AGENT_ID).lower()
        AGENT_REGISTRY[key] = cls
        return cls

    return decorator


def normalize_agent_name(name: str) -> str:
    return str(name).strip().lower()


def create_agent(name: str, project_path: Optional[str | Path] = None,
                 state_manager: Optional[StateManager] = None) -> BaseAgent:
    """Instantiate a registered agent by name (case-insensitive)."""
    key = normalize_agent_name(name)
    if key not in AGENT_REGISTRY:
        known = ", ".join(sorted(AGENT_REGISTRY)) or "[none registered]"
        raise AgentError(f"No agent registered under '{name}'. Known agents: {known}")
    cls = AGENT_REGISTRY[key]
    if state_manager is not None:
        return cls(state_manager=state_manager)
    if project_path is None:
        raise AgentError(f"create_agent('{name}') requires a project_path or state_manager")
    return cls(project_path=project_path)


def agent_names() -> List[str]:
    return sorted(AGENT_REGISTRY)


__all__ = [
    "AgentError",
    "AgentOutputError",
    "AgentOutput",
    "BaseAgent",
    "AGENT_REGISTRY",
    "register_agent",
    "create_agent",
    "agent_names",
    "normalize_agent_name",
]
