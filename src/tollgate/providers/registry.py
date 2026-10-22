"""Builds provider instances from the providers YAML."""

import logging
import os
from collections.abc import Mapping

import httpx

from tollgate.config import ModelConfig, ProvidersFile
from tollgate.providers.base import Provider
from tollgate.providers.openai_compat import OpenAICompatProvider

log = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(self, providers: Mapping[str, Provider], models: Mapping[str, ModelConfig]):
        self.providers = dict(providers)
        self.models = dict(models)

    @classmethod
    def from_config(
        cls,
        config: ProvidersFile,
        client: httpx.AsyncClient,
        env: Mapping[str, str] = os.environ,
    ) -> "ProviderRegistry":
        providers: dict[str, Provider] = {}
        for pc in config.providers:
            api_key = None
            if pc.api_key_env:
                api_key = env.get(pc.api_key_env)
                if not api_key:
                    log.warning("provider %s: env var %s is not set", pc.name, pc.api_key_env)
            providers[pc.name] = OpenAICompatProvider(pc, client, api_key)
        return cls(providers, config.models)
