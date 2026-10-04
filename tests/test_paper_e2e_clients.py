from __future__ import annotations

import pytest

from tests.paper_e2e._clients import _dashboard_token


def test_dashboard_token_prefers_environment_over_other_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "from-environment")
    token_file = tmp_path / "dashboard.token"
    token_file.write_text("from-file\n")
    monkeypatch.setenv("DASHBOARD_TOKEN_FILE", str(token_file))

    assert _dashboard_token() == "from-environment"


def test_dashboard_token_reads_config_file_when_environment_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("DASHBOARD_TOKEN_FILE", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    config_file = tmp_path / ".config" / "mmr" / "dashboard.token"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("from-config-file\n")

    assert _dashboard_token() == "from-config-file"


def test_dashboard_token_reads_repo_dotenv_when_other_sources_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("DASHBOARD_TOKEN_FILE", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".env").write_text('OTHER=value\nDASHBOARD_TOKEN="from-dotenv"\n')
    monkeypatch.setattr("tests.paper_e2e._clients._repo_root", lambda: tmp_path)

    assert _dashboard_token() == "from-dotenv"


def test_dashboard_token_explains_configured_sources_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("DASHBOARD_TOKEN_FILE", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("tests.paper_e2e._clients._repo_root", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="DASHBOARD_TOKEN.*DASHBOARD_TOKEN_FILE.*dashboard.token.*\\.env"):
        _dashboard_token()
