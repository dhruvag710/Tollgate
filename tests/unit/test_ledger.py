import json
import time

import pytest

from tests.helpers import MemoryLedger
from tollgate.ratelimit.estimate import estimate_prompt_tokens
from tollgate.usage.ledger import LogLedger, UsageEvent, UsageMeter, parse_usage

MESSAGES = [{"role": "user", "content": "a secret prompt"}]


def _meter(ledger: MemoryLedger, started_at: float) -> UsageMeter:
    return UsageMeter(
        ledger=ledger,
        request_id="r1",
        started_at=started_at,
        model="gpt-4o-mini",
        provider="mock",
        messages=MESSAGES,
    )


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


async def test_meter_reported_usage() -> None:
    ledger = MemoryLedger()
    start = time.perf_counter()
    await _meter(ledger, start).record("success", reported=(10, 5), first_token_at=start + 0.25)
    (event,) = ledger.events
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (10, 5, False)
    assert event.ttft_ms == 250
    assert event.latency_ms >= 0


async def test_meter_estimates_without_reported_usage() -> None:
    ledger = MemoryLedger()
    await _meter(ledger, time.perf_counter()).record("client_disconnect", generated="hello world")
    (event,) = ledger.events
    assert event.usage_estimated
    assert event.prompt_tokens == estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert event.completion_tokens == 2
    assert event.ttft_ms is None


async def test_meter_records_zero_when_nothing_was_billed() -> None:
    ledger = MemoryLedger()
    await _meter(ledger, time.perf_counter()).record("provider_error")
    (event,) = ledger.events
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


async def test_log_ledger_logs_event_without_prompt(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    event = UsageEvent("r1", "gpt-4o-mini", "mock", "success", 1, 2, False, 30, 10)
    await LogLedger().record(event)
    logged = json.loads(caplog.records[0].getMessage().removeprefix("usage_event "))
    assert logged["request_id"] == "r1"
    assert logged["completion_tokens"] == 2
    assert "secret" not in caplog.text
