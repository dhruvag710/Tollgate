"""The gateway's shutdown completes, and promptly.

Regression: shutdown waits for the circuit breakers' reports still running
(Breakers.wait_idle). It used to loop while any report task was in its set,
awaiting asyncio.gather on them. When every one had already finished but the
done callbacks that remove them hadn't run yet, gather returned a future that
was already done, awaiting it never yielded to the event loop, the callbacks
never ran, and the loop spun forever, past every timeout: a timeout needs the
loop to run. So the shutdown runs on a thread with its own event loop here,
and the test fails if that thread hasn't finished within a few seconds.
"""

import asyncio
import threading

from tests.helpers import TEST_PROVIDERS, MemoryLedger
from tollgate.config import Settings
from tollgate.main import create_app

SHUTDOWN_DEADLINE_S = 5.0
# Nothing is listening: the gateway connects lazily, and this test sends no requests.
SETTINGS = {
    "providers_config": TEST_PROVIDERS,
    "key_pepper": "shutdown-test-pepper-not-a-secret",
    "redis_url": "redis://127.0.0.1:1/0",
    "database_url": "postgresql+asyncpg://tollgate:tollgate@127.0.0.1:1/tollgate",
    "redis_min_connections": 0,
}


def finishes_within(seconds: float, scenario: "asyncio.Future[None] | object") -> bool:
    """Run the coroutine function `scenario` on its own thread and event loop;
    whether it finished in time. Its exceptions are re-raised here."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(scenario())  # type: ignore[operator]
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)  # a spinning one dies with the process
    thread.start()
    thread.join(seconds)
    if errors:
        raise errors[0]
    return not thread.is_alive()


def test_shutdown_completes_with_finished_breaker_reports_still_tracked() -> None:
    reached_shutdown = threading.Event()

    async def scenario() -> None:
        app = create_app(Settings(**SETTINGS), ledger=MemoryLedger())  # type: ignore[arg-type]
        async with app.router.lifespan_context(app):
            # A report that has finished, its removal from the set not yet run:
            # the state the old wait spun on.
            finished = asyncio.create_task(asyncio.sleep(0))
            await finished
            app.state.router.breakers._tasks.add(finished)
            reached_shutdown.set()

    assert finishes_within(SHUTDOWN_DEADLINE_S, scenario), (
        f"shutdown didn't complete within {SHUTDOWN_DEADLINE_S} s"
    )
    assert reached_shutdown.is_set()


def test_shutdown_still_waits_for_reports_in_flight() -> None:
    """The fix waits for the pending reports, rather than skipping them."""
    waited: list[bool] = []

    async def scenario() -> None:
        app = create_app(Settings(**SETTINGS), ledger=MemoryLedger())  # type: ignore[arg-type]
        async with app.router.lifespan_context(app):
            report_done = asyncio.Event()

            async def slow_report() -> None:
                await asyncio.sleep(0.3)
                report_done.set()

            breakers = app.state.router.breakers
            task = asyncio.create_task(slow_report())
            breakers._tasks.add(task)
            task.add_done_callback(breakers._tasks.discard)
        waited.append(report_done.is_set())

    assert finishes_within(SHUTDOWN_DEADLINE_S, scenario)
    assert waited == [True]
