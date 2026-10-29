"""Usage events and the ledger they go to (request lifecycle steps 8 and 9).

The ledger is Postgres: UsageWriter queues events in memory and a background
task batch-inserts them. Once an event is recorded, the request is settled in
Redis: its TPM reservation reconciled and its cost added to its tenant's spend.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

import anyio
from redis.exceptions import RedisError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import UsageEventRow
from tollgate.ratelimit import estimate
from tollgate.ratelimit.limiter import RateLimiter, Reservation
from tollgate.usage.pricing import ZERO, Pricing, micro_usd

log = logging.getLogger(__name__)

UsageStatus = Literal[
    "success",
    "provider_error",
    "client_disconnect",
    "gateway_shutdown",
    "gateway_error",
    "rate_limited",
    "budget_exceeded",
]


@dataclass(frozen=True)
class UsageEvent:
    request_id: str
    tenant_id: str
    api_key_id: str
    model: str
    provider: str | None  # None if the request never got as far as routing
    status: UsageStatus
    prompt_tokens: int
    completion_tokens: int
    # True when the provider never reported usage and the counts are tiktoken estimates.
    usage_estimated: bool
    latency_ms: int
    ttft_ms: int | None
    cost_usd: Decimal = ZERO


class Ledger(Protocol):
    async def record(self, event: UsageEvent) -> None: ...


WriteBatch = Callable[[Sequence[UsageEvent]], Awaitable[None]]


def postgres_writer(sessions: async_sessionmaker[AsyncSession]) -> WriteBatch:
    """Inserts a batch in one statement; an event already in the ledger (same
    request_id) is skipped, so a batch can be retried safely."""

    async def write(events: Sequence[UsageEvent]) -> None:
        rows = [
            {
                "request_id": uuid.UUID(e.request_id),
                "tenant_id": uuid.UUID(e.tenant_id),
                "api_key_id": uuid.UUID(e.api_key_id),
                "provider": e.provider,
                "model": e.model,
                "prompt_tokens": e.prompt_tokens,
                "completion_tokens": e.completion_tokens,
                "usage_estimated": e.usage_estimated,
                "cost_usd": e.cost_usd,
                "status": e.status,
                "latency_ms": e.latency_ms,
                "ttft_ms": e.ttft_ms,
            }
            for e in events
        ]
        statement = insert(UsageEventRow).values(rows)
        async with sessions() as session, session.begin():
            await session.execute(statement.on_conflict_do_nothing(index_elements=["request_id"]))

    return write


def _transient(exc: Exception) -> bool:
    """A failure that retrying can outlast: the database unreachable or restarting."""
    if isinstance(exc, OperationalError | InterfaceError | OSError | TimeoutError):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


class UsageWriter:
    """The ledger's writer. Events queue in memory; a background task inserts
    them every `interval_s` or `batch_size` events, whichever comes first.

    A batch that fails while the database is unreachable is retried until it
    is written, and inserts are idempotent, so a retry never duplicates. A
    batch the database refuses outright is logged by count and dropped, rather
    than block every event behind it. Shutdown writes what is queued, waiting
    up to a timeout. Events still queued if the process dies are lost (v2 moves
    the queue to Redis Streams).
    """

    def __init__(
        self, *, batch_size: int = 200, interval_s: float = 0.5, retry_s: float = 1.0
    ) -> None:
        self._batch_size = batch_size
        self._interval_s = interval_s
        self._retry_s = retry_s
        self._queue: asyncio.Queue[UsageEvent] = asyncio.Queue()
        self._unwritten = 0  # recorded, not yet written (or dropped)
        self._closing = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def pending(self) -> int:
        """Events recorded but not yet in the ledger."""
        return self._unwritten

    async def record(self, event: UsageEvent) -> None:
        self._queue.put_nowait(event)
        self._unwritten += 1
        self._wake.set()

    def start(self, write: WriteBatch) -> None:
        self._task = asyncio.create_task(self._run(write))

    async def close(self, timeout_s: float) -> None:
        """Write everything recorded so far, waiting up to `timeout_s`, then stop."""
        self._closing = True  # no more waiting for a batch to fill
        self._wake.set()
        if self._task is not None:
            with anyio.move_on_after(timeout_s):
                await self._queue.join()
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self.pending:
            log.error("shut down with %d usage events not written to the ledger", self.pending)

    async def _run(self, write: WriteBatch) -> None:
        loop = asyncio.get_running_loop()
        while True:
            batch = [await self._queue.get()]
            deadline = loop.time() + self._interval_s
            while True:
                while not self._queue.empty() and len(batch) < self._batch_size:
                    batch.append(self._queue.get_nowait())
                remaining = deadline - loop.time()
                if len(batch) == self._batch_size or self._closing or remaining <= 0:
                    break
                # Woken by the next event, or by close(): shutdown doesn't wait out
                # the interval.
                self._wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), remaining)
            await self._write(write, batch)
            self._unwritten -= len(batch)
            for _ in batch:
                self._queue.task_done()

    async def _write(self, write: WriteBatch, batch: list[UsageEvent]) -> None:
        while True:
            try:
                await write(batch)
                return
            except Exception as exc:
                # Type names only: an error's text can include the rows.
                if not _transient(exc):
                    log.error(
                        "dropped %d usage events the ledger refused: %s",
                        len(batch),
                        type(exc).__name__,
                    )
                    return
                log.warning(
                    "could not write %d usage events, retrying: %s",
                    len(batch),
                    type(exc).__name__,
                )
                await asyncio.sleep(self._retry_s)


def parse_usage(usage: Any) -> tuple[int, int] | None:
    """(prompt_tokens, completion_tokens) from a provider's usage object, if well-formed."""
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int:
        return None
    return prompt, completion


