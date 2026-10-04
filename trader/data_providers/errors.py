"""Typed failures for market-data providers. Callers catch ProviderError."""

from typing import Sequence


class ProviderError(Exception):
    pass


class ProviderNotConfigured(ProviderError):
    def __init__(self, provider: str, missing: Sequence[tuple[str, str]]):
        self.provider = provider
        self.missing = list(missing)
        settings = ', '.join(f'{key} (env {env})' for key, env in self.missing)
        super().__init__(f'{provider} is not configured: set {settings} in trader.yaml or the environment')


class CapabilityNotSupported(ProviderError):
    def __init__(self, capability: str, source: str, supported: Sequence[str]):
        self.capability = capability
        self.source = source
        self.supported = list(supported)
        super().__init__(
            f"source '{source}' does not support {capability}; "
            f"use one of: {', '.join(self.supported) or 'none configured'}"
        )


class ProviderEntitlementError(ProviderError):
    """The provider refused the request for plan, licence or key reasons."""


class ProviderRateLimited(ProviderError):
    """The provider's rate limit or quota is exhausted; retry later."""
