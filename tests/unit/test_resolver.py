"""Provider hostnames looked up in the background (SPEC Phase 2.1): what each
lookup changes, that connections only ever read the cache, and that a name that
hangs holds up nothing else. tests/integration/test_provider_dns.py checks it
through the whole gateway."""

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpcore
import httpx
import pytest

from tests.helpers import wait_until
from tollgate.config import ProvidersFile
from tollgate.observability.metrics import Metrics
from tollgate.providers.resolver import (
    CachedAddressBackend,
    ProviderResolver,
    ResolvedTransport,
    lookup_addresses,
    provider_hosts,
)

GONE = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
NO_ANSWER = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


class Names:
    """A scripted DNS: each name's next answer, an address tuple or an error."""

    def __init__(self, **answers: Any) -> None:
        self.answers = {k.replace("_", "."): v for k, v in answers.items()}
        self.asked: list[str] = []

    def __call__(self, name: str) -> tuple[str, ...]:
        self.asked.append(name)
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return tuple(answer)


def resolver_for(names: Names, *hosts: str, **options: Any) -> ProviderResolver:
    return ProviderResolver(hosts or names.answers, refresh_s=60, lookup=names, **options)


# --- what a lookup changes ---------------------------------------------------------------


def test_a_lookup_that_finds_addresses_caches_them() -> None:
    names = Names(a_test=["10.0.0.1", "10.0.0.2"])
    resolver = resolver_for(names)
    assert resolver.addresses("a.test") == ()  # nothing looked up yet
    assert resolver.refresh("a.test") == "ok"
    assert resolver.addresses("a.test") == ("10.0.0.1", "10.0.0.2")


@pytest.mark.parametrize("answer", [GONE, []], ids=["no-such-name", "no-addresses"])
def test_a_name_that_no_longer_exists_loses_its_addresses(answer: Any) -> None:
    names = Names(a_test=["10.0.0.1"])
    resolver = resolver_for(names)
    resolver.refresh("a.test")
    names.answers["a.test"] = answer
    assert resolver.refresh("a.test") == "gone"
    assert resolver.addresses("a.test") == ()


@pytest.mark.parametrize(
    "answer",
    [NO_ANSWER, OSError("network down"), TimeoutError(), UnicodeError("bad label")],
    ids=["eai-again", "oserror", "timeout", "anything-else"],
)
def test_no_answer_keeps_the_addresses_it_had(answer: Exception) -> None:
    """A resolver's outage mustn't take down a provider that is still up."""
    names = Names(a_test=["10.0.0.1"])
    resolver = resolver_for(names)
    resolver.refresh("a.test")
    names.answers["a.test"] = answer
    assert resolver.refresh("a.test") == "no_answer"
    assert resolver.addresses("a.test") == ("10.0.0.1",)


def test_no_answer_with_nothing_cached_leaves_nothing_to_connect_to() -> None:
    resolver = resolver_for(Names(a_test=NO_ANSWER))
    assert resolver.refresh("a.test") == "no_answer"
    assert resolver.addresses("a.test") == ()


def test_a_name_that_comes_back_is_used_again() -> None:
    names = Names(a_test=GONE)
    resolver = resolver_for(names)
    resolver.refresh("a.test")
    names.answers["a.test"] = ["10.0.0.9"]
    assert resolver.refresh("a.test") == "ok"
    assert resolver.addresses("a.test") == ("10.0.0.9",)


def test_address_literals_need_no_lookup() -> None:
    names = Names()
    resolver = ProviderResolver(["127.0.0.1", "::1"], refresh_s=60, lookup=names)
    resolver.start()
    assert resolver.addresses("127.0.0.1") == ("127.0.0.1",)
    assert resolver.addresses("::1") == ("::1",)
    resolver.stop()
    assert names.asked == []


def test_a_host_that_is_no_providers_is_never_looked_up() -> None:
    names = Names(a_test=["10.0.0.1"])
    resolver = resolver_for(names)
    assert resolver.addresses("elsewhere.test") == ()
    assert names.asked == []


def test_changes_are_logged_once_and_never_every_lookup(caplog: pytest.LogCaptureFixture) -> None:
    names = Names(a_test=["10.0.0.1"])
    resolver = resolver_for(names)
    with caplog.at_level(logging.INFO, logger="tollgate.providers.resolver"):
        for answer in (["10.0.0.1"], ["10.0.0.1"], GONE, GONE, ["10.0.0.1"], NO_ANSWER, NO_ANSWER):
            names.answers["a.test"] = answer
            resolver.refresh("a.test")
    assert [r.getMessage() for r in caplog.records] == [
        "provider host a.test resolves to 10.0.0.1",
        "provider host a.test doesn't resolve: attempts at it fail at once",
        "provider host a.test resolves to 10.0.0.1",
        "no answer looking up provider host a.test: keeping its 1 known addresses",
    ]


