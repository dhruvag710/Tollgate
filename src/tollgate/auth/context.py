"""What an API key resolves to: its tenant and that tenant's limits."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from tollgate.ratelimit.limiter import budget_arg


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    tenant_name: str
    api_key_id: str
    key_hash: str
    rpm_limit: int
    tpm_limit: int
    monthly_budget_usd: Decimal | None  # None: unlimited
    allowed_models: tuple[str, ...] | None  # None: every model
    cache_enabled: bool

    def allows(self, model: str) -> bool:
        return self.allowed_models is None or model in self.allowed_models

    def to_fields(self) -> dict[str, str]:
        """As the flat string fields of a Redis hash."""
        return {
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "api_key_id": self.api_key_id,
            "key_hash": self.key_hash,
            "rpm_limit": str(self.rpm_limit),
            "tpm_limit": str(self.tpm_limit),
            # Plain digits ('12.3456', never '1E+1'): the admission script reads them.
            "monthly_budget_usd": budget_arg(self.monthly_budget_usd),
            "allowed_models": json.dumps(
                None if self.allowed_models is None else list(self.allowed_models)
            ),
            "cache_enabled": "1" if self.cache_enabled else "0",
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, str]) -> "TenantContext":
        models = json.loads(fields["allowed_models"])
        return cls(
            tenant_id=fields["tenant_id"],
            tenant_name=fields["tenant_name"],
            api_key_id=fields["api_key_id"],
            key_hash=fields["key_hash"],
            rpm_limit=int(fields["rpm_limit"]),
            tpm_limit=int(fields["tpm_limit"]),
            monthly_budget_usd=Decimal(fields["monthly_budget_usd"])
            if fields["monthly_budget_usd"]
            else None,
            allowed_models=None if models is None else tuple(models),
            cache_enabled=fields["cache_enabled"] == "1",
        )
