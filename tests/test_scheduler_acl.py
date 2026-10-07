"""Owner answer 3: the scheduler has no RPC identity and no trading RPC rights.

The scheduler ACL may not grow beyond what the listed pycron jobs need.
Adding a job without a row here fails the first test.
"""
import inspect
from pathlib import Path

import pytest
import yaml

from tests.compose_rpc_helpers import load_compose, visible_rpc_files
from trader.messaging import principals
from trader.messaging.principals import STRATEGY_ACL, TRADER_ACL
from trader.messaging.rpc_keys import RpcKeyError

ROOT = Path(__file__).resolve().parents[1]

# Every pycron job and the typed methods it needs.
SCHEDULED_JOB_RPC_NEEDS = {
    # data refresh: the get_status probe and the resolve fallback are soft
    # (_handle_data_download catches every exception; symbols come from the
    # local universe DB first).
    "data_refresh_us": frozenset(),
    "data_refresh_asx": frozenset(),
    "db_backup": frozenset(),          # data backup: local files only
}
SCHEDULER_ACL_ALLOWED = frozenset().union(*SCHEDULED_JOB_RPC_NEEDS.values())


def _pycron_jobs(path="config_defaults/pycron.yaml"):
    cfg = yaml.safe_load((ROOT / path).read_text())
    return {job["name"]: job for job in cfg["jobs"]}


def test_every_scheduled_job_is_classified():
    assert set(_pycron_jobs()) == set(SCHEDULED_JOB_RPC_NEEDS)


def test_scheduler_acl_never_grows_beyond_the_listed_jobs():
    granted = {m for (_, m), who in {**TRADER_ACL, **STRATEGY_ACL}.items() if "scheduler" in who}
    assert granted <= SCHEDULER_ACL_ALLOWED
    assert "scheduler" not in principals.KNOWN_PRINCIPALS


def test_scheduler_container_has_no_rpc_keys():
    assert visible_rpc_files(load_compose()["services"]["scheduler"]) == set()


def test_scheduler_principal_is_refused_by_the_sdk(monkeypatch):
    from trader.sdk import MMR
    monkeypatch.setenv("MMR_RPC_PRINCIPAL", "scheduler")
    with pytest.raises(ValueError, match="MMR_RPC_PRINCIPAL"):
        MMR._client_principal_from_env()


def test_a_keyless_sdk_cannot_sign_and_the_data_refresh_probe_is_soft(monkeypatch):
    # With no keys (the scheduler container), the first typed call fails
    # loudly with RpcKeyError ...
    from trader.sdk import MMR
    monkeypatch.delenv("MMR_RPC_PRINCIPAL", raising=False)
    sdk = MMR.__new__(MMR)
    sdk._rpc_principal = "cli"
    sdk._rpc_keys_dir = None
    sdk._rpc_identity = None
    with pytest.raises(RpcKeyError):
        sdk._load_rpc_identity()
    # ... and the data-download trader probe treats any failure as "no
    # trader" (rpc_mmr = None) instead of aborting the refresh.
    from trader import mmr_cli
    source = inspect.getsource(mmr_cli._handle_data_download)
    probe = source.split("candidate._typed_query.call('get_status'")[1].split("rpc_mmr = None")[0]
    assert "except Exception:" in probe
