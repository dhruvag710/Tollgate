"""Cached completions in and out of SSE (SPEC Phase 2.3).

Streamed and non-streamed requests share cache entries, and an entry is
always a complete `chat.completion` object. A streamed request is answered
from it by replay(): the chunks a provider would have sent, in one go. A
streamed response is turned into an entry by StreamAssembler, which collects
the chunks as they are relayed and, once the stream has completed, builds the
completion they add up to.
"""

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any


def fresh(completion: dict[str, Any], *, object_: str = "chat.completion") -> dict[str, Any]:
    """A cached completion as a new response: its own id and creation time."""
    return {
        **completion,
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": object_,
        "created": int(time.time()),
    }


def _sse(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


@dataclass(frozen=True)
class Replay:
    events: list[bytes]
    first_token: int | None  # the index of the event carrying the first token


def replay(completion: dict[str, Any], *, include_usage: bool) -> Replay:
    """The SSE events of `completion` streamed: per choice its role, its
    content (and refusal), its finish; then the usage chunk if the client
    asked for one, and [DONE]."""
    response = fresh(completion, object_="chat.completion.chunk")
    head: dict[str, Any] = {
        k: response[k] for k in ("id", "object", "created", "model") if k in response
    }
    if "system_fingerprint" in response:
        head["system_fingerprint"] = response["system_fingerprint"]

    def chunk(choices: list[dict[str, Any]], **extra: Any) -> bytes:
        out = {**head, "choices": choices, **extra}
        if include_usage and "usage" not in extra:
            out["usage"] = None  # as providers send it on every chunk but the last
        return _sse(out)

    events: list[bytes] = []
    first_token: int | None = None
    for choice in completion.get("choices", []):
        index, message = choice.get("index", 0), choice.get("message") or {}
        role = {"role": message.get("role", "assistant"), "content": ""}
        events.append(chunk([_choice(index, role)]))
        delta = {k: message[k] for k in ("content", "refusal") if message.get(k)}
        if delta:
            if first_token is None:
                first_token = len(events)
            events.append(chunk([_choice(index, delta, logprobs=choice.get("logprobs"))]))
        events.append(chunk([_choice(index, {}, finish=choice.get("finish_reason"))]))
    if include_usage and completion.get("usage") is not None:
        events.append(chunk([], usage=completion["usage"]))
    events.append(b"data: [DONE]\n\n")
    return Replay(events, first_token)


def _choice(
    index: int, delta: dict[str, Any], *, finish: str | None = None, logprobs: Any = None
) -> dict[str, Any]:
    return {"index": index, "delta": delta, "logprobs": logprobs, "finish_reason": finish}


class StreamAssembler:
    """Builds the completion a stream's chunks add up to, for the cache."""

    def __init__(self) -> None:
        self._head: dict[str, Any] = {}
        self._choices: dict[int, dict[str, Any]] = {}
        self._usage: dict[str, Any] | None = None
        self._unusable = False  # something the cache doesn't keep (tool calls)

    def feed(self, chunk: dict[str, Any]) -> None:
        for key in ("id", "created", "model", "system_fingerprint"):
            if key in chunk and chunk[key] is not None:
                self._head.setdefault(key, chunk[key])
        if isinstance(chunk.get("usage"), dict):
            self._usage = chunk["usage"]
        choices = chunk.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict) or not isinstance(choice.get("index", 0), int):
                self._unusable = True
                continue
            delta = choice.get("delta") or {}
            if delta.get("tool_calls") or delta.get("function_call"):
                self._unusable = True
            state = self._choices.setdefault(
                choice.get("index", 0),
                {"role": "assistant", "content": [], "refusal": [], "logprobs": None},
            )
            if isinstance(delta.get("role"), str):
                state["role"] = delta["role"]
            if isinstance(delta.get("content"), str):
                state["content"].append(delta["content"])
            if isinstance(delta.get("refusal"), str):
                state["refusal"].append(delta["refusal"])
            if isinstance(choice.get("logprobs"), dict):
                state["logprobs"] = _merge_logprobs(state["logprobs"], choice["logprobs"])
            if choice.get("finish_reason"):
                state["finish_reason"] = choice["finish_reason"]

    def completion(self) -> dict[str, Any] | None:
        """The completion, if the stream gave all of one: every choice
        finished, the provider's usage reported, no tool calls."""
        if self._unusable or self._usage is None or not self._choices:
            return None
        if any("finish_reason" not in c for c in self._choices.values()):
            return None
        choices = []
        for index in sorted(self._choices):
            c = self._choices[index]
            message = {
                "role": c["role"],
                "content": "".join(c["content"]),
                "refusal": "".join(c["refusal"]) or None,
            }
            choices.append(
                {
                    "index": index,
                    "message": message,
                    "logprobs": c["logprobs"],
                    "finish_reason": c["finish_reason"],
                }
            )
        return {**self._head, "object": "chat.completion", "choices": choices, "usage": self._usage}


def _merge_logprobs(sofar: dict[str, Any] | None, more: dict[str, Any]) -> dict[str, Any]:
    merged = dict(sofar or {})
    for key, value in more.items():
        if isinstance(value, list):
            merged[key] = [*(merged.get(key) or []), *value]
        elif key not in merged:
            merged[key] = value
    return merged
