"""The gateway's Redis client: a bounded connection pool, opened at startup.

Opening a connection costs a TCP (and maybe TLS) handshake; a burst arriving
at a cold gateway would otherwise open the whole pool at once, while every
other request waits for a connection, and the slowest wait past the pool
timeout and fail closed with 503s. So startup opens the pool first.
"""

import asyncio
import logging

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.connection import AbstractConnection

from tollgate.config import Settings

log = logging.getLogger(__name__)


def create_redis(settings: Settings) -> Redis:
    """Connects lazily: nothing here fails if Redis is down."""
    return Redis(
        connection_pool=BlockingConnectionPool.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            timeout=settings.redis_pool_timeout_s,  # waiting for a free connection
            decode_responses=True,
            socket_timeout=settings.redis_timeout_s,
            socket_connect_timeout=settings.redis_timeout_s,
        )
    )


async def open_connections(redis: Redis, n: int) -> int:
    """Open `n` of the pool's connections now and leave them idle in it. Best
    effort: if Redis is down the gateway still starts, and requests fail closed
    until it is back. Returns how many were opened."""
    pool = redis.connection_pool
    # Held together, so each is a new connection rather than one just released.
    results = await asyncio.gather(
        *(pool.get_connection() for _ in range(n)),  # type: ignore[no-untyped-call]
        return_exceptions=True,
    )
    opened = [conn for conn in results if isinstance(conn, AbstractConnection)]
    for conn in opened:
        await pool.release(conn)
    if len(opened) < n:
        failure = next(r for r in results if isinstance(r, BaseException))
        log.warning(
            "opened %d of %d Redis connections at startup: %s",
            len(opened),
            n,
            type(failure).__name__,
        )
    return len(opened)