def test_every_lookup_is_counted_by_host_and_result() -> None:
    metrics = Metrics(["m"], ["p"])
    names = Names(a_test=["10.0.0.1"])
    resolver = resolver_for(names, "a.test", on_lookup=metrics.provider_lookup)
    resolver.refresh("a.test")
    names.answers["a.test"] = GONE
    resolver.refresh("a.test")
    text = metrics.exposition().decode()
    assert 'tollgate_provider_lookups_total{host="a.test",result="ok"} 1.0' in text
    assert 'tollgate_provider_lookups_total{host="a.test",result="gone"} 1.0' in text


def test_the_real_lookup_finds_this_machines_own_name() -> None:
    addresses = lookup_addresses("localhost")
    assert addresses and all(isinstance(a, str) for a in addresses)
    assert len(addresses) == len(set(addresses))  # each once, whatever the socket type


def test_provider_hosts_are_the_names_in_the_base_urls() -> None:
    config = ProvidersFile.model_validate(
        {
            "providers": [
                {"name": "a", "type": "openai_compat", "base_url": "https://API.Example.test/v1"},
                {"name": "b", "type": "openai_compat", "base_url": "http://mock_provider:8001/v1"},
                {"name": "c", "type": "openai_compat", "base_url": "http://127.0.0.1:8001/v1"},
                {"name": "d", "type": "openai_compat", "base_url": "http://[::1]:8001/v1"},
            ],
            "models": {},
        }
    )
    assert provider_hosts(config) == ["api.example.test", "mock_provider", "127.0.0.1", "::1"]


# --- in the background -------------------------------------------------------------------


class Hanging:
    """A DNS in which `hung` names block until released, and the rest answer."""

    def __init__(self, *hung: str) -> None:
        self.hung = set(hung)
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.in_flight = 0
        self.most_in_flight = 0
        self.lookups: dict[str, int] = {}

    def __call__(self, name: str) -> tuple[str, ...]:
        with self.lock:
            self.lookups[name] = self.lookups.get(name, 0) + 1
            if name in self.hung:
                self.in_flight += 1
                self.most_in_flight = max(self.most_in_flight, self.in_flight)
        if name in self.hung:
            self.release.wait(10)
            with self.lock:
                self.in_flight -= 1
            raise GONE
        return ("10.0.0.1",)


@pytest.fixture
def hanging() -> Iterator[Hanging]:
    dns = Hanging("dead.test")
    yield dns
    dns.release.set()


def test_a_name_that_hangs_holds_up_no_other_and_has_one_lookup_at_a_time(
    hanging: Hanging,
) -> None:
    resolver = ProviderResolver(["dead.test", "alive.test"], refresh_s=0.01, lookup=hanging)
    resolver.start()
    try:
        assert wait_until(lambda: hanging.lookups.get("alive.test", 0) >= 10)
        assert hanging.lookups["dead.test"] == 1  # still its first
        assert hanging.most_in_flight == 1
        assert resolver.addresses("alive.test") == ("10.0.0.1",)
    finally:
        resolver.stop()


def test_a_name_is_looked_up_again_and_again_until_stopped() -> None:
    names = Names(a_test=["10.0.0.1"])
    resolver = ProviderResolver(["a.test"], refresh_s=0.01, lookup=names)
    resolver.start()
    assert wait_until(lambda: len(names.asked) >= 3)
    resolver.stop()
    time.sleep(0.05)  # any lookup under way ends
    settled = len(names.asked)
    time.sleep(0.05)
    assert len(names.asked) == settled


async def test_starting_waits_for_the_first_lookups_but_never_for_a_name_that_hangs(
    hanging: Hanging,
) -> None:
    resolver = ProviderResolver(["dead.test", "alive.test"], refresh_s=60, lookup=hanging)
    resolver.start()
    started = time.perf_counter()
    await resolver.wait_ready(0.2)
    assert 0.15 < time.perf_counter() - started < 2  # gave up on it
    assert resolver.addresses("alive.test") == ("10.0.0.1",)
    resolver.stop()

    quick = ProviderResolver(["alive.test"], refresh_s=60, lookup=hanging)
    quick.start()
    started = time.perf_counter()
    await quick.wait_ready(5)
    assert time.perf_counter() - started < 2  # no need to wait it out
    quick.stop()


# --- connecting --------------------------------------------------------------------------


def scripted(**hosts: Any) -> ProviderResolver:
    resolver = resolver_for(Names(**hosts))
    for host in list(resolver._names):
        resolver.refresh(host)
    return resolver


