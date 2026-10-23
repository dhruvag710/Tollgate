"""Fake OpenAI-compatible provider for tests and benchmarks.

Self-contained (no tollgate imports) so it ships as its own container.

Behaviour knobs. Each is read from the env var at startup and can be
overridden per request with the matching header:

    MOCK_LATENCY_MS        X-Mock-Latency-Ms        non-streaming: delay before the response
    MOCK_TTFT_MS           X-Mock-TTFT-Ms           streaming: delay before the first chunk
    MOCK_TOKENS_PER_SEC    X-Mock-Tokens-Per-Sec    streaming: pacing after token 1 (0 = none)
    MOCK_FAIL_RATE         X-Mock-Fail-Rate         probability in [0, 1] that the request fails
    MOCK_FAIL_MODE         X-Mock-Fail-Mode         500 | 429 | timeout | drop_mid_stream
    MOCK_OUTPUT_TOKENS     X-Mock-Output-Tokens     completion length (capped by max_tokens)
    MOCK_SEED              X-Mock-Seed              content seed (header > body `seed` > env)

Process-only settings: MOCK_MODELS (comma-separated list for /v1/models),
MOCK_HANG_S (how long `timeout` mode hangs), MOCK_FAIL_SEED (seeds the
fail/no-fail draws).

Tokens are counted with tiktoken exactly as Tollgate estimates them (same
model-to-encoding rule, same chat framing), so the ledger's estimates for
requests the mock never finished answering match the mock's own counts.
Generated text is vocabulary words joined by spaces, each a single token with
or without its leading space, so completion_tokens is exactly the number of
words emitted.

GET /mock/stats reports the tokens a correct ledger must match:
prompt/completion_tokens from responses delivered in full, and
partial_prompt/completion_tokens from requests received but not finished
(dropped, or the client left first). Error responses count in neither, as
nothing is billed for them. Run with ONE worker, or stats are per-process.
"""

import asyncio
import hashlib
import json
import os
import random
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

import tiktoken
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

FAIL_MODES = ("500", "429", "timeout", "drop_mid_stream")
DEFAULT_MODELS = "gpt-4o-mini,llama3.1:8b,mock-small"

# Every word is one token with or without a leading space (a test enforces it).
VOCAB = (
    "the quick brown fox over lazy dog while gateway routes every request through "
    "a that counts tokens and tracks so nothing past budget when traffic cache hits "
    "save money providers fail often help streams flow chunk by chunk until done"
).split()

# Prompt counting mirrors tollgate.ratelimit.estimate; a test pins the two together.
_FALLBACK_ENCODING = "o200k_base"
_MEDIA_PARTS = frozenset({"image_url", "input_audio", "file"})


def _encoding(model: str) -> tiktoken.Encoding:
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding(_FALLBACK_ENCODING)


def count_tokens(text: str, model: str) -> int:
    return len(_encoding(model).encode(text, disallowed_special=()))


def count_prompt_tokens(messages: list[dict[str, Any]], model: str) -> int:
    total = 3  # reply primer
    for message in messages:
        total += 3  # per-message framing
        for key, value in message.items():
            total += count_tokens(_as_text(value), model)
            if key == "name":
                total += 1
    return total


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


