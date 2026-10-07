"""The real driver: ``mmr experiment acceptance preflight|run|finish|status|verify-report`` (Plan 6 Task 5).

Host only. It signs with the ``ai_research`` and ``ai_supervisor`` keys from
``MMR_RPC_KEYS_DIR`` (default ``~/.config/mmr/keys/rpc``) and, only with
``--place-orders``, with the operator ``cli`` key for the mark and the probe.
Order of checks for ``run``: host -> signing key -> identities -> the
scenario's preflight (two reads 30 s apart, ARMED, the confirmed account) ->
the scenario. A dry run (no ``--place-orders``) reads the preflight and prints
the planned calls; it sends nothing and writes nothing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from trader.acceptance.journal import RunJournal
from trader.acceptance.ports import RpcAcceptancePort
from trader.acceptance.preflight import READINGS_APART_SECONDS, evaluate_preflight
from trader.acceptance.report import AcceptanceReport, build_report
from trader.acceptance.scenario import (
    END_CHECKS, AcceptanceScenario, AcceptanceSettings, StepResult, new_run_id,
)

ACCEPTANCE_DIR_ENV = "MMR_ACCEPTANCE_DIR"
DEFAULT_ACCEPTANCE_DIR = "~/.local/share/mmr/acceptance"
DEFAULT_STRATEGY = "strategies/opening_range_breakout.py"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AcceptanceRefused(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Endpoints:
    address: str
    query_port: int
    command_port: int
    keys_dir: Optional[str]
    timeout: float = 30.0


def running_in_container() -> bool:
    """Plan 2 ruling 15: the same check ``mmr keys`` uses."""
    from trader.messaging.keys_cli import running_in_container as check
    return check()


def acceptance_root() -> Path:
    return Path(os.environ.get(ACCEPTANCE_DIR_ENV) or DEFAULT_ACCEPTANCE_DIR).expanduser()


def keys_dir_of(endpoints: Endpoints) -> Path:
    from trader.research.key_purpose import default_rpc_keys_dir
    return Path(endpoints.keys_dir).expanduser() if endpoints.keys_dir else default_rpc_keys_dir()


def load_identities(endpoints: Endpoints, principals: tuple[str, ...]) -> dict:
    from trader.messaging.typed_rpc import ServiceIdentity
    directory = keys_dir_of(endpoints)
    identities = {}
    for principal in principals:
        if not (directory / f"{principal}.key").is_file():
            raise AcceptanceRefused("KEY_MISSING", f"{principal}.key not found in {directory}")
        try:
            identities[principal] = ServiceIdentity.load(principal, str(directory))
        except Exception as exc:
            raise AcceptanceRefused("KEY_UNUSABLE", f"{principal}.key in {directory}: {exc}") from None
    return identities


class Clients:
    def __init__(self, endpoints: Endpoints, identities: dict):
        from trader.messaging.typed_rpc import TypedRpcClient
        self._clients = []

        def client(principal: str, role: str):
            made = TypedRpcClient(role, identities[principal], server="trader", address=endpoints.address,
                                  port=endpoints.query_port if role == "query" else endpoints.command_port,
                                  timeout=endpoints.timeout)
            made.connect()
            self._clients.append(made)
            return made
        self.client = client

    def close(self) -> None:
        for made in self._clients:
            try:
                made.close()
            except Exception:
                pass


def build_port(endpoints: Endpoints, *, research: bool, operator: bool,
               now: Callable[[], dt.datetime], sleep: Callable[[float], None]) -> tuple[RpcAcceptancePort, Clients]:
    principals = ("ai_supervisor",) + (("ai_research",) if research else ()) + (("cli",) if operator else ())
    identities = load_identities(endpoints, principals)
    clients = Clients(endpoints, identities)
    port = RpcAcceptancePort(
        clients.client("ai_research", "command") if research else None,
        clients.client("ai_supervisor", "command"), clients.client("ai_supervisor", "query"),
        operator_client=clients.client("cli", "command") if operator else None, now=now, sleep=sleep)
    return port, clients


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def preflight(query: Callable[[str, dict], dict], *, now: Callable[[], dt.datetime] = _utc_now,
              sleep: Callable[[float], None] = time.sleep) -> dict:
    """The operator gate (ruling 3): two reads 30 s apart. ``query`` is a cli-signed typed query."""
    first = query("get_acceptance_preflight", {})
    sleep(READINGS_APART_SECONDS)
    second = query("get_acceptance_preflight", {})
    result = evaluate_preflight(first, second, now=now())
    return {"passed": result.passed, "failures": list(result.failures), "first": first, "second": second}


# ---------------------------------------------------------------------------
# run / finish
# ---------------------------------------------------------------------------

def _strategy_bytes() -> bytes:
    path = _PROJECT_ROOT / DEFAULT_STRATEGY
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AcceptanceRefused("STRATEGY_FILE_MISSING", f"{path}: {exc}") from None


def _signer(path: Optional[str]):
    from trader.research.signing import AttestationSigner
    if not path:
        return None
    try:
        return AttestationSigner.from_key_file(str(Path(path).expanduser()))
    except Exception as exc:
        raise AcceptanceRefused("SIGNING_KEY_UNUSABLE", f"{path}: {exc}") from None


def _evidence_source(port: Any) -> str:
    try:
        source = port.evidence(None).get("source")
    except Exception:
        return "synthetic"
    return "ib_paper" if source == "ib" else "synthetic"


def _order_evidence(results: list) -> list[dict]:
    rows = []
    for result in results:
        evidence = result.evidence if isinstance(result, StepResult) else result.get("evidence") or {}
        name = result.name if isinstance(result, StepResult) else result.get("name")
        for key in ("legs", "s_legs", "pair"):
            value = evidence.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                rows.append({"step": name, "rows": value})
            elif key == "pair" and isinstance(value, dict):
                rows.append({"step": name, "pair": value})
    return rows


def _write_report(report: AcceptanceReport, signer: Any, path: Path) -> Path:
    from trader.research.signing import AttestationSigner
    report.sign(signer or AttestationSigner.generate(), key_source="operator" if signer else "ephemeral")
    return report.write(path)


@dataclass
class RunOutcome:
    results: list
    report_path: Optional[Path]
    run_id: str
    dry_run: bool
    passed: bool


def run(endpoints: Endpoints, *, run_id: Optional[str], conids: tuple[int, int], quantity_a: int, quantity_b: int,
        notional: float, place_orders: bool, confirm_account: Optional[str], report_path: Optional[str],
        signing_key: Optional[str], now: Callable[[], dt.datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep) -> RunOutcome:
    if running_in_container():
        raise AcceptanceRefused("HOST_ONLY", "mmr experiment acceptance runs on the host only, not in a container")
    if place_orders and not signing_key:
        raise AcceptanceRefused("SIGNING_KEY_REQUIRED", "--place-orders needs --signing-key (an operator key)")
    if place_orders and not confirm_account:
        raise AcceptanceRefused("CONFIRM_ACCOUNT_REQUIRED", "--place-orders needs --confirm-account DU...")
    signer = _signer(signing_key)
    resuming = False
    if run_id is not None:
        if not (acceptance_root() / run_id / "journal.jsonl").is_file():
            raise AcceptanceRefused("RUN_NOT_FOUND", f"no journal for run {run_id} in {acceptance_root()}")
        resuming = True
    if resuming and not place_orders:
        raise AcceptanceRefused("PLACE_ORDERS_REQUIRED", "a resume sends orders; it needs --place-orders")
    port, clients = build_port(endpoints, research=place_orders, operator=place_orders, now=now, sleep=sleep)
    try:
        chosen = run_id or new_run_id(now())
        settings = AcceptanceSettings(
            run_id=chosen, account_id=confirm_account or "", conid_a=int(conids[0]), conid_b=int(conids[1]),
            quantity_a=quantity_a, quantity_b=quantity_b, notional=float(notional),
            strategy_bytes=_strategy_bytes())
        if not place_orders:
            scenario = AcceptanceScenario(port, settings, _DryJournal())
            results = scenario.run()
            return RunOutcome(results, None, chosen, True, False)
        journal = RunJournal(acceptance_root() / chosen)
        if resuming:
            stored = (journal.settings() or {}).get("settings") or {}
            if stored.get("account_id") and stored.get("account_id") != confirm_account:
                raise AcceptanceRefused("ACCOUNT_MISMATCH", "the run journal names another account")
        scenario = AcceptanceScenario(port, settings, journal)
        results = scenario.run()
        outcome = AcceptanceScenario.outcome(results)
        report = build_report(run_id=chosen, account_id=settings.account_id, phase="run", steps=results,
                              oca_shrink=outcome.oca_shrink, evidence_source=_evidence_source(port),
                              order_evidence=_order_evidence(results), now=now())
        path = _write_report(report, signer, Path(report_path).expanduser() if report_path
                             else acceptance_root() / chosen / "run-report.json")
        return RunOutcome(results, path, chosen, False, outcome.passed)
    finally:
        clients.close()


class _DryJournal:
    """A dry run writes nothing: the scenario refuses before any write without the operator channel."""

    directory = Path(".")

    def settings(self):
        return None

    def append(self, kind, payload):
        raise AcceptanceRefused("DRY_RUN", "a dry run never writes the journal")


def finish(endpoints: Endpoints, *, run_id: str, report_path: Optional[str], signing_key: Optional[str],
           now: Callable[[], dt.datetime] = _utc_now, sleep: Callable[[float], None] = time.sleep) -> RunOutcome:
    if running_in_container():
        raise AcceptanceRefused("HOST_ONLY", "mmr experiment acceptance runs on the host only, not in a container")
    if not signing_key:
        raise AcceptanceRefused("SIGNING_KEY_REQUIRED", "finish needs --signing-key (an operator key)")
    signer = _signer(signing_key)
    directory = acceptance_root() / run_id
    if not (directory / "journal.jsonl").is_file():
        raise AcceptanceRefused("RUN_NOT_FOUND", f"no journal for run {run_id} in {acceptance_root()}")
    journal = RunJournal(directory)
    stored = dict((journal.settings() or {}).get("settings") or {})
    stored.pop("strategy_digest", None)
    settings = AcceptanceSettings(**stored) if stored else AcceptanceSettings(run_id=run_id, account_id="")
    port, clients = build_port(endpoints, research=False, operator=False, now=now, sleep=sleep)
    try:
        end = AcceptanceScenario(port, settings, journal).finish()
        steps = _latest_run_steps(journal)
        proof = next((s for s in steps if s["name"] == "shrink_proof"), None)
        oca = "NOT_RUN" if proof is None else (proof.get("evidence") or {}).get("oca_shrink", "UNPROVEN")
        report = build_report(run_id=run_id, account_id=settings.account_id, phase="finish", steps=steps,
                              end_checks=end, oca_shrink=oca, evidence_source=_evidence_source(port),
                              order_evidence=_order_evidence(steps), now=now())
        path = _write_report(report, signer, Path(report_path).expanduser() if report_path
                             else directory / "finish-report.json")
        return RunOutcome(end, path, run_id, False, report.passed)
    finally:
        clients.close()


def _latest_run_steps(journal: RunJournal) -> list[dict]:
    latest: dict[str, dict] = {}
    for entry in journal.of_kind("step"):
        if entry.get("phase", "run_step") == "run_step":
            latest[entry["name"]] = {k: entry[k] for k in ("name", "passed", "code", "evidence")}
    return list(latest.values())


def status(run_id: str) -> dict:
    """The local journal only: no RPC, no key."""
    directory = acceptance_root() / run_id
    if not (directory / "journal.jsonl").is_file():
        raise AcceptanceRefused("RUN_NOT_FOUND", f"no journal for run {run_id} in {acceptance_root()}")
    journal = RunJournal(directory)
    checks = [e for e in journal.of_kind("step") if e.get("phase") == "end_check"]
    return {"run_id": run_id, "settings": (journal.settings() or {}).get("settings"),
            "steps": _latest_run_steps(journal),
            "end_checks": [{k: e[k] for k in ("name", "passed", "code")} for e in checks],
            "intents": len(journal.of_kind("intent")), "receipts": len(journal.of_kind("receipt"))}


def verify_report(path: str, public_key_path: str) -> dict:
    from trader.research.signing import load_verify_key
    report = AcceptanceReport.load(Path(path))
    key = load_verify_key(str(Path(public_key_path).expanduser()))
    try:
        report.verify(key)
        signature_ok = True
    except Exception:
        signature_ok = False
    fields = {name: getattr(report, name) for name in ("signing_key", "evidence_source", "deployment_record",
                                                       "live_restart_recovery", "oca_shrink", "passed", "phase")}
    return {"signature_ok": signature_ok, "valid": signature_ok and report.signing_key == "operator", **fields}


def print_lines_for(results: list) -> list[str]:
    lines = []
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        lines.append(f"[{mark}] {result.name}" + ("" if result.code is None else f": {result.code}"))
    return lines


def to_json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


__all__ = ["AcceptanceRefused", "Endpoints", "END_CHECKS", "finish", "preflight", "run", "status",
           "verify_report"]
