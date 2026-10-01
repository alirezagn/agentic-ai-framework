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
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Dict, List, Optional, Set, Type

from .. import config
from ..context_monitor import payload_chars
from ..state_manager import StateManager, load_text_file, utc_now_iso


# JSON only allows \" \\ \/ \b \f \n \r \t \uXXXX — anything else (e.g. a
# literal backslash in a path, or a \u with non-hex digits) must be
# escaped before parsing.
_INVALID_JSON_ESCAPE = re.compile(r'\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})')
_CONTROL_ESCAPES = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}


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

    ``preexisting`` is the task-start snapshot (see
    :func:`preexisting_expected`); when ``None`` the current filesystem state
    is used as the snapshot.
    """
    problems: List[str] = []
    expected = task.get("expected_outputs") or []
    if not isinstance(expected, list):
        return problems
    project = Path(project_path)
    docs_dir = project / "docs"
    preexisting_set = set(preexisting) if preexisting is not None else None
    data: Dict[str, Any] = {}
    if output is not None and isinstance(output.data, dict):
        data = output.data
    documents = data.get("documents") if isinstance(data.get("documents"), dict) else {}
    edits = data.get("edits") if isinstance(data.get("edits"), dict) else {}
    applied = data.get("edits_applied") if isinstance(data.get("edits_applied"), list) else []
    delivered = set(documents) | set(edits) | set(applied)

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
        return cls(
            agent_id=str(payload.get("agent_id", "")),
            task_id=str(payload.get("task_id", "")),
            status=str(payload.get("status", "")),
            summary=str(payload.get("summary", "")),
            data=dict(payload.get("data") or {}),
            artifacts=[str(item) for item in (payload.get("artifacts") or [])],
            errors=[str(item) for item in (payload.get("errors") or [])],
            warnings=[str(item) for item in (payload.get("warnings") or [])],
            produced_at=str(payload.get("produced_at") or utc_now_iso()),
        )


class BaseAgent:
    """Parent class: builds payloads, executes, parses and validates output."""

    AGENT_ID = "base_agent"
    # Human-readable operating rules injected into every payload.
    SYSTEM_RULES = (
        "1. Project files are the source of truth; chat history is temporary.\n"
        "2. Do not silently change approved architecture or decisions.\n"
        "3. Stop repeating the same failing strategy after the configured retry limit.\n"
        "4. Report measurable results only; vague claims are rejected.\n"
        "5. Never mark your own significant work DONE without independent review.\n"
        "6. Report acceptance evidence: for every acceptance criterion you verified "
        "return data.acceptance_results = [{name, status: PASS|FAIL, detail}]; "
        "failed checks block completion."
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
        "NOT RUN unless the payload contains real execution output."
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
        rules = [self.SYSTEM_RULES, self.OFFLINE_EXECUTION_RULE, self.AUTHORING_CONTRACT]
        for rule in self.extra_rules:
            rules.append(rule)
        return "\n".join(rules)

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

    def _apply_edits(self, task: Dict[str, Any], output: AgentOutput) -> None:
        """Apply ``data.edits`` search/replace patches to real project files.

        The post-edit content is mirrored into ``data.documents`` so the
        normal ``docs/`` materialization (and the DoD existence check) sees
        the actual file body instead of a fallback wrapper. Any problem
        (path escape, missing/ambiguous search, unreadable file) fails the
        output with a precise error instead of silently delivering a stub.
        """
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
                if not resolved.is_file():
                    raise FileNotFoundError(f"edits target does not exist: {rel}")
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
                    if not isinstance(search, str) or not search or not isinstance(replace, str):
                        raise ValueError(
                            f"edits[{rel!r}][{index}] needs a non-empty string "
                            "'search' and a string 'replace'"
                        )
                    parsed.append({"search": search, "replace": replace})
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.append(str(exc))
                return
            updated = content
            for index, step in enumerate(parsed):
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
                save_text_file(resolved, updated)
            except OSError as exc:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.append(f"edits[{rel!r}] write failed: {exc}")
                return
            documents.setdefault(rel, updated)
            output.artifacts.append(rel)

    def _materialize_artifacts(self, task: Dict[str, Any], output: AgentOutput) -> None:
        """Write every expected output of the task into ``docs/``."""
        from ..state_manager import save_text_file

        expected = task.get("expected_outputs") or []
        if not isinstance(expected, list) or not expected:
            return
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
