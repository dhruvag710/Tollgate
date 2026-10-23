from pathlib import Path

import pytest
from pydantic import ValidationError

from tollgate.config import (
    DEV_ADMIN_TOKEN,
    DEV_KEY_PEPPER,
    ProvidersFile,
    Settings,
    load_providers_file,
)
from tollgate.main import create_app

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


# --- development secrets ---------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "providers_config": ROOT / "config" / "providers.test.yaml",
        "key_pepper": "a-real-secret-pepper-value",
        "admin_token": None,
        **overrides,
    }
    return Settings(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [
        ({"key_pepper": DEV_KEY_PEPPER}, "KEY_PEPPER"),
        ({"admin_token": DEV_ADMIN_TOKEN}, "ADMIN_TOKEN"),
    ],
)
def test_development_secrets_refused_without_dev_mode(
    overrides: dict[str, object], variable: str
) -> None:
    with pytest.raises(ValidationError, match=f"{variable} is the development default"):
        _settings(**overrides)


def test_development_secrets_accepted_in_dev_mode_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(key_pepper=DEV_KEY_PEPPER, admin_token=DEV_ADMIN_TOKEN, dev_mode=True)
    assert settings.uses_development_secrets
    caplog.set_level("WARNING")
    create_app(settings)
    assert "development KEY_PEPPER/ADMIN_TOKEN" in caplog.text


def test_real_secrets_need_no_dev_mode(caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(admin_token="a-real-admin-token-value")
    assert not settings.uses_development_secrets
    caplog.set_level("WARNING")
    create_app(settings)
    assert "DEV_MODE" not in caplog.text


def test_compose_defaults_are_the_development_secrets() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"KEY_PEPPER: ${{KEY_PEPPER:-{DEV_KEY_PEPPER}}}" in compose
    assert f"ADMIN_TOKEN: ${{ADMIN_TOKEN:-{DEV_ADMIN_TOKEN}}}" in compose
