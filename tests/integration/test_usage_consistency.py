"""The usage invariant end to end, as C4 checks it at scale: ledger totals equal
the mock's own counters exactly, including requests cut off after they were
sent, except for disconnected streams. Tokens in flight when the gateway hangs
up on those are counted by the mock but never reach the gateway, so their
completions may fall short, within a bound (SPEC §4, C4).
"""

from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from mock_provider.app import create_app as create_mock_app
from tests.helpers import (
    MemoryLedger,
    in_flight_bound,
    run_server,
    serve,
    wait_until,
    write_providers,
)
from tollgate.config import Settings
from tollgate.main import create_app

FAIL = "X-Mock-Fail-Rate"
MODE = "X-Mock-Fail-Mode"


def test_ledger_totals_match_mock_counters_exactly(tmp_path: Path) -> None:
    ledger = MemoryLedger()
    with serve(create_mock_app(env={"MOCK_HANG_S": "30"}), timeout_graceful_shutdown=0.1) as (
        mock_url,
        _,
    ):
        # A short read timeout, so a hung provider cuts requests off quickly.
        config = write_providers(tmp_path, f"{mock_url}/v1", timeout_s=0.5)
        app = create_app(Settings(providers_config=config), ledger=ledger)
        with run_server(app) as url:
            requests: list[tuple[bool, dict[str, str], int]] = [
                (False, {}, 200),  # completed
                (True, {}, 200),  # completed, streamed
                (False, {FAIL: "1", MODE: "500"}, 500),  # error responses: nothing billed
                (True, {FAIL: "1", MODE: "429"}, 429),
                (False, {FAIL: "1", MODE: "timeout"}, 504),  # sent, then cut off
                (True, {FAIL: "1", MODE: "timeout"}, 504),
                (False, {FAIL: "1", MODE: "drop_mid_stream"}, 502),  # sent, response broken
                (True, {FAIL: "1", MODE: "drop_mid_stream"}, 200),
            ]
            for i, (stream, headers, status) in enumerate(requests):
                # Prompts of different lengths, so totals can't match by symmetry.
                messages = [{"role": "user", "content": "how many tokens " * (i + 1)}]
                r = httpx.post(
                    f"{url}/v1/chat/completions",
                    json={"model": "gpt-4o-mini", "messages": messages, "stream": stream},
                    headers={"X-Mock-Output-Tokens": "10", **headers},
                    timeout=10,
                )
                assert r.status_code == status, (i, r.text)

            def totals_match() -> bool:
                stats = _mock_stats(mock_url)
                return len(ledger.events) == len(requests) and (
                    sum(e.prompt_tokens for e in ledger.events),
                    sum(e.completion_tokens for e in ledger.events),
                ) == (
                    stats["prompt_tokens"] + stats["partial_prompt_tokens"],
                    stats["completion_tokens"] + stats["partial_completion_tokens"],
                )

            # The mock notices a cut-off a moment after the gateway records it.
            assert wait_until(totals_match), (ledger.events, _mock_stats(mock_url))
            stats = _mock_stats(mock_url)

    assert Counter(e.status for e in ledger.events) == {"success": 2, "provider_error": 6}
    estimated = [e for e in ledger.events if e.usage_estimated]
    assert len(estimated) == 4  # the timeouts and the drops
    assert stats["partial_prompt_tokens"] == sum(e.prompt_tokens for e in estimated)
    assert stats["partial_completion_tokens"] == 5  # half the dropped stream's 10 tokens


def test_disconnects_fall_short_by_at_most_the_tokens_in_flight(tmp_path: Path) -> None:
    rate = 10  # tokens/s: 1000 tokens would take 100 s if nobody hung up
    reads = [2, 4, 6]  # tokens each client reads before hanging up
    ledger = MemoryLedger()
    with run_server(create_mock_app(env={})) as mock_url:
        config = write_providers(tmp_path, f"{mock_url}/v1")
        app = create_app(Settings(providers_config=config), ledger=ledger)
        with run_server(app) as url, httpx.Client(timeout=10) as client:
            for i, k in enumerate(reads):
                messages = [{"role": "user", "content": "how many tokens " * (i + 1)}]
                with client.stream(
                    "POST",
                    f"{url}/v1/chat/completions",
                    json={"model": "gpt-4o-mini", "messages": messages, "stream": True},
                    headers={"X-Mock-Tokens-Per-Sec": str(rate), "X-Mock-Output-Tokens": "1000"},
                ) as r:
                    seen = 0
                    for line in r.iter_lines():
                        seen += '"content":"' in line and '"content":""' not in line
                        if seen == k:
                            break  # leaving the block hangs up

            def all_settled() -> bool:
                stats = _mock_stats(mock_url)
                return stats["streams_cancelled"] == len(reads) == len(ledger.events)

            assert wait_until(all_settled)
            stats = _mock_stats(mock_url)

    assert [e.status for e in ledger.events] == ["client_disconnect"] * len(reads)
    assert (stats["completed"], stats["prompt_tokens"], stats["completion_tokens"]) == (0, 0, 0)
    # Prompts reconcile exactly.
    assert sum(e.prompt_tokens for e in ledger.events) == stats["partial_prompt_tokens"]
    # Completions: at least what each client read, never more than the mock
    # sent, and short of it by no more than the tokens in flight.
    for event, k in zip(ledger.events, reads, strict=True):
        assert event.completion_tokens >= k
    gap = stats["partial_completion_tokens"] - sum(e.completion_tokens for e in ledger.events)
    assert 0 <= gap <= len(reads) * in_flight_bound(rate)


def _mock_stats(mock_url: str) -> dict[str, Any]:
    stats: dict[str, Any] = httpx.get(f"{mock_url}/mock/stats").json()
    return stats
