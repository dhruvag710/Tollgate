from typing import Annotated, Any

from fastapi import APIRouter, Depends

from tollgate.auth.context import TenantContext
from tollgate.auth.dependency import require_tenant
from tollgate.deps import get_router
from tollgate.routing.router import Router

router = APIRouter()


@router.get("/v1/models")
async def list_models(
    router_: Annotated[Router, Depends(get_router)],
    tenant: Annotated[TenantContext, Depends(require_tenant)],
) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": cfg.routes[0]}
            for name, cfg in router_.registry.models.items()
            if tenant.allows(name)
        ],
    }
