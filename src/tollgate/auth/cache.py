"""Redis cache of API key lookups, so authenticating doesn't touch Postgres.

`tg:key:{key_hash}` holds a key's TenantContext plus the tenant generation it
was built under. `tg:tenant_gen:{tenant_id}` is bumped after every revoke or
limit change, so entries built before it stop counting at once, on every
replica. Deleting entries instead would race: a lookup that read Postgres just
before a revoke could write its stale entry back just after the delete. Here a
refill is refused if the generation moved while Postgres was being read.

`tg:key_tenant:{key_hash}` remembers a key's tenant (which never changes), so
a lookup knows which generation to read before it goes to Postgres. On the
very first sight of a key only that is recorded: its data was read before its
generation was known. A key's first two requests therefore read Postgres.

Given a TPM reservation, the lookup also applies the tenant's rate limits when
the entry is current, using the limits cached in it: one round trip both
authenticates and rate limits a request (the limiter's own check runs only
after a miss).
"""

from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis

from tollgate.auth.context import TenantContext
from tollgate.ratelimit.limiter import BUCKET_LIBRARY, Decision, decision

ENTRY_TTL_S = 60
TENANT_OF_TTL_S = 86_400

_LOOKUP = (
    BUCKET_LIBRARY
    + """
-- ARGV[1]: TPM tokens to reserve, or '' to authenticate only.
local tenant = redis.call('GET', KEYS[2])
if not tenant then return {false, false, {}, false} end
-- Keys named from data, so this needs standalone Redis (not Redis Cluster).
local gen = redis.call('GET', 'tg:tenant_gen:' .. tenant) or '0'
local flat = redis.call('HGETALL', KEYS[1])
if ARGV[1] == '' then return {tenant, gen, flat, false} end
local entry = {}
for i = 1, #flat, 2 do entry[flat[i]] = flat[i + 1] end
if entry.gen ~= gen then return {tenant, gen, flat, false} end
local rpm, tpm = tonumber(entry.rpm_limit), tonumber(entry.tpm_limit)
return {tenant, gen, flat, reserve(tenant, rpm, tpm, tonumber(ARGV[1]))}
"""
)

_FILL = """
local gen = redis.call('GET', KEYS[2]) or '0'
if gen ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'gen', gen, unpack(ARGV, 3))
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


def _entry(key_hash: str) -> str:
    return f"tg:key:{key_hash}"


def _tenant_of(key_hash: str) -> str:
    return f"tg:key_tenant:{key_hash}"


def _generation(tenant_id: str) -> str:
    return f"tg:tenant_gen:{tenant_id}"


@dataclass(frozen=True)
class Lookup:
    context: TenantContext | None  # set only if the entry is current
    tenant_id: str | None  # known even when the entry is stale or gone
    generation: str | None  # the tenant's generation at lookup time
    decision: Decision | None = None  # the rate limits, if asked for and the entry is current


class KeyCache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._lookup = redis.register_script(_LOOKUP)
        self._fill = redis.register_script(_FILL)

    async def lookup(self, key_hash: str, *, reservation: int | None = None) -> Lookup:
        """Authenticate from the cache, and with a `reservation`, also apply the
        tenant's rate limits, in the same round trip."""
        reply = cast(
            list[Any],
            await self._lookup(
                keys=[_entry(key_hash), _tenant_of(key_hash)],
                args=["" if reservation is None else reservation],
            ),
        )
        tenant_id, generation, flat, limits = reply
        fields = dict(zip(flat[::2], flat[1::2], strict=True))
        if tenant_id is None or fields.get("gen") != generation:
            return Lookup(None, tenant_id, generation)
        context = TenantContext.from_fields(fields)
        if limits is None or reservation is None:
            return Lookup(context, tenant_id, generation)
        limited = decision(
            limits,
            rpm_limit=context.rpm_limit,
            tpm_limit=context.tpm_limit,
            reservation=reservation,
        )
        return Lookup(context, tenant_id, generation, limited)

    async def store(self, context: TenantContext, seen: Lookup) -> bool:
        """Cache `context`, read from Postgres after the lookup `seen`. Returns
        whether it was stored: not on first sight, nor if the tenant changed
        in the meantime."""
        if seen.tenant_id is None or seen.generation is None:
            await self._redis.set(
                _tenant_of(context.key_hash), context.tenant_id, ex=TENANT_OF_TTL_S
            )
            return False
        fields = [part for item in context.to_fields().items() for part in item]
        stored = await self._fill(
            keys=[_entry(context.key_hash), _generation(context.tenant_id)],
            args=[seen.generation, ENTRY_TTL_S, *fields],
        )
        return bool(stored)

    async def invalidate(self, tenant_id: str, *, key_hash: str | None = None) -> None:
        """Call after committing a change to the tenant or one of its keys: every
        entry built before it stops counting. A revoked key's entry also goes."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(_generation(tenant_id))
            if key_hash is not None:
                pipe.delete(_entry(key_hash))
            await pipe.execute()
