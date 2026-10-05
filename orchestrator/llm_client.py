"""LLMClient — HTTP backends that turn specialist-agent prompts into model calls.

Four providers are supported, resolved from :mod:`orchestrator.config`
``SYSTEM_KEYS`` so no credential is ever hard-coded:

* ``anthropic``  — Messages API (``ANTHROPIC_API_KEY``),
* ``ollama``     — local server (``OLLAMA_BASE_URL``), native or OpenAI-compatible,
* ``openrouter`` — OpenAI-compatible fallback (``OPENROUTER_API_KEY``),
* ``gemini``     — Google ``generateContent`` (``GEMINI_API_KEY``, or the
  ``GOOGLE_API_KEY`` alias).

Discovery order is fixed and the optional providers are appended, so adding one
never redirects a project that is already configured. Every key is
``required=False``: an unconfigured provider must leave behaviour untouched.

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
PROVIDER_GEMINI = "gemini"
PROVIDERS: Tuple[str, ...] = (
    PROVIDER_ANTHROPIC,
    PROVIDER_OLLAMA,
    PROVIDER_OPENROUTER,
    PROVIDER_GEMINI,
)

DEFAULT_MODELS: Dict[str, str] = {
    PROVIDER_ANTHROPIC: "claude-sonnet-4-5",
    PROVIDER_OLLAMA: "qwen2.5-coder:14b",
    PROVIDER_OPENROUTER: "anthropic/claude-sonnet-4.5",
    PROVIDER_GEMINI: "gemini-2.5-pro",
}

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

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
        max_tokens: Optional[int] = None,
        num_ctx: Optional[int] = None,
        timeout: Optional[float] = None,
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
        if max_tokens is not None:
            self.max_tokens = int(max_tokens)
        else:
            # Output budget: 4096 truncates data-heavy agent replies (the
            # cut JSON then parses as a stray inner object). Override with
            # ORCHESTRATOR_LLM_MAX_TOKENS.
            env_max = os.environ.get("ORCHESTRATOR_LLM_MAX_TOKENS")
            self.max_tokens = int(env_max) if env_max else 4096
        # Ollama context window: the server default (often 4096) silently
        # caps prompt+completion and truncates JSON mid-object. 16384 fits a
        # ~4k prompt plus the default 4k output budget.
        if num_ctx is not None:
            self.num_ctx = int(num_ctx)
        else:
            env_ctx = os.environ.get("ORCHESTRATOR_LLM_NUM_CTX")
            self.num_ctx = int(env_ctx) if env_ctx else 16384
        # Generations on a busy LAN server can exceed the 120s default.
        if timeout is not None:
            self.timeout = float(timeout)
        else:
            env_timeout = os.environ.get("ORCHESTRATOR_LLM_TIMEOUT")
            self.timeout = float(env_timeout) if env_timeout else 120.0
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
        if config.SYSTEM_KEYS.resolve("gemini_api_key") or config.SYSTEM_KEYS.resolve(
            "gemini_api_key_alias"
        ):
            return PROVIDER_GEMINI
        raise LLMUnavailableError(
            "No LLM provider configured. Set ANTHROPIC_API_KEY, OPENROUTER_API_KEY, "
            "GEMINI_API_KEY (or GOOGLE_API_KEY) or OLLAMA_BASE_URL "
            "(or ORCHESTRATOR_LLM_PROVIDER) and retry."
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
        if self.provider == PROVIDER_GEMINI:
            # GOOGLE_API_KEY is the alias Google's own tooling expects, so an
            # operator who already exports it needs no second variable.
            return config.SYSTEM_KEYS.resolve(
                "gemini_api_key"
            ) or config.SYSTEM_KEYS.resolve("gemini_api_key_alias")
        return None

    def _gemini_url(self) -> str:
        base = (
            self._base_url
            or os.environ.get("ORCHESTRATOR_LLM_BASE_URL")
            or GEMINI_BASE_URL
        )
        return base.rstrip("/")

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
        if self.provider == PROVIDER_GEMINI:
            return self._complete_gemini(system, messages, **kwargs)
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
        # Thinking/hybrid models (gemma4, qwen3, ...) spend the whole
        # num_predict budget on their reasoning field and return an empty
        # completion. `think: false` forces a direct answer; servers that
        # predate the field reject it, so retry once without it.
        options = {
            "temperature": kwargs.get("temperature", self.temperature),
            "num_predict": kwargs.get("max_tokens", self.max_tokens),
        }
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        body = json.dumps(
            {
                "model": self.model,
                "messages": chat_messages,
                "stream": False,
                "think": False,
                "options": options,
            }
        ).encode("utf-8")
        status, payload = self.transport("POST", url, {"content-type": "application/json"}, body, self.timeout)
        if (status < 200 or status >= 300) and b'"think"' in body:
            body = json.dumps(
                {
                    "model": self.model,
                    "messages": chat_messages,
                    "stream": False,
                    "options": options,
                }
            ).encode("utf-8")
            status, payload = self.transport(
                "POST", url, {"content-type": "application/json"}, body, self.timeout
            )
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

    def _complete_gemini(
        self, system: str, messages: List[Dict[str, str]], **kwargs: Any
    ) -> LLMResult:
        """Google Gemini ``generateContent``.

        Not an OpenAI-shaped endpoint: the request uses ``contents`` /
        ``systemInstruction`` / ``generationConfig`` and the reply arrives in
        ``candidates[].content.parts[].text``. Reusing the OpenAI path here
        would silently produce an empty answer on every call.

        The key travels in the ``x-goog-api-key`` header rather than a
        ``?key=`` query parameter, which would end up in proxy and server logs.
        """
        api_key = self._key()
        if not api_key:
            raise LLMUnavailableError(
                "gemini provider requires GEMINI_API_KEY (or GOOGLE_API_KEY)"
            )
        model = kwargs.get("model") or self.model
        url = f"{self._gemini_url()}/models/{model}:generateContent"
        body = json.dumps(
            {
                "contents": [
                    {
                        "role": "model" if entry.get("role") == "assistant" else "user",
                        "parts": [{"text": str(entry.get("content") or "")}],
                    }
                    for entry in messages
                ],
                "systemInstruction": {"parts": [{"text": system or ""}]},
                "generationConfig": {
                    "temperature": kwargs.get("temperature", self.temperature),
                    "maxOutputTokens": kwargs.get("max_tokens", self.max_tokens),
                    # Hybrid reasoning models otherwise spend the whole output
                    # budget on internal reasoning and can return no visible
                    # text. Same intent as the Ollama path's `think: false`.
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
        ).encode("utf-8")
        status, payload = self.transport(
            "POST",
            url,
            {"content-type": "application/json", "x-goog-api-key": api_key},
            body,
            self.timeout,
        )
        if status < 200 or status >= 300:
            raise LLMClientError(
                f"Gemini API returned HTTP {status}: "
                f"{_gemini_error_text(payload)}",
                status=status,
            )
        data = _decode_json(payload, "Gemini API")
        return LLMResult(
            text=_gemini_reply_text(data),
            model=str(data.get("modelVersion") or model),
            provider=PROVIDER_GEMINI,
            usage={
                "input_tokens": int((data.get("usageMetadata") or {}).get("promptTokenCount", 0) or 0),
                "output_tokens": int(
                    (data.get("usageMetadata") or {}).get("candidatesTokenCount", 0) or 0
                ),
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


def _gemini_error_text(payload: bytes) -> str:
    """The server's own error message, not a wall of escaped JSON."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload[:300].decode("utf-8", errors="replace")
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return json.dumps(data)[:300]


def _gemini_reply_text(data: Dict[str, Any]) -> str:
    """Concatenate the visible text of the first viable candidate.

    A reply is routinely split across several parts, so reading only
    ``parts[0]`` truncates it mid-JSON. Parts flagged ``thought`` are skipped:
    they are internal reasoning, not answer text.
    """
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise LLMClientError("Gemini API returned no candidates")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and not part.get("thought")
        )
        if text:
            return text
    # Every candidate carried reasoning only: the output budget went to
    # thinking. Returning "" would look like a clean empty delivery, so this
    # has to be loud — it is the same class as a truncated reply.
    reasons = sorted(
        {
            str(item.get("finishReason"))
            for item in candidates
            if isinstance(item, dict) and item.get("finishReason")
        }
    )
    detail = f" (finishReason: {', '.join(reasons)})" if reasons else ""
    raise LLMClientError(
        "Gemini API returned no text content — the reply held reasoning only, "
        f"which usually means the output budget was spent on thinking{detail}"
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
    "PROVIDER_GEMINI",
    "DEFAULT_MODELS",
]