def generate_words(seed_material: bytes | None, n: int) -> list[str]:
    if seed_material is None:
        rng = random.Random()
    else:
        rng = random.Random(int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big"))
    return [rng.choice(VOCAB) for _ in range(n)]


class BadKnob(ValueError):
    pass


@dataclass(frozen=True)
class Behaviour:
    latency_ms: float = 0.0
    ttft_ms: float = 0.0
    tokens_per_sec: float = 0.0
    fail_rate: float = 0.0
    fail_mode: str = "500"
    output_tokens: int = 32

    @staticmethod
    def env_name(name: str) -> str:
        return "MOCK_" + name.upper()

    @staticmethod
    def header_name(name: str) -> str:
        return "x-mock-" + name.replace("_", "-")

    def with_overrides(self, source: Mapping[str, str], key: Any) -> "Behaviour":
        changes: dict[str, Any] = {}
        for f in fields(self):
            raw = source.get(key(f.name))
            if raw is not None:
                changes[f.name] = _parse_knob(f.name, raw)
        return replace(self, **changes)


def _parse_knob(name: str, raw: str) -> Any:
    raw = raw.strip()
    if name == "fail_mode":
        if raw not in FAIL_MODES:
            raise BadKnob(f"fail_mode must be one of {FAIL_MODES}, got {raw!r}")
        return raw
    try:
        value: float | int = int(raw) if name == "output_tokens" else float(raw)
    except ValueError as exc:
        raise BadKnob(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise BadKnob(f"{name} must be >= 0")
    if name == "fail_rate" and value > 1:
        raise BadKnob("fail_rate must be in [0, 1]")
    return value


def _parse_seed(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise BadKnob(f"seed must be an integer, got {raw!r}") from exc


@dataclass
class Stats:
    requests: int = 0
    completed: int = 0
    prompt_tokens: int = 0  # summed from usage of responses delivered in full
    completion_tokens: int = 0
    failures: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FAIL_MODES, 0))
    streams_cancelled: int = 0  # client went away mid-stream
    # Requests received but not finished: dropped, or the client left first.
    partial_prompt_tokens: int = 0
    partial_completion_tokens: int = 0  # tokens streamed before a drop or cancel

    def record_completed(self, usage: dict[str, int]) -> None:
        self.completed += 1
        self.prompt_tokens += usage["prompt_tokens"]
        self.completion_tokens += usage["completion_tokens"]

    def record_partial(self, prompt_tokens: int, completion_tokens: int = 0) -> None:
        self.partial_prompt_tokens += prompt_tokens
        self.partial_completion_tokens += completion_tokens


class MockDrop(Exception):
    """Raised inside a response body to sever the connection mid-response."""


def _error(
    status: int,
    message: str,
    type_: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": type_, "param": None, "code": code}},
        status_code=status,
        headers=headers,
    )


async def _client_stays(request: Request, seconds: float) -> bool:
    """Wait `seconds`. Returns False, at once, if the client disconnects first."""

    async def disconnected() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    try:
        await asyncio.wait_for(disconnected(), seconds)
    except TimeoutError:
        return True
    return False


def _sse(obj: Any) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


def create_app(env: Mapping[str, str] = os.environ) -> FastAPI:
    defaults = Behaviour().with_overrides(env, Behaviour.env_name)
    env_seed = _parse_seed(env["MOCK_SEED"]) if env.get("MOCK_SEED") else None
    hang_s = float(env.get("MOCK_HANG_S", "600"))
    fail_seed = env.get("MOCK_FAIL_SEED")
    fail_rng = random.Random(int(fail_seed) if fail_seed else None)
    models = [m.strip() for m in env.get("MOCK_MODELS", DEFAULT_MODELS).split(",") if m.strip()]

    app = FastAPI(title="Tollgate mock provider", docs_url=None, redoc_url=None)
    app.state.stats = Stats()

    def stats() -> Stats:
        s: Stats = app.state.stats
        return s

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "mock"} for m in models
            ],
        }

    @app.get("/mock/stats")
    async def get_stats() -> dict[str, Any]:
        return asdict(stats())

    @app.post("/mock/stats/reset")
    async def reset_stats() -> dict[str, Any]:
        app.state.stats = Stats()
        return asdict(stats())

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "Request body is not valid JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return _error(400, "Request body must be a JSON object", "invalid_request_error")

        try:
            beh = defaults.with_overrides(request.headers, Behaviour.header_name)
            header_seed = request.headers.get("x-mock-seed")
            seed = (
                _parse_seed(header_seed) if header_seed is not None else body.get("seed", env_seed)
            )
        except BadKnob as exc:
            return _error(400, str(exc), "invalid_request_error", "bad_mock_header")

        model = body.get("model")
        messages = body.get("messages")
        if not isinstance(model, str) or not model:
            return _error(400, "`model` is required", "invalid_request_error")
        if (
            not isinstance(messages, list)
            or not messages
            or not all(isinstance(m, dict) for m in messages)
        ):
            return _error(
                400, "`messages` must be a non-empty list of objects", "invalid_request_error"
            )
        if body.get("n", 1) != 1:
            return _error(400, "mock provider only supports n=1", "invalid_request_error")

        max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))
        n_tokens = beh.output_tokens
        finish_reason = "stop"
        if isinstance(max_tokens, int) and 0 <= max_tokens < n_tokens:
            n_tokens, finish_reason = max_tokens, "length"

        seed_material = None
        if seed is not None:
            seed_material = json.dumps([seed, model, messages], sort_keys=True).encode()
        words = generate_words(seed_material, n_tokens)
        prompt_tokens = count_prompt_tokens(messages, model)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": n_tokens,
            "total_tokens": prompt_tokens + n_tokens,
        }

        st = stats()
        st.requests += 1
        # Set once a response reaches the client, or a body generator takes
        # over the counting. Otherwise the request counts as partial.
        answered = False
        try:
            fail_mode = None
            if beh.fail_rate > 0 and fail_rng.random() < beh.fail_rate:
                fail_mode = beh.fail_mode
                st.failures[fail_mode] += 1
                if fail_mode == "500":
                    answered = True
                    return _error(500, "Mock provider internal error", "server_error")
                if fail_mode == "429":
                    answered = True
                    return _error(
                        429,
                        "Mock provider rate limit",
                        "rate_limit_exceeded",
                        "rate_limit_exceeded",
                        headers={"Retry-After": "1"},
                    )
                if fail_mode == "timeout":
                    if not await _client_stays(request, hang_s):
                        return Response()
                    answered = True
                    return _error(504, "Mock provider hung past MOCK_HANG_S", "server_error")

            drop = fail_mode == "drop_mid_stream"
            completion_id = "chatcmpl-" + secrets.token_hex(12)
            created = int(time.time())

            if body.get("stream"):
                include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                answered = True
                return StreamingResponse(
                    _stream(
                        st,
                        beh,
                        words,
                        usage,
                        finish_reason,
                        drop,
                        include_usage,
                        completion_id,
                        created,
                        model,
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                )

            payload = {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "system_fingerprint": "fp_mock",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": " ".join(words),
                            "refusal": None,
                        },
                        "logprobs": None,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": usage,
            }
            if beh.latency_ms and not await _client_stays(request, beh.latency_ms / 1000):
                return Response()
            answered = True
            if drop:
                return StreamingResponse(
                    _truncated(st, usage, json.dumps(payload).encode()),
                    media_type="application/json",
                )
            st.record_completed(usage)
            return JSONResponse(payload)
        finally:
            if not answered:
                st.record_partial(prompt_tokens)

    return app


