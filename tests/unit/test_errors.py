import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from tollgate.errors import install_error_handlers


class Body(BaseModel):
    n: int


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/typed")
    async def typed(body: Body) -> dict[str, int]:
        return {"n": body.n}

    @app.get("/raise/{status}")
    async def raise_status(status: int) -> None:
        raise HTTPException(status, "boom")

    return app


async def _get(path: str, **kw: object) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://t"
    ) as c:
        return await c.request("POST" if path == "/typed" else "GET", path, **kw)  # type: ignore[arg-type]


async def test_validation_error_is_openai_shaped_400() -> None:
    r = await _get("/typed", json={"n": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_http_exception_types() -> None:
    for status, type_ in [
        (401, "authentication_error"),
        (404, "not_found_error"),
        (409, "invalid_request_error"),
        (503, "api_error"),
    ]:
        r = await _get(f"/raise/{status}")
        assert r.status_code == status
        assert r.json() == {
            "error": {"message": "boom", "type": type_, "param": None, "code": None}
        }
