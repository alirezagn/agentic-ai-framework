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

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .. import config
from ..llm_client import LLMClient, LLMError, LLMResult
from ..prompt_builder import build_prompt, load_agent_spec, render_system_prompt
from .base_agent import AgentOutput, AgentOutputError, BaseAgent


class LLMAgent(BaseAgent):
    """Base class for agents that call a language model and parse JSON back."""

    # Optional overrides; when None the client discovers them from the env.
    PROVIDER: Optional[str] = None
    MODEL: Optional[str] = None

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
            return self.failed(
                task_id,
                f"Model output for '{self.AGENT_ID}' was not parseable JSON",
                errors=[str(exc), _snippet(result.text)],
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
