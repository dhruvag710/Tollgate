import asyncio
import json
import time

import pytest

from tests.helpers import MemoryLedger
from tollgate.ratelimit.estimate import estimate_prompt_tokens
from tollgate.usage.ledger import LogLedger, Metering, UsageEvent, UsageMeter, parse_usage

MESSAGES = [{"role": "user", "content": "a secret prompt"}]


def _meter(
    ledger: MemoryLedger | None = None, started_at: float | None = None
) -> tuple[UsageMeter, MemoryLedger, Metering]:
    ledger = ledger or MemoryLedger()
    metering = Metering(ledger)
    meter = metering.meter(
        request_id="r1",
        started_at=time.perf_counter() if started_at is None else started_at,
        model="gpt-4o-mini",
        provider="mock",
        messages=MESSAGES,
    )
    return meter, ledger, metering


def test_parse_usage() -> None:
    assert parse_usage({"prompt_tokens": 1, "completion_tokens": 2}) == (1, 2)
    for bad in (
        None,
        [],
        {},
        {"prompt_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 1},
    ):
        assert parse_usage(bad) is None


async def test_reported_usage() -> None:
    start = time.perf_counter()
    meter, ledger, _ = _meter(started_at=start)
    async with meter.measure():
        meter.reported = (10, 5)
        meter.first_token_at = start + 0.25
        meter.settle("success")
    (event,) = ledger.events
    assert (event.status, event.prompt_tokens, event.completion_tokens) == ("success", 10, 5)
    assert not event.usage_estimated
    assert event.ttft_ms == 250
    assert event.latency_ms >= 0


async def test_unreported_usage_is_estimated() -> None:
    meter, ledger, _ = _meter()
    async with meter.measure():
        meter.mark_sent()
        meter.generated = "hello world"
        meter.settle("client_disconnect")
    (event,) = ledger.events
    assert event.usage_estimated
    assert event.prompt_tokens == estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert event.completion_tokens == 2
    assert event.ttft_ms is None


async def test_sent_without_response_estimates_the_prompt() -> None:
    meter, ledger, _ = _meter()
    async with meter.measure():
        meter.mark_sent()
        meter.settle("gateway_shutdown")
    (event,) = ledger.events
    assert event.usage_estimated
    assert event.prompt_tokens == estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert event.completion_tokens == 0


async def test_never_sent_records_zero_tokens() -> None:
    meter, ledger, _ = _meter()
    async with meter.measure():
        meter.settle("provider_error")
    (event,) = ledger.events
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


async def test_error_response_records_zero_tokens() -> None:
    meter, ledger, _ = _meter()
    async with meter.measure():
        meter.mark_sent()
        meter.reported = (0, 0)  # what the endpoint sets for an error status
        meter.settle("provider_error")
    (event,) = ledger.events
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


async def test_first_settle_wins() -> None:
    meter, ledger, _ = _meter()
    async with meter.measure():
        meter.settle("success")
        meter.settle("client_disconnect")
    assert ledger.events[0].status == "success"


async def test_internal_error_is_gateway_error_with_known_usage() -> None:
    meter, ledger, _ = _meter()
    with pytest.raises(RuntimeError, match="bug"):
        async with meter.measure():
            meter.mark_sent()
            meter.generated = "hello world"
            raise RuntimeError("bug")
    (event,) = ledger.events
    assert event.status == "gateway_error"
    assert event.usage_estimated
    assert event.completion_tokens == 2


async def test_leaving_without_an_outcome_is_gateway_error() -> None:
    meter, ledger, _ = _meter()
    async with meter.measure():
        pass
    assert ledger.events[0].status == "gateway_error"


async def test_cancellation_is_gateway_shutdown() -> None:
    meter, ledger, _ = _meter()
    entered = asyncio.Event()

    async def request() -> None:
        async with meter.measure():
            entered.set()
            await asyncio.Event().wait()  # e.g. waiting on the provider

    task = asyncio.create_task(request())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (event,) = ledger.events
    assert event.status == "gateway_shutdown"


async def test_measured_only_once() -> None:
    meter, ledger, _ = _meter()
    async with meter.measure():
        meter.settle("success")
    with pytest.raises(RuntimeError, match="already measured"):
        async with meter.measure():
            pass
    assert len(ledger.events) == 1


class SlowLedger(MemoryLedger):
    def __init__(self) -> None:
        super().__init__()
        self.writing = asyncio.Event()

    async def record(self, event: UsageEvent) -> None:
        self.writing.set()
        await asyncio.sleep(0.05)
        await super().record(event)


async def test_cancellation_mid_write_still_records_the_event() -> None:
    """uvicorn's shutdown can cancel a request while its event is being written."""
    ledger = SlowLedger()
    meter, _, metering = _meter(ledger)

    async def request() -> None:
        async with meter.measure():
            meter.settle("success")

    task = asyncio.create_task(request())
    await ledger.writing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(1):
        await metering.wait_idle()  # what the lifespan waits for at shutdown
    (event,) = ledger.events
    assert event.status == "success"


async def test_wait_idle_waits_for_every_open_meter() -> None:
    first, ledger, metering = _meter()
    second = metering.meter(
        request_id="r2", started_at=time.perf_counter(), model="m", provider="p", messages=[]
    )
    async with asyncio.timeout(1):
        await metering.wait_idle()  # nothing open

    async def request(meter: UsageMeter, entered: asyncio.Event, release: asyncio.Event) -> None:
        async with meter.measure():
            meter.settle("success")
            entered.set()
            await release.wait()

    gates = [(asyncio.Event(), asyncio.Event()) for _ in range(2)]
    tasks = [
        asyncio.create_task(request(m, *g)) for m, g in zip((first, second), gates, strict=True)
    ]
    for entered, _ in gates:
        await entered.wait()
    gates[0][1].set()
    await tasks[0]
    with pytest.raises(TimeoutError):  # the second request is still open
        async with asyncio.timeout(0.1):
            await metering.wait_idle()
    gates[1][1].set()
    await tasks[1]
    async with asyncio.timeout(1):
        await metering.wait_idle()
    assert [e.request_id for e in ledger.events] == ["r1", "r2"]


async def test_log_ledger_logs_event_without_prompt(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    event = UsageEvent("r1", "gpt-4o-mini", "mock", "success", 1, 2, False, 30, 10)
    await LogLedger().record(event)
    logged = json.loads(caplog.records[0].getMessage().removeprefix("usage_event "))
    assert logged["request_id"] == "r1"
    assert logged["completion_tokens"] == 2
    assert "secret" not in caplog.text
