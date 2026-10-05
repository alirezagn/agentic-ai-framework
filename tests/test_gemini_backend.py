"""Google Gemini as an optional fourth provider.

The three existing backends are Anthropic, Ollama and OpenRouter. Gemini is
added as an *optional* provider: it must be reachable only when the operator
configures it, and its absence must never change the behaviour of a project
that does not use it. Two properties carry that "optional" requirement and both
are pinned here rather than asserted in prose:

1. **Discovery must not drift.** ``discover_provider`` picks the first
   configured backend in a fixed order. Adding a key to the registry must not
   let an unset ``GEMINI_API_KEY`` shadow a configured ``ANTHROPIC_API_KEY``,
   and must not make ``is_available()`` true on a machine with no backend at
   all. ``test_unavailable_without_any_keys`` is the pre-existing guard for the
   second half and it must stay green.

2. **The wire shape is not OpenAI's.** ``generateContent`` takes
   ``contents``/``systemInstruction``/``generationConfig`` and returns
   ``candidates[].content.parts[].text``; a response with the text in a
   different place must not be silently read as empty. ``thinkingConfig`` is
   needed to turn thinking off, mirroring the ``think: false`` the Ollama path
   already sends for hybrid models.

Tests are hermetic: an injected transport answers every call, so no test reaches
the network. The repository's autouse ``block_external_network`` fixture is the
backstop.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from orchestrator import config
from orchestrator.llm_client import (
    DEFAULT_MODELS,
    LLMClient,
    LLMClientError,
    LLMUnavailableError,
    PROVIDERS,
)


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ORCHESTRATOR_LLM_PROVIDER",
        "ORCHESTRATOR_LLM_MODEL",
        "ORCHESTRATOR_LLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


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


def _canned(text: str = "answer", **usage: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": text}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt", 11),
            "candidatesTokenCount": usage.get("completion", 5),
            "totalTokenCount": usage.get("total", 16),
        },
        "modelVersion": "gemini-2.5-pro",
    }
    return body


def _client(transport: Any, **kwargs: Any) -> LLMClient:
    return LLMClient(provider="gemini", api_key="gem-key", transport=transport, **kwargs)


# ===========================================================================
# Registration and optionality
# ===========================================================================


class TestGeminiIsRegisteredButOptional:
    def test_gemini_is_a_known_provider(self) -> None:
        assert "gemini" in PROVIDERS

    def test_gemini_has_a_default_model(self) -> None:
        assert DEFAULT_MODELS["gemini"]

    def test_the_key_is_registered_and_never_required(self) -> None:
        """A registered key that reports ``required`` would be a breaking change.

        ``build_system_keys`` feeds whatever the runtime treats as mandatory;
        making Gemini required would fail startup for every existing operator
        who does not use it.
        """
        key = config.SYSTEM_KEYS.get("gemini_api_key")

        assert key.required is False
        assert key.env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def test_no_backend_configured_still_raises(self) -> None:
        """Adding a key must not make the client think a backend exists."""
        with pytest.raises(LLMUnavailableError):
            LLMClient()
        assert LLMClient.is_available() is False

    def test_unset_gemini_key_does_not_shadow_a_configured_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery order must survive the new entry.

        An empty ``GEMINI_API_KEY`` must not win over a real
        ``ANTHROPIC_API_KEY``: the resolved provider would change for anyone who
        exported a blank variable in their shell profile.
        """
        monkeypatch.setenv("GEMINI_API_KEY", "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        assert LLMClient.discover_provider() == "anthropic"

    def test_gemini_is_discovered_when_it_is_the_only_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")

        assert LLMClient.discover_provider() == "gemini"

    def test_explicit_provider_env_still_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        monkeypatch.setenv("ORCHESTRATOR_LLM_PROVIDER", "gemini")

        assert LLMClient.discover_provider() == "gemini"

    def test_missing_key_is_a_clear_error_not_a_keyless_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configured but keyless must fail before a request goes out.

        Sending an unauthenticated request and reporting the server's rejection
        would be both wasteful and misleading in the log.
        """
        client = LLMClient(provider="gemini", api_key=None)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        with pytest.raises(LLMUnavailableError):
            client.complete("sys", [{"role": "user", "content": "hi"}])


# ===========================================================================
# Wire shape
# ===========================================================================


class TestGeminiRequestShape:
    def test_url_and_auth_header(self) -> None:
        transport = RecordingTransport(200, _canned())

        _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        call = transport.calls[0]
        assert call["url"].endswith(":generateContent")
        assert call["url"].startswith("https://generativelanguage.googleapis.com")
        assert call["headers"]["x-goog-api-key"] == "gem-key"
        assert call["method"] == "POST"

    def test_key_is_not_placed_in_the_query_string(self) -> None:
        """A URL query string lands in proxy and server logs.

        The Anthropic path uses a header and the OpenAI-shaped paths use
        ``Authorization``; the Gemini key must not regress to ``?key=``.
        """
        transport = RecordingTransport(200, _canned())

        _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert "key=" not in transport.calls[0]["url"]

    def test_system_prompt_goes_to_system_instruction(self) -> None:
        """``generateContent`` has no ``system`` role; it takes a separate field.

        Sending the system prompt as a ``system`` turn instead would make the
        model treat it as conversation history.
        """
        transport = RecordingTransport(200, _canned())

        _client(transport).complete("BE RULES", [{"role": "user", "content": "q"}])

        body = transport.calls[0]["body"]
        assert body["systemInstruction"]["parts"][0]["text"] == "BE RULES"

    def test_messages_become_contents_with_user_role(self) -> None:
        transport = RecordingTransport(200, _canned())

        _client(transport).complete(
            "sys",
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ],
        )

        contents = transport.calls[0]["body"]["contents"]
        assert [entry["role"] for entry in contents] == ["user", "model"]
        assert contents[0]["parts"][0]["text"] == "first"
        assert contents[1]["parts"][0]["text"] == "second"

    def test_output_budget_and_temperature_reach_generation_config(self) -> None:
        """The budget must actually be sent.

        A Gemini call that silently omits ``maxOutputTokens`` uses the server
        default, which reproduces exactly the mid-JSON truncation the
        truncation-recovery path exists to clean up after.
        """
        transport = RecordingTransport(200, _canned())

        _client(transport, max_tokens=8192).complete(
            "sys", [{"role": "user", "content": "q"}]
        )

        config_block = transport.calls[0]["body"]["generationConfig"]
        assert config_block["maxOutputTokens"] == 8192
        assert "temperature" in config_block

    def test_thinking_is_disabled_for_hybrid_models(self) -> None:
        """Mirror the Ollama path's ``think: false``.

        Hybrid reasoning models otherwise spend the output budget on internal
        reasoning and can return no visible text at all.
        """
        transport = RecordingTransport(200, _canned())

        _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        thinking = transport.calls[0]["body"]["generationConfig"].get("thinkingConfig")
        assert thinking is not None
        assert thinking.get("thinkingBudget") == 0

    def test_http_error_is_surfaced_with_status(self) -> None:
        transport = RecordingTransport(401, {"error": {"message": "API key not valid"}})

        with pytest.raises(LLMClientError) as excinfo:
            _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert excinfo.value.status == 401
        assert "API key not valid" in str(excinfo.value)

    def test_non_json_body_raises(self) -> None:
        def transport(method, url, headers, body, timeout):
            return 200, b"<html>gateway timeout</html>"

        with pytest.raises(LLMClientError):
            _client(transport).complete("sys", [{"role": "user", "content": "q"}])


# ===========================================================================
# Response parsing
# ===========================================================================


class TestGeminiResponseParsing:
    def test_text_and_usage_are_extracted(self) -> None:
        transport = RecordingTransport(200, _canned("hi", prompt=30, completion=7))

        result = _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert result.text == "hi"
        assert result.input_tokens == 30
        assert result.output_tokens == 7
        assert result.provider == "gemini"
        assert result.total_tokens == 37

    def test_multi_part_reply_is_concatenated(self) -> None:
        """A reply can arrive as several parts; taking only the first truncates it."""
        transport = RecordingTransport(
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": '{"status":'}, {"text": '"completed"}'}],
                        }
                    }
                ],
                "usageMetadata": {},
            },
        )

        result = _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert result.text == '{"status":"completed"}'

    def test_usage_is_zero_when_metadata_is_absent(self) -> None:
        """Missing usage must not raise — it feeds context accounting, not parsing."""
        transport = RecordingTransport(
            200,
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
        )

        result = _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert result.text == "ok"
        assert result.input_tokens == 0
        assert result.output_tokens == 0

    def test_thinking_only_reply_is_an_error_not_empty_text(self) -> None:
        """An empty text would be stored as a silent empty delivery.

        A candidate whose only parts carry ``thought`` and no text means the
        budget went to reasoning. Returning "" would let the task look like a
        clean no-op delivery, so it has to be a loud failure.
        """
        transport = RecordingTransport(
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "reasoning...", "thought": True}],
                        },
                        "finishReason": "MAX_TOKENS",
                    }
                ],
                "usageMetadata": {},
            },
        )

        with pytest.raises(LLMClientError) as excinfo:
            _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert "MAX_TOKENS" in str(excinfo.value) or "text" in str(excinfo.value)

    def test_missing_candidates_raises(self) -> None:
        transport = RecordingTransport(200, {"usageMetadata": {}})

        with pytest.raises(LLMClientError):
            _client(transport).complete("sys", [{"role": "user", "content": "q"}])

    def test_finish_reason_max_tokens_is_reported(self) -> None:
        """A budget-truncated reply is the truncation class; say so plainly."""
        transport = RecordingTransport(
            200,
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": '{"status": "comp'}]},
                        "finishReason": "MAX_TOKENS",
                    }
                ],
                "usageMetadata": {},
            },
        )

        result = _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert result.text == '{"status": "comp'
        assert result.raw["candidates"][0]["finishReason"] == "MAX_TOKENS"


# ===========================================================================
# Credential hygiene
# ===========================================================================


class TestGeminiKeyIsRedacted:
    def test_the_key_is_redacted_in_telemetry_under_either_name(
        self, tmp_path: Path
    ) -> None:
        """The key must not reach the telemetry file in cleartext.

        Redaction matches on the *field* name, so registering the key without
        adding it to ``TELEMETRY_REDACT_KEYS`` would write it to disk verbatim.
        Both names are checked, since a caller may use either.
        """
        os.environ["ORCHESTRATOR_TELEMETRY_DIR"] = str(tmp_path / "t")

        config.emit_telemetry(
            "command",
            command="run",
            gemini_api_key="AIzaSy-should-never-appear",
            gemini_api_key_alias="AIzaSz-also-never",
        )

        body = (tmp_path / "t" / "telemetry.jsonl").read_text()
        assert "AIzaSy-should-never-appear" not in body
        assert "AIzaSz-also-never" not in body

    def test_the_key_is_not_reachable_by_a_spawned_command(self) -> None:
        """The deploy channel must not hand the key to project code.

        ``ENV_ALLOWLIST`` is an allowlist, so an unlisted key cannot leak by
        omission — worth pinning rather than assuming, since a future edit that
        adds it for convenience would expose every credential to every build
        script a project runs.
        """
        from orchestrator.deploy_runner import (
            ENV_ALLOWLIST,
            _scrubbed_environment,
        )

        for name in ENV_ALLOWLIST:
            assert "GEMINI" not in name.upper(), (
                f"{name} would expose the Gemini key to spawned build scripts"
            )
            assert "GOOGLE" not in name.upper(), name

        scrubbed = _scrubbed_environment()
        assert not [name for name in scrubbed if "GEMINI" in name.upper()]

    def test_the_key_never_appears_in_an_error_message(self) -> None:
        """A failure log is the other place a key tends to leak."""
        transport = RecordingTransport(403, {"error": {"message": "forbidden"}})

        with pytest.raises(LLMClientError) as excinfo:
            _client(transport).complete("sys", [{"role": "user", "content": "q"}])

        assert "gem-key" not in str(excinfo.value)


# ===========================================================================
# Agent integration
# ===========================================================================


class TestGeminiWorksThroughAnAgent:
    def test_an_agent_completes_a_task_over_gemini(self, tmp_path: Path) -> None:
        """End-to-end shape: agent payload in, validated AgentOutput out.

        ``project_path`` must be a throwaway directory: the agent materialises
        the delivery for real, so pointing it at "." writes ``src/app.py`` into
        the repository — which is exactly the litter this test was found to be
        creating on its first run.
        """
        from orchestrator.agents.specialists import SoftwareAgent

        answer = json.dumps(
            {
                "agent_id": "software_agent",
                "task_id": "TASK-001",
                "status": "completed",
                "summary": "delivered",
                "data": {"documents": {"src/app.py": "x = 1\n"}},
            }
        )
        transport = RecordingTransport(200, _canned(answer))
        agent = SoftwareAgent(project_path=tmp_path, llm_client=_client(transport))

        output = agent.execute(
            {"task": {"id": "TASK-001", "expected_outputs": ["src/app.py"]}}
        )

        assert output.status == "completed"
        assert output.data["documents"] == {"src/app.py": "x = 1\n"}
        assert output.data["usage"]["provider"] == "gemini"
        assert (tmp_path / "src" / "app.py").is_file()