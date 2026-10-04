import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import CapabilityNotSupported, ProviderNotConfigured
from trader.data_providers.registry import ProviderRegistry, ProviderSpec


class FakeHistory:
    def __init__(self, key):
        self.key = key

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        raise NotImplementedError


def _specs():
    return [
        ProviderSpec('alpha', (('alpha_key', 'ALPHA_KEY'),),
                     {Capability.HISTORY: lambda cfg: FakeHistory(cfg['alpha_key'])}),
        ProviderSpec('beta', (('beta_key', 'BETA_KEY'),),
                     {Capability.HISTORY: lambda cfg: FakeHistory(cfg['beta_key'])}),
        ProviderSpec('nohistory', (), {}),
    ]


def _registry(**config):
    return ProviderRegistry(config, _specs(), {Capability.HISTORY: 'alpha'})


def test_sources_for_lists_only_providers_with_a_builder():
    assert _registry().sources_for(Capability.HISTORY) == ['alpha', 'beta']


def test_get_builds_named_source_with_config():
    provider = _registry(beta_key='b-123').get(Capability.HISTORY, 'beta')
    assert isinstance(provider, FakeHistory)
    assert provider.key == 'b-123'


def test_get_without_source_uses_builtin_default():
    assert _registry(alpha_key='a').get(Capability.HISTORY).key == 'a'


def test_default_data_source_overrides_builtin_default_when_supported():
    registry = _registry(alpha_key='a', beta_key='b', default_data_source='beta')
    assert registry.default_source(Capability.HISTORY) == 'beta'


def test_default_data_source_ignored_when_it_lacks_the_capability():
    registry = _registry(default_data_source='nohistory')
    assert registry.default_source(Capability.HISTORY) == 'alpha'


def test_data_providers_mapping_wins_over_everything():
    registry = _registry(default_data_source='alpha', data_providers={'history': 'beta'})
    assert registry.default_source(Capability.HISTORY) == 'beta'


def test_unknown_source_raises_with_alternatives():
    with pytest.raises(CapabilityNotSupported) as info:
        _registry().get(Capability.HISTORY, 'gamma')
    assert info.value.supported == ['alpha', 'beta']


def test_source_without_capability_raises():
    with pytest.raises(CapabilityNotSupported):
        _registry().get(Capability.HISTORY, 'nohistory')


@pytest.mark.parametrize('value', ['', '   ', None])
def test_missing_or_blank_key_raises_not_configured(value):
    with pytest.raises(ProviderNotConfigured) as info:
        _registry(alpha_key=value).get(Capability.HISTORY, 'alpha')
    assert 'ALPHA_KEY' in str(info.value)


def test_each_get_returns_a_new_instance():
    registry = _registry(alpha_key='a')
    assert registry.get(Capability.HISTORY) is not registry.get(Capability.HISTORY)
