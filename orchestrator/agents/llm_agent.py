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

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .. import config
from ..llm_client import LLMClient, LLMError, LLMResult
from ..prompt_builder import build_prompt, load_agent_spec, render_system_prompt
from .base_agent import AgentOutput, AgentOutputError, BaseAgent, delivery_problems, preexisting_expected

logger = logging.getLogger(__name__)


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

    # Sent when the Definition of Done rejects a parseable-but-empty delivery
    # (summary/prose JSON instead of real file content).
    DOD_REPAIR_NOTE = (
        "\n\nThe Definition of Done REJECTED your previous reply as a delivery:\n"
        "{problems}\n"
        "It was valid JSON but did not deliver real file content. Reply ONLY "
        "with one compact JSON object (under 600 tokens) that actually patches "
        "the project files:\n"
        '{"status": "completed", "summary": "<one line>", "data": {"edits": '
        '{"<project-relative path>": {"search": "<shortest unique snippet>", '
        '"replace": "<replacement>"}}}}\n'
        "Use data.edits for files that already exist (never repeat whole "
        "files), or data.documents only for a brand-new file under 60 lines. "
        "Do not reply with descriptions, scopes, or metadata.\n"
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
                feedback = "your edits could not be applied:\n" + "\n".join(
                    f"- {err}" for err in errs[-4:]
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
            note = self.DOD_REPAIR_NOTE.replace("{problems}", bullets)
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


def _snippet(text: str, limit: int = 400) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned or "[empty model output]"
    return cleaned[:limit] + "..."


__all__ = ["LLMAgent"]
