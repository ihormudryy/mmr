"""Offline bootstrap never manufactures normal activation evidence."""
import runpy
import datetime as dt
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_paper_automation.py"


def run_bootstrap(monkeypatch, tmp_path, *args):
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--config-dir", str(tmp_path / "config"),
                                  "--share-dir", str(tmp_path / "share"), *args])
    return runpy.run_path(str(SCRIPT))["main"]()


def test_bootstrap_requires_research_or_explicit_fixture_before_any_write(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_bootstrap(monkeypatch, tmp_path)
    assert exc.value.code == 2
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "share").exists()
    assert "existing research" in capsys.readouterr().err


def test_bootstrap_reads_existing_research_without_private_material(tmp_path, monkeypatch, capsys):
    from . import paper_evidence_helpers

    monkeypatch.setattr(paper_evidence_helpers, "NOW", dt.datetime.now(dt.timezone.utc))
    bundle, key_ring = paper_evidence_helpers.research_bundle(tmp_path)
    assert run_bootstrap(
        monkeypatch, tmp_path, "--artifact-bundle-path", str(bundle),
        "--public-key-ring-path", str(key_ring), "--expected-artifact-id", bundle.name,
    ) == 0
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "share").exists()
    output = capsys.readouterr().out
    assert str(bundle) in output
    assert "enabled: false" in output


def test_bootstrap_offline_fixture_prints_no_activation_snippets(tmp_path, monkeypatch, capsys):
    assert run_bootstrap(monkeypatch, tmp_path, "--offline-fixture") == 0
    output = capsys.readouterr().out
    assert "OFFLINE FIXTURE" in output
    assert "not qualification or promotion evidence" in output
    assert "enabled: true" not in output
    assert not (tmp_path / "config" / "keys").exists()
    assert not (tmp_path / "share" / "artifacts").exists()
