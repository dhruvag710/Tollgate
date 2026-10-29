"""Admission: each tenant's monthly budget and its RPM and TPM token buckets,
all in Redis.

The Lua lives in lua/ as libraries (token_bucket.lua, tpm_reserve.lua,
tpm_reconcile.lua, budget.lua, admit.lua, settle.lua), composed here into the
scripts Redis runs. The key cache composes the same library into its lookup,
so a request with a cached key is authenticated, budget checked and rate
limited in one round trip (auth/cache.py); this module's own check runs only
after a cache miss. Once a request's usage is known, one more call settles it:
its TPM reservation and its spend.

Keys are named from the tenant inside the scripts, so like the key cache this
needs standalone Redis, not Redis Cluster.
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from redis.asyncio import Redis

from tollgate.errors import TollgateError

_LUA = Path(__file__).parent / "lua"
LIBRARY = "\n".join(
    (_LUA / name).read_text(encoding="utf-8")
    for name in (
        "token_bucket.lua",
        "tpm_reserve.lua",
        "tpm_reconcile.lua",
        "budget.lua",
        "admit.lua",
        "settle.lua",
    )
)
_CHECK = (
    LIBRARY
    + """
return admit(ARGV[1], ARGV[2], tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5]))
"""
)
_SETTLE = (
    LIBRARY
    + """
return settle(ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]))
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
    outcome, wait, requests, tokens, month = cast(list[Any], reply)
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
    ) -> Decision:
        reply = await self._check(
            args=[tenant_id, budget_arg(budget_usd), rpm_limit, tpm_limit, reservation]
        )
        return decision(
            reply,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            reservation=reservation,
            budget_usd=budget_usd,
        )

    async def settle(self, reservation: Reservation, used: int, cost_micro_usd: int) -> int:
        """Settle `reservation` against the tokens actually `used`, and add the
        request's cost to its tenant's spend; returns the new TPM balance."""
        balance = await self._settle(
            args=[
                reservation.tenant_id,
                reservation.tpm_limit,
                used - reservation.tokens,
                cost_micro_usd,
            ]
        )
        return int(cast(int, balance))
