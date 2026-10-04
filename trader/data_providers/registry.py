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
    ):
        self._config = config
        self._specs = {spec.name: spec for spec in specs}
        self._defaults = dict(defaults)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'ProviderRegistry':
        from trader.data_providers.builtin import BUILTIN_DEFAULTS, builtin_specs
        return cls(config, builtin_specs(), BUILTIN_DEFAULTS)

    def sources_for(self, capability: Capability) -> list[str]:
        return sorted(name for name, spec in self._specs.items() if capability in spec.builders)

    def default_source(self, capability: Capability) -> str:
        overrides = self._config.get('data_providers') or {}
        if overrides.get(capability.value):
            return overrides[capability.value]
        global_default = self._config.get('default_data_source')
        if global_default in self.sources_for(capability):
            return global_default
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
