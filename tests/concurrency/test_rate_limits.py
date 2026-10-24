"""C1-C3 (SPEC §8): rate limits hold under concurrency, on one gateway and
across replicas, and TPM reservations reconcile to what was actually used.

The RPM bucket refills continuously: at 100 rpm, one request every 0.6 s. A
burst admits its 100, plus whatever refills before its last check, so C1 and
C2 assert 100 <= admitted <= 100 + floor(span * rpm / 60), with the span
measured in Redis time; that is exactly 100 whenever the burst fits in 0.6 s.
The barrier variant of C1 holds every request at the limiter until all have
arrived, then releases them together, which pins it to exactly 100.

Each burst opens all its connections first, then writes every request before
reading any reply: an HTTP client library spends too long per request to send
500 at once. Keys are cached beforehand, so each check is one Redis call.
"""

import asyncio
import json
import os
import platform
import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import h11
import httpx
from redis.asyncio import Redis
from testcontainers.core.container import DockerContainer

from tests.helpers import (
    ROOT,
    MemoryLedger,
    issue_key,
    run_server,
    run_uvicorn_process,
    wait_until,
    write_providers,
)
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.ratelimit.estimate import estimate_prompt_tokens

MODEL = "gpt-4o-mini"
MESSAGES = [{"role": "user", "content": "one more"}]
RPM = 100
BURST = 500


def chat(**fields: Any) -> dict[str, Any]:
    return {"model": MODEL, "messages": MESSAGES, "max_tokens": 10, **fields}


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