async def test_a_name_that_does_not_resolve_fails_at_once_without_looking_anything_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_lookups(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a connection looked a name up")

    monkeypatch.setattr(socket, "getaddrinfo", no_lookups)
    backend = CachedAddressBackend(scripted(gone_test=GONE))
    started = time.perf_counter()
    with pytest.raises(httpcore.ConnectError, match=r"gone\.test doesn't resolve"):
        await backend.connect_tcp("gone.test", 443, timeout=30)
    assert time.perf_counter() - started < 0.1


async def test_connecting_goes_to_the_cached_address_not_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name itself is never passed on: that would look it up."""
    seen: list[tuple[str, int]] = []

    async def inner(self: Any, host: str, port: int, *args: Any) -> str:
        seen.append((host, port))
        return "stream"

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", inner)
    backend = CachedAddressBackend(scripted(a_test=["10.0.0.1"]))
    assert await backend.connect_tcp("a.test", 8001, timeout=1) == "stream"
    assert seen == [("10.0.0.1", 8001)]


async def test_each_address_gets_half_of_what_is_left_and_the_last_all_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets: list[tuple[str, float]] = []

    async def inner(self: Any, host: str, port: int, budget: float, *args: Any) -> Any:
        budgets.append((host, budget))
        if host == "10.0.0.3":
            return "stream"
        raise httpcore.ConnectError("refused")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", inner)
    backend = CachedAddressBackend(scripted(a_test=["10.0.0.1", "10.0.0.2", "10.0.0.3"]))
    assert await backend.connect_tcp("a.test", 8001, timeout=1.0) == "stream"
    assert [host for host, _ in budgets] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert [round(b, 1) for _, b in budgets] == [0.5, 0.5, 1.0]  # none used any of its time


async def test_when_every_address_fails_the_last_failure_is_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inner(self: Any, host: str, *args: Any) -> Any:
        raise httpcore.ConnectTimeout(host)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", inner)
    backend = CachedAddressBackend(scripted(a_test=["10.0.0.1", "10.0.0.2"]))
    with pytest.raises(httpcore.ConnectTimeout, match=r"10\.0\.0\.2"):
        await backend.connect_tcp("a.test", 8001, timeout=1.0)


async def test_no_timeout_means_none_for_any_address(monkeypatch: pytest.MonkeyPatch) -> None:
    budgets: list[Any] = []

    async def inner(self: Any, host: str, port: int, budget: Any, *args: Any) -> Any:
        budgets.append(budget)
        return "stream"

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", inner)
    backend = CachedAddressBackend(scripted(a_test=["10.0.0.1", "10.0.0.2"]))
    await backend.connect_tcp("a.test", 8001)
    assert budgets == [None]


# --- through httpx -----------------------------------------------------------------------


LIMITS = httpx.Limits(max_connections=5, max_keepalive_connections=2)


async def test_requests_really_go_through_the_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name no DNS knows reaches a local server, keeping its Host header. This
    fails if httpx ever stops using the pool ResolvedTransport puts in place."""
    seen: list[bytes] = []

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = ResolvedTransport(scripted(provider_test=["127.0.0.1"]), limits=LIMITS)
    async with server, httpx.AsyncClient(transport=transport) as client:
        r = await client.get(f"http://provider.test:{port}/v1/models")
    assert (r.status_code, r.text) == (200, "ok")
    assert f"Host: provider.test:{port}".encode() in seen[0].replace(b"host:", b"Host:")


async def test_a_name_that_does_not_resolve_is_a_connect_error_to_httpx() -> None:
    """What the provider turns into an unreachable upstream, which fails over."""
    transport = ResolvedTransport(scripted(gone_test=GONE), limits=LIMITS)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://gone.test/v1/models")


async def test_tls_is_still_verified_against_the_name_not_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The certificate check must see the provider's hostname, whatever address
    the connection went to."""
    tls: dict[str, Any] = {}
    connected: list[str] = []

    class Stream(httpcore.AsyncNetworkStream):
        async def start_tls(self, ssl_context: Any, server_hostname: Any = None, **_: Any) -> Any:
            tls["server_hostname"] = server_hostname
            tls["verifies"] = ssl_context.check_hostname
            raise httpcore.ConnectError("stop here: the handshake is what this test checks")

    async def inner(self: Any, host: str, *args: Any) -> Stream:
        connected.append(host)
        return Stream()

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", inner)
    transport = ResolvedTransport(scripted(api_example_test=["10.0.0.1"]), limits=LIMITS)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://api.example.test/v1/models")
    assert connected == ["10.0.0.1"]
    assert tls == {"server_hostname": "api.example.test", "verifies": True}
