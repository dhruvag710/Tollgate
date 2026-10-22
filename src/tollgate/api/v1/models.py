from typing import Annotated, Any

from fastapi import APIRouter, Depends

from tollgate.deps import get_router
from tollgate.routing.router import Router

router = APIRouter()


@router.get("/v1/models")
async def list_models(router_: Annotated[Router, Depends(get_router)]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": cfg.routes[0]}
            for name, cfg in router_.registry.models.items()
        ],
    }
