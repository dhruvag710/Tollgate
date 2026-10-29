from decimal import Decimal
from typing import get_args

import pytest

from tests.helpers import TEST_PROVIDERS
from tollgate.config import Price, load_providers_file
from tollgate.db.models import USAGE_STATUSES
from tollgate.usage.budget import month_bounds, spend_key
from tollgate.usage.ledger import UsageStatus
from tollgate.usage.pricing import Pricing, micro_usd

PRICING = Pricing(
    {
        "gpt-4o-mini": Price(input=Decimal("0.15"), output=Decimal("0.60")),
        "llama3.1:8b": Price(input=Decimal("0"), output=Decimal("0")),
    }
)


def test_cost_is_per_million_tokens() -> None:
    assert PRICING.cost_usd("gpt-4o-mini", 1_000_000, 0) == Decimal("0.15")
    assert PRICING.cost_usd("gpt-4o-mini", 0, 1_000_000) == Decimal("0.60")
    assert PRICING.cost_usd("gpt-4o-mini", 1_000, 2_000) == Decimal("0.001350")


def test_cost_rounds_half_up_to_six_places() -> None:
    assert PRICING.cost_usd("gpt-4o-mini", 9, 10) == Decimal("0.000007")  # 0.00000735
    assert PRICING.cost_usd("gpt-4o-mini", 10, 0) == Decimal("0.000002")  # 0.0000015
    assert PRICING.cost_usd("gpt-4o-mini", 3, 0) == Decimal("0.000000")  # 0.00000045


def test_zero_price_models_cost_nothing() -> None:
    assert PRICING.cost_usd("llama3.1:8b", 10_000, 10_000) == 0


def test_no_tokens_cost_nothing_even_for_an_unpriced_model() -> None:
    assert PRICING.cost_usd("not-configured", 0, 0) == 0
    with pytest.raises(KeyError):
        PRICING.cost_usd("not-configured", 1, 0)


def test_prices_come_from_the_providers_config() -> None:
    pricing = Pricing.from_config(load_providers_file(TEST_PROVIDERS))
    assert pricing.cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == Decimal("0.75")


def test_micro_usd() -> None:
    assert micro_usd(Decimal("0.001350")) == 1_350
    assert micro_usd(Decimal("12.3456")) == 12_345_600
    assert micro_usd(Decimal("0")) == 0


def test_month_bounds() -> None:
    start, end = month_bounds("202612")
    assert (start.isoformat(), end.isoformat()) == (
        "2026-12-01T00:00:00+00:00",
        "2027-01-01T00:00:00+00:00",
    )
    assert month_bounds("202602")[1].isoformat() == "2026-03-01T00:00:00+00:00"
    assert spend_key("t1", "202610") == "tg:spend:t1:202610"


def test_the_ledger_statuses_match_the_schema() -> None:
    assert set(get_args(UsageStatus)) == set(USAGE_STATUSES)
