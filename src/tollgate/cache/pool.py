"""A bounded pool of threads for one of the semantic cache's models (SPEC
Phase 2.4), so a model runs off the event loop and only so many calls wait
for it.

A call that finds the pool full, or that takes longer than its timeout
(waiting and running together), is turned away; the semantic layer treats
either as a miss, and the request goes on to a provider without waiting
further. A thread can't be stopped once it has started a call: a call that
timed out runs to its end and its result is thrown away, and it keeps its
place in the pool until then, so the bound counts the work the CPU is really
doing. A call that times out before a thread has taken it never runs.
"""

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


class PoolFull(Exception):
    """As many calls as the pool takes are already running or waiting."""


@dataclass(frozen=True)
class Done[T]:
    value: T
    waited_s: float  # in the queue, before a thread took the call
    ran_s: float  # the call itself


class ModelPool:
    """`workers` threads and room for `queue_size` calls waiting for them."""

    def __init__(self, name: str, *, workers: int = 1, queue_size: int) -> None:
        if workers < 1 or queue_size < 0:
            raise ValueError("a pool needs a worker, and a queue of zero or more")
        self.name = name
        self.capacity = workers + queue_size
        self._executor = ThreadPoolExecutor(workers, thread_name_prefix=f"tollgate-{name}")
        self._lock = threading.Lock()
        self._taken = 0

    def in_flight(self) -> int:
        """Calls running or waiting, including any that timed out but still run."""
        return self._taken

    async def run[T](self, call: Callable[[], T], timeout_s: float) -> Done[T]:
        """Run `call()` in the pool. Raises PoolFull if there is no room,
        TimeoutError if it isn't done within `timeout_s`, or what it raised."""
        with self._lock:
            if self._taken >= self.capacity:
                raise PoolFull(self.name)
            self._taken += 1
        submitted = time.perf_counter()
        times: list[float] = []

        def timed() -> T:
            times.append(time.perf_counter())
            try:
                return call()
            finally:
                times.append(time.perf_counter())

        try:
            future = self._executor.submit(timed)
        except BaseException:
            self._release(None)
            raise
        # Released when the call ends, or when it is cancelled before starting.
        future.add_done_callback(self._release)
        value = await asyncio.wait_for(asyncio.wrap_future(future), timeout_s)
        started, ended = times
        return Done(value, started - submitted, ended - started)

    def _release(self, _: object) -> None:
        with self._lock:
            self._taken -= 1

    def close(self) -> None:
        """Stop taking calls; those still waiting never run."""
        self._executor.shutdown(wait=False, cancel_futures=True)
