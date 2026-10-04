"""Maps (capability, source) to a provider instance built from config."""

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import CapabilityNotSupported, ProviderNotConfigured

ProviderBuilder = Callable[[Mapping[str, Any]], object]


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    required_keys: tuple[tuple[str, str], ...]
    builders: Mapping[Capability, ProviderBuilder]


class ProviderRegistry:
    def __init__(
        self,
        config: Mapping[str, Any],
        specs: Iterable[ProviderSpec],
        defaults: Mapping[Capability, str],
        inherits_global_default: Optional[Iterable[Capability]] = None,
        falls_back_to: Optional[Mapping[Capability, Capability]] = None,
    ):
        self._config = config
        self._specs = {spec.name: spec for spec in specs}
        self._defaults = dict(defaults)
        self._inherits_global_default = (frozenset(Capability) if inherits_global_default is None
                                         else frozenset(inherits_global_default))
        self._falls_back_to = dict(falls_back_to or {})

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'ProviderRegistry':
        from trader.data_providers.builtin import (
            BUILTIN_DEFAULTS, FALLBACK_CAPABILITIES, INHERITS_DEFAULT_DATA_SOURCE, builtin_specs)
        return cls(config, builtin_specs(), BUILTIN_DEFAULTS, INHERITS_DEFAULT_DATA_SOURCE, FALLBACK_CAPABILITIES)

    def sources_for(self, capability: Capability) -> list[str]:
        return sorted(name for name, spec in self._specs.items() if capability in spec.builders)

    def default_source(self, capability: Capability) -> str:
        overrides = self._config.get('data_providers') or {}
        if overrides.get(capability.value):
            return overrides[capability.value]
        fallback = self._falls_back_to.get(capability)
        if fallback and overrides.get(fallback.value) in self.sources_for(capability):
            return overrides[fallback.value]
        global_default = self._config.get('default_data_source')
        if capability in self._inherits_global_default and global_default in self.sources_for(capability):
            return global_default
        if capability not in self._defaults:
            raise CapabilityNotSupported(capability.value, '(no default)', self.sources_for(capability))
        return self._defaults[capability]

    def get(self, capability: Capability, source: Optional[str] = None) -> object:
        name = source or self.default_source(capability)
        spec = self._specs.get(name)
        if spec is None or capability not in spec.builders:
            raise CapabilityNotSupported(capability.value, name, self.sources_for(capability))
        self._require_keys(spec)
        return spec.builders[capability](self._config)

    def _require_keys(self, spec: ProviderSpec) -> None:
        missing = [(key, env) for key, env in spec.required_keys
                   if not str(self._config.get(key) or '').strip()]
        if missing:
            raise ProviderNotConfigured(spec.name, missing)
