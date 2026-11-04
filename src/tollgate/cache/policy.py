"""What the response cache may serve (SPEC Phase 2.4's cacheability policy,
which governs the exact layer too), and the exact layer's key.

A request is cacheable only if its answer doesn't vary: a low temperature
(by default at most 0.3; a request without one runs at the provider's default,
1, so it never is), one choice, and no tools, whether offered or already
called in the conversation. The tenant must have the cache enabled, and the
client mustn't have sent `X-Tollgate-Cache: bypass`. The tenant's setting is
only known from its admission call, so it is checked there.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

# Fields that don't change the answer: how it is delivered, and who asked.
# Everything else in the body is part of the exact-match key.
UNKEYED_FIELDS = frozenset({"stream", "stream_options", "user"})
BYPASS_HEADER = "x-tollgate-cache"


def cacheable(body: Mapping[str, Any], headers: Mapping[str, str], max_temperature: float) -> bool:
    """Whether the request itself allows a cached answer (the tenant's setting aside)."""
    if headers.get(BYPASS_HEADER, "").strip().lower() == "bypass":
        return False
    temperature = body.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        return False  # none at all: the provider's default, 1
    if not 0 <= temperature <= max_temperature:
        return False
    if body.get("n", 1) != 1:
        return False
    if body.get("tools") or body.get("functions"):
        return False
    messages = body.get("messages")
    return isinstance(messages, list) and not any(_uses_tools(m) for m in messages)


def _uses_tools(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    return (
        message.get("role") in ("tool", "function")
        or bool(message.get("tool_calls"))
        or bool(message.get("function_call"))
    )


def exact_key(body: Mapping[str, Any]) -> str:
    """The exact-match key: the SHA-256 of the canonical body, without the
    fields that don't change the answer. Two requests share it only if every
    parameter that can change the answer is the same; the tenant is in the
    Redis key around it."""
    keyed = {k: v for k, v in body.items() if k not in UNKEYED_FIELDS}
    canonical = json.dumps(keyed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
