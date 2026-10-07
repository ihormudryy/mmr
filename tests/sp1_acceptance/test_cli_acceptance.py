"""Plan 6 Task 5: `mmr experiment acceptance` against the served composed stack, keys read from files.

The keys are real PEM files written with write_keyset into MMR_RPC_KEYS_DIR; the
trader serves with the matching identities. The run directory is a temp dir.
"""
from __future__ import annotations

import json
import os

import pytest

from tests.rpc_identity_fixtures import make_identities, write_keyset
from tests.sp1_acceptance.test_acceptance_run import drive_session_to_flat, market
from tests.sp1_fixtures import served_stack
from trader import mmr_cli
from trader.mmr_cli import build_parser
from trader.sdk import MMR

ACCOUNT = "DU111111"


def _json(out):
    """The --json result is the last JSON object printed (log lines may come first)."""
    return json.loads(out[out.rindex('{"data"'):])["data"]


@pytest.fixture
def rpc_keys_dir(tmp_path):
    return tmp_path / "keys" / "rpc"


@pytest.fixture
def served_with_keys(tmp_path, loop_thread, monkeypatch, rpc_keys_dir):
    keys = write_keyset(rpc_keys_dir)
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc_keys_dir))
    monkeypatch.setenv("MMR_ACCEPTANCE_DIR", str(tmp_path / "acceptance"))
    stack = served_stack(tmp_path, loop_thread, monkeypatch, identities=make_identities(keys=keys),
                         acceptance_probe=True)

    def command_count():
        rows = stack.trader.journal_db.execute(
            "SELECT COUNT(*) FROM command_ledger WHERE action <> 'start_experiment'", fetch="one")
        return rows[0]
    stack.command_count = command_count
    yield stack
    stack.close()


def _sdk(served, keys_dir):
    sdk = object.__new__(MMR)
    sdk._typed_address = "tcp://127.0.0.1"
    sdk._typed_query_port = served.sockets.ports[("trader", "query")]
    sdk._typed_command_port = served.sockets.ports[("trader", "command")]
    sdk._rpc_keys_dir, sdk._rpc_principal, sdk._rpc_identity = str(keys_dir), "cli", None
    sdk._typed_query_client = sdk._typed_command_client = None
    sdk._timeout = 30
    return sdk


@pytest.fixture
def signing_key(tmp_path):
    from trader.research.signing import generate_private_key_pem
    path = tmp_path / "operator.key"
    path.write_bytes(generate_private_key_pem())
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def cli(served_with_keys, rpc_keys_dir, capsys, monkeypatch):
    sdk = _sdk(served_with_keys, rpc_keys_dir)

    def run(line, json_mode=False):
        monkeypatch.setattr(mmr_cli, "_json_mode", json_mode)
        args = build_parser().parse_args(line.split())
        code = 0
        try:
            mmr_cli._handle_experiment_acceptance(sdk, args, now=served_with_keys.now,
                                                  sleep=served_with_keys.advance_and_promote)
        except SystemExit as exc:
            code = exc.code
        out = capsys.readouterr().out
        run.code = code
        return out
    yield run
    for client in (sdk._typed_query_client, sdk._typed_command_client):
        if client is not None:
            client.close()


def test_dry_run_sends_no_command(served_with_keys, cli):
    out = cli("experiment acceptance run")
    assert "dry run" in out and "PASS" in out and served_with_keys.command_count() == 0
    assert "acceptance_shrink_probe" in out and cli.code == 0


def test_preflight_prints_pass_on_a_clean_account(served_with_keys, cli):
    out = cli("experiment acceptance preflight")
    assert out.strip().splitlines()[-1] == "PASS" and cli.code == 0


def test_preflight_prints_stop_and_exits_one_when_not_clean(served_with_keys, cli):
    served_with_keys.sim.held[265598] = 2.0
    served_with_keys.sim.promote()
    out = cli("experiment acceptance preflight")
    assert "STOP" in out and "POSITIONS_OPEN" in out and cli.code == 1


def test_run_refuses_wrong_account_and_container(served_with_keys, cli, signing_key, monkeypatch):     # Focus 4
    out = cli(f"experiment acceptance run --place-orders --confirm-account DU999999 --signing-key {signing_key}")
    assert "ACCOUNT_MISMATCH" in out and cli.code == 1
    monkeypatch.setattr("trader.messaging.keys_cli.running_in_container", lambda: True)
    out = cli(f"experiment acceptance run --place-orders --confirm-account {ACCOUNT} --signing-key {signing_key}")
    assert "HOST_ONLY" in out and "host only" in out
    assert served_with_keys.command_count() == 0


def test_missing_supervisor_key_refuses_before_any_call(served_with_keys, cli, rpc_keys_dir, signing_key):
    (rpc_keys_dir / "ai_supervisor.key").unlink()
    out = cli(f"experiment acceptance run --place-orders --confirm-account {ACCOUNT} --signing-key {signing_key}")
    assert "ai_supervisor.key" in out and served_with_keys.command_count() == 0


def test_missing_cli_key_refuses_a_real_run_but_not_the_dry_run(served_with_keys, cli, rpc_keys_dir, signing_key):
    (rpc_keys_dir / "cli.key").unlink()
    out = cli(f"experiment acceptance run --place-orders --confirm-account {ACCOUNT} --signing-key {signing_key}")
    assert "cli.key" in out and served_with_keys.command_count() == 0
    assert "dry run" in cli("experiment acceptance run")


