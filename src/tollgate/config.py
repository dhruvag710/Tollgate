"""Process settings (env) and the provider/model routing file (YAML)."""

from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Matches the docker compose stack's published ports.
DEFAULT_DATABASE_URL = "postgresql+asyncpg://tollgate:tollgate@localhost:5432/tollgate"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
# The docker compose stack's out-of-the-box secrets, accepted only with dev_mode.
DEV_KEY_PEPPER = "dev-only-pepper-change-me"
DEV_ADMIN_TOKEN = "dev-only-admin-token"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    providers_config: Path = Path("config/providers.yaml")
    max_request_body_bytes: int = 1_048_576
    upstream_max_connections: int = 1000
    upstream_max_keepalive: int = 200
    log_level: str = "INFO"
    # Report per-stage timings in a Server-Timing response header (diagnostics).
    server_timing: bool = False

    database_url: str = DEFAULT_DATABASE_URL
    # Bounds every query, so an auth lookup can't outlive a cache invalidation.
    db_command_timeout_s: float = 5.0
    redis_url: str = DEFAULT_REDIS_URL
    # Connecting to Redis and each command: a dead Redis is a quick 503.
    redis_timeout_s: float = 1.0
    # A bounded pool: under a burst, commands wait for a connection instead of
    # each opening its own.
    redis_max_connections: int = 100
    # Opened at startup (capped at the pool size), so a cold gateway doesn't
    # open them in the middle of its first burst.
    redis_min_connections: int = 100
    # How long a command may wait for a free connection. A burst queues through
    # the pool in milliseconds; this only sheds load when Redis is slow.
    redis_pool_timeout_s: float = 5.0
    # TPM tokens reserved for the completion of a request that sets no max_tokens.
    tpm_default_max_tokens: int = 1024
    # The usage ledger's writer: a batch insert every interval or batch size,
    # whichever comes first, and how long shutdown waits for the last ones.
    ledger_batch_size: int = Field(default=200, ge=1)
    ledger_flush_interval_s: float = Field(default=0.5, gt=0)
    ledger_shutdown_timeout_s: float = 10.0
    # HMAC key for stored API key hashes. Changing it invalidates every key.
    key_pepper: SecretStr = Field(min_length=16)
    # The admin API is disabled unless this is set.
    admin_token: SecretStr | None = Field(default=None, min_length=16)
    # Accept the development secrets above; for local development only.
    dev_mode: bool = False

    @model_validator(mode="after")
    def _no_development_secrets(self) -> Self:
        if self.dev_mode:
            return self
        if self.key_pepper.get_secret_value() == DEV_KEY_PEPPER:
            raise ValueError(
                "KEY_PEPPER is the development default: set a secret one "
                "(or DEV_MODE=true for local development only)"
            )
        if self.admin_token is not None and self.admin_token.get_secret_value() == DEV_ADMIN_TOKEN:
            raise ValueError(
                "ADMIN_TOKEN is the development default: set a secret one, or unset it "
                "(or DEV_MODE=true for local development only)"
            )
        return self

    @property
    def uses_development_secrets(self) -> bool:
        return self.key_pepper.get_secret_value() == DEV_KEY_PEPPER or (
            self.admin_token is not None and self.admin_token.get_secret_value() == DEV_ADMIN_TOKEN
        )


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
