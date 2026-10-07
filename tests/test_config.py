import os
import pytest
from trader.config import (
    IB_GATEWAY_LIVE_PORT, IB_GATEWAY_PAPER_PORT,
    IB_LIVE_PORT, IB_PAPER_PORT, MMRConfig,
)


class TestMMRConfig:
    def test_from_yaml_parses_values(self, test_config_file):
        config = MMRConfig.from_yaml(test_config_file)
        assert config.ib.server_address == '127.0.0.1'
        # Fixture has trading_mode: paper → auto-resolves paper account/port
        assert config.ib.server_port == IB_PAPER_PORT
        assert config.ib.account == 'TESTPAPER'
        assert config.ib.paper_account == 'TESTPAPER'
        assert config.ib.live_account == 'TESTLIVE'
        assert config.zmq.rpc_server_port == 42001
        assert config.storage.universe_library == 'Universes'

    def test_defaults_when_keys_missing(self, tmp_path):
        minimal = tmp_path / "minimal.yaml"
        minimal.write_text("ib_server_address: 10.0.0.1\n")
        config = MMRConfig.from_yaml(str(minimal))
        assert config.ib.server_address == '10.0.0.1'
        # Default trading_mode is 'live' → live port
        assert config.ib.server_port == IB_LIVE_PORT
        # duckdb_path is resolved to absolute against project root
        assert config.storage.duckdb_path.endswith('data/mmr.duckdb')
        assert os.path.isabs(config.storage.duckdb_path)
        assert config.zmq.rpc_server_port == 42001

    def test_env_var_override_port(self, test_config_file, monkeypatch):
        monkeypatch.setenv('IB_SERVER_PORT', '9999')
        config = MMRConfig.from_yaml(test_config_file)
        assert config.ib.server_port == 9999

    def test_env_var_override_account(self, test_config_file, monkeypatch):
        monkeypatch.setenv('IB_ACCOUNT', 'ENVACCT')
        config = MMRConfig.from_yaml(test_config_file)
        assert config.ib.account == 'ENVACCT'

    def test_to_flat_dict_roundtrip(self, test_config_file):
        config = MMRConfig.from_yaml(test_config_file)
        flat = config.to_flat_dict()
        assert flat['ib_server_address'] == '127.0.0.1'
        assert flat['ib_server_port'] == IB_PAPER_PORT
        assert flat['ib_account'] == 'TESTPAPER'
        assert flat['ib_paper_account'] == 'TESTPAPER'
        assert flat['ib_live_account'] == 'TESTLIVE'
        assert flat['zmq_rpc_server_port'] == 42001
        assert 'duckdb_path' in flat

    # ── Account auto-resolution ──────────────────────────────────────────

    def test_paper_mode_selects_paper_account(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: paper\nib_paper_account: DU111\nib_live_account: U222\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.ib.account == 'DU111'
        assert config.paper_trading is True

    def test_live_mode_selects_live_account(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: live\nib_paper_account: DU111\nib_live_account: U222\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.ib.account == 'U222'
        assert config.paper_trading is False

    def test_ib_account_env_overrides_auto_selection(self, tmp_path, monkeypatch):
        monkeypatch.setenv('IB_ACCOUNT', 'OVERRIDE')
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: paper\nib_paper_account: DU111\nib_live_account: U222\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.ib.account == 'OVERRIDE'

    # ── Port auto-resolution ─────────────────────────────────────────────

    def test_paper_mode_selects_paper_port(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: paper\nib_paper_port: 7497\nib_live_port: 7496\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.ib.server_port == 7497

    def test_live_mode_selects_live_port(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: live\nib_paper_port: 7497\nib_live_port: 7496\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.ib.server_port == 7496

    def test_custom_ports_respected(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: paper\nib_paper_port: 4004\nib_live_port: 4003\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.ib.server_port == 4004

    def test_ib_server_port_env_overrides_auto_selection(self, tmp_path, monkeypatch):
        monkeypatch.setenv('IB_SERVER_PORT', '5555')
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: paper\nib_paper_port: 7497\nib_live_port: 7496\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.ib.server_port == 5555

    # ── Typed RPC config (G0 Task 3) ─────────────────────────────────────

    def test_typed_rpc_defaults(self, test_config_file):
        config = MMRConfig.from_yaml(test_config_file)
        assert config.typed_rpc.query_port == 42101
        assert config.typed_rpc.command_port == 42102
        assert config.typed_rpc.feed_port == 42103
        # Safe loopback default -- Compose's `trader` overrides to 0.0.0.0.
        assert config.typed_rpc.address == 'tcp://127.0.0.1'
        assert config.unsafe_legacy_rpc is False

    def test_typed_rpc_yaml_overrides(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "typed_bind_address: tcp://0.0.0.0\n"
            "typed_query_port: 51101\n"
            "typed_command_port: 51102\n"
            "typed_feed_port: 51103\n"
        )
        config = MMRConfig.from_yaml(str(cfg))
        assert config.typed_rpc.address == 'tcp://0.0.0.0'
        assert config.typed_rpc.query_port == 51101
        assert config.typed_rpc.command_port == 51102
        assert config.typed_rpc.feed_port == 51103

    def test_typed_bind_address_env_override(self, test_config_file, monkeypatch):
        # G0 Task 5: the Compose `trader` service sets TYPED_BIND_ADDRESS so
        # the typed ROUTER sockets bind all interfaces (0.0.0.0), making the
        # loopback host-publish and cross-container reach work. Env must win.
        monkeypatch.setenv('TYPED_BIND_ADDRESS', 'tcp://0.0.0.0')
        config = MMRConfig.from_yaml(test_config_file)
        assert config.typed_rpc.address == 'tcp://0.0.0.0'

    def test_typed_rpc_ports_in_flat_dict(self, test_config_file):
        config = MMRConfig.from_yaml(test_config_file)
        flat = config.to_flat_dict()
        assert flat['typed_bind_address'] == 'tcp://127.0.0.1'
        assert flat['typed_query_port'] == 42101
        assert flat['typed_command_port'] == 42102
        assert flat['typed_feed_port'] == 42103
        assert flat['unsafe_legacy_rpc'] is False

    # ── unsafe_legacy_rpc top-level bool env-var coercion ────────────────
    #
    # Regression guard: `unsafe_legacy_rpc` is the first TOP-LEVEL (not
    # nested) bool in _FLAT_KEY_MAP. The env-var branch for top-level fields
    # used to do `type(current_val)(value)`, and since bool is a subclass of
    # int, `bool("false")` is True (any non-empty string is truthy) -- so
    # UNSAFE_LEGACY_RPC=false would have silently ENABLED the unsafe legacy
    # RPC path. Assert both directions parse correctly via the env var.

    def test_unsafe_legacy_rpc_env_var_false_string_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv('UNSAFE_LEGACY_RPC', 'false')
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("unsafe_legacy_rpc: true\n")  # YAML says true; env var must win as False
        config = MMRConfig.from_yaml(str(cfg))
        assert config.unsafe_legacy_rpc is False

    def test_unsafe_legacy_rpc_env_var_true_string_is_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv('UNSAFE_LEGACY_RPC', 'true')
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("unsafe_legacy_rpc: false\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.unsafe_legacy_rpc is True

    def test_unsafe_legacy_rpc_yaml_bool_parses_directly(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("unsafe_legacy_rpc: true\n")
        config = MMRConfig.from_yaml(str(cfg))
        assert config.unsafe_legacy_rpc is True

    def test_nested_automation_block_loads(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "trading_mode: paper\n"
            "automation:\n"
            "  enabled: true\n"
            "  live_enabled: false\n"
            "  artifact_bundle_path: ~/artifacts/x\n"
            "  public_key_ring_path: ~/keys\n"
            "  expected_artifact_id: artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "  strategy_name: orb_googl\n"
        )
        config = MMRConfig.from_yaml(str(cfg))
        assert config.automation.enabled is True
        assert config.automation.live_enabled is False
        assert config.automation.strategy_name == "orb_googl"
        assert config.automation.expected_artifact_id.startswith("artifact-")
        assert "artifacts/x" in config.automation.artifact_bundle_path

    def test_flat_automation_overrides_nested(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "automation:\n"
            "  enabled: true\n"
            "  strategy_name: nested_name\n"
            "automation_enabled: false\n"
            "automation_strategy_name: flat_name\n"
        )
        config = MMRConfig.from_yaml(str(cfg))
        assert config.automation.enabled is False
        assert config.automation.strategy_name == "flat_name"

    def test_automation_live_enabled_refused(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("automation:\n  live_enabled: true\n")
        with pytest.raises(ValueError, match="automation.live_enabled"):
            MMRConfig.from_yaml(str(cfg))

    def test_quote_fallback_is_off_by_default(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("automation:\n  enabled: false\n")
        assert MMRConfig.from_yaml(str(cfg)).automation.quote_fallback == ''

    @pytest.mark.parametrize("value", ["alpaca_iex", "''"])
    def test_quote_fallback_known_values_load(self, tmp_path, value):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(f"automation:\n  quote_fallback: {value}\n")
        assert MMRConfig.from_yaml(str(cfg)).automation.quote_fallback == value.strip("'")

    @pytest.mark.parametrize("value", ["alpaca", "ALPACA_IEX", "' alpaca_iex'", "true", "iex"])
    def test_quote_fallback_unknown_value_fails_loudly(self, tmp_path, value):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(f"automation:\n  quote_fallback: {value}\n")
        with pytest.raises(ValueError, match="automation.quote_fallback"):
            MMRConfig.from_yaml(str(cfg))

    def test_quote_fallback_flat_env_key_is_validated_too(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("automation:\n  quote_fallback: alpaca_iex\n")
        monkeypatch.setenv('AUTOMATION_QUOTE_FALLBACK', 'polygon')
        with pytest.raises(ValueError, match="automation.quote_fallback"):
            MMRConfig.from_yaml(str(cfg))


class TestEmptyEnvProviderKeys:
    """docker-compose passes `KEY: ${KEY:-}`, so an unset host var arrives as ''."""

    def _write(self, tmp_path):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "alpaca_api_key_id: yaml-alpaca-id\n"
            "alpaca_api_secret_key: yaml-alpaca-secret\n"
            "massive_api_key: yaml-massive\n"
            "twelvedata_api_key: yaml-twelve\n"
        )
        return str(cfg)

    def test_empty_env_keeps_yaml_value(self, tmp_path, monkeypatch):
        for name in ('ALPACA_API_KEY_ID', 'ALPACA_API_SECRET_KEY', 'MASSIVE_API_KEY', 'TWELVEDATA_API_KEY'):
            monkeypatch.setenv(name, '')
        config = MMRConfig.from_yaml(self._write(tmp_path))
        assert config.alpaca.api_key_id == 'yaml-alpaca-id'
        assert config.alpaca.secret_key == 'yaml-alpaca-secret'
        assert config.massive.api_key == 'yaml-massive'
        assert config.twelvedata.api_key == 'yaml-twelve'

    def test_whitespace_env_keeps_yaml_value(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ALPACA_API_KEY_ID', '  ')
        config = MMRConfig.from_yaml(self._write(tmp_path))
        assert config.alpaca.api_key_id == 'yaml-alpaca-id'

    def test_non_empty_env_overrides_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ALPACA_API_KEY_ID', 'env-alpaca-id')
        monkeypatch.setenv('MASSIVE_API_KEY', 'env-massive')
        config = MMRConfig.from_yaml(self._write(tmp_path))
        assert config.alpaca.api_key_id == 'env-alpaca-id'
        assert config.massive.api_key == 'env-massive'

    def test_env_alone_still_sets_alpaca_keys(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("trading_mode: paper\n")
        monkeypatch.setenv('ALPACA_API_KEY_ID', 'env-id')
        monkeypatch.setenv('ALPACA_API_SECRET_KEY', 'env-secret')
        config = MMRConfig.from_yaml(str(cfg))
        assert config.alpaca.api_key_id == 'env-id'
        assert config.alpaca.secret_key == 'env-secret'


class TestRetiredHmacConfig:
    def test_retired_service_hmac_key_file_is_ignored_with_a_warning(self, tmp_path, caplog, monkeypatch):
        import logging
        from trader.config import MMRConfig
        monkeypatch.delenv("MMR_SERVICE_HMAC_KEY_FILE", raising=False)
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("service_hmac_key_file: /run/secrets/service_hmac.key\n")
        with caplog.at_level(logging.WARNING, logger="trader.config"):
            config = MMRConfig.from_yaml(str(cfg))
        assert not hasattr(config.typed_rpc, "service_hmac_key_file")
        warnings = [r for r in caplog.records if "retired" in r.getMessage()]
        assert len(warnings) == 1 and "service_hmac_key_file" in warnings[0].getMessage()

    def test_retired_hmac_env_var_is_ignored_with_a_warning(self, tmp_path, caplog, monkeypatch):
        import logging
        from trader.config import MMRConfig
        monkeypatch.setenv("MMR_SERVICE_HMAC_KEY_FILE", "/x/service_hmac.key")
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("typed_query_port: 42101\n")
        with caplog.at_level(logging.WARNING, logger="trader.config"):
            MMRConfig.from_yaml(str(cfg))
        assert any("MMR_SERVICE_HMAC_KEY_FILE" in r.getMessage() and "retired" in r.getMessage()
                   for r in caplog.records)

    def test_no_warning_without_leftovers(self, tmp_path, caplog, monkeypatch):
        import logging
        from trader.config import MMRConfig
        monkeypatch.delenv("MMR_SERVICE_HMAC_KEY_FILE", raising=False)
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("typed_query_port: 42101\n")
        with caplog.at_level(logging.WARNING, logger="trader.config"):
            MMRConfig.from_yaml(str(cfg))
        assert not [r for r in caplog.records if "retired" in r.getMessage()]
