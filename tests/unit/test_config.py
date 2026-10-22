from pathlib import Path

import pytest
from pydantic import ValidationError

from tollgate.config import ProvidersFile, load_providers_file

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["providers.yaml", "providers.test.yaml"])
def test_shipped_configs_load(name: str) -> None:
    cfg = load_providers_file(ROOT / "config" / name)
    assert "gpt-4o-mini" in cfg.models


def test_unknown_route_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown provider"):
        ProvidersFile.model_validate(
            {
                "providers": [{"name": "a", "type": "openai_compat", "base_url": "http://a"}],
                "models": {"m": {"routes": ["b"], "price_per_1m": {"input": 0, "output": 0}}},
            }
        )


def test_duplicate_provider_rejected() -> None:
    p = {"name": "a", "type": "openai_compat", "base_url": "http://a"}
    with pytest.raises(ValidationError, match="unique"):
        ProvidersFile.model_validate({"providers": [p, p], "models": {}})


def test_prices_are_exact_decimals() -> None:
    cfg = load_providers_file(ROOT / "config" / "providers.yaml")
    assert str(cfg.models["gpt-4o-mini"].price_per_1m.input) == "0.15"
