"""The response cache's parts that need no Redis (SPEC Phase 2.3): what may be
cached, the exact-match key, entries, and replaying a completion as SSE."""

import json
from typing import Any

import pytest

from tollgate.cache import exact
from tollgate.cache.policy import cacheable, exact_key
from tollgate.cache.replay import StreamAssembler, fresh, replay

MESSAGES = [{"role": "user", "content": "what is two plus two?"}]
BODY: dict[str, Any] = {"model": "gpt-4o-mini", "messages": MESSAGES, "temperature": 0}
COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-original",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-4o-mini",
    "system_fingerprint": "fp_x",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Four.", "refusal": None},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 14, "completion_tokens": 2, "total_tokens": 16},
}


# --- the policy --------------------------------------------------------------------------


@pytest.mark.parametrize("temperature", [0, 0.0, 0.2, 0.3])
def test_low_temperatures_are_cacheable(temperature: float) -> None:
    assert cacheable({**BODY, "temperature": temperature}, {}, 0.3)


@pytest.mark.parametrize(
    "change",
    [
        {"temperature": 0.31},
        {"temperature": 1},
        {"temperature": "0"},  # not a number
        {"temperature": True},
        {"temperature": -1},
        {"n": 2},  # several samples asked for
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        {"functions": [{"name": "f"}]},
        {"messages": [*MESSAGES, {"role": "assistant", "tool_calls": [{"id": "1"}]}]},
        {"messages": [*MESSAGES, {"role": "tool", "tool_call_id": "1", "content": "4"}]},
        {"messages": [*MESSAGES, {"role": "assistant", "function_call": {"name": "f"}}]},
        {"messages": [*MESSAGES, {"role": "function", "name": "f", "content": "4"}]},
    ],
)
def test_requests_whose_answer_varies_or_uses_tools_are_not(change: dict[str, Any]) -> None:
    assert not cacheable({**BODY, **change}, {}, 0.3)


def test_no_temperature_means_the_providers_default_so_never_cached() -> None:
    body = {k: v for k, v in BODY.items() if k != "temperature"}
    assert not cacheable(body, {}, 0.3)


def test_the_client_can_opt_out() -> None:
    assert not cacheable(BODY, {"x-tollgate-cache": "bypass"}, 0.3)
    assert not cacheable(BODY, {"x-tollgate-cache": " BYPASS "}, 0.3)
    assert cacheable(BODY, {"x-tollgate-cache": "please"}, 0.3)


def test_the_temperature_limit_is_configurable() -> None:
    assert cacheable({**BODY, "temperature": 0.7}, {}, 0.7)
    assert not cacheable({**BODY, "temperature": 0.1}, {}, 0.0)


# --- the key -----------------------------------------------------------------------------


def test_identical_requests_share_a_key_whatever_their_field_order() -> None:
    reordered = dict(reversed(list(BODY.items())))
    assert exact_key(BODY) == exact_key(reordered) == exact_key(json.loads(json.dumps(BODY)))
    assert len(exact_key(BODY)) == 64  # SHA-256, hex


@pytest.mark.parametrize(
    "unkeyed", [{"stream": True}, {"stream_options": {"include_usage": True}}, {"user": "u-1"}]
)
def test_how_the_answer_is_delivered_and_who_asked_are_not_in_it(unkeyed: dict[str, Any]) -> None:
    assert exact_key({**BODY, **unkeyed}) == exact_key(BODY)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "llama3.1:8b"},
        {"messages": [{"role": "user", "content": "what is two plus three?"}]},
        {"messages": [{"role": "system", "content": "Be terse."}, *MESSAGES]},
        {"temperature": 0.1},
        {"max_tokens": 5},
        {"max_completion_tokens": 5},
        {"top_p": 0.5},
        {"stop": ["."]},
        {"seed": 7},
        {"response_format": {"type": "json_object"}},
        {"presence_penalty": 0.5},
        {"frequency_penalty": 0.5},
        {"logit_bias": {"1": 5}},
        {"logprobs": True},
        {"some_future_param": 1},  # unknown to Tollgate: still part of the key
    ],
)
def test_changing_any_other_param_changes_the_key(change: dict[str, Any]) -> None:
    assert exact_key({**BODY, **change}) != exact_key(BODY)


