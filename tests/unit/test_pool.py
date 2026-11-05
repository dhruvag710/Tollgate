"""The semantic cache's model pools (SPEC Phase 2.4): calls run off the event
loop, a full pool turns a call away at once, and a call that takes too long is
given up on, without the pool losing count of the work still running."""

import asyncio
import threading
import time

import pytest

from tollgate.cache.pool import ModelPool, PoolFull


async def test_a_call_runs_in_a_thread_of_the_pool_not_the_event_loop() -> None:
    pool = ModelPool("embed", queue_size=0)
    done = await pool.run(lambda: threading.current_thread().name, 5)
    assert done.value.startswith("tollgate-embed")
    assert done.value != threading.current_thread().name
    assert done.waited_s >= 0 and done.ran_s >= 0
    assert pool.in_flight() == 0
    pool.close()


async def test_a_call_that_finds_no_room_is_turned_away_at_once() -> None:
    pool = ModelPool("verify", queue_size=1)  # one running, one waiting
    gate = threading.Event()
    first = asyncio.ensure_future(pool.run(lambda: gate.wait(5), 5))
    second = asyncio.ensure_future(pool.run(lambda: "second", 5))
    await asyncio.sleep(0.05)
    assert pool.in_flight() == 2
    started = time.perf_counter()
    with pytest.raises(PoolFull):
        await pool.run(lambda: "third", 5)
    assert time.perf_counter() - started < 0.05  # never waited
    gate.set()
    assert (await first).value is True
    assert (await second).value == "second"
    assert pool.in_flight() == 0
    assert (await pool.run(lambda: "room again", 5)).value == "room again"
    pool.close()


async def test_a_call_that_takes_too_long_is_given_up_on_but_keeps_its_place() -> None:
    pool = ModelPool("verify", queue_size=0)
    gate, finished = threading.Event(), threading.Event()

    def slow() -> str:
        gate.wait(5)
        finished.set()
        return "late"

    with pytest.raises(TimeoutError):
        await pool.run(slow, 0.05)
    # The thread is still running the call: the pool counts it.
    assert pool.in_flight() == 1
    with pytest.raises(PoolFull):
        await pool.run(lambda: "no room", 5)
    gate.set()
    assert finished.wait(5)
    await asyncio.sleep(0.01)
    assert pool.in_flight() == 0  # released once it really ended


async def test_a_call_still_waiting_when_it_times_out_never_runs() -> None:
    pool = ModelPool("embed", queue_size=1)
    gate = threading.Event()
    ran: list[str] = []
    running = asyncio.ensure_future(pool.run(lambda: gate.wait(5), 5))
    await asyncio.sleep(0.02)
    with pytest.raises(TimeoutError):
        await pool.run(lambda: ran.append("waiting"), 0.05)
    assert pool.in_flight() == 1  # its place is free again
    gate.set()
    await running
    await asyncio.sleep(0.05)
    assert ran == []
    pool.close()


async def test_what_a_call_raises_reaches_the_caller() -> None:
    pool = ModelPool("embed", queue_size=0)

    def broken() -> None:
        raise ValueError("model failed")

    with pytest.raises(ValueError, match="model failed"):
        await pool.run(broken, 5)
    assert pool.in_flight() == 0
    pool.close()


async def test_time_waiting_for_the_pool_is_told_apart_from_the_call() -> None:
    pool = ModelPool("embed", queue_size=1)
    first = asyncio.ensure_future(pool.run(lambda: time.sleep(0.1), 5))
    await asyncio.sleep(0.01)
    second = await pool.run(lambda: time.sleep(0.02), 5)
    await first
    assert second.waited_s >= 0.05  # behind the first call
    assert 0.015 <= second.ran_s < 0.1
    pool.close()


def test_a_pool_needs_a_worker() -> None:
    with pytest.raises(ValueError):
        ModelPool("embed", workers=0, queue_size=1)
    with pytest.raises(ValueError):
        ModelPool("embed", queue_size=-1)