class Metering:
    """Hands out one UsageMeter per request and counts those still measuring.

    At shutdown uvicorn cancels requests that outlive its graceful timeout,
    then runs the app's lifespan shutdown without waiting for them, and asyncio
    then cancels whatever is left (no shield stops that). The lifespan waits
    for wait_idle() first, so cut-off requests still record their usage.
    """

    def __init__(self, ledger: Ledger, pricing: Pricing | None = None) -> None:
        self.ledger = ledger
        # Without prices every request costs nothing (tests that don't need costs).
        self.pricing = pricing
        # Settles requests (TPM and spend) as usage is recorded. Set once Redis is up.
        self.limiter: RateLimiter | None = None
        self._open = 0  # meters measuring, or still recording their event
        self._idle = asyncio.Event()
        self._idle.set()

    def meter(
        self,
        *,
        request_id: str,
        tenant_id: str,
        api_key_id: str,
        started_at: float,
        model: str,
        provider: str,
        messages: list[dict[str, Any]],
        reservation: Reservation | None = None,
    ) -> "UsageMeter":
        meter = UsageMeter(
            self, request_id, tenant_id, api_key_id, started_at, model, provider, messages
        )
        meter.reservation = reservation
        return meter

    def cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
        if self.pricing is None:
            return ZERO
        return self.pricing.cost_usd(model, prompt_tokens, completion_tokens)

    def reconcile(self, reservation: Reservation | None, used: int, cost: Decimal = ZERO) -> None:
        """Settle a request once its usage is known: reconcile its TPM
        reservation with the tokens actually used, and add its cost to its
        tenant's spend. In the background: it is a second Redis call, and
        needn't hold up a response. Counted as open, so shutdown waits for it."""
        if reservation is None or self.limiter is None:
            return
        self._opened()
        task = asyncio.create_task(
            self._reconcile(self.limiter, reservation, used, micro_usd(cost))
        )
        task.add_done_callback(self._closed)

    @staticmethod
    async def _reconcile(
        limiter: RateLimiter, reservation: Reservation, used: int, cost_micro_usd: int
    ) -> None:
        # Best effort: if Redis is down the bucket stays charged the whole
        # reservation, which only errs towards admitting less, and refill
        # clears it within a minute. The spend counter misses this cost until
        # it is next refilled from the ledger, which has it.
        try:
            await limiter.settle(reservation, used, cost_micro_usd)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("could not settle a request in Redis: %s", type(exc).__name__)

    def release(self, reservation: Reservation | None) -> None:
        """Give back a reservation whose request never reached a provider."""
        self.reconcile(reservation, 0)

    async def record_rejected(
        self,
        *,
        request_id: str,
        tenant_id: str,
        api_key_id: str,
        started_at: float,
        model: str,
        status: UsageStatus,
    ) -> None:
        """The usage event of a request turned away before any provider: no tokens."""
        await self.ledger.record(
            UsageEvent(
                request_id=request_id,
                tenant_id=tenant_id,
                api_key_id=api_key_id,
                model=model,
                provider=None,
                status=status,
                prompt_tokens=0,
                completion_tokens=0,
                usage_estimated=False,
                latency_ms=_ms_since(started_at, time.perf_counter()),
                ttft_ms=None,
            )
        )

    def _opened(self) -> None:
        self._open += 1
        self._idle.clear()

    def _closed(self, _: object = None) -> None:
        self._open -= 1
        if self._open == 0:
            self._idle.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()


