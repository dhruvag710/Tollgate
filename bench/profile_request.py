"""Where a chat request's CPU goes: drives the gateway in-process (as the
container runs it, against the stack's Redis, Postgres and mock) and profiles
a run of sequential requests. A diagnostic, not a benchmark: run it inside the
gateway container so it sees the same CPU limit and libraries.

    docker compose -p tollgate-bench ... exec -T gateway python - < bench/profile_request.py
"""

import asyncio
import cProfile
import io
import pstats
import time

import httpx

from tollgate.config import Settings
from tollgate.main import create_app

N = 1000
BODY = {
    "model": "gpt-4o-mini",
    "messages": [
        {"role": "user", "content": "Explain a token bucket rate limiter in one sentence."}
    ],
    "max_tokens": 32,
}


async def main() -> None:
    admin = {"Authorization": "Bearer dev-only-admin-token"}
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30) as c:
        name = f"profile-{time.time_ns()}"
        tenant = await c.post(
            "/admin/tenants",
            json={"name": name, "rpm_limit": 2**31 - 1, "tpm_limit": 2**31 - 1},
            headers=admin,
        )
        key = (
            await c.post(f"/admin/tenants/{tenant.json()['id']}/keys", json={}, headers=admin)
        ).json()["key"]

    app = create_app(Settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {key}"},
        ) as client,
    ):
        for _ in range(200):  # warm caches and connection pools
            assert (await client.post("/v1/chat/completions", json=BODY)).status_code == 200
        started = time.perf_counter()
        for _ in range(N):
            await client.post("/v1/chat/completions", json=BODY)
        plain = (time.perf_counter() - started) / N

        profile = cProfile.Profile()
        profile.enable()
        for _ in range(N):
            await client.post("/v1/chat/completions", json=BODY)
        profile.disable()
        await app.state.metering.wait_idle()

    print(f"sequential, unprofiled: {plain * 1000:.2f} ms a request (wall, with the client)")
    for order in ("tottime", "cumulative"):
        out = io.StringIO()
        pstats.Stats(profile, stream=out).sort_stats(order).print_stats(35)
        print(f"--- by {order}\n{out.getvalue()}")
    # Import machinery on a request's path means something imports per call.
    out = io.StringIO()
    stats = pstats.Stats(profile, stream=out)
    stats.print_callers("_find_and_load|find_spec|_handle_fromlist")
    print(f"--- callers of the import machinery\n{out.getvalue()}")


asyncio.run(main())
