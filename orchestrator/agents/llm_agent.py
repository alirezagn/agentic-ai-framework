"""LLMAgent — the execution path shared by every prompt-driven specialist.

Pipeline (IMPLEMENTATION_ROADMAP "Approach 2")::

    payload -> prompt_builder.build_prompt (+ framework/XX_AGENT.md spec)
            -> llm_client.complete(...)
            -> BaseAgent.parse_structured_output (extract_json_block)
            -> AgentOutput (validated)

Subclasses only declare ``AGENT_ID`` and ``SYSTEM_RULES``; everything else is
inherited. Clients are injectable so tests run fully offline::

    agent = ArchitectureAgent(state_manager=sm, llm_client=FakeLLMClient(...))
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple, Union

from .. import config
from ..llm_client import LLMClient, LLMError, LLMResult
from ..prompt_builder import build_prompt, load_agent_spec, render_system_prompt
from .base_agent import (
    AgentOutput,
    AgentOutputError,
    BaseAgent,
    _repair_json_candidate,
    delivery_problems,
    preexisting_expected,
)

logger = logging.getLogger(__name__)

# --- DoD repair remedies ---------------------------------------------------
#
# One generic repair note used to be sent for *every* DoD rejection, and it
# always claimed the rejection was about missing file content
# ("It was valid JSON but did not deliver real file content ... patch the
# project files"). For an evidence rejection — "report test_status=NOT
# RUN" — that instruction is not merely unhelpful, it is the wrong fix: the
# model dutifully re-delivers edits, never sets the field, the DoD rejects
# the repair for the same reason, and the task fails with a complaint about
# evidence it was never told how to satisfy in structured form. The remedy is
# therefore chosen from the problems themselves.

_EVIDENCE_MARKERS = ("test_status", "ground-truth", "ground truth", "nothing was executed")
_INTERFACE_MARKERS = (
    "does not define",
    "does not exist in the project",
    "does not parse",
    "no module exists at that relative path",
    "no such module",
)
_FILE_MARKERS = (
    "shares no line",
    "not materialized",
    "missing from the project",
    "update it with data.edits",
    "summary/prose",
    "expected output",
)

_REMEDY_EVIDENCE = (
    "This is an EVIDENCE problem, not a file problem: no test can run in this "
    "environment, so answer with a completed reply that sets the field the "
    "problem names, e.g. "
    '{"status": "completed", "summary": "<one line>", "data": {"test_status": '
    '"NOT RUN"}}. Keep the files exactly as they are — do not re-deliver them — '
    "and never claim a run you did not perform.\n"
)
_REMEDY_INTERFACE = (
    "This is an IMPORT problem: the producer file does not define a name the "
    "consumer imports. Read the producer file named in the problem and patch "
    "the consumer with data.edits (search/replace on the exact import line), "
    "or deliver the missing module.\n"
)
_REMEDY_FILE = (
    "It was valid JSON but did not deliver real file content: patch the "
    "project files with data.edits for files that already exist (never repeat "
    "whole files), or data.documents only for a brand-new file under 60 lines.\n"
)
_REMEDY_GENERIC = (
    "Address exactly what the problems say — patch files with data.edits, or "
    "set the fields they ask for (for example data.test_status).\n"
)


def repair_remedies(problems: Sequence[str]) -> str:
    """The remedy block for this rejection: one block per problem class."""
    text = [str(problem) for problem in problems]
    remedies: List[str] = []
    if any(any(marker in item for marker in _EVIDENCE_MARKERS) for item in text):
        remedies.append(_REMEDY_EVIDENCE)
    if any(any(marker in item for marker in _INTERFACE_MARKERS) for item in text):
        remedies.append(_REMEDY_INTERFACE)
    if any(any(marker in item for marker in _FILE_MARKERS) for item in text):
        remedies.append(_REMEDY_FILE)
    if not remedies:
        remedies.append(_REMEDY_GENERIC)
    return "".join(remedies)


class LLMAgent(BaseAgent):
    """Base class for agents that call a language model and parse JSON back."""

    # Optional overrides; when None the client discovers them from the env.
    PROVIDER: Optional[str] = None
    MODEL: Optional[str] = None

    # One-shot recovery when the model overruns the output token limit and
    # its JSON gets cut mid-string (typically while echoing whole files).
    REPAIR_NOTE = (
        "\n\nIMPORTANT: Your previous reply was cut off by the output token "
        "limit and could not be parsed. Reply again with ONLY one compact "
        "JSON object, under 500 tokens, containing NO file bodies and NO "
        "full code listings:\n"
        '{"status": "completed", "summary": "<one line>", "data": {"edits": '
        '{"<project-relative path>": {"search": "<shortest unique snippet>", '
        '"replace": "<replacement>"}}}}\n'
        "Express any change as data.edits with the smallest unique snippet "
        "(or data.documents only for a brand-new file under 60 lines). Never "
        "repeat whole files. If the work cannot be expressed compactly, reply "
        '{"status": "failed", "summary": "<reason>", "data": {}} instead.\n'
        "Head of your truncated attempt (for intent only, do not repeat it):\n"
    )

    # Sent when the Definition of Done rejects a parseable delivery. The
    # `{remedies}` block is chosen per problem class by repair_remedies():
    # an evidence rejection must be answered with data.test_status, an import
    # rejection with a patched consumer, a content rejection with file edits.
    # Sending the content remedy for an evidence rejection is what used to
    # make the repair round useless and the task fail anyway.
    DOD_REPAIR_NOTE = (
        "\n\nThe Definition of Done REJECTED your previous reply as a delivery:\n"
        "{problems}\n"
        "{remedies}"
        "Reply ONLY with one compact JSON object (under 600 tokens) that fixes "
        "the problems above:\n"
        '{"status": "completed", "summary": "<one line>", "data": {...}}\n'
        "Include every field the problems ask for. Do not reply with "
        "descriptions, scopes, or metadata.\n"
        "If you truly cannot deliver, reply "
        '{"status": "failed", "summary": "<why>", "data": {}} instead.'
    )

    # > 1 switches execute() from the single-shot JSON delivery to a bounded
    # multi-turn edit session: small change-set replies, deterministic apply
    # between turns, verification feedback fed back to the model.
    EDIT_SESSION_TURNS = 0

    EDIT_SESSION_INSTRUCTION = (
        "\n\n# Edit session — turn {turn} of {max}\n"
        "This task is delivered through a MULTI-TURN EDIT SESSION. Reply with "
        "ONE small JSON object containing ONLY the next change set — no file "
        "bodies, no retyping of content already applied, no prose:\n"
        '{"status": "completed", "summary": "<one line>", "data": {"edits": '
        '{"<project-relative path>": {"search": "<shortest unique snippet>", '
        '"replace": "<replacement>"}}}}\n'
        "Rules: change existing files ONLY through data.edits (search/replace, "
        "the snippet must match exactly once); create a NEW file through "
        "data.documents carrying the complete body (small files only); never "
        "repeat content that was already applied. When nothing more is needed "
        'reply with an empty "edits" object.\n'
        "Feedback from the previous turn:\n{feedback}\n"
    )

    def __init__(
        self,
        project_path: Optional[str | Path] = None,
        state_manager: Optional[Any] = None,
        extra_rules: Optional[List[str]] = None,
        llm_client: Optional[LLMClient] = None,
        specs_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        super().__init__(
            project_path=project_path,
            state_manager=state_manager,
            extra_rules=extra_rules,
        )
        self._llm_client = llm_client
        self.specs_dir = specs_dir

    # ------------------------------------------------------------------
    # Client access
    # ------------------------------------------------------------------

    def client(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = LLMClient(provider=self.PROVIDER, model=self.MODEL)
        return self._llm_client

    def use_client(self, client: LLMClient) -> LLMClient:
        self._llm_client = client
        return client

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def spec_text(self) -> str:
        return load_agent_spec(self.AGENT_ID, self.specs_dir)

    def execute(self, payload: Dict[str, Any]) -> AgentOutput:
        task = payload.get("task") or {}
        task_id = str(task.get("id", "")) if isinstance(task, dict) else ""
        if self.EDIT_SESSION_TURNS > 1 and isinstance(task, dict) and task_id:
            return self._execute_edit_session(payload, task, task_id)
        spec = self.spec_text()
        prompt = build_prompt(payload, agent_spec=spec)
        system = render_system_prompt(self.system_rules(), spec)

        try:
            result = self.client().complete(
                system=system, messages=[{"role": "user", "content": prompt}]
            )
        except LLMError as exc:
            message = f"LLM backend unavailable for '{self.AGENT_ID}': {exc}"
            return self.failed(task_id, message, errors=[message])

        try:
            parsed = self.parse_structured_output(result.text)
        except AgentOutputError as exc:
            salvaged = self._recover_truncated_output(result.text, task_id)
            if salvaged is not None:
                return salvaged
            recovered = self._repair_truncated_output(system, prompt, result.text, task_id)
            if recovered is not None:
                recovered.warnings.append(
                    "output recovered via truncation-repair (first reply was unparseable)"
                )
                return recovered
            return self.failed(
                task_id,
                f"Model output for '{self.AGENT_ID}' was not parseable JSON",
                errors=[str(exc), _snippet(result.text), "truncation-repair attempt also failed"],
            )

        try:
            return self.output_from_parsed(parsed, task_id=task_id, result=result)
        except AgentOutputError as exc:
            # Validation failures (e.g. a review missing data.review_status)
            # are model-output problems, not crashes: fail the task with the
            # offending snippet instead of raising a traceback through
            # dispatch.
            return self.failed(
                task_id,
                f"Model output for '{self.AGENT_ID}' failed validation",
                errors=[str(exc), _snippet(result.text)],
            )

    def _execute_edit_session(
        self, payload: Dict[str, Any], task: Dict[str, Any], task_id: str
    ) -> AgentOutput:
        """Bounded multi-turn delivery: small replies, apply, verify, feed back.

        Replaces the fragile single giant JSON: each turn asks only for the
        next change set, the deterministic applier patches the real files,
        ``delivery_problems`` verifies the workspace (not the JSON), and the
        results — apply errors or remaining DoD problems — go back to the
        model as turn feedback. At most ``EDIT_SESSION_TURNS`` model calls.
        """
        base_prompt = build_prompt(payload, agent_spec=self.spec_text())
        system = render_system_prompt(self.system_rules(), self.spec_text())
        max_turns = int(self.EDIT_SESSION_TURNS)
        snapshot = getattr(self, "_delivery_snapshot", None)
        if snapshot is None:
            snapshot = preexisting_expected(self.project_path, task)
        feedback = "(first turn — no previous feedback)"
        for turn in range(1, max_turns + 1):
            logger.info(
                "%s edit session turn %d/%d for %s",
                self.AGENT_ID, turn, max_turns, task_id,
            )
            prompt = (
                base_prompt
                + self.EDIT_SESSION_INSTRUCTION.replace("{turn}", str(turn))
                .replace("{max}", str(max_turns))
                .replace("{feedback}", feedback)
            )
            try:
                result = self.client().complete(
                    system=system, messages=[{"role": "user", "content": prompt}]
                )
            except LLMError as exc:
                message = f"LLM backend unavailable for '{self.AGENT_ID}': {exc}"
                return self.failed(task_id, message, errors=[message])

            output: Optional[AgentOutput] = None
            parse_errors: List[str] = []
            try:
                parsed = self.parse_structured_output(result.text)
                output = self.output_from_parsed(parsed, task_id=task_id, result=result)
            except AgentOutputError as exc:
                output = self._recover_truncated_output(result.text, task_id)
                if output is None:
                    output = self._repair_truncated_output(
                        system, prompt, result.text, task_id
                    )
                parse_errors = [str(exc), _snippet(result.text)]
            if output is None:
                return self.failed(
                    task_id,
                    f"Model output for '{self.AGENT_ID}' was not parseable JSON",
                    errors=parse_errors + ["truncation-repair attempt also failed"],
                )

            problems = self.validate_output(output)
            if problems:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.extend(problems)
            if output.status == config.AGENT_STATUS_BLOCKED:
                return output
            if output.status != config.AGENT_STATUS_COMPLETED:
                # apply/validation error or explicit give-up: feed it back
                # unless this was the last turn.
                if turn >= max_turns:
                    return output
                errs = list(output.errors) or [output.summary]
                feedback = "your previous reply failed:\n" + "\n".join(
                    f"- {err}" for err in errs[-4:]
                )
                continue

            self._apply_edits(task, output)
            if output.status == config.AGENT_STATUS_COMPLETED:
                applied = list((output.data.get("edits") or {}).keys())
                if applied:
                    # Consume the change set: run() must not re-apply it, and
                    # delivery_problems must know the real file was patched.
                    output.data["edits_applied"] = applied
                    output.data.pop("edits", None)
                self._materialize_artifacts(task, output)
            if output.status != config.AGENT_STATUS_COMPLETED:
                if turn >= max_turns:
                    return output
                errs = list(output.errors) or ["edit application failed"]
                feedback = (
                    "your edits could not be applied:\n"
                    + "\n".join(f"- {err}" for err in errs[-4:])
                    + self._edits_current_content(output)
                )
                continue

            delivery = delivery_problems(self.project_path, task, output, preexisting=snapshot)
            if not delivery:
                output.warnings.append(
                    f"delivered via {turn}-turn edit session (max {max_turns})"
                )
                return output
            if turn >= max_turns:
                output.status = config.AGENT_STATUS_FAILED
                output.errors.extend(delivery)
                return output
            feedback = (
                "your changes were applied; Definition-of-Done problems remain:\n"
                + "\n".join(f"- {item}" for item in delivery[:6])
            )
        return self.failed(  # unreachable: every loop path returns
            task_id, "edit session exhausted without a deliverable output"
        )

    def _recover_truncated_output(
        self, raw: str, task_id: str
    ) -> Optional[AgentOutput]:
        """Local chunked re-assembly of a reply the output cap cut off.

        Runs BEFORE the network truncation-repair: the repair note forbids
        file bodies, so its compact answer cannot carry the delivery that the
        truncated reply already contained — re-asking would throw the
        recovered work away. A cut that landed inside file content yields a
        BLOCKED output with the partial bodies staged in
        ``data.edit_buffers`` (an incomplete edit must never be applied);
        a re-assembled complete delivery keeps its parsed status.

        Returns ``None`` when nothing was recovered so the caller keeps the
        repair path and its original failure detail.
        """
        recovery = recover_truncated_payload(raw)
        if recovery is None:
            return None
        detail = [note for note in recovery.notes if note]
        if recovery.parsed is None:
            parsed: Dict[str, Any] = {
                "status": config.AGENT_STATUS_BLOCKED,
                "summary": _buffer_summary(recovery.buffers),
                "data": {EDIT_BUFFER_KEY: dict(recovery.buffers), "truncated": True},
                "warnings": detail
                + ["partial file content staged in data.edit_buffers (incomplete)"],
            }
            return self.output_from_parsed(parsed, task_id=task_id)

        parsed = dict(recovery.parsed)
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        data = dict(data)
        warnings = parsed.get("warnings")
        if isinstance(warnings, str):
            warnings = [warnings]
        elif not isinstance(warnings, list):
            warnings = []
        warnings = [str(item) for item in warnings]
        warnings.append("recovered from a truncated payload (local chunked parse)")
        if recovery.buffers:
            staged = dict(recovery.buffers)
            existing = data.get(EDIT_BUFFER_KEY)
            if isinstance(existing, dict):
                staged = {**staged, **existing}
            data[EDIT_BUFFER_KEY] = staged
            data["truncated"] = True
            parsed["status"] = config.AGENT_STATUS_BLOCKED
            parsed["summary"] = _buffer_summary(
                staged, model_summary=str(parsed.get("summary") or "")
            )
            warnings.append(
                "cut inside file content: partial bodies staged in "
                "data.edit_buffers, not applied"
            )
        warnings.extend(detail)
        parsed["data"] = data
        parsed["warnings"] = warnings
        return self.output_from_parsed(parsed, task_id=task_id)

    def _repair_truncated_output(
        self, system: str, prompt: str, raw: str, task_id: str
    ) -> Optional[AgentOutput]:
        """One compact follow-up after an unparseable/truncated reply.

        Resends the original prompt plus a repair note that caps the answer
        size (models truncate by echoing whole files; the repair forbids
        that). Returns None when the repair itself is unusable so callers
        keep the original failure detail.
        """
        note = self.REPAIR_NOTE + _snippet(raw or "", limit=400)
        try:
            result = self.client().complete(
                system=system, messages=[{"role": "user", "content": prompt + note}]
            )
        except LLMError:
            return None
        try:
            parsed = self.parse_structured_output(result.text)
            return self.output_from_parsed(parsed, task_id=task_id, result=result)
        except AgentOutputError:
            return None

    def repair_delivery(
        self,
        task: Dict[str, Any],
        output: AgentOutput,
        problems: List[str],
    ) -> Optional[AgentOutput]:
        """Send DoD rejection reasons back to the model for one re-attempt.

        Best-effort: any failure to produce a usable completed delivery
        (network, unparseable repair, still-empty content) returns None so
        dispatch keeps the original rejection.
        """
        task_id = str(task.get("id") or "")
        try:
            payload = self.build_payload(task)
            prompt = build_prompt(payload, agent_spec=self.spec_text())
            system = render_system_prompt(self.system_rules(), self.spec_text())
            bullets = "\n".join(f"- {problem}" for problem in list(problems)[:8])
            note = self.DOD_REPAIR_NOTE.replace("{problems}", bullets).replace(
                "{remedies}", repair_remedies(problems)
            )
            result = self.client().complete(
                system=system, messages=[{"role": "user", "content": prompt + note}]
            )
            parsed = self.parse_structured_output(result.text)
            repaired = self.output_from_parsed(parsed, task_id=task_id, result=result)
            if repaired.status != config.AGENT_STATUS_COMPLETED:
                return None
            self._apply_edits(task, repaired)
            if repaired.status != config.AGENT_STATUS_COMPLETED:
                return None
            self._materialize_artifacts(task, repaired)
            if repaired.status != config.AGENT_STATUS_COMPLETED:
                return None
        except Exception:  # noqa: BLE001 — repair is best-effort, never fatal
            return None
        repaired.warnings.append(
            "delivery repaired via DoD auto-repair (first reply was rejected)"
        )
        return repaired

    def output_from_parsed(
        self,
        parsed: Dict[str, Any],
        task_id: str = "",
        result: Optional[LLMResult] = None,
    ) -> AgentOutput:
        """Normalise a parsed model answer into a validated AgentOutput."""
        if not isinstance(parsed, dict):
            raise AgentOutputError("parsed model output must be a JSON object")
        parsed = dict(parsed)
        parsed["agent_id"] = self.AGENT_ID
        if task_id:
            # Force the executed task id: a model-invented task_id can never
            # pass validate_output (it must match the executed task), so
            # honoring it would only poison otherwise-good output.
            parsed["task_id"] = task_id
        parsed.setdefault("status", config.AGENT_STATUS_COMPLETED)
        parsed.setdefault("summary", "")
        data = parsed.get("data")
        parsed["data"] = data if isinstance(data, dict) else {}
        if result is not None and "usage" not in parsed["data"]:
            parsed["data"]["usage"] = {
                "provider": result.provider,
                "model": result.model,
                **result.usage,
            }
        return AgentOutput.from_dict(parsed)


def _buffer_summary(
    buffers: Dict[str, Dict[str, Any]], model_summary: str = ""
) -> str:
    paths = ", ".join(sorted(buffers)) or "unknown file"
    summary = (
        f"model output truncated; partial content for {len(buffers)} file(s) "
        f"staged in data.edit_buffers ({paths})"
    )
    if model_summary:
        summary += f"; model said: {model_summary[:120]}"
    return summary


def _snippet(text: str, limit: int = 400) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned or "[empty model output]"
    return cleaned[:limit] + "..."


# ---------------------------------------------------------------------------
# Truncation recovery — chunked re-assembly of a reply cut by the token cap
# ---------------------------------------------------------------------------

#: ``data`` keys that carry actual delivery (file content the Definition of
#: Done looks for). A recovered payload counts as content only when one of
#: these holds non-empty value — a recovered ``{"status": "completed",
#: "summary": "..."}`` is just a promise, not a delivery.
DELIVERY_KEYS: Tuple[str, ...] = ("edits", "documents")

#: Value keys that hold file bodies, used to recognise a cut that happened
#: *inside* file content (``... "replace": "half a fi``).
CONTENT_KEYS: FrozenSet[str] = frozenset({"search", "replace", "content"})

#: ``data`` key where partial file content is staged for a blocked task.
EDIT_BUFFER_KEY = "edit_buffers"

#: Fenced block languages whose body is JSON (never a file body to stage).
_JSON_LANGS: FrozenSet[str] = frozenset({"json", "jsonc", "json5", "jsonl"})

_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.DOTALL)

#: Hard cap on locally assembled candidates; the model's first reply is the
#: only input, so a runaway scan must not turn into unbounded work.
_MAX_CANDIDATES = 40

#: How many ``{`` positions of one reply may be scanned as the JSON root.
_MAX_JSON_STARTS = 6

#: Guess which file an unstaged ```python fence belongs to, from nearby
#: prose (``wrote src/app.py``` -> ``src/app.py``).
_PATH_IN_TEXT_RE = re.compile(r"(?<![\w./-])([\w./-]+\.[A-Za-z][A-Za-z0-9_]{0,9})")


@dataclass(frozen=True)
class TruncationRecovery:
    """What could be salvaged from one truncated model reply."""

    parsed: Optional[Dict[str, Any]] = None
    buffers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """True when the salvage carries a real delivery (file content)."""
        return _parsed_has_content(self.parsed)

    @property
    def has_delivery(self) -> bool:
        """True when there is anything worth staging or returning."""
        return bool(self.buffers) or self.has_content


def _parsed_has_content(parsed: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(parsed, dict):
        return False
    data = parsed.get("data")
    if not isinstance(data, dict):
        return False
    for key in DELIVERY_KEYS:
        value = data.get(key)
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, list) and value:
            return True
    return False


def _closers(kinds: Sequence[str]) -> str:
    return "".join(
        "}" if kind == "obj" else "]" for kind in reversed(list(kinds))
    )


def _close_open_string(text: str) -> str:
    r"""Terminate the string that was open when the output was cut.

    Drops a dangling escape (``...\`` or a partial ``\u12``) first so the
    result is lexically valid JSON rather than merely unterminated.
    """
    if text.endswith("\\") and not text.endswith("\\\\"):
        text = text[:-1]
    else:
        partial = re.search(r"\\u[0-9a-fA-F]{0,3}$", text)
        if partial:
            text = text[: partial.start()]
    return text + '"'


def _finish_value_boundary(text: str) -> str:
    """Trim a tail that stops between values (bare token / dangling key)."""
    for _ in range(4):
        text = text.rstrip()
        if text.endswith(","):
            text = text[:-1]
            continue
        if text.endswith(":"):
            text = text[:-1].rstrip()
            if text.endswith('"'):
                start = _string_open_index(text, len(text) - 1)
                if start >= 0:
                    text = text[:start]
            continue
        token = re.search(r"[A-Za-z0-9_.+\-eE]+$", text)
        if token:
            text = text[: token.start()]
            continue
        break
    return text.rstrip()


def _string_open_index(text: str, quote_index: int) -> int:
    """Offset of the ``"`` that opened the string ending at ``quote_index``."""
    pos = quote_index - 1
    backslashes = 0
    while pos >= 0 and text[pos] != '"':
        if text[pos] == "\\":
            backslashes += 1
        else:
            backslashes = 0
        pos -= 1
    if backslashes % 2:
        return -1  # the quote is escaped: not a string delimiter
    return pos


