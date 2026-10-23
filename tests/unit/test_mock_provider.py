"""Phase 1.2: the mock provider. Acceptance: deterministic output given a seed."""

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from mock_provider.app import VOCAB, Behaviour, count_prompt_tokens, count_tokens, create_app
from tests.helpers import run_server, wait_until
from tollgate.ratelimit.estimate import estimate_prompt_tokens

MESSAGES = [{"role": "user", "content": "What is a token bucket?"}]


def _client(env: dict[str, str] | None = None) -> httpx.AsyncClient:
    app = create_app(env=env or {})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")


async def _chat(
    client: httpx.AsyncClient, headers: dict[str, str] | None = None, **body: Any
) -> httpx.Response:
    payload = {"model": "gpt-4o-mini", "messages": MESSAGES, **body}
    return await client.post("/v1/chat/completions", json=payload, headers=headers)


def _parse_sse(text: str) -> list[Any]:
    events = []
    for block in text.strip().split("\n\n"):
        assert block.startswith("data: ")
        data = block[len("data: ") :]
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


# --- determinism (the acceptance criterion) --------------------------------


async def test_same_seed_same_output_across_instances() -> None:
    async with _client() as a, _client() as b:
        ra = (await _chat(a, seed=42)).json()
        rb = (await _chat(b, seed=42)).json()
    assert ra["choices"] == rb["choices"]
    assert ra["usage"] == rb["usage"]


async def test_different_seed_different_output() -> None:
    async with _client() as c:
        r1 = (await _chat(c, seed=1)).json()
        r2 = (await _chat(c, seed=2)).json()
    assert r1["choices"][0]["message"]["content"] != r2["choices"][0]["message"]["content"]


async def test_seed_precedence_header_over_body_over_env() -> None:
    async with _client({"MOCK_SEED": "7"}) as c:
        env_only = (await _chat(c)).json()
        body_7 = (await _chat(c, seed=7)).json()
        body_8 = (await _chat(c, seed=8)).json()
        header_7 = (await _chat(c, headers={"X-Mock-Seed": "7"}, seed=8)).json()
    content = [r["choices"][0]["message"]["content"] for r in (env_only, body_7, body_8, header_7)]
    assert content[0] == content[1] == content[3] != content[2]


async def test_streamed_text_matches_non_streamed_for_same_seed() -> None:
    async with _client() as c:
        plain = (await _chat(c, seed=5)).json()
        streamed = await _chat(c, seed=5, stream=True)
    events = _parse_sse(streamed.text)
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events[:-1] if e["choices"])
    assert text == plain["choices"][0]["message"]["content"]


# --- usage accuracy -----------------------------------------------------------


async def test_usage_matches_generated_text() -> None:
    async with _client() as c:
        r = (await _chat(c, headers={"X-Mock-Output-Tokens": "17"})).json()
    content = r["choices"][0]["message"]["content"]
    assert r["usage"]["completion_tokens"] == count_tokens(content, "gpt-4o-mini") == 17
    assert r["usage"]["prompt_tokens"] == count_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert r["usage"]["total_tokens"] == r["usage"]["prompt_tokens"] + 17
    assert r["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4", "llama3.1:8b"])
def test_prompt_counts_equal_tollgate_estimates(model: str) -> None:
    """Tollgate estimates the prompt of a request the mock never finished; the
    mock's count for it must be the same number, or ledger and mock disagree."""
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    cases: list[list[dict[str, Any]]] = [
        MESSAGES,
        [
            {"role": "system", "content": "Be brief, please!"},
            {"role": "user", "name": "u", "content": [{"type": "text", "text": "hi"}, image]},
        ],
        [{"role": "assistant", "content": None, "tool_calls": [{"type": "function"}]}],
        [{"role": "user", "content": {"unexpected": "shape"}}, {"role": "user", "content": ["x"]}],
    ]
    for messages in cases:
        assert count_prompt_tokens(messages, model) == estimate_prompt_tokens(messages, model)


def test_every_vocabulary_word_is_one_token() -> None:
    # So a completion's token count is its word count however it is split.
    for word in VOCAB:
        assert count_tokens(word, "gpt-4o-mini") == count_tokens(" " + word, "gpt-4o-mini") == 1


async def test_max_tokens_truncates_with_length_finish() -> None:
    async with _client() as c:
        r = (await _chat(c, max_tokens=5)).json()
    assert r["usage"]["completion_tokens"] == 5
    assert r["choices"][0]["finish_reason"] == "length"


async def test_stream_final_chunk_carries_usage() -> None:
    async with _client() as c:
        r = await _chat(
            c,
            stream=True,
            stream_options={"include_usage": True},
            headers={"X-Mock-Output-Tokens": "4"},
        )
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    assert events[-1] == "[DONE]"
    usage_chunk = events[-2]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["completion_tokens"] == 4
    assert all(e["usage"] is None for e in events[:-2])
    assert events[-3]["choices"][0]["finish_reason"] == "stop"