class UsageMeter:
    """What is known about one request's usage. Wrap everything from the
    provider call on in measure(): whatever happens inside, exactly one usage
    event is recorded on the way out."""

    def __init__(
        self,
        metering: Metering,
        request_id: str,
        tenant_id: str,
        api_key_id: str,
        started_at: float,  # time.perf_counter() when the request arrived
        model: str,
        provider: str,
        messages: list[dict[str, Any]],
    ) -> None:
        self._metering = metering
        self.request_id = request_id
        self.tenant_id = tenant_id
        self.api_key_id = api_key_id
        self.started_at = started_at
        self.model = model
        self.provider = provider
        self.messages = messages
        self.status: UsageStatus | None = None
        # (prompt, completion) tokens the provider reported, if it did; an
        # error response reports (0, 0): nothing is billed for it.
        self.reported: tuple[int, int] | None = None
        # The request reached the provider, which may bill its prompt even if
        # no response comes back. Until then there is nothing to estimate.
        self.sent = False
        # Completion text received so far, for estimating unreported usage.
        self.generated = ""
        self.first_token_at: float | None = None
        self.reservation: Reservation | None = None  # TPM tokens held, settled on record
        self._measured = False

    def mark_sent(self) -> None:
        self.sent = True

    def settle(self, status: UsageStatus) -> None:
        """Decide the outcome, unless something already has: the first wins."""
        if self.status is None:
            self.status = status

    @asynccontextmanager
    async def measure(self) -> AsyncIterator[None]:
        if self._measured:
            raise RuntimeError(f"request {self.request_id} is already measured")
        self._measured = True
        self._metering._opened()
        try:
            yield
        except asyncio.CancelledError:
            # Nothing but a server shutdown cancels a request task.
            self.settle("gateway_shutdown")
            raise
        except Exception:
            self.settle("gateway_error")
            raise
        finally:
            # Written by its own task: a cancellation landing mid-write (as
            # uvicorn's shutdown can) abandons only this wait, not the write.
            recording = asyncio.create_task(self._record())
            recording.add_done_callback(self._metering._closed)
            await asyncio.shield(recording)

    async def _record(self) -> None:
        estimated = self.reported is None and self.sent
        if self.reported is not None:
            prompt, completion = self.reported
        elif estimated:
            # Off the event loop: a 1 MB prompt takes tiktoken a noticeable while.
            prompt, completion = await anyio.to_thread.run_sync(self._estimate)
        else:
            prompt = completion = 0  # never sent: nothing billed
        cost = self._metering.cost_usd(self.model, prompt, completion)
        await self._metering.ledger.record(
            UsageEvent(
                request_id=self.request_id,
                tenant_id=self.tenant_id,
                api_key_id=self.api_key_id,
                model=self.model,
                provider=self.provider,
                # Leaving measure() without settling anything is itself a bug.
                status=self.status or "gateway_error",
                prompt_tokens=prompt,
                completion_tokens=completion,
                usage_estimated=estimated,
                latency_ms=_ms_since(self.started_at, time.perf_counter()),
                ttft_ms=None
                if self.first_token_at is None
                else _ms_since(self.started_at, self.first_token_at),
                cost_usd=cost,
            )
        )
        # After the event: the ledger is the record; Redis only follows it.
        self._metering.reconcile(self.reservation, prompt + completion, cost)

    def _estimate(self) -> tuple[int, int]:
        return (
            estimate.estimate_prompt_tokens(self.messages, self.model),
            estimate.count_tokens(self.generated, self.model),
        )


def _ms_since(start: float, end: float) -> int:
    return round((end - start) * 1000)
