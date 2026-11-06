"""Provider hostnames, looked up in the background, so no request ever waits on
a name lookup (SPEC Phase 2.1).

Why: B3 killed a provider's container, which takes its name out of Docker's DNS,
and a lookup of the name then took 3 to 8 s to fail. Each new connection made
one on the event loop's few lookup threads, and a lookup its request gave up on
stayed queued and still ran, so every later lookup waited behind it: the healthy
provider's too, whose connections then timed out (BENCHMARKS.md).

Here each name has a thread of its own, which looks it up every `refresh_s`, one
lookup at a time, so a name that hangs holds up nothing but itself. A connection
only reads what the last lookup found, and never waits:

- Addresses: it connects to them, in order.
- The name doesn't exist (the resolver said so, or found no address): nothing is
  kept, and connecting fails at once. The router counts that attempt against the
  provider's breaker like any failed attempt, and the request fails over with no
  time lost.
- No answer (the resolver is down or timed out): what was cached stays. A
  resolver's outage mustn't take down providers that are still up; one that is
  down too fails to connect within its connect timeout. With nothing cached,
  connecting fails at once.

TLS is unaffected: httpcore verifies the provider's certificate against the name
in its URL, not the address connected to.
"""

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterable
from typing import Literal

import anyio
import httpcore
import httpx

from tollgate.config import ProvidersFile

log = logging.getLogger(__name__)

LookupResult = Literal["ok", "gone", "no_answer"]
# gaierror codes that are the resolver's answer: no such name. Any other means
# it gave none (EAI_AGAIN and the like). Not every platform has each.
_NAME_GONE = frozenset(
    getattr(socket, name) for name in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, name)
)


def lookup_addresses(host: str) -> tuple[str, ...]:
    """The name's addresses, as the C library finds them. Blocking."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


def provider_hosts(config: ProvidersFile) -> list[str]:
    """Each provider's host, as httpcore will ask to connect to it."""
    return [httpx.URL(p.base_url).raw_host.decode("ascii") for p in config.providers]


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class ProviderResolver:
    def __init__(
        self,
        hosts: Iterable[str],
        *,
        refresh_s: float,
        lookup: Callable[[str], tuple[str, ...]] = lookup_addresses,
        on_lookup: Callable[[str, LookupResult], None] | None = None,
    ) -> None:
        self._names = tuple(dict.fromkeys(h for h in hosts if not _is_address(h)))
        self._refresh_s = refresh_s
        self._lookup = lookup
        self._on_lookup = on_lookup
        # Written by the names' threads, read by connections on the event loop:
        # each entry is replaced whole.
        self._found: dict[str, tuple[str, ...]] = {}
        self._last: dict[str, LookupResult] = {}
        self._stopping = threading.Event()
        self._each_looked_up = threading.Event()
        if not self._names:
            self._each_looked_up.set()

    def addresses(self, host: str) -> tuple[str, ...]:
        """Where to connect to `host` right now: none if it doesn't resolve or
        isn't a provider's. Never waits, and never looks anything up."""
        found = self._found.get(host)
        if found is not None:
            return found
        return (host,) if _is_address(host) else ()

    def start(self) -> None:
        """A daemon thread per name: a lookup still hanging can't hold up the
        gateway's exit. It only reads and writes this object."""
        for name in self._names:
            thread = threading.Thread(
                target=self._keep_fresh, args=(name,), name=f"resolve-{name}", daemon=True
            )
            thread.start()

    async def wait_ready(self, timeout_s: float) -> None:
        """Give every name's first lookup up to `timeout_s`, so a starting
        gateway has addresses for its first requests. A name that hasn't
        answered by then doesn't stop it starting."""
        await anyio.to_thread.run_sync(self._each_looked_up.wait, timeout_s)

    def stop(self) -> None:
        self._stopping.set()

    def _keep_fresh(self, name: str) -> None:
        while not self._stopping.is_set():
            started = time.monotonic()
            self.refresh(name)
            self._stopping.wait(max(0.0, self._refresh_s - (time.monotonic() - started)))

    def refresh(self, name: str) -> LookupResult:
        """One lookup of `name`, and what it changes. Blocking."""
        found: tuple[str, ...] = ()
        result: LookupResult
        try:
            found = self._lookup(name)
        except socket.gaierror as exc:
            result = "gone" if exc.errno in _NAME_GONE else "no_answer"
        except Exception as exc:  # whatever it is, this thread must go on
            log.warning("looking up provider host %s failed: %s", name, type(exc).__name__)
            result = "no_answer"
        else:
            result = "ok" if found else "gone"
        before, last = self._found.get(name, ()), self._last.get(name)
        if result != "no_answer":
            self._found[name] = found
        self._last[name] = result
        if len(self._last) == len(self._names):
            self._each_looked_up.set()
        self._log_change(name, result, last, before, found)
        if self._on_lookup is not None:
            self._on_lookup(name, result)
        return result

    @staticmethod
    def _log_change(
        name: str,
        result: LookupResult,
        last: LookupResult | None,
        before: tuple[str, ...],
        found: tuple[str, ...],
    ) -> None:
        if result == "ok" and found != before:
            log.info("provider host %s resolves to %s", name, ", ".join(found))
        elif result == "gone" and last != "gone":
            log.warning("provider host %s doesn't resolve: attempts at it fail at once", name)
        elif result == "no_answer" and last != "no_answer":
            log.warning(
                "no answer looking up provider host %s: keeping its %d known addresses",
                name,
                len(before),
            )


class CachedAddressBackend(httpcore.AnyIOBackend):
    """Connects to a provider at an address the resolver already has."""

    def __init__(self, resolver: ProviderResolver) -> None:
        self._resolver = resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109  # httpcore's interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = self._resolver.addresses(host)
        if not addresses:
            raise httpcore.ConnectError(f"{host} doesn't resolve")
        # The timeout covers them all: each gets half of what is left, and the
        # last all of it, so the first (usually the only) one has the most time.
        deadline = None if timeout is None else time.monotonic() + timeout
        failure: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for remaining, address in zip(range(len(addresses) - 1, -1, -1), addresses, strict=True):
            budget = None
            if deadline is not None:
                budget = max(0.0, deadline - time.monotonic()) / (2 if remaining else 1)
            try:
                return await super().connect_tcp(
                    address, port, budget, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None
        raise failure


class ResolvedTransport(httpx.AsyncHTTPTransport):
    """httpx's own transport, with connections made through the resolver. The
    one thing httpx won't let a caller choose is its pool's network backend, so
    the pool is built here as httpx builds it (tests/unit/test_resolver.py
    checks that requests really go through this one)."""

    def __init__(self, resolver: ProviderResolver, *, limits: httpx.Limits) -> None:
        ssl_context = httpx.create_ssl_context()
        super().__init__(verify=ssl_context, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=CachedAddressBackend(resolver),
        )