def _scan_json_tail(body: str) -> List[Tuple[str, Tuple[Any, ...], str]]:
    """Rebuild parseable variants of a JSON object cut off mid-flight.

    Walks the text once, tracking the container stack (so every cut point can
    be closed with the right number of ``}``/``]``), which key is being read,
    and whether the cut landed inside a *value* string — the common case,
    since models truncate while echoing file bodies.

    Returns candidate ``(text, cut_path, note)`` tuples in preference order:
    first the longest re-assembly, then progressively earlier cut points.
    ``cut_path`` is the JSON path of the value that was being written when
    the output stopped (``()`` when the cut was not inside a string).
    """
    frames: List[List[Any]] = []  # [kind, current_key, expects_key]
    path: List[Any] = []  # element each frame is stored under
    boundaries: List[Tuple[int, Tuple[str, ...]]] = []
    in_string = False
    escaped = False
    string_start = -1
    string_is_key = False
    string_path: Tuple[Any, ...] = ()
    index = 0
    total = len(body)

    while index < total:
        char = body[index]
        if in_string:
            if not escaped and char == "\\":
                escaped = True
            elif not escaped and char == '"':
                if string_is_key and frames:
                    # remember which key this frame is filling, so a cut
                    # inside its value can be located in the parsed tree
                    frames[-1][1] = body[string_start + 1 : index]
                in_string = False
                escaped = False
            else:
                escaped = False
            index += 1
            continue
        if char == '"':
            in_string = True
            string_start = index
            parent = frames[-1] if frames else None
            string_is_key = bool(parent and parent[0] == "obj" and parent[2])
            if string_is_key:
                string_path = ()
            else:
                current = parent[1] if parent else None
                string_path = tuple(path[1:]) + (current,)
            index += 1
            continue
        if char in "{[":
            element = frames[-1][1] if frames else None
            # an array counts elements from 0: ``current`` is the index of
            # the element being written, advanced on every comma
            frames.append(
                ["obj" if char == "{" else "arr", None if char == "{" else 0, char == "{"]
            )
            path.append(element)
            index += 1
            boundaries.append((index, tuple(frame[0] for frame in frames)))
            continue
        if char in "}]":
            if frames:
                frames.pop()
                path.pop()
            index += 1
            continue
        if char == ",":
            if frames:
                if frames[-1][0] == "obj":
                    frames[-1][1] = None
                    frames[-1][2] = True
                elif isinstance(frames[-1][1], int):
                    frames[-1][1] += 1
                else:
                    frames[-1][1] = 0
            index += 1
            boundaries.append((index, tuple(frame[0] for frame in frames)))
            continue
        if char == ":" and frames:
            frames[-1][2] = False
            index += 1
            continue
        index += 1

    closers = _closers(frame[0] for frame in frames)
    candidates: List[Tuple[str, Tuple[Any, ...], str]] = []

    if in_string:
        if string_is_key:
            candidates.append(
                (body[:string_start] + closers, (), "cut inside a JSON key")
            )
        else:
            where = ".".join(str(part) for part in string_path if part is not None)
            candidates.append(
                (
                    _close_open_string(body) + closers,
                    string_path,
                    f"cut inside the string at '{where}'" if where else "cut inside a string",
                )
            )
    else:
        stripped = body.rstrip()
        candidates.append((stripped + closers, (), "cut after a value"))
        trimmed = _finish_value_boundary(body)
        if trimmed and trimmed != stripped:
            candidates.append((trimmed + closers, (), "cut between values"))

    # Every earlier cut point (right after a `,` or an opening brace) closed
    # with the stack as it stood there — the fallback when the tail itself
    # cannot be repaired.
    for offset, kinds in reversed(boundaries):
        segment = body[:offset].rstrip()
        if segment.endswith(","):
            segment = segment[:-1].rstrip()
        if not segment:
            continue
        candidates.append((segment + _closers(kinds), (), "closed at an earlier cut"))
        if len(candidates) >= _MAX_CANDIDATES:
            break
    return candidates


