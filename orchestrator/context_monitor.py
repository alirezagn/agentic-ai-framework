"""Context utilisation accounting.

framework/00_MASTER_ORCHESTRATOR.md:63-91 requires the orchestrator to measure
how much of the model context window every agent call consumes and to act at
70% (compact) / 85% (critical). Two estimators are supported:

* exact token usage reported by the provider (``LLMResult.usage``,
  surfaced as ``output.data["usage"]``),
* a character-based estimate of the assembled payload
  (``chars / 4`` tokens) for deterministic agents without a model call.

The window size defaults to 128k tokens and can be tuned with the
``ORCHESTRATOR_CONTEXT_WINDOW_TOKENS`` environment variable.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DEFAULT_CONTEXT_WINDOW_TOKENS = 128000
CHARS_PER_TOKEN = 4


def context_window_tokens() -> int:
    raw = os.environ.get("ORCHESTRATOR_CONTEXT_WINDOW_TOKENS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def utilization_from_tokens(used_tokens: int, window: Optional[int] = None) -> float:
    window = window or context_window_tokens()
    if window <= 0:
        return 0.0
    percent = (float(used_tokens) / float(window)) * 100.0
    return max(0.0, min(100.0, round(percent, 1)))


def utilization_from_chars(chars: int, window: Optional[int] = None) -> float:
    return utilization_from_tokens(max(0, int(chars)) // CHARS_PER_TOKEN, window=window)


def payload_chars(payload: Dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def utilization_for_output(
    output_data: Optional[Dict[str, Any]], payload_char_count: int
) -> float:
    """Prefer exact token usage; fall back to the payload estimate."""
    if isinstance(output_data, dict):
        usage = output_data.get("usage")
        if isinstance(usage, dict):
            try:
                used = int(usage.get("input_tokens", 0) or 0) + int(
                    usage.get("output_tokens", 0) or 0)
            except (TypeError, ValueError):
                used = 0
            if used > 0:
                return utilization_from_tokens(used)
    return utilization_from_chars(payload_char_count)


def tokens_for_output(
    output_data: Optional[Dict[str, Any]], payload_char_count: int
) -> int:
    """Tokens consumed by one agent output (B2 cumulative accounting).

    Exact provider usage when reported, otherwise ``chars / 4``.
    """
    if isinstance(output_data, dict):
        usage = output_data.get("usage")
        if isinstance(usage, dict):
            try:
                used = int(usage.get("input_tokens", 0) or 0) + int(
                    usage.get("output_tokens", 0) or 0)
            except (TypeError, ValueError):
                used = 0
            if used > 0:
                return used
    return max(0, int(payload_char_count)) // CHARS_PER_TOKEN


__all__ = [
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "CHARS_PER_TOKEN",
    "context_window_tokens",
    "utilization_from_tokens",
    "utilization_from_chars",
    "payload_chars",
    "utilization_for_output",
    "tokens_for_output",
]