def _request(
    url: str, method: str, target: str, headers: dict[str, str], body: bytes = b""
) -> list[h11.Event]:
    return [
        h11.Request(
            method=method,
            target=target,
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


async def _fire(
    url: str, request: list[h11.Event], n: int, redis: Redis
) -> tuple[list[Reply], float]:
    """`request` on `n` connections at once, and how long they took in Redis time."""
    packer = h11.Connection(h11.CLIENT)
    packed = b"".join(packer.send(event) or b"" for event in request)
    parts = urlsplit(url)
    conns = await asyncio.gather(
        *(asyncio.open_connection(parts.hostname, parts.port) for _ in range(n))
    )
    started = await redis_now(redis)
    for _, writer in conns:
        writer.write(packed)
    replies = await asyncio.wait_for(
        asyncio.gather(*(_reply(r, w, request) for r, w in conns)), timeout=60
    )
    return list(replies), await redis_now(redis) - started


async def burst(
    url: str, key: str, n: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> tuple[list[Reply], float]:
    """`n` concurrent chat requests, and how long they took in Redis time."""
    auth = {"Authorization": f"Bearer {key}"}
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            # Fill the key cache: a key's first two sightings read Postgres.
            for _ in range(6):
                assert (await client.get("/v1/models", headers=auth)).status_code == 200
        # Then a concurrent wave, which isn't rate limited, so every gateway has
        # its Redis connections open before the burst. While they open, requests
        # left waiting for one past REDIS_TIMEOUT_S fail closed with a 503.
        models, _ = await _fire(url, _request(url, "GET", "/v1/models", auth), n, redis)
        assert 200 in {r.status_code for r in models} <= {200, 503}
        chat = _request(
            url,
            "POST",
            "/v1/chat/completions",
            {**auth, **(headers or {})},
            json.dumps(body).encode(),
        )
        return await _fire(url, chat, n, redis)
    finally:
        await redis.aclose()


def admitted(replies: list[Reply]) -> int:
    """How many got through; asserts every other was turned away by the RPM limit."""
    statuses = Counter(r.status_code for r in replies)
    assert set(statuses) <= {200, 429}, statuses
    rejected = {r.json()["error"]["code"] for r in replies if r.status_code == 429}
    assert rejected <= {"rpm_exceeded"}
    return statuses[200]


def assert_rpm_held(replies: list[Reply], span: float) -> None:
    """The burst admitted the bucket's capacity plus at most what refilled while
    it ran, and not one request fewer."""
    refilled = int(span * RPM / 60)
    assert RPM <= admitted(replies) <= RPM + refilled, (
        f"{admitted(replies)} admitted over {span:.3f}s of Redis time, "
        f"in which {refilled} requests refilled"
    )


def gateway_env(tmp_path: Path, mock_url: str) -> dict[str, str]:
    return {
        "PROVIDERS_CONFIG": str(write_providers(tmp_path, f"{mock_url}/v1")),
        "LOG_LEVEL": "WARNING",
    }


# --- C1 --------------------------------------------------------------------------------


async def test_c1_single_instance_admits_the_rpm_limit(tmp_path: Path) -> None:
    issued = await issue_key(rpm_limit=RPM, tpm_limit=100_000_000)
    # The gateway in a process of its own, as deployed, not sharing this one's CPU.
    with (
        run_uvicorn_process("mock_provider.app:app") as mock_url,
        run_uvicorn_process(
            "--factory", "tollgate.main:create_app", env=gateway_env(tmp_path, mock_url)
        ) as url,
    ):
        replies, span = await burst(url, issued.raw_key, BURST, chat())
    assert_rpm_held(replies, span)


async def test_c1_atomic_checks_admit_exactly_the_rpm_limit(tmp_path: Path) -> None:
    """All 500 checks reach Redis together, so no refill can land among them:
    exactly 100 admitted, 400 turned away. The gateway holds them back
    (tests/concurrency/barrier_app.py), in a process of its own so neither this
    one nor coverage tracing slows the checks down."""
    issued = await issue_key(rpm_limit=RPM, tpm_limit=100_000_000)
    with run_uvicorn_process("mock_provider.app:app") as mock_url:
        env = {**gateway_env(tmp_path, mock_url), "BARRIER_SIZE": str(BURST)}
        with run_uvicorn_process(
            "--factory", "tests.concurrency.barrier_app:create_app", env=env
        ) as url:
            replies, _ = await burst(url, issued.raw_key, BURST, chat())
            async with httpx.AsyncClient(base_url=url) as client:
                checks_s = (await client.get("/barrier")).json()["checks_s"]
    assert len(replies) == BURST
    assert admitted(replies) == RPM, f"the checks took {checks_s:.3f}s"


# --- C2 --------------------------------------------------------------------------------


def nginx_config(ports: list[int]) -> str:
    """The shipped nginx/nginx.conf, upstreams pointed at gateways on this host.
    Each response names the replica that served it, which the shipped config
    doesn't reveal."""
    shipped = (ROOT / "nginx" / "nginx.conf").read_text()
    servers = "\n".join(f"        server host.docker.internal:{port};" for port in ports)
    config, n = re.subn(r"^\s*server gateway:8000;$", servers, shipped, flags=re.MULTILINE)
    assert n == 1
    config, n = re.subn(
        r"^(\s*)proxy_pass http://gateway;$",
        r"\g<0>\n\1add_header X-Upstream $upstream_addr always;",
        config,
        flags=re.MULTILINE,
    )
    assert n == 1
    return config


@contextmanager
def nginx_in_front_of(ports: list[int], tmp_path: Path) -> Iterator[str]:
    conf = tmp_path / "nginx.conf"
    conf.write_text(nginx_config(ports), newline="\n")
    container = (
        DockerContainer("nginx:1.27-alpine")
        .with_volume_mapping(str(conf), "/etc/nginx/nginx.conf", "ro")
        .with_exposed_ports(8000)
        # Docker Desktop provides host.docker.internal; on Linux this maps it.
        .with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8000)}"

        def proxying() -> bool:
            try:
                return httpx.get(f"{url}/healthz", timeout=1).status_code == 200
            except httpx.TransportError:
                return False

        assert wait_until(proxying, timeout=30), container.get_logs()
        yield url


async def test_c2_replicas_behind_nginx_admit_the_rpm_limit(tmp_path: Path) -> None:
    issued = await issue_key(rpm_limit=RPM, tpm_limit=100_000_000)
    # Docker Desktop reaches host loopback; a Linux bridge needs a real interface.
    host = "0.0.0.0" if platform.system() == "Linux" else "127.0.0.1"
    with ExitStack() as stack:
        mock_url = stack.enter_context(run_uvicorn_process("mock_provider.app:app"))
        env = gateway_env(tmp_path, mock_url)
        replicas = [
            stack.enter_context(
                run_uvicorn_process("--factory", "tollgate.main:create_app", env=env, host=host)
            )
            for _ in range(3)
        ]
        ports = [urlsplit(url).port or 0 for url in replicas]
        url = stack.enter_context(nginx_in_front_of(ports, tmp_path))
        replies, span = await burst(url, issued.raw_key, BURST, chat())
    assert_rpm_held(replies, span)
    # Spread across all three: each replica rate limited part of the burst.
    served = Counter(int(r.headers["x-upstream"].rsplit(":", 1)[1]) for r in replies)
    assert set(served) == set(ports), served


# --- C3 --------------------------------------------------------------------------------


async def test_c3_tpm_reservations_admit_what_fits_and_reconcile_to_actual_use(
    tmp_path: Path,
) -> None:
    tpm, max_tokens, output, n = 1_000, 100, 10, 20
    reservation = estimate_prompt_tokens(MESSAGES, MODEL) + max_tokens
    fits = tpm // reservation  # 9 of the 20
    issued = await issue_key(rpm_limit=10_000, tpm_limit=tpm)
    bucket_key = f"tg:rl:tpm:{issued.key.tenant_id}"
    ledger = MemoryLedger()
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        with run_uvicorn_process("mock_provider.app:app") as mock_url:
            config = write_providers(tmp_path, f"{mock_url}/v1")
            app = create_app(Settings(providers_config=config), ledger=ledger)
            with run_server(app) as url:
                started = await redis_now(redis)
                body = chat(max_tokens=max_tokens)
                mock_output = {"X-Mock-Output-Tokens": str(output)}
                responses, _ = await burst(url, issued.raw_key, n, body, mock_output)
                assert wait_until(lambda: len(ledger.events) == n)
                used = sum(e.prompt_tokens + e.completion_tokens for e in ledger.events)
                # Each reconciliation runs just after its usage event is recorded;
                # until the last one has, the balance is below limit - used.
                deadline = time.monotonic() + 5
                while float((bucket := await redis.hgetall(bucket_key))["tokens"]) < tpm - used:
                    assert time.monotonic() < deadline, (bucket, tpm - used)
                    await asyncio.sleep(0.01)
    finally:
        await redis.aclose()

    # Over-limit requests, and only those, were turned away (refill during the
    # burst, ~17 tokens a second, is far short of another reservation).
    assert sum(r.status_code == 200 for r in responses) == fits
    rejected = [r.json()["error"]["code"] for r in responses if r.status_code != 200]
    assert rejected == ["tpm_exceeded"] * (n - fits)
    assert used == fits * (reservation - max_tokens + output)  # the mock's own counts
    # The balance is limit - actual use, plus at most what refilled since the
    # burst began, by the bucket's own (Redis) clock.
    balance, refill = float(bucket["tokens"]), tpm / 60 * (float(bucket["ts"]) - started)
    assert 0 <= balance - (tpm - used) <= refill, (balance, tpm - used, refill)