def test_place_orders_without_a_signing_key_is_refused_before_any_call(served_with_keys, cli):   # ruling 20
    out = cli(f"experiment acceptance run --place-orders --confirm-account {ACCOUNT}")
    assert "SIGNING_KEY_REQUIRED" in out and served_with_keys.command_count() == 0


def test_finish_without_a_signing_key_is_refused(served_with_keys, cli):
    assert "SIGNING_KEY_REQUIRED" in cli("experiment acceptance finish --run-id acc-20260717-abcdef")


def test_a_resume_of_an_unknown_run_is_refused(served_with_keys, cli, signing_key):
    out = cli(f"experiment acceptance run --run-id acc-20260717-ffffff --place-orders --confirm-account {ACCOUNT} "
              f"--signing-key {signing_key}")
    assert "RUN_NOT_FOUND" in out and served_with_keys.command_count() == 0


def test_run_and_finish_end_to_end_over_the_cli(served_with_keys, cli, signing_key, tmp_path):
    market(served_with_keys)
    served_with_keys.sim.script_target_fills([1])
    out = cli(f"experiment acceptance run --place-orders --confirm-account {ACCOUNT} --signing-key {signing_key}",
              json_mode=True)
    data = _json(out)
    run_id = data["run_id"]
    assert data["passed"] is True, data
    drive_session_to_flat(served_with_keys)
    out = cli(f"experiment acceptance finish --run-id {run_id} --signing-key {signing_key}", json_mode=True)
    finish = _json(out)
    assert all(r["passed"] for r in finish["results"]), finish
    assert finish["passed"] is False                     # synthetic evidence never makes a passing session
    assert cli.code == 1                                 # ... so finish exits 1 although every end check passed
    report = tmp_path / "acceptance" / run_id / "finish-report.json"
    loaded = json.loads(report.read_text())
    assert (loaded["evidence_source"], loaded["signing_key"], loaded["oca_shrink"]) == (
        "synthetic", "operator", "PROVEN")
    status = cli(f"experiment acceptance status --run-id {run_id}")
    assert "[PASS] settle_s" in status and "[PASS] session_flat" in status


def test_a_real_report_signed_with_an_ephemeral_key_does_not_verify_as_the_operator(tmp_path, cli, signing_key):
    from trader.acceptance.report import build_report
    from trader.research.signing import AttestationSigner, public_key_pem
    ephemeral = AttestationSigner.generate()
    report = build_report(steps=[{"name": "x", "passed": True, "code": None, "evidence": {}}],
                          oca_shrink="PROVEN", evidence_source="ib_paper")
    report.sign(ephemeral, key_source="ephemeral")
    path = report.write(tmp_path / "eph.json")
    pub = tmp_path / "eph.pub"
    pub.write_bytes(public_key_pem(ephemeral.public_key))
    out = cli(f"experiment acceptance verify-report {path} --public-key {pub}")
    assert "signature OK" in out and "signing_key: ephemeral" in out and cli.code == 1
    operator = AttestationSigner.from_key_file(str(signing_key))
    report.sign(operator, key_source="operator")
    report.write(path)
    out = cli(f"experiment acceptance verify-report {path} --public-key {pub}")          # the wrong public key
    assert "signature BAD" in out and cli.code == 1
    pub.write_bytes(public_key_pem(operator.public_key))
    out = cli(f"experiment acceptance verify-report {path} --public-key {pub}")
    assert "signature OK" in out and "signing_key: operator" in out and cli.code == 0


def test_status_reads_the_local_journal_only(tmp_path, monkeypatch, capsys):
    from trader.acceptance.journal import RunJournal
    monkeypatch.setenv("MMR_ACCEPTANCE_DIR", str(tmp_path))
    journal = RunJournal(tmp_path / "acc-20260717-abcdef")
    journal.append("step", {"name": "preflight", "passed": True, "code": None, "evidence": {}, "phase": "run_step"})
    args = build_parser().parse_args("experiment acceptance status --run-id acc-20260717-abcdef".split())
    mmr_cli._handle_experiment_acceptance(object(), args)                  # no SDK method is ever called
    assert "[PASS] preflight" in capsys.readouterr().out


@pytest.mark.parametrize("report_passed,code", [(False, 1), (True, 0)])
def test_finish_exit_status_follows_the_signed_report(monkeypatch, capsys, report_passed, code):   # review #35
    from trader.acceptance import runner
    from trader.acceptance.scenario import END_CHECKS, StepResult
    checks = [StepResult(name, True, None, {}) for name in END_CHECKS]       # every end check passed
    monkeypatch.setattr(runner, "finish", lambda *a, **k: runner.RunOutcome(
        checks, "finish-report.json", "acc-20260717-abcdef", False, report_passed))
    sdk = type("Sdk", (), {"acceptance_endpoints": lambda self: None})()
    args = build_parser().parse_args("experiment acceptance finish --run-id acc-20260717-abcdef "
                                     "--signing-key k".split())
    try:
        mmr_cli._handle_experiment_acceptance(sdk, args)
        exit_code = 0
    except SystemExit as exc:
        exit_code = exc.code
    assert exit_code == code
