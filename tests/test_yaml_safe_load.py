"""Config YAML must load with the safe loader: Python object tags are refused."""
import argparse
from pathlib import Path

import pytest
import yaml

from trader.config import MMRConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
# FullLoader refuses the first payload but still builds the second, so the
# second one is what proves the loader is really the safe one.
COMMAND_PAYLOAD = '!!python/object/apply:os.system ["touch {marker}"]\n'
PYTHON_TUPLE_PAYLOAD = 'strategies: !!python/tuple [1, 2]\n'


@pytest.fixture
def marker(tmp_path):
    return tmp_path / 'command_ran'


@pytest.fixture(params=[COMMAND_PAYLOAD, PYTHON_TUPLE_PAYLOAD], ids=['apply_tag', 'tuple_tag'])
def malicious_text(request, marker):
    return request.param.format(marker=marker)


@pytest.fixture
def malicious_yaml(tmp_path, malicious_text):
    path = tmp_path / 'malicious.yaml'
    path.write_text(malicious_text)
    return path


@pytest.fixture
def home_with_malicious_strategy_config(tmp_path, monkeypatch, malicious_text):
    home = tmp_path / 'home'
    config_dir = home / '.config' / 'mmr'
    config_dir.mkdir(parents=True)
    (config_dir / 'strategy_runtime.yaml').write_text(malicious_text)
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.mark.parametrize('config_file', sorted((REPO_ROOT / 'config_defaults').glob('*.yaml')), ids=lambda p: p.name)
def test_shipped_default_configs_load_with_safe_loader(config_file):
    yaml.safe_load(config_file.read_text())


def test_mmr_config_from_yaml_refuses_python_tags(malicious_yaml, marker):
    with pytest.raises(yaml.YAMLError):
        MMRConfig.from_yaml(str(malicious_yaml))
    assert not marker.exists()


@pytest.mark.parametrize(
    'handler_name, args',
    [
        ('_handle_strategies_from_config', ()),
        ('_handle_strategy_deploy', (argparse.Namespace(name='x'),)),
        ('_handle_strategy_undeploy', (argparse.Namespace(name='x'),)),
        ('_handle_strategy_backtest', (argparse.Namespace(name='x'),)),
    ],
)
def test_cli_strategy_handlers_refuse_python_tags(
    home_with_malicious_strategy_config, marker, handler_name, args
):
    from trader import mmr_cli

    with pytest.raises(yaml.YAMLError):
        getattr(mmr_cli, handler_name)(*args)
    assert not marker.exists()


def test_pycron_main_refuses_python_tags(malicious_yaml, marker, monkeypatch):
    import socket

    from pycron import pycron

    class OfflineSocket:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args):
            pass

        def getsockname(self):
            return ('127.0.0.1', 0)

        def close(self):
            pass

    monkeypatch.setattr(socket, 'socket', OfflineSocket)
    with pytest.raises(yaml.YAMLError):
        pycron.main(str(malicious_yaml), [], [])
    assert not marker.exists()
