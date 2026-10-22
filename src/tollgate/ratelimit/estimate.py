"""Token estimates with tiktoken.

Phase 1.3 uses these when the provider never reported usage (the client
disconnected, the stream broke, or the provider omits usage). Phase 1.5
reuses estimate_prompt_tokens for TPM reservations.
"""

import functools
import json
from collections.abc import Iterable
from typing import Any

import tiktoken

FALLBACK_ENCODING = "o200k_base"

# Chat framing OpenAI documents for gpt-4o-family models.
TOKENS_PER_MESSAGE = 3
TOKENS_PER_NAME = 1
REPLY_PRIMER = 3

# Content parts that are not tokenized as text, so cannot be estimated here.
_MEDIA_PARTS = frozenset({"image_url", "input_audio", "file"})
_GENERATED_TEXT_FIELDS = ("content", "refusal", "reasoning", "reasoning_content")


@functools.lru_cache(maxsize=64)
def encoding_for(model: str) -> tiktoken.Encoding:
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding(FALLBACK_ENCODING)


def warm_up(models: Iterable[str]) -> None:
    """Load encodings up front (tiktoken downloads them on first use)."""
    for model in models:
        encoding_for(model)


def count_tokens(text: str, model: str) -> int:
    return len(encoding_for(model).encode(text, disallowed_special=()))


def estimate_prompt_tokens(messages: list[dict[str, Any]], model: str) -> int:
    total = REPLY_PRIMER
    for message in messages:
        total += TOKENS_PER_MESSAGE
        for key, value in message.items():
            total += count_tokens(_as_text(value), model)
            if key == "name":
                total += TOKENS_PER_NAME
    return total


def generated_text(message: Any) -> str:
    """Text a response message, or a stream delta, adds to the completion."""
    if not isinstance(message, dict):
        return ""
    parts = [message[f] for f in _GENERATED_TEXT_FIELDS if isinstance(message.get(f), str)]
    tool_calls = message.get("tool_calls")
    for call in tool_calls if isinstance(tool_calls, list) else []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict):
            parts += [
                v for v in (function.get("name"), function.get("arguments")) if isinstance(v, str)
            ]
    return "".join(parts)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_part_text(part) for part in value)
    return json.dumps(value)


def _part_text(part: Any) -> str:
    if isinstance(part, dict):
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            return str(part["text"])
        if part.get("type") in _MEDIA_PARTS:
            return ""
    return json.dumps(part)