# --- entries -----------------------------------------------------------------------------


def test_only_complete_answers_with_usage_are_storable() -> None:
    assert exact.storable(COMPLETION)
    no_usage = {**COMPLETION, "usage": None}
    unfinished = {**COMPLETION, "choices": [{**COMPLETION["choices"][0], "finish_reason": None}]}
    tool_call = {
        **COMPLETION,
        "choices": [
            {**COMPLETION["choices"][0], "message": {"role": "assistant", "tool_calls": [{}]}}
        ],
    }
    for bad in (no_usage, unfinished, tool_call, {**COMPLETION, "choices": []}, [], "x"):
        assert not exact.storable(bad)


def test_entries_round_trip_and_large_ones_are_not_stored() -> None:
    store = exact.encode(COMPLETION, "h", ttl_s=60, max_bytes=10_000)
    assert store is not None and (store.hash, store.ttl_s) == ("h", 60)
    assert exact.decode(store.entry) == COMPLETION
    assert exact.encode(COMPLETION, "h", ttl_s=60, max_bytes=50) is None


@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({**COMPLETION, "usage": None})])
def test_an_unreadable_entry_is_a_miss(raw: str) -> None:
    assert exact.decode(raw) is None


def test_a_cached_answer_goes_out_under_a_new_id() -> None:
    first, second = fresh(COMPLETION), fresh(COMPLETION)
    assert first["id"] != second["id"] != COMPLETION["id"]
    assert first["choices"] == COMPLETION["choices"] and first["usage"] == COMPLETION["usage"]


# --- replay ------------------------------------------------------------------------------


def events(raw: list[bytes]) -> list[Any]:
    out: list[Any] = []
    for event in raw:
        assert event.startswith(b"data: ") and event.endswith(b"\n\n")
        data = event[6:-2].decode()
        out.append(data if data == "[DONE]" else json.loads(data))
    return out


@pytest.mark.parametrize("include_usage", [False, True])
def test_replay_streams_the_completion_as_a_provider_would(include_usage: bool) -> None:
    played = replay(COMPLETION, include_usage=include_usage)
    *chunks, done = events(played.events)
    assert done == "[DONE]"
    if include_usage:
        *chunks, last = chunks
        assert (last["choices"], last["usage"]) == ([], COMPLETION["usage"])
        assert all(c["usage"] is None for c in chunks)
    else:
        assert all("usage" not in c for c in chunks)
    assert {c["object"] for c in chunks} == {"chat.completion.chunk"}
    assert len({c["id"] for c in chunks}) == 1 and chunks[0]["id"] != COMPLETION["id"]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert "".join(d.get("content", "") for d in deltas) == "Four."
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert played.first_token == 1  # the chunk after the role's


def test_a_stream_assembles_into_the_completion_it_replays() -> None:
    assembler = StreamAssembler()
    for chunk in events(replay(COMPLETION, include_usage=True).events)[:-1]:
        assembler.feed(chunk)
    rebuilt = assembler.completion()
    assert rebuilt is not None
    assert (rebuilt["choices"], rebuilt["usage"], rebuilt["model"]) == (
        COMPLETION["choices"],
        COMPLETION["usage"],
        COMPLETION["model"],
    )
    assert exact.storable(rebuilt)


def test_a_streamed_answer_in_many_chunks_assembles_whole() -> None:
    assembler = StreamAssembler()
    head = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    assembler.feed({**head, "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    for word in ("Four", ",", " obviously", "."):
        assembler.feed({**head, "choices": [{"index": 0, "delta": {"content": word}}]})
    assert assembler.completion() is None  # not finished yet
    assembler.feed({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    assert assembler.completion() is None  # no usage yet
    assembler.feed({**head, "choices": [], "usage": COMPLETION["usage"]})
    rebuilt = assembler.completion()
    assert rebuilt is not None
    assert rebuilt["choices"][0]["message"]["content"] == "Four, obviously."


def test_a_stream_with_tool_calls_is_not_assembled() -> None:
    assembler = StreamAssembler()
    head = {"id": "c", "model": "m"}
    assembler.feed({**head, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0}]}}]})
    assembler.feed({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    assembler.feed({**head, "choices": [], "usage": COMPLETION["usage"]})
    assert assembler.completion() is None
