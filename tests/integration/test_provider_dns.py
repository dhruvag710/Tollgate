"""A provider whose hostname stops resolving (SPEC Phase 2.1), through the whole
gateway, with mock providers on real sockets and a scripted DNS behind
socket.getaddrinfo.

B3 found the bug this guards against. Killing a provider's container takes its
name out of Docker's DNS, and a lookup of it then takes seconds to fail. Each new
connection made one, on the event loop's few lookup threads, so lookups for the
healthy provider waited behind them and its connections timed out too. Here a
killed name's lookups hang, then say the name doesn't exist, as B3 measured them
doing.
"""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from mock_provider.app import create_app as create_mock_app
from tests.helpers import serve
from tests.integration.test_failover import (
    MESSAGES,
    MIN_REQUESTS,
    Gateway,
    Mock,
    eventually,
    gateway,
    providers_config,
)

PRIMARY, SECONDARY = "primary.test", "secondary.test"
BURST = 64  # more than any pool of lookup threads a machine's event loop has
HANG_S = 2.0  # what a killed name's lookup takes to fail (B3: 3 to 8 s)
CONNECT_TIMEOUT_S = 0.3
UNLIMITED = {"rpm_limit": 10_000_000, "tpm_limit": 2_000_000_000}


class ScriptedDNS:
    """Stands in for socket.getaddrinfo. `addresses` answers for each known name;
    a name in `killed` hangs, then doesn't exist; one in `silent` gets no answer.
    Anything else (Postgres, Redis) goes to the real thing."""

    def __init__(self) -> None:
        self.real = socket.getaddrinfo
        self.addresses = {PRIMARY: "127.0.0.1", SECONDARY: "127.0.0.1"}
        self.killed: dict[str, float] = {}
        self.silent: set[str] = set()
        self.asked: dict[str, int] = {}
        self.stop_hanging = threading.Event()

    def __call__(self, name: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        host = name.decode() if isinstance(name, bytes) else name  # anyio passes bytes
        if host not in self.addresses:
            return self.real(name, port, *args, **kwargs)
        self.asked[host] = self.asked.get(host, 0) + 1
        if host in self.killed:
            self.stop_hanging.wait(self.killed[host])
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host in self.silent:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return self.real(self.addresses[host], port, *args, **kwargs)


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[ScriptedDNS]:
    scripted = ScriptedDNS()
    monkeypatch.setattr(socket, "getaddrinfo", scripted)
    monkeypatch.setenv("PROVIDER_DNS_REFRESH_S", "0.05")
    yield scripted
    scripted.stop_hanging.set()


def by_name(url: str, name: str) -> str:
    return url.replace("127.0.0.1", name)


def port_of(url: str) -> int:
    return httpx.URL(url).port or 80


def closed(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


async def chat_timed(g: Gateway, **fields: Any) -> tuple[httpx.Response, float]:
    body = {"model": "gpt-4o-mini", "messages": MESSAGES, **fields}
    started = time.perf_counter()
    r = await g.client.post("/v1/chat/completions", json=body)
    return r, time.perf_counter() - started


async def test_a_provider_whose_name_stops_resolving_does_not_slow_requests_to_the_healthy_one(
    tmp_path: Path, dns: ScriptedDNS
) -> None:
    primary, secondary = create_mock_app(env={}), create_mock_app(env={})
    with serve(primary) as (p_url, p_server), serve(secondary) as (s_url, _):
        config = providers_config(
            tmp_path,
            by_name(p_url, PRIMARY),
            by_name(s_url, SECONDARY),
            connect_timeout_s=CONNECT_TIMEOUT_S,
            secondary_overrides={"connect_timeout_s": CONNECT_TIMEOUT_S},
        )
        async with gateway(config, **UNLIMITED) as g:
            # All well: the primary answers, and the secondary has no connections.
            r, _ = await chat_timed(g)
            assert r.headers["x-tollgate-provider"] == "mock", r.text
            await g.settled()

            # The primary's container is killed: it stops, and its name goes.
            dns.killed[PRIMARY] = HANG_S
            p_server.should_exit = True
            assert await eventually(lambda: closed(port_of(p_url)), within_s=10)
            asked_before = dns.asked.get(PRIMARY, 0)

            # A burst at once, while the dead name's lookups are hanging.
            results = await asyncio.gather(*(chat_timed(g) for _ in range(BURST)))
            await g.settled()

    statuses = [r.status_code for r, _ in results]
    assert statuses == [200] * BURST, [r.text for r, _ in results if r.status_code != 200][:2]
    served_by = {r.headers["x-tollgate-provider"] for r, _ in results}
    assert served_by == {"mock-secondary"}
    # Each paid at most the connect timeout at the dead provider, and no lookup.
    slowest = max(elapsed for _, elapsed in results)
    assert slowest < HANG_S, f"the slowest request took {slowest:.2f} s"
    # Requests never looked the dead name up: only the background did, one at a time.
    assert dns.asked.get(PRIMARY, 0) - asked_before <= 2


async def test_a_name_that_stops_resolving_fails_attempts_at_once_and_counts_against_the_breaker(
    tmp_path: Path, dns: ScriptedDNS
) -> None:
    """The provider itself is still up here, with no connection open to it: that
    no attempt reaches it shows each one failed on the lookup's result, without
    connecting or waiting. (A connection already open needs no lookup, and goes
    on working; a killed provider's die with it.)"""
    primary, secondary = create_mock_app(env={}), create_mock_app(env={})
    with serve(primary) as (p_url, _), serve(secondary) as (s_url, _):
        config = providers_config(tmp_path, by_name(p_url, PRIMARY), by_name(s_url, SECONDARY))
        async with gateway(config, **UNLIMITED) as g:
            dns.killed[PRIMARY] = 0.0  # this time the lookup fails straight away
            asked = dns.asked[PRIMARY]
            assert await eventually(lambda: dns.asked[PRIMARY] >= asked + 2)

            for _ in range(MIN_REQUESTS):
                r, elapsed = await chat_timed(g)
                assert r.headers["x-tollgate-provider"] == "mock-secondary", r.text
                assert elapsed < 1.0
            await g.settled()
            attempts = await g.metric(
                "tollgate_provider_attempts_total", provider="mock", result="connect_error"
            )
            state = (await g.breaker())["state"]
            lookups = await g.metric("tollgate_provider_lookups_total", host=PRIMARY, result="gone")

    assert Mock(primary, p_url).stats.requests == 0  # never connected
    assert attempts == MIN_REQUESTS
    assert state == "open"
    assert lookups >= 2


async def test_a_name_that_comes_back_takes_traffic_again(tmp_path: Path, dns: ScriptedDNS) -> None:
    primary, secondary = create_mock_app(env={}), create_mock_app(env={})
    with serve(primary) as (p_url, _), serve(secondary) as (s_url, _):
        config = providers_config(tmp_path, by_name(p_url, PRIMARY), by_name(s_url, SECONDARY))
        async with gateway(config, **UNLIMITED) as g:
            dns.killed[PRIMARY] = 0.0
            asked = dns.asked[PRIMARY]
            assert await eventually(lambda: dns.asked[PRIMARY] >= asked + 2)
            assert (await g.chat()).headers["x-tollgate-provider"] == "mock-secondary"

            del dns.killed[PRIMARY]
            asked = dns.asked[PRIMARY]
            assert await eventually(lambda: dns.asked[PRIMARY] >= asked + 2)
            assert (await g.chat()).headers["x-tollgate-provider"] == "mock"


async def test_a_name_that_never_resolved_does_not_stop_the_gateway_or_slow_requests(
    tmp_path: Path, dns: ScriptedDNS
) -> None:
    secondary = create_mock_app(env={})
    dns.killed[PRIMARY] = 30.0  # never answers while this test runs
    with serve(secondary) as (s_url, _):
        config = providers_config(tmp_path, f"http://{PRIMARY}:9", by_name(s_url, SECONDARY))
        started = time.perf_counter()
        async with gateway(config, **UNLIMITED) as g:
            assert time.perf_counter() - started < 15  # started without it
            r, elapsed = await chat_timed(g)
            assert r.headers["x-tollgate-provider"] == "mock-secondary", r.text
            assert elapsed < 1.0
            # A model with no other provider: an error at once, not a wait.
            only, elapsed = await chat_timed(g, model="llama3.1:8b")
            assert (only.status_code, only.json()["error"]["code"]) == (502, "upstream_unreachable")
            assert elapsed < 1.0
    dns.stop_hanging.set()


async def test_a_resolver_outage_does_not_take_down_a_provider_that_is_up(
    tmp_path: Path, dns: ScriptedDNS
) -> None:
    primary, secondary = create_mock_app(env={}), create_mock_app(env={})
    with serve(primary) as (p_url, _), serve(secondary) as (s_url, _):
        config = providers_config(tmp_path, by_name(p_url, PRIMARY), by_name(s_url, SECONDARY))
        async with gateway(config, **UNLIMITED) as g:
            assert (await g.chat()).headers["x-tollgate-provider"] == "mock"
            dns.silent.update({PRIMARY, SECONDARY})
            asked = dns.asked[PRIMARY]
            assert await eventually(lambda: dns.asked[PRIMARY] >= asked + 2)
            for _ in range(MIN_REQUESTS + 2):
                r = await g.chat()
                assert r.headers["x-tollgate-provider"] == "mock", r.text
            assert (await g.breaker())["state"] == "closed"
            lookups = await g.metric(
                "tollgate_provider_lookups_total", host=PRIMARY, result="no_answer"
            )
    assert lookups >= 2


async def test_with_a_proxy_configured_the_proxy_resolves_the_names(
    tmp_path: Path, dns: ScriptedDNS, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx sends everything through the proxy, as it did before the resolver
    existed: the gateway itself looks nothing up."""
    monkeypatch.setattr("tollgate.main.getproxies", lambda: {"http": "http://proxy.test:3128"})
    primary, secondary = create_mock_app(env={}), create_mock_app(env={})
    with serve(primary) as (p_url, _), serve(secondary) as (s_url, _):
        config = providers_config(tmp_path, by_name(p_url, PRIMARY), by_name(s_url, SECONDARY))
        async with gateway(config, **UNLIMITED):
            await asyncio.sleep(0.2)
    assert dns.asked == {}
