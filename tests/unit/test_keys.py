from decimal import Decimal

import pytest

from tollgate.auth import keys
from tollgate.auth.context import TenantContext


def test_generated_keys_are_well_formed_and_distinct() -> None:
    generated = {keys.generate_key() for _ in range(200)}
    assert len(generated) == 200
    for raw_key in generated:
        assert keys.is_well_formed(raw_key)
        assert raw_key.startswith("tg_live_")
        assert len(raw_key) == len("tg_live_") + 43


@pytest.mark.parametrize(("byte", "digits"), [(0x00, "0" * 43), (0xFF, None)])
def test_every_256_bit_value_fits_in_43_digits(
    byte: int, digits: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keys.secrets, "token_bytes", lambda n: bytes([byte]) * n)
    raw_key = keys.generate_key()
    assert keys.is_well_formed(raw_key)  # leading zeros kept; the maximum still fits
    if digits is not None:
        assert raw_key == "tg_live_" + digits


@pytest.mark.parametrize(
    "raw_key",
    [
        "",
        "tg_live_",
        "tg_live_" + "a" * 42,
        "tg_live_" + "a" * 44,
        "tg_test_" + "a" * 43,
        "tg_live_" + "a" * 42 + "-",
        " tg_live_" + "a" * 43,
        "tg_live_" + "a" * 43 + "\n",
    ],
)
def test_malformed_keys(raw_key: str) -> None:
    assert not keys.is_well_formed(raw_key)


def test_hash_depends_on_key_and_pepper() -> None:
    raw_key = keys.generate_key()
    pepper = "p" * 16
    digest = keys.hash_key(raw_key, pepper)
    assert digest == keys.hash_key(raw_key, pepper)
    assert len(digest) == 64
    assert digest != keys.hash_key(raw_key, "q" * 16)
    assert digest != keys.hash_key(keys.generate_key(), pepper)


def test_hashes_match() -> None:
    digest = keys.hash_key(keys.generate_key(), "p" * 16)
    assert keys.hashes_match(digest, digest)
    assert not keys.hashes_match(digest, digest[:-1] + ("0" if digest[-1] != "0" else "1"))
    assert not keys.hashes_match(digest, "")


def test_display_prefix() -> None:
    raw_key = keys.generate_key()
    assert keys.display_prefix(raw_key) == raw_key[:14]


def _context(**overrides: object) -> TenantContext:
    fields: dict[str, object] = {
        "tenant_id": "t1",
        "tenant_name": "acme",
        "api_key_id": "k1",
        "key_hash": "h",
        "rpm_limit": 60,
        "tpm_limit": 1000,
        "monthly_budget_usd": Decimal("12.5000"),
        "allowed_models": ("gpt-4o-mini",),
        "cache_enabled": False,
        **overrides,
    }
    return TenantContext(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "context",
    [_context(), _context(monthly_budget_usd=None, allowed_models=None, cache_enabled=True)],
)
def test_context_round_trips_through_redis_fields(context: TenantContext) -> None:
    fields = context.to_fields()
    assert all(isinstance(value, str) for value in fields.values())
    assert TenantContext.from_fields(fields) == context


def test_allows() -> None:
    assert _context().allows("gpt-4o-mini")
    assert not _context().allows("llama3.1:8b")
    assert _context(allowed_models=None).allows("anything")
    assert not _context(allowed_models=()).allows("gpt-4o-mini")
