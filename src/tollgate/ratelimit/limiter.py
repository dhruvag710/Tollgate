"""Per-tenant rate limits: RPM and TPM token buckets in Redis.

The Lua lives in lua/ as libraries (token_bucket.lua, tpm_reserve.lua,
tpm_reconcile.lua), composed here into the scripts Redis runs. The key cache
composes the same library into its lookup, so a request with a cached key is
authenticated and rate limited in one round trip (auth/cache.py); this module's
own check runs only after a cache miss.

Bucket keys are named from the tenant inside the scripts, so like the key cache
this needs standalone Redis, not Redis Cluster.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from redis.asyncio import Redis

from tollgate.errors import TollgateError

_LUA = Path(__file__).parent / "lua"
BUCKET_LIBRARY = "\n".join(
    (_LUA / name).read_text(encoding="utf-8")
    for name in ("token_bucket.lua", "tpm_reserve.lua", "tpm_reconcile.lua")
)
_CHECK = (
    BUCKET_LIBRARY
    + """
return reserve(ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]))
"""
)
_RECONCILE = (
    BUCKET_LIBRARY
    + """
return reconcile(ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]))
"""
)

Outcome = Literal["ok", "requests", "tokens", "too_large"]


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    retry_after_s: float  # until the exhausted limit has room; 0 unless limited
    rpm_limit: int
    tpm_limit: int
    remaining_requests: int
    remaining_tokens: int
    reserved: int  # TPM tokens this request asked for (taken only if allowed)

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


def decision(reply: Any, *, rpm_limit: int, tpm_limit: int, reservation: int) -> Decision:
    outcome, wait, requests, tokens = cast(list[Any], reply)
    return Decision(
        outcome=outcome,
        retry_after_s=float(wait),
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        remaining_requests=int(requests),
        remaining_tokens=int(tokens),
        reserved=reservation,
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
        self._reconcile = redis.register_script(_RECONCILE)

    async def check(
        self, tenant_id: str, *, rpm_limit: int, tpm_limit: int, reservation: int
    ) -> Decision:
        reply = await self._check(args=[tenant_id, rpm_limit, tpm_limit, reservation])
        return decision(reply, rpm_limit=rpm_limit, tpm_limit=tpm_limit, reservation=reservation)

    async def reconcile(self, reservation: Reservation, used: int) -> int:
        """Settle `reservation` against the tokens actually `used`; returns the new
        balance."""
        balance = await self._reconcile(
            args=[reservation.tenant_id, reservation.tpm_limit, used - reservation.tokens]
        )
        return int(cast(int, balance))
