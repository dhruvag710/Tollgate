"""Usage as a provider reports it."""

from typing import Any


def parse_usage(usage: Any) -> tuple[int, int] | None:
    """(prompt_tokens, completion_tokens) from a provider's usage object, if well-formed."""
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int:
        return None
    return prompt, completion