async def test_stream_without_include_usage_has_no_usage() -> None:
    async with _client() as c:
        r = await _chat(c, stream=True)
    events = _parse_sse(r.text)
    assert all("usage" not in e for e in events[:-1])


# --- failure modes ------------------------------------------------------------


async def test_fail_500() -> None:
    async with _client() as c:
        r = await _chat(c, headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "500"})
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "server_error"


async def test_fail_429_has_retry_after() -> None:
    async with _client() as c:
        r = await _chat(c, headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "429"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "1"


async def test_fail_rate_zero_never_fails() -> None:
    async with _client({"MOCK_FAIL_MODE": "500"}) as c:
        codes = {(await _chat(c)).status_code for _ in range(50)}
    assert codes == {200}


async def test_fail_rate_is_roughly_honoured_and_seedable() -> None:
    async def failures(seed: str) -> int:
        async with _client({"MOCK_FAIL_RATE": "0.3", "MOCK_FAIL_SEED": seed}) as c:
            return sum([(await _chat(c)).status_code == 500 for _ in range(200)])

    first = await failures("123")
    assert 35 <= first <= 85
    assert await failures("123") == first


def test_fail_timeout_hangs_until_the_client_gives_up() -> None:
    # Real socket: the in-process ASGI transport ignores client timeouts.
    with run_server(create_app(env={"MOCK_HANG_S": "30"})) as url:
        with pytest.raises(httpx.ReadTimeout):
            httpx.post(
                f"{url}/v1/chat/completions",
                json={"model": "m", "messages": MESSAGES},
                headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "timeout"},
                timeout=0.3,
            )
        # Received but never answered: its prompt counts as partial.
        assert wait_until(lambda: _stats(url)["partial_prompt_tokens"] == _prompt("m"))


async def test_bad_header_is_400() -> None:
    async with _client() as c:
        for headers in (
            {"X-Mock-Fail-Rate": "2"},
            {"X-Mock-Fail-Mode": "explode"},
            {"X-Mock-Latency-Ms": "-1"},
            {"X-Mock-Seed": "abc"},
            {"X-Mock-Output-Tokens": "1.5"},
        ):
            r = await _chat(c, headers=headers)
            assert r.status_code == 400, headers


def test_bad_env_fails_at_startup() -> None:
    with pytest.raises(ValueError):
        create_app(env={"MOCK_FAIL_MODE": "nope"})


def test_env_and_header_names_cover_spec() -> None:
    b = Behaviour()
    names = {Behaviour.header_name(f) for f in b.__dataclass_fields__}
    assert names == {
        "x-mock-latency-ms",
        "x-mock-ttft-ms",
        "x-mock-tokens-per-sec",
        "x-mock-fail-rate",
        "x-mock-fail-mode",
        "x-mock-output-tokens",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"messages": MESSAGES},
        {"model": "m", "messages": []},
        {"model": "m", "messages": ["hi"]},
        {"model": "m", "messages": MESSAGES, "n": 2},
    ],
)
async def test_invalid_requests(body: dict[str, Any]) -> None:
    async with _client() as c:
        r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 400
    assert "error" in r.json()


async def test_invalid_json() -> None:
    async with _client() as c:
        r1 = await c.post("/v1/chat/completions", content=b"{nope")
        r2 = await c.post("/v1/chat/completions", json=[1])
    assert r1.status_code == r2.status_code == 400


# --- models, stats ------------------------------------------------------------


async def test_models_and_health() -> None:
    async with _client({"MOCK_MODELS": "a, b"}) as c:
        models = (await c.get("/v1/models")).json()
        assert (await c.get("/healthz")).status_code == 200
    assert [m["id"] for m in models["data"]] == ["a", "b"]


async def test_stats_count_served_tokens_and_reset() -> None:
    async with _client() as c:
        r1 = (await _chat(c)).json()
        await _chat(c, stream=True, stream_options={"include_usage": True})
        await _chat(c, headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "429"})
        await _chat(c, headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "500"})
        stats = (await c.get("/mock/stats")).json()
        assert stats["requests"] == 4
        assert stats["completed"] == 2
        assert stats["completion_tokens"] == 2 * r1["usage"]["completion_tokens"]
        assert stats["prompt_tokens"] == 2 * r1["usage"]["prompt_tokens"]
        assert stats["failures"]["429"] == stats["failures"]["500"] == 1
        # Error responses are billed nothing, so count nowhere.
        assert stats["partial_prompt_tokens"] == stats["partial_completion_tokens"] == 0
        reset = (await c.post("/mock/stats/reset")).json()
    assert reset["requests"] == 0


# --- timing and connection behaviour over a real socket -----------------------