def _json_candidates(raw: str) -> Iterator[Tuple[str, Tuple[Any, ...], str]]:
    """Every chunked-parse candidate in ``raw``, fenced JSON first.

    The global scan starts at the first ``{`` in the reply; when the reply
    wraps its JSON in a fence, scanning that fence's body directly comes
    first so a brace in the surrounding prose cannot hijack the scan.
    Non-JSON fences (file bodies) are Stage B, not JSON candidates.
    """
    seen: set = set()
    for match in _FENCE_RE.finditer(raw or ""):
        lang = (match.group(1) or "").strip().lower()
        body = match.group(2)
        if lang not in _JSON_LANGS and body.lstrip()[:1] != "{":
            continue
        start = body.find("{")
        if start < 0:
            continue
        for candidate in _scan_json_tail(body[start:]):
            if candidate[0] not in seen:
                seen.add(candidate[0])
                yield candidate
    start = (raw or "").find("{")
    if start < 0:
        return
    cursor = 0
    starts: List[int] = []
    # A brace in the surrounding prose must not hijack the scan: try each
    # opening brace in turn (the consumer stops at the first candidate that
    # parses with content, so the wasted work is bounded in practice).
    while len(starts) < _MAX_JSON_STARTS:
        position = (raw or "").find("{", cursor)
        if position < 0:
            break
        starts.append(position)
        cursor = position + 1
    for begin in starts:
        for candidate in _scan_json_tail(raw[begin:]):
            if candidate[0] not in seen:
                seen.add(candidate[0])
                yield candidate


