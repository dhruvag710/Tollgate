"""FastAPI dependencies resolving objects created in the app lifespan."""

from fastapi import Request

from tollgate.config import Settings
from tollgate.routing.router import Router


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_router(request: Request) -> Router:
    router: Router = request.app.state.router
    return router
