"""Create a tenant and its first API key. The key is printed once; only its hash
is stored.

    python scripts/create_tenant.py --name acme [--rpm 60] [--tpm 100000]
        [--budget 50] [--models gpt-4o-mini,llama3.1:8b] [--key-name default]

Reads DATABASE_URL, KEY_PEPPER and PROVIDERS_CONFIG like the gateway does.
"""

import argparse
import asyncio
import sys
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from tollgate.config import Settings, load_providers_file
from tollgate.db import tenants
from tollgate.db.session import create_engine, create_sessionmaker


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a tenant and its first API key.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--rpm", type=_positive, default=60, help="requests per minute")
    parser.add_argument("--tpm", type=_positive, default=100_000, help="tokens per minute")
    parser.add_argument("--budget", type=Decimal, help="monthly budget in USD (default: none)")
    parser.add_argument("--models", help="comma-separated allowed models (default: all)")
    parser.add_argument("--key-name", default="default")
    return parser.parse_args(argv)


async def create(settings: Settings, args: argparse.Namespace) -> tenants.IssuedKey:
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else None
    unknown = sorted(
        set(models or ()) - load_providers_file(settings.providers_config).models.keys()
    )
    if unknown:
        raise SystemExit(f"error: unknown model(s): {', '.join(unknown)}")
    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            tenant = await tenants.create_tenant(
                session,
                name=args.name,
                rpm_limit=args.rpm,
                tpm_limit=args.tpm,
                monthly_budget_usd=args.budget,
                allowed_models=models,
            )
            issued = await tenants.create_api_key(
                session,
                tenant.id,
                pepper=settings.key_pepper.get_secret_value(),
                name=args.key_name,
            )
    finally:
        await engine.dispose()
    assert issued is not None  # the tenant was created in the same transaction
    return issued


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        issued = asyncio.run(create(Settings(), args))
    except IntegrityError:
        print(f"error: a tenant named {args.name!r} already exists", file=sys.stderr)
        return 1
    print(f"tenant_id: {issued.key.tenant_id}")
    print(f"key_id:    {issued.key.id}")
    print(f"api_key:   {issued.raw_key}")
    print("Store the API key now: it is not shown again.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
