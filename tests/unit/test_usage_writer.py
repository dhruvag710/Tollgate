"""The ledger's batched writer, against a fake database."""

import asyncio
import logging
import time
from collections.abc import Sequence

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from tollgate.usage.ledger import UsageEvent, UsageWriter


def event(i: int) -> UsageEvent:
    return UsageEvent(f"r{i}", "t1", "k1", "gpt-4o-mini", "mock", "success", 1, 2, False, 30, 10)


class FakeDatabase:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.batches: list[list[str]] = []
        self.written_at: list[float] = []
        self._failures = failures or []

    async def write(self, events: Sequence[UsageEvent]) -> None:
        if self._failures:
            raise self._failures.pop(0)
        self.batches.append([e.request_id for e in events])
        self.written_at.append(time.monotonic())


def unreachable() -> OperationalError:
    return OperationalError("INSERT ...", {}, ConnectionRefusedError())


async def test_a_full_batch_is_written_at_once() -> None:
    db, writer = FakeDatabase(), UsageWriter(batch_size=3, interval_s=10)
    writer.start(db.write)
    for i in range(7):
        await writer.record(event(i))
    await asyncio.sleep(0.05)
    assert db.batches == [["r0", "r1", "r2"], ["r3", "r4", "r5"]]  # r6 waits for more
    await writer.close(timeout_s=1)
    assert db.batches[-1] == ["r6"]


async def test_a_partial_batch_waits_at_most_the_interval() -> None:
    db, writer = FakeDatabase(), UsageWriter(batch_size=200, interval_s=0.2)
    writer.start(db.write)
    recorded = time.monotonic()
    await writer.record(event(1))
    await writer.record(event(2))
    await asyncio.sleep(0.1)
    assert db.batches == []
    await asyncio.sleep(0.25)
    assert db.batches == [["r1", "r2"]]
    assert 0.15 <= db.written_at[0] - recorded <= 0.35
    await writer.close(timeout_s=1)


async def test_shutdown_writes_what_is_queued_without_waiting_the_interval() -> None:
    db, writer = FakeDatabase(), UsageWriter(batch_size=200, interval_s=60)
    writer.start(db.write)
    for i in range(3):
        await writer.record(event(i))
    started = time.monotonic()
    await writer.close(timeout_s=5)
    assert db.batches == [["r0", "r1", "r2"]]
    assert time.monotonic() - started < 1
    assert writer.pending == 0


async def test_a_batch_is_retried_while_the_database_is_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = FakeDatabase(failures=[unreachable(), unreachable()])
    writer = UsageWriter(batch_size=2, interval_s=0.01, retry_s=0.01)
    writer.start(db.write)
    await writer.record(event(1))
    await writer.record(event(2))
    await writer.close(timeout_s=2)
    assert db.batches == [["r1", "r2"]]  # written once, after two failures
    assert caplog.text.count("could not write 2 usage events, retrying: OperationalError") == 2


async def test_a_batch_the_database_refuses_is_dropped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refused = IntegrityError("INSERT ...", {}, ValueError("violates check constraint"))
    db = FakeDatabase(failures=[refused])
    writer = UsageWriter(batch_size=1, interval_s=0.01)
    writer.start(db.write)
    await writer.record(event(1))
    await writer.record(event(2))
    await writer.close(timeout_s=2)
    assert db.batches == [["r2"]]  # the next batch isn't blocked behind it
    assert "dropped 1 usage events the ledger refused: IntegrityError" in caplog.text
    assert "check constraint" not in caplog.text  # type names only


async def test_shutdown_reports_what_it_could_not_write(caplog: pytest.LogCaptureFixture) -> None:
    db = FakeDatabase(failures=[unreachable()] * 1000)
    writer = UsageWriter(batch_size=10, interval_s=0.01, retry_s=0.01)
    writer.start(db.write)
    for i in range(3):
        await writer.record(event(i))
    with caplog.at_level(logging.ERROR):
        await writer.close(timeout_s=0.2)
    assert db.batches == []
    assert "shut down with 3 usage events not written to the ledger" in caplog.text
