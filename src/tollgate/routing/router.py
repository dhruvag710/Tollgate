"""Model -> provider routing. v1: static, always the first configured route."""

from tollgate.errors import TollgateError
from tollgate.providers.base import Provider
from tollgate.providers.registry import ProviderRegistry


def model_not_found(model: str) -> TollgateError:
    """Also the answer for a model the tenant may not use: no hint that it exists."""
    return TollgateError(
        404,
        f"The model `{model}` does not exist or you do not have access to it.",
        "invalid_request_error",
        "model_not_found",
    )


class Router:
    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def route(self, model: str) -> Provider:
        cfg = self.registry.models.get(model)
        if cfg is None:
            raise model_not_found(model)
        return self.registry.providers[cfg.routes[0]]
