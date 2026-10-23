"""Phase 1.4: the admin API (SPEC 6)."""

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tests.helpers import ADMIN, TEST_PEPPER
from tests.integration.test_auth import assert_error, bearer, replica
from tollgate.auth import keys
from tollgate.db.models import ApiKey


def unique_name() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}"


async def create_tenant(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    r = await client.post("/admin/tenants", json={"name": unique_name(), **fields}, headers=ADMIN)
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


# --- the admin token ---------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-the-admin-token-at-all"}])
async def test_admin_token_required(headers: dict[str, str]) -> None:
    async with replica() as (_, client):
        r = await client.post("/admin/tenants", json={"name": unique_name()}, headers=headers)
    assert_error(r, 401, "invalid_admin_token")


async def test_a_tenant_key_is_not_an_admin_token() -> None:
    async with replica() as (_, client):
        tenant = await create_tenant(client)
        key = (await client.post(f"/admin/tenants/{tenant['id']}/keys", headers=ADMIN)).json()
        r = await client.post("/admin/tenants", json={"name": "x"}, headers=bearer(key["key"]))
    assert_error(r, 401, "invalid_admin_token")


async def test_admin_api_disabled_without_a_token() -> None:
    async with replica(admin_token=None) as (_, client):
        r = await client.post("/admin/tenants", json={"name": unique_name()}, headers=ADMIN)
    assert_error(r, 503, "admin_disabled")


# --- tenants -------------------------------------------------------------------


async def test_create_tenant_with_defaults() -> None:
    async with replica() as (_, client):
        name = unique_name()
        r = await client.post("/admin/tenants", json={"name": name}, headers=ADMIN)
    assert r.status_code == 201
    body = r.json()
    uuid.UUID(body["id"])
    assert body["created_at"]
    assert {k: v for k, v in body.items() if k not in ("id", "created_at")} == {
        "name": name,
        "rpm_limit": 60,
        "tpm_limit": 100_000,
        "monthly_budget_usd": None,
        "allowed_models": None,
        "cache_enabled": True,
    }


async def test_create_tenant_with_every_field() -> None:
    async with replica() as (_, client):
        body = await create_tenant(
            client,
            rpm_limit=5,
            tpm_limit=500,
            monthly_budget_usd="12.5",
            allowed_models=["gpt-4o-mini"],
            cache_enabled=False,
        )
    assert (body["rpm_limit"], body["tpm_limit"]) == (5, 500)
    assert Decimal(body["monthly_budget_usd"]) == Decimal("12.5")
    assert body["allowed_models"] == ["gpt-4o-mini"]
    assert body["cache_enabled"] is False


async def test_duplicate_tenant_name() -> None:
    async with replica() as (_, client):
        tenant = await create_tenant(client)
        r = await client.post("/admin/tenants", json={"name": tenant["name"]}, headers=ADMIN)
    assert_error(r, 409, "tenant_exists")


@pytest.mark.parametrize(
    "fields",
    [
        {"rpm_limit": 0},
        {"tpm_limit": -1},
        {"rpm_limit": 2**31},
        {"monthly_budget_usd": "-1"},
        {"monthly_budget_usd": "0.00001"},
        {"surprise": True},
        {"name": ""},
    ],
)
async def test_invalid_tenant_rejected(fields: dict[str, Any]) -> None:
    async with replica() as (_, client):
        r = await client.post(
            "/admin/tenants", json={"name": unique_name(), **fields}, headers=ADMIN
        )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_unknown_model_rejected() -> None:
    async with replica() as (_, client):
        r = await client.post(
            "/admin/tenants",
            json={"name": unique_name(), "allowed_models": ["gpt-4o-mini", "gpt-9"]},
            headers=ADMIN,
        )
    assert_error(r, 400, "unknown_model")
    assert "gpt-9" in r.json()["error"]["message"]


async def test_update_tenant() -> None:
    async with replica() as (_, client):
        tenant = await create_tenant(
            client, monthly_budget_usd="50", allowed_models=["gpt-4o-mini"]
        )
        r = await client.patch(
            f"/admin/tenants/{tenant['id']}",
            json={"rpm_limit": 120, "monthly_budget_usd": None, "allowed_models": None},
            headers=ADMIN,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["rpm_limit"] == 120
    assert body["monthly_budget_usd"] is None  # null clears: unlimited
    assert body["allowed_models"] is None  # every model
    assert body["tpm_limit"] == tenant["tpm_limit"]  # untouched


@pytest.mark.parametrize("field", ["name", "rpm_limit", "tpm_limit", "cache_enabled"])
async def test_update_cannot_null_required_fields(field: str) -> None:
    async with replica() as (_, client):
        tenant = await create_tenant(client)
        r = await client.patch(f"/admin/tenants/{tenant['id']}", json={field: None}, headers=ADMIN)
    assert r.status_code == 400
    assert "can't be null" in r.json()["error"]["message"]


async def test_update_unknown_tenant() -> None:
    async with replica() as (_, client):
        r = await client.patch(
            f"/admin/tenants/{uuid.uuid4()}", json={"rpm_limit": 1}, headers=ADMIN
        )
    assert_error(r, 404, "tenant_not_found")


async def test_update_to_a_taken_name() -> None:
    async with replica() as (_, client):
        first, second = await create_tenant(client), await create_tenant(client)
        r = await client.patch(
            f"/admin/tenants/{second['id']}", json={"name": first["name"]}, headers=ADMIN
        )
    assert_error(r, 409, "tenant_exists")


# --- keys ----------------------------------------------------------------------


async def test_issued_key_is_shown_once_and_stored_only_as_a_hash() -> None:
    async with replica() as (app, client):
        tenant = await create_tenant(client)
        r = await client.post(
            f"/admin/tenants/{tenant['id']}/keys", json={"name": "ci"}, headers=ADMIN
        )
        assert r.status_code == 201
        issued = r.json()
        works = await client.get("/v1/models", headers=bearer(issued["key"]))
        async with app.state.sessions() as session:
            row = await session.scalar(select(ApiKey).where(ApiKey.id == uuid.UUID(issued["id"])))
    raw_key = issued["key"]
    assert keys.is_well_formed(raw_key)
    assert issued["key_prefix"] == raw_key[:14]
    assert (issued["tenant_id"], issued["name"]) == (tenant["id"], "ci")
    assert works.status_code == 200
    assert row is not None
    assert row.key_hash == keys.hash_key(raw_key, TEST_PEPPER)
    stored = [row.key_prefix, row.key_hash, row.name]
    assert not any(raw_key in str(value) for value in stored)


async def test_key_without_a_body() -> None:
    async with replica() as (_, client):
        tenant = await create_tenant(client)
        r = await client.post(f"/admin/tenants/{tenant['id']}/keys", headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["name"] is None


async def test_key_for_unknown_tenant() -> None:
    async with replica() as (_, client):
        r = await client.post(f"/admin/tenants/{uuid.uuid4()}/keys", headers=ADMIN)
    assert_error(r, 404, "tenant_not_found")


async def test_revoke_key() -> None:
    async with replica() as (_, client):
        tenant = await create_tenant(client)
        issued = (await client.post(f"/admin/tenants/{tenant['id']}/keys", headers=ADMIN)).json()
        first = await client.delete(f"/admin/keys/{issued['id']}", headers=ADMIN)
        again = await client.delete(f"/admin/keys/{issued['id']}", headers=ADMIN)
        rejected = await client.get("/v1/models", headers=bearer(issued["key"]))
    assert (first.status_code, again.status_code) == (204, 204)  # idempotent
    assert_error(rejected, 401, "invalid_api_key")


async def test_revoke_unknown_key() -> None:
    async with replica() as (_, client):
        r = await client.delete(f"/admin/keys/{uuid.uuid4()}", headers=ADMIN)
    assert_error(r, 404, "key_not_found")


async def test_change_saved_but_not_invalidated_is_reported() -> None:
    """With Redis down after the commit, the admin is told to retry."""
    async with replica() as (_, healthy):
        tenant = await create_tenant(healthy)
    async with replica(redis_url="redis://127.0.0.1:1/0", redis_timeout_s=0.2) as (_, client):
        r = await client.patch(
            f"/admin/tenants/{tenant['id']}", json={"rpm_limit": 7}, headers=ADMIN
        )
    assert_error(r, 503, "cache_invalidation_failed")
    async with replica() as (_, healthy):  # the change itself was committed
        retried = await healthy.patch(
            f"/admin/tenants/{tenant['id']}", json={"rpm_limit": 7}, headers=ADMIN
        )
    assert retried.json()["rpm_limit"] == 7
