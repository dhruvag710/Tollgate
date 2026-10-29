"""Monthly budgets (request lifecycle step 3).

The check itself runs in Redis, in the script that also authenticates and rate
limits (ratelimit/lua/admit.lua): a request is admitted while its tenant's
month-to-date spend is below the budget, and its cost is added once its usage
is recorded (lua/settle.lua). Spend is counted in integer micro-dollars under
`tg:spend:{tenant_id}:{yyyymm}`, the month in UTC by Redis's clock.

A counter can be missing: at the first request of a month, or after Redis lost
its data. It is refilled here from the ledger, with SET NX so a refill never
overwrites spend added meanwhile. Events still queued for the ledger aren't in
it yet, so a refill can miss up to LEDGER_FLUSH_INTERVAL_S of a tenant's spend.
"""

from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import UsageEventRow
from tollgate.usage.pricing import micro_usd

# Outlives the month it counts; the next month has a key of its own.
SPEND_TTL_S = 40 * 86_400


def spend_key(tenant_id: str, month: str) -> str:
    return f"tg:spend:{tenant_id}:{month}"


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """The UTC start of 'YYYYMM', and of the month after."""
    year, number = int(month[:4]), int(month[4:])
    start = datetime(year, number, 1, tzinfo=UTC)
    end = datetime(year + number // 12, number % 12 + 1, 1, tzinfo=UTC)
    return start, end


class Budgets:
    def __init__(self, redis: Redis, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._redis = redis
        self._sessions = sessions

    async def refill(self, tenant_id: str, month: str) -> None:
        """Set the month's spend counter from the ledger, unless one exists by now."""
        start, end = month_bounds(month)
        async with self._sessions() as session:
            spent = await session.scalar(
                select(func.coalesce(func.sum(UsageEventRow.cost_usd), 0)).where(
                    UsageEventRow.tenant_id == tenant_id,
                    UsageEventRow.created_at >= start,
                    UsageEventRow.created_at < end,
                )
            )
        await self._redis.set(
            spend_key(tenant_id, month), micro_usd(Decimal(spent or 0)), nx=True, ex=SPEND_TTL_S
        )
