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
"""

from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis

from tollgate.auth.context import TenantContext

ENTRY_TTL_S = 60
TENANT_OF_TTL_S = 86_400

_LOOKUP = """
local tenant = redis.call('GET', KEYS[2])
if not tenant then return {false, false, {}} end
-- A key named from data, so this needs standalone Redis (not Redis Cluster).
local gen = redis.call('GET', 'tg:tenant_gen:' .. tenant) or '0'
return {tenant, gen, redis.call('HGETALL', KEYS[1])}
"""

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


class KeyCache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._lookup = redis.register_script(_LOOKUP)
        self._fill = redis.register_script(_FILL)

    async def lookup(self, key_hash: str) -> Lookup:
        reply = cast(list[Any], await self._lookup(keys=[_entry(key_hash), _tenant_of(key_hash)]))
        tenant_id, generation, flat = reply
        fields = dict(zip(flat[::2], flat[1::2], strict=True))
        current = tenant_id is not None and fields.get("gen") == generation
        return Lookup(TenantContext.from_fields(fields) if current else None, tenant_id, generation)

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
