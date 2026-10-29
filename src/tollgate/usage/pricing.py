"""What a request cost, from the prices in the providers config (SPEC 5.3).

Prices are per million tokens. A cost is rounded half up to the ledger's six
decimal places once per request, so the ledger's SUM(cost_usd) and the
month-to-date spend in Redis (integer micro-dollars) add up the same amounts.
"""

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from tollgate.config import Price, ProvidersFile

MICRO_USD = Decimal("0.000001")
ZERO = Decimal("0.000000")


def micro_usd(amount: Decimal) -> int:
    """`amount` dollars (at most six decimal places) in micro-dollars."""
    return int(amount.scaleb(6))


class Pricing:
    def __init__(self, prices: Mapping[str, Price]) -> None:
        self._prices = dict(prices)

    @classmethod
    def from_config(cls, config: ProvidersFile) -> "Pricing":
        return cls({name: model.price_per_1m for name, model in config.models.items()})

    def cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
        if prompt_tokens == completion_tokens == 0:
            # Nothing used: the model needn't even be configured (a rejected request).
            return ZERO
        price = self._prices[model]
        cost = (price.input * prompt_tokens + price.output * completion_tokens) / 1_000_000
        return cost.quantize(MICRO_USD, rounding=ROUND_HALF_UP)
