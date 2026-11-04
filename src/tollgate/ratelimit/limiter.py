"""Admission: each tenant's monthly budget and its RPM and TPM token buckets,
all in Redis.

The Lua lives in lua/ as libraries (token_bucket.lua, tpm_reserve.lua,
tpm_reconcile.lua, budget.lua, admit.lua, settle.lua, and the circuit
breakers' routing/lua/breaker.lua), composed here into the scripts Redis
runs. The key cache composes the same library into its lookup, so a request
with a cached key is authenticated, budget checked and rate limited in one
round trip (auth/cache.py); this module's own check runs only after a cache
miss. An admitted request's reply also carries the breakers of its model's
providers, so routing needs no call of its own. Once a request's usage is
known, one more call settles it: its TPM reservation, its spend, and a
success against its provider's breaker.

Keys are named from the tenant inside the scripts, so like the key cache this
needs standalone Redis, not Redis Cluster.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from redis.asyncio import Redis

from tollgate.cache import exact
from tollgate.cache.exact import CacheStore
from tollgate.errors import TollgateError
from tollgate.routing import circuit_breaker
from tollgate.routing.circuit_breaker import BreakerSuccess, BreakerView, parse_views

_LUA = Path(__file__).parent / "lua"


def _lua(*names: str) -> list[str]:
    return [(_LUA / name).read_text(encoding="utf-8") for name in names]


LIBRARY = "\n".join(
    [
        *_lua("token_bucket.lua", "tpm_reserve.lua", "tpm_reconcile.lua", "budget.lua"),
        circuit_breaker.LIBRARY,
        exact.LIBRARY,
        *_lua("admit.lua", "settle.lua"),
    ]
)
_CHECK = (
    LIBRARY
    + """
-- ARGV[6]: the request's exact-cache key, '' if it isn't cached. ARGV[7], ...:
-- the providers whose breakers an admitted request is routed by.
local reply = admit(ARGV[1], ARGV[2], tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5]))
if reply[1] == 'ok' then
  reply[6] = breaker_views(ARGV, 7, breaker_now_ms())
  reply[7] = exact_get(ARGV[1], ARGV[6])
end
return reply
"""
)
_SETTLE = (
    LIBRARY
    + """
-- ARGV[7], ARGV[8], ARGV[9]: an exact-cache entry to store, its key ('' for
-- none) and how many seconds it lasts.
exact_put(ARGV[1], ARGV[7], ARGV[8], tonumber(ARGV[9]))
return settle(ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]), ARGV[5],
  tonumber(ARGV[6]))
"""
)

Outcome = Literal["ok", "requests", "tokens", "too_large", "budget", "spend_unknown"]


def budget_arg(budget_usd: Decimal | None) -> str:
    """A budget as the scripts take it: plain digits, '' for unlimited."""
    return "" if budget_usd is None else format(budget_usd, "f")


def _dollars(amount: Decimal) -> str:
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    retry_after_s: float  # until the exhausted limit has room; 0 unless limited
    rpm_limit: int
    tpm_limit: int
    remaining_requests: int
    remaining_tokens: int
    reserved: int  # TPM tokens this request asked for (taken only if allowed)
    budget_usd: Decimal | None  # the tenant's monthly budget; None: unlimited
    month: str  # 'YYYYMM', UTC, by Redis's clock: the month whose spend was checked
    # An admitted request's providers' breakers, in the order asked for, read
    # in the same call.
    breakers: tuple[BreakerView, ...] = ()
    # An admitted request's exact-cache entry, if it has one (and asked).
    cached: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "ok"

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit-Requests": str(self.rpm_limit),
            "X-RateLimit-Remaining-Requests": str(max(0, self.remaining_requests)),
            "X-RateLimit-Remaining-Tokens": str(max(0, self.remaining_tokens)),
        }
        if self.outcome in ("requests", "tokens"):
            headers["Retry-After"] = str(max(1, math.ceil(self.retry_after_s)))
        return headers

    def error(self) -> TollgateError:
        if self.outcome == "budget":
            assert self.budget_usd is not None
            # 402: OpenAI SDKs don't retry it, as they would a 429.
            return TollgateError(
                402,
                f"Monthly budget of ${_dollars(self.budget_usd)} reached. Requests are "
                "refused until the budget is raised or the month ends.",
                "insufficient_quota",
                "budget_exceeded",
            )
        if self.outcome == "requests":
            message, code = (
                f"Rate limit reached: {self.rpm_limit} requests per minute.",
                "rpm_exceeded",
            )
        elif self.outcome == "tokens":
            message, code = (
                f"Rate limit reached: {self.tpm_limit} tokens per minute.",
                "tpm_exceeded",
            )
        else:
            message, code = (
                f"Request too large: it reserves {self.reserved} tokens, more than the limit "
                f"of {self.tpm_limit} tokens per minute. Lower max_tokens or shorten the prompt.",
                "request_too_large",
            )
        return TollgateError(429, message, "rate_limit_exceeded", code, headers=self.headers())


def decision(
    reply: Any, *, rpm_limit: int, tpm_limit: int, reservation: int, budget_usd: Decimal | None
) -> Decision:
    outcome, wait, requests, tokens, month, *admitted = cast(list[Any], reply)
    views, cached = [*admitted, None, None][:2]
    return Decision(
        outcome=outcome,
        retry_after_s=float(wait),
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        remaining_requests=int(requests),
        remaining_tokens=int(tokens),
        reserved=reservation,
        budget_usd=budget_usd,
        month=month,
        breakers=parse_views(views) if views else (),
        cached=cached or None,
    )


@dataclass(frozen=True)
class Reservation:
    """TPM tokens a request holds until its usage is known."""

    tenant_id: str
    tpm_limit: int
    tokens: int


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._check = redis.register_script(_CHECK)
        self._settle = redis.register_script(_SETTLE)

    async def check(
        self,
        tenant_id: str,
        *,
        budget_usd: Decimal | None,
        rpm_limit: int,
        tpm_limit: int,
        reservation: int,
        routes: Sequence[str] = (),
        cache_key: str = "",
    ) -> Decision:
        """Admit a request; if admitted, also read the breakers of `routes`
        and the exact-cache entry under `cache_key` ('' for none)."""
        reply = await self._check(
            args=[
                tenant_id,
                budget_arg(budget_usd),
                rpm_limit,
                tpm_limit,
                reservation,
                cache_key,
                *routes,
            ]
        )
        return decision(
            reply,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            reservation=reservation,
            budget_usd=budget_usd,
        )

    async def settle(
        self,
        reservation: Reservation,
        used: int,
        cost_micro_usd: int,
        success: BreakerSuccess | None = None,
        store: CacheStore | None = None,
    ) -> int:
        """Settle `reservation` against the tokens actually `used`, add the
        request's cost to its tenant's spend, count a `success` against a
        provider's breaker, and `store` the request's answer in the exact
        cache; returns the new TPM balance."""
        balance = await self._settle(
            args=[
                reservation.tenant_id,
                reservation.tpm_limit,
                used - reservation.tokens,
                cost_micro_usd,
                "" if success is None else success.provider,
                0 if success is None else success.window_ms,
                "" if store is None else store.hash,
                "" if store is None else store.entry,
                0 if store is None else store.ttl_s,
            ]
        )
        return int(cast(int, balance))
