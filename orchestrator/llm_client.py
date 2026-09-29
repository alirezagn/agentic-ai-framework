"""LLMClient — HTTP backends that turn specialist-agent prompts into model calls.

Three providers are supported, resolved from :mod:`orchestrator.config`
``SYSTEM_KEYS`` so no credential is ever hard-coded:

* ``anthropic``  — Messages API (``ANTHROPIC_API_KEY``),
* ``ollama``     — local server (``OLLAMA_BASE_URL``), native or OpenAI-compatible,
* ``openrouter`` — OpenAI-compatible fallback (``OPENROUTER_API_KEY``).

The HTTP transport is a plain callable so tests can inject a fake without any
network access::

    client = LLMClient(provider="ollama", transport=fake_transport)

Every call returns an :class:`LLMResult` carrying the generated text plus the
token usage the orchestrator feeds into context-utilisation accounting.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENROUTER = "openrouter"
PROVIDERS: Tuple[str, ...] = (PROVIDER_ANTHROPIC, PROVIDER_OLLAMA, PROVIDER_OPENROUTER)

DEFAULT_MODELS: Dict[str, str] = {
    PROVIDER_ANTHROPIC: "claude-sonnet-4-5",
    PROVIDER_OLLAMA: "qwen2.5-coder:14b",
    PROVIDER_OPENROUTER: "anthropic/claude-sonnet-4.5",
}

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"

# transport(method, url, headers, body, timeout) -> (status_code, response_bytes)
Transport = Callable[[str, str, Dict[str, str], bytes, float], Tuple[int, bytes]]


class LLMError(RuntimeError):
    """Base error for the LLM backend."""


class LLMUnavailableError(LLMError):
    """No provider could be resolved from configuration/environment."""


class LLMClientError(LLMError):
    """A provider answered with an error, or the request could not be sent."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class LLMResult:
    """Outcome of one chat completion."""

    text: str
    model: str
    provider: str
    usage: Dict[str, int] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def input_tokens(self) -> int:
        return int(self.usage.get("input_tokens", 0) or 0)

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("output_tokens", 0) or 0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _default_transport(
    method: str, url: str, headers: Dict[str, str], body: bytes, timeout: float
) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise LLMClientError(f"Cannot reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMClientError(f"Request to {url} timed out after {timeout}s") from exc


def _decode_json(payload: bytes, context: str) -> Dict[str, Any]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        snippet = payload[:300].decode("utf-8", errors="replace")
        raise LLMClientError(f"{context} returned non-JSON body: {snippet}") from exc
    if not isinstance(data, dict):
        raise LLMClientError(f"{context} returned JSON that is not an object")
    return data


class LLMClient:
    """Small, dependency-free client for the supported chat providers."""

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: float = 120.0,
        transport: Optional[Transport] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.provider = (provider or self.discover_provider()).lower()
        if self.provider not in PROVIDERS:
            raise LLMUnavailableError(
                f"Unknown LLM provider '{self.provider}'. Supported: {', '.join(PROVIDERS)}"
            )
        self.model = model or self._default_model()
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.transport: Transport = transport or _default_transport
        self._api_key = api_key
        self._base_url = base_url

    # ------------------------------------------------------------------
    # Discovery / configuration
    # ------------------------------------------------------------------

    @staticmethod
    def discover_provider() -> str:
        """Pick a provider from ORCHESTRATOR_LLM_PROVIDER or available keys."""
        explicit = os.environ.get("ORCHESTRATOR_LLM_PROVIDER")
        if explicit:
            return explicit.strip().lower()
        if config.SYSTEM_KEYS.resolve("anthropic_api_key"):
            return PROVIDER_ANTHROPIC
        if config.SYSTEM_KEYS.resolve("openrouter_api_key"):
            return PROVIDER_OPENROUTER
        if config.SYSTEM_KEYS.resolve("ollama_base_url"):
            return PROVIDER_OLLAMA
        raise LLMUnavailableError(
            "No LLM provider configured. Set ANTHROPIC_API_KEY, OPENROUTER_API_KEY "
            "or OLLAMA_BASE_URL (or ORCHESTRATOR_LLM_PROVIDER) and retry."
        )

    @staticmethod
    def is_available() -> bool:
        try:
            LLMClient.discover_provider()
        except LLMUnavailableError:
            return False
        return True

    def _default_model(self) -> str:
        return os.environ.get("ORCHESTRATOR_LLM_MODEL") or DEFAULT_MODELS[self.provider]

    def _key(self) -> Optional[str]:
        if self._api_key is not None:
            return self._api_key
        if self.provider == PROVIDER_ANTHROPIC:
            return config.SYSTEM_KEYS.resolve("anthropic_api_key")
        if self.provider == PROVIDER_OPENROUTER:
            return config.SYSTEM_KEYS.resolve("openrouter_api_key")
        return None

    def _ollama_url(self) -> str:
        if self._base_url:
            return self._base_url.rstrip("/")
        configured = config.SYSTEM_KEYS.resolve("ollama_base_url")
        return (configured or DEFAULT_OLLAMA_BASE_URL).rstrip("/")

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def complete(
        self,
        system: str,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResult:
        if not messages:
            raise LLMClientError("complete() requires at least one message")
        kwargs: Dict[str, Any] = {
            "max_tokens": int(max_tokens or self.max_tokens),
            "temperature": self.temperature if temperature is None else float(temperature),
        }
        if self.provider == PROVIDER_ANTHROPIC:
            return self._complete_anthropic(system, messages, **kwargs)
        return self._complete_openai_compatible(system, messages, **kwargs)

    def complete_json(
        self,
        system: str,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Complete and require the answer to contain a parseable JSON object."""
        from .agents.base_agent import BaseAgent  # local import: avoid cycles

        result = self.complete(system, messages, max_tokens=max_tokens)
        return BaseAgent.extract_json_block(result.text)

    # ------------------------------------------------------------------
    # Anthropic Messages API
    # ------------------------------------------------------------------

    def _complete_anthropic(
        self, system: str, messages: List[Dict[str, str]], **kwargs: Any
    ) -> LLMResult:
        api_key = self._key()
        if not api_key:
            raise LLMUnavailableError("anthropic provider requires ANTHROPIC_API_KEY")
        base = (self._base_url or os.environ.get("ANTHROPIC_BASE_URL") or ANTHROPIC_BASE_URL)
        url = base.rstrip("/") + "/v1/messages"
        body = json.dumps(
            {
                "model": self.model,
                "system": system or "",
                "messages": messages,
                **kwargs,
            }
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        status, payload = self.transport("POST", url, headers, body, self.timeout)
        if status < 200 or status >= 300:
            raise LLMClientError(
                f"Anthropic API returned HTTP {status}: "
                f"{payload[:300].decode('utf-8', errors='replace')}",
                status=status,
            )
        data = _decode_json(payload, "Anthropic API")
        blocks = [block for block in data.get("content") or [] if block.get("type") == "text"]
        text = "".join(str(block.get("text", "")) for block in blocks)
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            model=str(data.get("model") or self.model),
            provider=PROVIDER_ANTHROPIC,
            usage={
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            },
            raw=data,
        )

    # ------------------------------------------------------------------
    # OpenAI-compatible (Ollama / OpenRouter)
    # ------------------------------------------------------------------

    def _complete_openai_compatible(
        self, system: str, messages: List[Dict[str, str]], **kwargs: Any
    ) -> LLMResult:
        chat_messages: List[Dict[str, str]] = []
        if system:
            chat_messages.append({"role": "system", "content": system})
        chat_messages.extend(messages)

        if self.provider == PROVIDER_OLLAMA:
            return self._complete_ollama(chat_messages, **kwargs)

        api_key = self._key()
        if not api_key:
            raise LLMUnavailableError("openrouter provider requires OPENROUTER_API_KEY")
        url = (self._base_url or OPENROUTER_BASE_URL).rstrip("/") + "/chat/completions"
        return self._openai_call(
            url,
            {
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            chat_messages,
            PROVIDER_OPENROUTER,
            **kwargs,
        )

    def _complete_ollama(self, chat_messages: List[Dict[str, str]], **kwargs: Any) -> LLMResult:
        base = self._ollama_url()
        if base.endswith("/v1"):
            return self._openai_call(
                base + "/chat/completions",
                {"content-type": "application/json"},
                chat_messages,
                PROVIDER_OLLAMA,
                **kwargs,
            )
        url = base + "/api/chat"
        body = json.dumps(
            {
                "model": self.model,
                "messages": chat_messages,
                "stream": False,
                "options": {
                    "temperature": kwargs.get("temperature", self.temperature),
                    "num_predict": kwargs.get("max_tokens", self.max_tokens),
                },
            }
        ).encode("utf-8")
        status, payload = self.transport("POST", url, {"content-type": "application/json"}, body, self.timeout)
        if status < 200 or status >= 300:
            raise LLMClientError(
                f"Ollama API returned HTTP {status}: "
                f"{payload[:300].decode('utf-8', errors='replace')}",
                status=status,
            )
        data = _decode_json(payload, "Ollama API")
        text = str((data.get("message") or {}).get("content", ""))
        return LLMResult(
            text=text,
            model=str(data.get("model") or self.model),
            provider=PROVIDER_OLLAMA,
            usage={
                "input_tokens": int(data.get("prompt_eval_count", 0) or 0),
                "output_tokens": int(data.get("eval_count", 0) or 0),
            },
            raw=data,
        )

    def _openai_call(
        self,
        url: str,
        headers: Dict[str, str],
        chat_messages: List[Dict[str, str]],
        provider: str,
        **kwargs: Any,
    ) -> LLMResult:
        body = json.dumps(
            {
                "model": self.model,
                "messages": chat_messages,
                "temperature": kwargs.get("temperature", self.temperature),
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            }
        ).encode("utf-8")
        status, payload = self.transport("POST", url, headers, body, self.timeout)
        if status < 200 or status >= 300:
            raise LLMClientError(
                f"{provider} API returned HTTP {status}: "
                f"{payload[:300].decode('utf-8', errors='replace')}",
                status=status,
            )
        data = _decode_json(payload, f"{provider} API")
        choices = data.get("choices") or []
        text = ""
        if choices:
            text = str((choices[0].get("message") or {}).get("content", ""))
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            model=str(data.get("model") or self.model),
            provider=provider,
            usage={
                "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "output_tokens": int(usage.get("completion_tokens", 0) or 0),
            },
            raw=data,
        )


def get_client(**kwargs: Any) -> LLMClient:
    """Convenience factory that discovers the provider from the environment."""
    return LLMClient(**kwargs)


__all__ = [
    "LLMError",
    "LLMUnavailableError",
    "LLMClientError",
    "LLMResult",
    "LLMClient",
    "get_client",
    "PROVIDERS",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OLLAMA",
    "PROVIDER_OPENROUTER",
    "DEFAULT_MODELS",
]
