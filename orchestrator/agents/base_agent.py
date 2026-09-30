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
from typing import Any, Callable, Dict, List, Optional, Type

from .. import config
from ..context_monitor import payload_chars
from ..state_manager import StateManager, load_text_file, utc_now_iso


# JSON only allows \" \\ \/ \b \f \n \r \t \uXXXX — anything else (e.g. a
# literal backslash in a path, or a \u with non-hex digits) must be
# escaped before parsing.
_INVALID_JSON_ESCAPE = re.compile(r'\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})')
_CONTROL_ESCAPES = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}


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
        "once (include a few surrounding lines to make it unique). Use one "
        "mechanism per file.\n"
        "- Replying with a JSON summary only (no documents/edits content) does NOT "
        "deliver the file: the orchestrator wraps such output as-is and the task is "
        "not really done.\n"
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
        # B4: always surface decisions gating this task and its REQ traceability.
        decisions = self._decisions_affecting(str(task.get("id") or ""))
        if decisions:
            context["decisions_affecting_task"] = decisions
        requirements = self._requirements_for(task)
        if requirements:
            context["requirements"] = requirements
        return context

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

    def run(self, task: Dict[str, Any], materialize: bool = True) -> AgentOutput:
        """Build the payload, execute, and never let exceptions escape.

        ``materialize=False`` suppresses artifact emission — used by the
        independent review pass so a reviewer never overwrites the artifact
        produced by the task's own agent.
        """
        task_id = str(task.get("id", "UNKNOWN")) if isinstance(task, dict) else "UNKNOWN"
        self._current_task_id = task_id
        self.last_payload_chars = 0
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
                if not isinstance(spec, dict):
                    raise ValueError(
                        f"edits[{rel!r}] must be an object with 'search' and 'replace'"
                    )
                search = spec.get("search")
                replace = spec.get("replace")
                if not isinstance(search, str) or not search or not isinstance(replace, str):
                    raise ValueError(
                        f"edits[{rel!r}] needs a non-empty string 'search' and a string 'replace'"
                    )
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.append(str(exc))
                return
            matches = content.count(search)
            if matches != 1:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.append(
                    f"edits[{rel!r}] search matched {matches} time(s) (need exactly 1)"
                )
                return
            updated = content.replace(search, replace, 1)
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
        written: List[str] = []
        try:
            docs_dir.mkdir(parents=True, exist_ok=True)
            for raw_name in expected:
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                filename = Path(raw_name.strip()).name
                if filename in ("", ".", ".."):
                    continue
                content = documents.get(raw_name)
                if not isinstance(content, str) or not content.strip():
                    content = documents.get(filename)
                if not isinstance(content, str) or not content.strip():
                    content = self.render_artifact(filename, task, output)
                save_text_file(docs_dir / filename, content)
                written.append(f"docs/{filename}")
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
