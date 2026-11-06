"""B3's trickle of timeouts at the healthy provider, reproduced on its own: do
name lookups a caller has given up on still hold up later ones?

Before the gateway resolved provider names in the background, each new
connection looked its name up through the event loop. The gateway runs on
uvloop, which hands lookups to libuv's small thread pool, and nothing cancels a
lookup that is queued there when the caller stops waiting for it.

This does what a request did: starts a lookup of the killed provider's name and
gives up after the connect timeout of the time, 3 s, ABANDONED times over. Then it
times a lookup of the healthy provider's name.

Runs inside a gateway container (bench/b3_dns_check.sh), with the primary killed:

    docker exec -i <gateway> python - [ABANDONED] < bench/b3_lookup_backlog.py
"""

import asyncio
import socket
import sys
import time

import uvloop

DEAD, ALIVE = "mock_provider", "mock_provider_secondary"
GIVE_UP_S = 3.0


async def lookup(name: str) -> None:
    await asyncio.get_running_loop().getaddrinfo(name, 8001, type=socket.SOCK_STREAM)


async def given_up_on() -> None:
    try:
        await asyncio.wait_for(lookup(DEAD), GIVE_UP_S)
    except (TimeoutError, OSError):
        pass


async def main(abandoned: int) -> None:
    started = time.perf_counter()
    await lookup(ALIVE)
    print(f"backlog healthy name, nothing queued: {time.perf_counter() - started:.3f} s")

    started = time.perf_counter()
    await asyncio.gather(*(given_up_on() for _ in range(abandoned)))
    gave_up = time.perf_counter() - started
    print(f"backlog {abandoned} lookups of the killed name, all given up on within {gave_up:.1f} s")

    started = time.perf_counter()
    await lookup(ALIVE)
    print(
        f"backlog healthy name, once those were given up on: {time.perf_counter() - started:.1f} s"
    )


if __name__ == "__main__":
    abandoned_lookups = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    asyncio.run(main(abandoned_lookups), loop_factory=uvloop.new_event_loop)
