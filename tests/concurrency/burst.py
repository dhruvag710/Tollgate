"""Bursts of truly concurrent requests: every connection is opened first, then
every request written before any reply is read. An HTTP client library spends
too long per request to send 500 at once."""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import h11
import httpx
from redis.asyncio import Redis


async def redis_now(redis: Redis) -> float:
    seconds, micros = await redis.time()
    return seconds + micros / 1e6


@dataclass(frozen=True)
class Reply:
    status_code: int
    headers: dict[str, str]  # names lowercased
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


def _chat_request(url: str, headers: dict[str, str], body: bytes) -> list[h11.Event]:
    return [
        h11.Request(
            method="POST",
            target="/v1/chat/completions",
            headers=[
                ("Host", urlsplit(url).netloc),
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
                *headers.items(),
            ],
        ),
        h11.Data(data=body),
        h11.EndOfMessage(),
    ]


async def _reply(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: list[h11.Event]
) -> Reply:
    conn = h11.Connection(h11.CLIENT)
    for sent in request:
        conn.send(sent)  # already written; this only advances the parser's state
    status, headers, body = 0, {}, b""
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            conn.receive_data(await reader.read(65536))
        elif isinstance(event, h11.Response):
            status = event.status_code
            headers = {k.decode().lower(): v.decode() for k, v in event.headers}
        elif isinstance(event, h11.Data):
            body += event.data
        elif isinstance(event, h11.EndOfMessage):
            writer.close()
            return Reply(status, headers, body)


async def burst(
    url: str, key: str, n: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> tuple[list[Reply], float]:
    """`n` concurrent chat requests, and how long they took in Redis time. The
    key is cached first (a key's first two sightings read Postgres); nothing
    else is warmed up."""
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(base_url=url, timeout=10) as client:
        for _ in range(2):
            assert (await client.get("/v1/models", headers=auth)).status_code == 200
    request = _chat_request(url, {**auth, **(headers or {})}, json.dumps(body).encode())
    packer = h11.Connection(h11.CLIENT)
    packed = b"".join(packer.send(event) or b"" for event in request)
    parts = urlsplit(url)
    conns = await asyncio.gather(
        *(asyncio.open_connection(parts.hostname, parts.port) for _ in range(n))
    )
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        started = await redis_now(redis)
        for _, writer in conns:
            writer.write(packed)
        replies = await asyncio.wait_for(
            asyncio.gather(*(_reply(r, w, request) for r, w in conns)), timeout=60
        )
        return list(replies), await redis_now(redis) - started
    finally:
        await redis.aclose()
