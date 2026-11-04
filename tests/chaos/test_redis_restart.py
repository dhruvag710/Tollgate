"""X4 (SPEC §8): Redis restarted during load. While it is down the gateway
fails closed, refusing requests with a 503 rather than serving them without
authentication or rate limits; once it is back, requests succeed again
without the gateway being restarted."""

import asyncio
import subprocess
import time
from dataclasses import dataclass

import httpx
import redis

from tests.chaos.conftest import GATEWAY, REDIS_URL, TIMEOUT, Tenant, compose

WORKERS = 8
# While Redis is down a request takes about REDIS_TIMEOUT_S (1 s) to be refused,
# so each worker gets through a few in this time.
OUTAGE_S = 5
BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "still there?"}]}


@dataclass(frozen=True)
class Outcome:
    started: float
    ended: float
    status: int
    code: str | None  # the OpenAI error code, if an error


def redis_answers() -> bool:
    client = redis.Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1)
    try:
        return bool(client.ping())
    except redis.RedisError:
        return False
    finally:
        client.close()


def started(container: str) -> str:
    """When the container last started, and how often it has restarted."""
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}} {{.RestartCount}}", container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


async def test_x4_redis_restarted_under_load_fails_closed_and_recovers(tenant: Tenant) -> None:
    container = compose("ps", "-q", "gateway").strip()
    assert container, "no gateway container"
    before = started(container)
    outcomes: list[Outcome] = []
    stop = asyncio.Event()

    async def load(client: httpx.AsyncClient) -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                r = await client.post("/v1/chat/completions", json=BODY)
                status, code = r.status_code, None
                if status != 200:
                    code = r.json()["error"]["code"]
            except httpx.TransportError:
                status, code = 0, "transport_error"
            outcomes.append(Outcome(started, time.monotonic(), status, code))
            await asyncio.sleep(0.02)

    async with httpx.AsyncClient(base_url=GATEWAY, headers=tenant.auth, timeout=TIMEOUT) as client:
        workers = [asyncio.create_task(load(client)) for _ in range(WORKERS)]
        await asyncio.sleep(3)  # steady load first
        await asyncio.to_thread(compose, "stop", "redis")
        down_at = time.monotonic()  # stopped: anything that starts now finds it gone
        assert not await asyncio.to_thread(redis_answers)
        await asyncio.sleep(OUTAGE_S)
        not_ready = await client.get("/readyz")
        up_from = time.monotonic()  # from here on Redis may be back at any moment
        await asyncio.to_thread(compose, "start", "redis")
        deadline = time.monotonic() + 30
        while not await asyncio.to_thread(redis_answers):
            assert time.monotonic() < deadline, "redis never came back"
            await asyncio.sleep(0.2)
        back_at = time.monotonic()
        await asyncio.sleep(8)  # load carries on
        stop.set()
        await asyncio.gather(*workers)
        ready = await client.get("/readyz")

    # Nothing but successes and fail-closed refusals, at any point.
    assert {(o.status, o.code) for o in outcomes} <= {(200, None), (503, "auth_unavailable")}, {
        (o.status, o.code) for o in outcomes
    }
    # While Redis was down, every request was refused: none got through
    # without its key checked and its limits applied.
    during = [o for o in outcomes if o.started >= down_at and o.ended <= up_from]
    assert len(during) >= WORKERS, len(during)  # every worker, at least once
    assert {o.status for o in during} == {503}
    assert (not_ready.status_code, not_ready.json()["redis"]) == (503, "unavailable")
    # Once it was back, requests succeeded again, with the same gateway process.
    settled = [o for o in outcomes if o.started >= back_at + 3]
    assert len(settled) >= 20 and {o.status for o in settled} == {200}
    assert ready.status_code == 200
    assert started(container) == before  # never restarted
    assert sum(o.status == 200 for o in outcomes if o.ended <= down_at) > 0  # load was real