def _load_candidate(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    try:
        salvaged = _repair_json_candidate(text)
    except Exception:  # noqa: BLE001 — salvage is best-effort
        salvaged = None
    return salvaged if isinstance(salvaged, dict) else None


def _lookup_path(obj: Any, path: Sequence[Any]) -> Any:
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def _buffer_from_cut(
    path: Sequence[Any], value: Any, parsed: Any = None
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Stage a half-written file body as an incomplete edit buffer.

    Works for both delivery shapes: ``data.edits["file.py"]["replace"]`` (the
    key is the path) and ``data.documents[0]["content"]`` (an index — the
    path is read from the record's own ``path`` field).
    """
    if not isinstance(value, str) or not value:
        return None
    parts = list(path)
    if parts and parts[0] == "data":
        parts = parts[1:]
    if not parts or str(parts[0]) not in DELIVERY_KEYS:
        return None
    file_path = ""
    record = _lookup_path(parsed, list(path)[:-1]) if parsed is not None else None
    if isinstance(record, dict):
        for key in ("path", "file", "filename", "target"):
            candidate = record.get(key)
            if isinstance(candidate, str) and candidate.strip():
                file_path = candidate.strip()
                break
    if not file_path:
        for part in parts[1:]:
            if isinstance(part, int) or part is None:
                continue
            file_path = str(part)
            break
    if not file_path:
        return None
    last = parts[-1]
    field_name = "content" if isinstance(last, int) else str(last)
    return file_path, {
        "path": file_path,
        "field": field_name,
        "text": value,
        "truncated": True,
        "source": "json",
    }


def _fence_buffers(raw: str) -> Dict[str, Dict[str, Any]]:
    """Partial file bodies the model fenced off instead of JSON-encoding.

    Stage B of the recovery: prose plus a ```python fence that the output cap
    cut in half. The file path is read from the prose around the fence; an
    unclosed fence is marked truncated so a staged body is never mistaken for
    a complete file.
    """
    buffers: Dict[str, Dict[str, Any]] = {}
    matches = list(_FENCE_RE.finditer(raw or ""))
    for position, match in enumerate(matches):
        lang = (match.group(1) or "").strip().lower()
        body = match.group(2)
        if lang in _JSON_LANGS or body.lstrip()[:1] in ("{", "["):
            continue
        if lang and lang not in ("py", "python"):
            continue
        closed = match.group(0).endswith("```")
        text = body
        if not text.strip():
            continue
        context = (raw or "")[: match.start()]
        guessed = ""
        for candidate in reversed(_PATH_IN_TEXT_RE.findall(context)):
            if candidate.count(".") == 1:
                guessed = candidate
                break
        suffix = {"python": "py", "py": "py"}.get(lang) or lang or "txt"
        file_path = guessed or f"recovered/part_{position + 1}.{suffix}"
        if file_path in buffers:
            continue
        buffers[file_path] = {
            "path": file_path,
            "field": "content",
            "text": text,
            "truncated": not closed,
            "source": "fence",
            "path_source": "text" if guessed else "guessed",
        }
    return buffers


def recover_truncated_payload(raw: str) -> Optional[TruncationRecovery]:
    """Salvage a reply that the output token cap cut mid-flight.

    Stage A re-assembles the JSON chunk by chunk: every cut point is closed
    with the right brackets, an interrupted string is terminated, and the
    result is parsed. When the cut landed inside file content, that content
    is staged as an incomplete buffer (never as a delivery).

    Stage B handles prose-plus-fence replies whose file body never made it
    into JSON at all.

    Returns ``None`` when neither stage produces content *and* no buffers —
    the caller then falls back to the network truncation-repair, which is
    the right move when the reply carried no delivery to salvage.
    """
    notes: List[str] = []
    for candidate_text, cut_path, note in _json_candidates(raw):
        parsed = _load_candidate(candidate_text)
        if parsed is None:
            continue
        buffers: Dict[str, Dict[str, Any]] = {}
        if cut_path:
            staged = _buffer_from_cut(
                cut_path, _lookup_path(parsed, cut_path), parsed
            )
            if staged:
                buffers[staged[0]] = staged[1]
        if buffers or _parsed_has_content(parsed):
            if note not in notes:
                notes.append(note)
            return TruncationRecovery(parsed=parsed, buffers=buffers, notes=notes)
    buffers = _fence_buffers(raw)
    if buffers:
        notes.append("recovered partial file bodies from fenced blocks")
        return TruncationRecovery(parsed=None, buffers=buffers, notes=notes)
    return None


__all__ = ["LLMAgent"]
