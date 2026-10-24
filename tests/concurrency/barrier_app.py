"""The gateway, with its rate-limit checks made to wait for each other (C1,
atomic variant). Run it as its own process:

    BARRIER_SIZE=500 uvicorn --factory tests.concurrency.barrier_app:create_app

Each chat request is held just before its auth + rate-limit call until
BARRIER_SIZE have arrived, then all are released together, and each is held
again after its call until every one has returned, so nothing else runs on the
gateway while the checks do. GET /barrier reports how long they took.
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tollgate.auth.cache import KeyCache, Lookup
from tollgate.main import create_app as create_gateway


class Barrier:
    def __init__(self, cache: KeyCache, size: int) -> None:
        self._lookup = cache.lookup
        self._size = size
        self._arrived = self._checked = 0
        self._everyone_here, self._everyone_checked = asyncio.Event(), asyncio.Event()
        self.released = self.finished = 0.0

    async def lookup(self, key_hash: str, *, reservation: int | None = None) -> Lookup:
        if reservation is None:  # not a chat request: no rate limit
            return await self._lookup(key_hash)
        self._arrived += 1
        if self._arrived == self._size:
            self.released = time.perf_counter()
            self._everyone_here.set()
        await self._everyone_here.wait()
        seen = await self._lookup(key_hash, reservation=reservation)
        self._checked += 1
        if self._checked == self._size:
            self.finished = time.perf_counter()
            self._everyone_checked.set()
        await self._everyone_checked.wait()
        return seen


def create_app() -> FastAPI:
    app = create_gateway()
    size = int(os.environ["BARRIER_SIZE"])
    gateway_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with gateway_lifespan(app):
            cache: KeyCache = app.state.key_cache
            app.state.barrier = Barrier(cache, size)
            cache.lookup = app.state.barrier.lookup  # type: ignore[method-assign]
            yield

    app.router.lifespan_context = lifespan

    @app.get("/barrier")
    async def barrier() -> dict[str, float]:
        held: Barrier = app.state.barrier
        return {"checks_s": held.finished - held.released}

    return app
