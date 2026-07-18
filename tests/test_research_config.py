"""[P2 Task 1] Isolated research-database configuration.

``research_duckdb_path`` is the offline research process's OWN store —
``trader_service`` never opens it. The config must expose it with a sensible
default, honour a ``RESEARCH_DUCKDB_PATH`` environment override, expand ``~``,
and FAIL CLOSED if the resolved path collides with the operational, history, or
journal DuckDB files (a shared file would let research writes contend with the
trader's authoritative stores and their cross-process locks).
"""
from __future__ import annotations

import os

import pytest

from trader.config import MMRConfig, StorageConfig


def _cfg(tmp_path, body: str = "") -> str:
    p = tmp_path / "trader.yaml"
    p.write_text(body)
    return str(p)


class TestResearchPathConfig:
    def test_storage_config_has_research_path_default(self):
        # Distinct from every operational default so the guard never trips on
        # the bundled defaults.
        s = StorageConfig()
        assert s.research_duckdb_path.endswith("mmr_research.duckdb")
        assert len({s.research_duckdb_path, s.duckdb_path,
                    s.history_duckdb_path, s.journal_duckdb_path}) == 4

    def test_default_research_path_is_expanded_and_absolute(self, tmp_path):
        cfg = MMRConfig.from_yaml(_cfg(tmp_path))
        assert cfg.storage.research_duckdb_path.endswith("mmr_research.duckdb")
        assert os.path.isabs(cfg.storage.research_duckdb_path)
        assert "~" not in cfg.storage.research_duckdb_path

    def test_yaml_override(self, tmp_path):
        cfg = MMRConfig.from_yaml(_cfg(tmp_path, "research_duckdb_path: ~/x/r.duckdb\n"))
        assert cfg.storage.research_duckdb_path == os.path.expanduser("~/x/r.duckdb")

    def test_env_override_takes_precedence(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RESEARCH_DUCKDB_PATH", "/tmp/p2_env_research.duckdb")
        cfg = MMRConfig.from_yaml(_cfg(tmp_path, "research_duckdb_path: ~/ignored.duckdb\n"))
        assert cfg.storage.research_duckdb_path == "/tmp/p2_env_research.duckdb"

    @pytest.mark.parametrize(
        "collide_key", ["duckdb_path", "history_duckdb_path", "journal_duckdb_path"])
    def test_guard_rejects_collision_with_operational_paths(self, tmp_path, collide_key):
        shared = str(tmp_path / "shared.duckdb")
        body = f"{collide_key}: {shared}\nresearch_duckdb_path: {shared}\n"
        with pytest.raises(ValueError, match="research_duckdb_path"):
            MMRConfig.from_yaml(_cfg(tmp_path, body))

    def test_distinct_paths_pass(self, tmp_path):
        op = str(tmp_path / "op.duckdb")
        research = str(tmp_path / "research.duckdb")
        cfg = MMRConfig.from_yaml(
            _cfg(tmp_path, f"duckdb_path: {op}\nresearch_duckdb_path: {research}\n"))
        assert cfg.storage.research_duckdb_path == research
        assert cfg.storage.research_duckdb_path != cfg.storage.duckdb_path
