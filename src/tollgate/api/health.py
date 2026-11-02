from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tollgate.observability.metrics import CONTENT_TYPE

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """The process is alive."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Postgres and Redis are reachable, so requests can be authenticated."""
    state = request.app.state
    checks: dict[str, str] = {}
    try:
        async with state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except (SQLAlchemyError, OSError, TimeoutError):
        checks["postgres"] = "unavailable"
    try:
        await state.redis.ping()
        checks["redis"] = "ok"
    except (RedisError, OSError, TimeoutError):
        checks["redis"] = "unavailable"
    ready = all(result == "ok" for result in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", **checks},
        status_code=200 if ready else 503,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus metrics for this process. Scrape each replica directly: through
    nginx (which refuses the path) a scrape would reach a random one."""
    return Response(request.app.state.metrics.exposition(), media_type=CONTENT_TYPE)