async def _truncated(st: Stats, usage: dict[str, int], data: bytes) -> AsyncIterator[bytes]:
    yield data[: len(data) // 2]
    st.record_partial(usage["prompt_tokens"])
    raise MockDrop("drop_mid_stream")


async def _stream(
    st: Stats,
    beh: Behaviour,
    words: list[str],
    usage: dict[str, int],
    finish_reason: str,
    drop: bool,
    include_usage: bool,
    completion_id: str,
    created: int,
    model: str,
) -> AsyncIterator[bytes]:
    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "system_fingerprint": "fp_mock",
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
        }
        if include_usage:
            out["usage"] = None
        return out

    sent = 0
    finished = False
    try:
        if beh.ttft_ms:
            await asyncio.sleep(beh.ttft_ms / 1000)
        yield _sse(chunk({"role": "assistant", "content": "", "refusal": None}))
        drop_at = len(words) // 2 if drop else -1
        for i, word in enumerate(words):
            if i == drop_at:
                raise MockDrop("drop_mid_stream")
            if i and beh.tokens_per_sec:
                await asyncio.sleep(1 / beh.tokens_per_sec)
            yield _sse(chunk({"content": word if i == 0 else " " + word}))
            sent += 1
        yield _sse(chunk({}, finish_reason))
        if include_usage:
            final = chunk({})
            final["choices"] = []
            final["usage"] = usage
            yield _sse(final)
        yield b"data: [DONE]\n\n"
        finished = True
        st.record_completed(usage)
    except MockDrop:
        finished = True
        st.record_partial(usage["prompt_tokens"], sent)
        raise
    finally:
        if not finished:
            st.streams_cancelled += 1
            st.record_partial(usage["prompt_tokens"], sent)


app = create_app()
