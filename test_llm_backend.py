"""Tests for the LLM backend, prompt builder and the LLMAgent execution path.

Runs fully offline: transports and model clients are injected fakes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from conftest import FakeLLMClient
from orchestrator import config
from orchestrator.agents.base_agent import AgentOutput, AgentOutputError, BaseAgent, create_agent
from orchestrator.agents.llm_agent import LLMAgent
from orchestrator.agents.requirements_agent import RequirementsAgent
from orchestrator.llm_client import (
    LLMClient,
    LLMClientError,
    LLMResult,
    LLMUnavailableError,
)
from orchestrator.prompt_builder import (
    AGENT_SPEC_FILES,
    PromptBuilderError,
    build_prompt,
    framework_specs_dir,
    load_agent_spec,
    render_system_prompt,
    spec_path,
    truncate_middle,
)

REPO_ROOT = Path(__file__).resolve().parent

FAKE_JSON_ANSWER = json.dumps(
    {
        "agent_id": "dummy_agent",
        "task_id": "TASK-002",
        "status": "completed",
        "summary": "produced 3 findings",
        "data": {"findings": ["a", "b", "c"]},
        "errors": [],
        "warnings": [],
    }
)


class RecordingTransport:
    """Fake HTTP transport returning a canned JSON body."""

    def __init__(self, status: int = 200, body: Optional[Dict[str, Any]] = None) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self, method: str, url: str, headers: Dict[str, str], body: bytes, timeout: float
    ) -> Tuple[int, bytes]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": json.loads(body.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return self.status, json.dumps(self.body).encode("utf-8")


class DummyAgent(LLMAgent):
    AGENT_ID = "dummy_agent"
    SYSTEM_RULES = "dummy rules"


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "ORCHESTRATOR_LLM_PROVIDER",
        "ORCHESTRATOR_LLM_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


class TestLLMClientDiscovery:
    def test_unavailable_without_any_keys(self) -> None:
        with pytest.raises(LLMUnavailableError):
            LLMClient()
        assert LLMClient.is_available() is False

    def test_provider_from_explicit_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCHESTRATOR_LLM_PROVIDER", "ollama")
        client = LLMClient()
        assert client.provider == "ollama"

    def test_provider_from_anthropic_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert LLMClient.discover_provider() == "anthropic"
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        assert LLMClient.discover_provider() == "openrouter"

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(LLMUnavailableError):
            LLMClient(provider="bogus")


class TestLLMClientCalls:
    def test_anthropic_request_and_usage_parsing(self) -> None:
        transport = RecordingTransport(
            200,
            {
                "content": [{"type": "text", "text": '{"status":"completed"}'}],
                "model": "claude-test",
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        )
        client = LLMClient(
            provider="anthropic", api_key="sk-abc", transport=transport
        )
        result = client.complete("system text", [{"role": "user", "content": "hi"}])
        assert result.text == '{"status":"completed"}'
        assert result.input_tokens == 11
        assert result.output_tokens == 7
        assert result.provider == "anthropic"
        call = transport.calls[0]
        assert call["url"].endswith("/v1/messages")
        assert call["headers"]["x-api-key"] == "sk-abc"
        assert call["headers"]["anthropic-version"] == "2023-06-01"
        assert call["body"]["system"] == "system text"

    def test_anthropic_error_raises_with_status(self) -> None:
        transport = RecordingTransport(429, {"error": "rate limited"})
        client = LLMClient(provider="anthropic", api_key="sk", transport=transport)
        with pytest.raises(LLMClientError) as excinfo:
            client.complete("s", [{"role": "user", "content": "x"}])
        assert excinfo.value.status == 429

    def test_anthropic_requires_key(self) -> None:
        client = LLMClient(provider="anthropic")
        with pytest.raises(LLMUnavailableError):
            client.complete("s", [{"role": "user", "content": "x"}])

    def test_ollama_native_chat_endpoint(self) -> None:
        transport = RecordingTransport(
            200,
            {
                "model": "qwen",
                "message": {"role": "assistant", "content": "hello"},
                "prompt_eval_count": 30,
                "eval_count": 12,
            },
        )
        client = LLMClient(
            provider="ollama", base_url="http://ollama.local:11434", transport=transport
        )
        result = client.complete("sys", [{"role": "user", "content": "hi"}])
        assert result.text == "hello"
        assert result.input_tokens == 30
        assert result.output_tokens == 12
        call = transport.calls[0]
        assert call["url"] == "http://ollama.local:11434/api/chat"
        assert call["body"]["stream"] is False

    def test_ollama_v1_base_uses_openai_shape(self) -> None:
        transport = RecordingTransport(
            200,
            {
                "model": "qwen",
                "choices": [{"message": {"role": "assistant", "content": "hey"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )
        client = LLMClient(
            provider="ollama", base_url="http://host:11434/v1", transport=transport
        )
        result = client.complete("sys", [{"role": "user", "content": "hi"}])
        assert result.text == "hey"
        assert transport.calls[0]["url"] == "http://host:11434/v1/chat/completions"

    def test_openrouter_sends_bearer_and_parses_usage(self) -> None:
        transport = RecordingTransport(
            200,
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )
        client = LLMClient(
            provider="openrouter", api_key="or-key", transport=transport
        )
        result = client.complete("sys", [{"role": "user", "content": "q"}])
        assert result.text == "answer"
        call = transport.calls[0]
        assert call["headers"]["Authorization"] == "Bearer or-key"
        assert result.usage == {"input_tokens": 9, "output_tokens": 4}

    def test_complete_json_extracts_block(self) -> None:
        transport = RecordingTransport(
            200,
            {"choices": [{"message": {"content": f"noise ```json\n{FAKE_JSON_ANSWER}\n```"}}]},
        )
        client = LLMClient(
            provider="openrouter", api_key="k", transport=transport
        )
        parsed = client.complete_json("s", [{"role": "user", "content": "x"}])
        assert parsed["status"] == "completed"

    def test_non_json_body_raises(self) -> None:
        def transport(method, url, headers, body, timeout):
            return 200, b"not json at all"

        client = LLMClient(provider="ollama", base_url="http://x:1", transport=transport)
        with pytest.raises(LLMClientError):
            client.complete("s", [{"role": "user", "content": "x"}])

    def test_empty_messages_rejected(self) -> None:
        client = LLMClient(provider="ollama", base_url="http://x:1")
        with pytest.raises(LLMClientError):
            client.complete("s", [])


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


class TestPromptBuilder:
    def test_loads_every_mapped_agent_spec(self) -> None:
        for agent_id in AGENT_SPEC_FILES:
            text = load_agent_spec(agent_id)
            assert text, f"framework spec missing for {agent_id}"
            assert text.startswith("# ")

    def test_missing_spec_returns_empty(self) -> None:
        assert load_agent_spec("no_such_agent") == ""
        assert spec_path("no_such_agent") is None

    def test_specs_dir_override(self, tmp_path: Path) -> None:
        custom = tmp_path / "framework"
        custom.mkdir()
        (custom / "02_REQUIREMENTS_AGENT.md").write_text("# custom spec", encoding="utf-8")
        assert load_agent_spec("requirements_agent", custom) == "# custom spec"
        assert framework_specs_dir(custom) == custom
        assert load_agent_spec("review_agent", custom) == ""

    def test_truncate_middle_keeps_head_and_tail(self) -> None:
        text = "A" * 100 + "MIDDLE" + "B" * 100
        out = truncate_middle(text, 50)
        assert len(out) < len(text)
        assert out.startswith("A" * 30)
        assert "truncated" in out

    def test_truncate_middle_no_op_when_short(self) -> None:
        assert truncate_middle("short", 100) == "short"

    def test_build_prompt_contains_all_sections(self) -> None:
        payload = {
            "agent_id": "dummy_agent",
            "system_rules": "rule one",
            "project_memory": "# memory content",
            "task": {"id": "TASK-002", "title": "T"},
            "context": {"PROJECT_MEMORY.md": "ctx"},
            "thresholds": {"loop": {"max_retries": 3}},
        }
        prompt = build_prompt(payload, agent_spec="# spec text")
        assert "# Operating rules" in prompt
        assert "rule one" in prompt
        assert "# Agent specification" in prompt and "# spec text" in prompt
        assert "# PROJECT_MEMORY.md" in prompt
        assert "TASK-002" in prompt
        assert "PROJECT_MEMORY.md" in prompt
        assert "# Thresholds you must respect" in prompt
        assert "```json" in prompt

    def test_build_prompt_rejects_bad_payload(self) -> None:
        with pytest.raises(PromptBuilderError):
            build_prompt({"task": {}})
        with pytest.raises(PromptBuilderError):
            build_prompt("not a dict")

    def test_render_system_prompt_includes_contract(self) -> None:
        system = render_system_prompt("rules", "# spec")
        assert "Operating rules" in system
        assert "# spec" in system
        assert "status" in system


# ---------------------------------------------------------------------------
# LLMAgent execution path
# ---------------------------------------------------------------------------


class TestLLMAgentExecution:
    def _payload(self, project_path: Path) -> Dict[str, Any]:
        from orchestrator.state_manager import StateManager

        task = StateManager(project_path).get_task("TASK-002")
        agent = DummyAgent(project_path=project_path)
        return agent.build_payload(task)

    def test_successful_run_parses_structured_output(self, test_project: Path) -> None:
        fake = FakeLLMClient([FAKE_JSON_ANSWER])
        agent = DummyAgent(project_path=test_project, llm_client=fake)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED
        assert output.agent_id == "dummy_agent"
        assert output.task_id == "TASK-002"
        assert output.data["findings"] == ["a", "b", "c"]
        assert output.data["usage"]["input_tokens"] == 100
        assert fake.calls, "the model must have been called"
        system = fake.calls[0]["system"]
        assert "Operating rules" in system or "dummy rules" in system

    def test_unparseable_output_fails_cleanly(self, test_project: Path) -> None:
        fake = FakeLLMClient(["I cannot answer in JSON, sorry."])
        agent = DummyAgent(project_path=test_project, llm_client=fake)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert output.errors
        assert any("parseable" in err or "JSON" in err for err in output.errors)

    def test_llm_unavailable_fails_with_clear_error(self, test_project: Path) -> None:
        agent = DummyAgent(project_path=test_project)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("LLM backend unavailable" in err for err in output.errors)

    def test_model_exception_fails_cleanly(self, test_project: Path) -> None:
        fake = FakeLLMClient([LLMClientError("boom", status=500)])
        agent = DummyAgent(project_path=test_project, llm_client=fake)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("boom" in err for err in output.errors)

    def test_wrong_agent_id_in_output_is_rejected(self, test_project: Path) -> None:
        bad = json.loads(FAKE_JSON_ANSWER)
        bad["agent_id"] = "someone_else"
        fake = FakeLLMClient([json.dumps(bad)])
        agent = DummyAgent(project_path=test_project, llm_client=fake)
        task = agent.state_manager.get_task("TASK-002")
        # output_from_parsed forces agent_id, so validation passes by design
        output = agent.run(task)
        assert output.agent_id == "dummy_agent"
        assert output.status == config.AGENT_STATUS_COMPLETED

    def test_failed_status_from_model_propagates(self, test_project: Path) -> None:
        parsed = json.loads(FAKE_JSON_ANSWER)
        parsed["status"] = "blocked"
        parsed["errors"] = ["waiting on hardware"]
        fake = FakeLLMClient([json.dumps(parsed)])
        agent = DummyAgent(project_path=test_project, llm_client=fake)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_BLOCKED
        assert output.errors == ["waiting on hardware"]

    def test_extract_json_block_used_as_real_parse_path(
        self, test_project: Path
    ) -> None:
        fenced = "Here you go:\n```json\n" + FAKE_JSON_ANSWER + "\n```\ndone."
        fake = FakeLLMClient([fenced])
        agent = DummyAgent(project_path=test_project, llm_client=fake)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_COMPLETED
        assert BaseAgent.parse_structured_output(fenced)["task_id"] == "TASK-002"

    def test_registry_still_creates_requirements_agent(self, test_project: Path) -> None:
        agent = create_agent("requirements_agent", project_path=test_project)
        assert isinstance(agent, RequirementsAgent)
        assert not isinstance(agent, LLMAgent)


class TestStructuredOutputParsing:
    """Structured-output JSON parsing (GAP_ANALYSIS task T20, finding 20)."""

    def test_plain_json_object(self) -> None:
        parsed = BaseAgent.parse_structured_output(
            '{"status": "completed", "summary": "ok", "task_id": "TASK-001"}'
        )
        assert parsed["task_id"] == "TASK-001"
        assert parsed["status"] == "completed"

    def test_fence_without_language_tag(self) -> None:
        text = "```\n{\"summary\": \"plain fence\"}\n```"
        assert BaseAgent.parse_structured_output(text)["summary"] == "plain fence"

    def test_unfenced_json_embedded_in_prose(self) -> None:
        text = 'Sure — here it is: {"summary": "from prose", "task_id": "TASK-009"} done.'
        parsed = BaseAgent.parse_structured_output(text)
        assert parsed["task_id"] == "TASK-009"

    def test_non_json_braces_before_real_payload(self) -> None:
        text = 'Template {placeholder} ignored; real: {"summary": "second block"}'
        assert BaseAgent.parse_structured_output(text)["summary"] == "second block"

    def test_closing_brace_inside_json_string(self) -> None:
        payload = {"summary": "contains } brace", "task_id": "TASK-010"}
        text = json.dumps(payload)
        assert BaseAgent.parse_structured_output(text) == payload

    def test_empty_text_raises(self) -> None:
        with pytest.raises(AgentOutputError, match="empty"):
            BaseAgent.parse_structured_output("   ")

    def test_garbage_raises_with_reason(self) -> None:
        with pytest.raises(AgentOutputError, match="No parseable JSON object"):
            BaseAgent.parse_structured_output("I could not produce JSON today.")

    def test_non_object_json_is_rejected(self) -> None:
        with pytest.raises(AgentOutputError):
            BaseAgent.parse_structured_output("[1, 2, 3]")

    def test_llm_agent_without_json_fails_task(self, test_project: Path) -> None:
        fake = FakeLLMClient(["no structured answer, sorry"])
        agent = DummyAgent(project_path=test_project, llm_client=fake)
        task = agent.state_manager.get_task("TASK-002")
        output = agent.run(task)
        assert output.status == config.AGENT_STATUS_FAILED
        assert any("parseable JSON" in err for err in output.errors)
