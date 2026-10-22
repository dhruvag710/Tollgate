"""Process settings (env) and the provider/model routing file (YAML)."""

from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    providers_config: Path = Path("config/providers.yaml")
    max_request_body_bytes: int = 1_048_576
    upstream_max_connections: int = 1000
    upstream_max_keepalive: int = 200
    log_level: str = "INFO"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["openai_compat"]
    base_url: str
    api_key_env: str | None = None
    timeout_s: float = 60.0
    connect_timeout_s: float = 3.0
    # Forward X-Mock-* request headers upstream. Only for mock_provider.
    forward_mock_headers: bool = False


class Price(BaseModel):
    input: Decimal
    output: Decimal


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: list[str] = Field(min_length=1)
    price_per_1m: Price


class ProvidersFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderConfig]
    models: dict[str, ModelConfig]

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        names = [p.name for p in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        for model, cfg in self.models.items():
            unknown = [r for r in cfg.routes if r not in names]
            if unknown:
                raise ValueError(f"model {model!r} routes to unknown provider(s): {unknown}")
        return self


def load_providers_file(path: Path) -> ProvidersFile:
    with path.open(encoding="utf-8") as f:
        return ProvidersFile.model_validate(yaml.safe_load(f))