def test_latency_knob() -> None:
    with run_server(create_app(env={})) as url:
        start = time.perf_counter()
        httpx.post(
            f"{url}/v1/chat/completions",
            json={"model": "m", "messages": MESSAGES},
            headers={"X-Mock-Latency-Ms": "150"},
        )
        assert time.perf_counter() - start >= 0.15


def test_ttft_and_pacing_over_socket() -> None:
    with run_server(create_app(env={})) as url:
        start = time.perf_counter()
        arrivals = []
        with httpx.stream(
            "POST",
            f"{url}/v1/chat/completions",
            json={"model": "m", "messages": MESSAGES, "stream": True},
            headers={
                "X-Mock-TTFT-Ms": "200",
                "X-Mock-Tokens-Per-Sec": "20",
                "X-Mock-Output-Tokens": "5",
            },
        ) as r:
            for line in r.iter_lines():
                if (
                    line.startswith("data: ")
                    and '"content":"' in line
                    and '"content":""' not in line
                ):
                    arrivals.append(time.perf_counter() - start)
    assert len(arrivals) == 5
    assert arrivals[0] >= 0.2
    # 4 gaps of 50 ms after the first token
    assert arrivals[-1] - arrivals[0] >= 0.19


def test_drop_mid_stream_severs_connection() -> None:
    app = create_app(env={})
    with run_server(app) as url:
        received = []
        with (
            pytest.raises(httpx.RemoteProtocolError),
            httpx.stream(
                "POST",
                f"{url}/v1/chat/completions",
                json={"model": "m", "messages": MESSAGES, "stream": True},
                headers={
                    "X-Mock-Fail-Rate": "1",
                    "X-Mock-Fail-Mode": "drop_mid_stream",
                    "X-Mock-Output-Tokens": "10",
                },
            ) as r,
        ):
            for line in r.iter_lines():
                received.append(line)
        stats = httpx.get(f"{url}/mock/stats").json()
    assert not any("[DONE]" in line for line in received)
    assert stats["failures"]["drop_mid_stream"] == 1
    assert stats["partial_completion_tokens"] == 5
    assert stats["partial_prompt_tokens"] == _prompt("m")
    assert stats["completed"] == 0


def test_drop_non_streaming_severs_connection() -> None:
    with run_server(create_app(env={})) as url:
        with pytest.raises(httpx.RemoteProtocolError):
            httpx.post(
                f"{url}/v1/chat/completions",
                json={"model": "m", "messages": MESSAGES},
                headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "drop_mid_stream"},
            )
        stats = _stats(url)
    assert (stats["completed"], stats["prompt_tokens"]) == (0, 0)
    assert stats["partial_prompt_tokens"] == _prompt("m")


def test_client_disconnect_is_counted_as_cancelled() -> None:
    with run_server(create_app(env={})) as url:
        with httpx.stream(
            "POST",
            f"{url}/v1/chat/completions",
            json={"model": "m", "messages": MESSAGES, "stream": True},
            headers={"X-Mock-Tokens-Per-Sec": "20", "X-Mock-Output-Tokens": "100"},
        ) as r:
            for i, _ in enumerate(r.iter_lines()):
                if i >= 3:
                    break
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stats = httpx.get(f"{url}/mock/stats").json()
            if stats["streams_cancelled"]:
                break
            time.sleep(0.05)
    assert stats["streams_cancelled"] == 1
    assert stats["completed"] == 0
    assert 0 < stats["partial_completion_tokens"] < 100
    assert stats["partial_prompt_tokens"] == _prompt("m")


def test_client_leaving_during_latency_counts_as_partial() -> None:
    with run_server(create_app(env={})) as url:
        with pytest.raises(httpx.ReadTimeout):
            httpx.post(
                f"{url}/v1/chat/completions",
                json={"model": "m", "messages": MESSAGES},
                headers={"X-Mock-Latency-Ms": "30000"},
                timeout=0.3,
            )
        assert wait_until(lambda: _stats(url)["partial_prompt_tokens"] == _prompt("m"))
        stats = _stats(url)
    assert (stats["completed"], stats["prompt_tokens"], stats["completion_tokens"]) == (0, 0, 0)


async def test_cancelled_handler_counts_as_partial() -> None:
    # In-process, a cancelled client cancels the mock's handler with it.
    app = create_app(env={})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as c:
        task = asyncio.create_task(
            c.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": MESSAGES},
                headers={"X-Mock-Latency-Ms": "30000"},
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert app.state.stats.partial_prompt_tokens == _prompt("m")
    assert app.state.stats.completed == 0


def _stats(url: str) -> dict[str, Any]:
    stats: dict[str, Any] = httpx.get(f"{url}/mock/stats").json()
    return stats


def _prompt(model: str) -> int:
    return count_prompt_tokens(MESSAGES, model)
