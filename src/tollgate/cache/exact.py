"""The exact-match response cache (SPEC Phase 2.3), in Redis.

Entries are read by the request's admission call and written by its settle
call (lua/exact.lua, composed into ratelimit/limiter.py's scripts), so the
cache adds no Redis round trip to a request: a hit is answered from the reply
that admitted it, and a miss stores its answer in the call that settles it.

An entry is a complete `chat.completion` object as JSON, the provider's own
usage included; it serves streamed requests too (replay.py).
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tollgate.usage.reported import parse_usage

log = logging.getLogger(__name__)

LIBRARY = (Path(__file__).parent / "lua" / "exact.lua").read_text(encoding="utf-8")


@dataclass(frozen=True)
class CacheStore:
    """An entry for the settle call to store."""

    hash: str
    entry: str
    ttl_s: int


def storable(completion: Any) -> bool:
    """A complete answer worth keeping: every choice finished, no tool calls,
    and the provider's own usage, which a hit's saved cost is computed from."""
    if not isinstance(completion, dict) or parse_usage(completion.get("usage")) is None:
        return False
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    for choice in choices:
        if not isinstance(choice, dict) or not choice.get("finish_reason"):
            return False
        message = choice.get("message")
        if not isinstance(message, dict):
            return False
        if message.get("tool_calls") or message.get("function_call"):
            return False
    return True


def encode(completion: dict[str, Any], hash_: str, ttl_s: int, max_bytes: int) -> CacheStore | None:
    """The entry to store for `completion`, or None if it is too large."""
    entry = json.dumps(completion, separators=(",", ":"), ensure_ascii=False)
    if len(entry.encode()) > max_bytes:
        return None
    return CacheStore(hash_, entry, ttl_s)


def decode(raw: str) -> dict[str, Any] | None:
    """A cached completion, or None if the entry isn't one (it is then a miss)."""
    try:
        completion = json.loads(raw)
    except ValueError:
        completion = None
    if not storable(completion):
        log.warning("ignored an unreadable exact-cache entry")
        return None
    result: dict[str, Any] = completion
    return result
