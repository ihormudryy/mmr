"""Resume validation (Plan 6 ruling 15): a resume is not a first run.

After ``ENTER A`` the account rightly holds A and its stop, so the flat-account
preflight cannot apply. Instead the account may hold only what the journal
says was sent: positions on the journal's conids up to the quantities its
steps could have produced, orders of the journal's own order groups, and no
unresolved command except the journal's own in-flight ones.
"""
from __future__ import annotations

from typing import Any, Optional

from trader.acceptance.preflight import PreflightResult

_ENTRY_STEPS = {"enter_a": ("conid_a", "quantity_a"), "enter_b": ("conid_b", "quantity_b"),
                "enter_s": ("conid_a", "quantity_s")}
# The position a completed step leaves on its conid (None: the step does not fix it).
_AFTER_STEP = {"enter_a": ("conid_a", lambda s: s.quantity_a), "enter_b": ("conid_b", lambda s: s.quantity_b),
               "partial_close_a": ("conid_a", lambda s: s.quantity_a - s.partial_quantity),
               "close_a": ("conid_a", lambda s: 0), "enter_s": ("conid_a", lambda s: s.quantity_s)}


def _settings(journal: Any, settings: Any) -> Any:
    from trader.acceptance.scenario import AcceptanceSettings
    if settings is not None:
        return settings
    record = journal.settings()
    if record is None:
        return AcceptanceSettings(run_id=journal.directory.name, account_id="")
    fields = dict(record["settings"])
    fields.pop("strategy_digest", None)
    return AcceptanceSettings(**fields)


def _sent_steps(journal: Any) -> list[str]:
    steps = []
    for entry in journal.entries():
        if entry["kind"] in ("intent", "receipt") and entry["step"] not in steps:
            steps.append(entry["step"])
    return steps


def _own_ids(journal: Any, run_id: str) -> set[str]:
    ids = {entry.get("command_id") for entry in journal.of_kind("intent") if entry.get("command_id")}
    return ids | {run_id}


def validate_resume(journal: Any, port: Any, settings: Any = None) -> PreflightResult:
    settings = _settings(journal, settings)
    failures: list[str] = []
    sent = _sent_steps(journal)
    allowed: dict[int, float] = {}
    for step in sent:
        if step in _ENTRY_STEPS:
            conid_field, quantity_field = _ENTRY_STEPS[step]
            conid = getattr(settings, conid_field)
            allowed[conid] = max(allowed.get(conid, 0.0), float(getattr(settings, quantity_field)))

    reading = port.supervisor("get_acceptance_preflight", {})
    if reading.get("capture_error"):
        return PreflightResult(False, ("CAPTURE_UNAVAILABLE",))
    for row in reading.get("positions") or []:
        conid, quantity = int(row["conid"]), float(row["quantity"])
        if conid not in allowed:
            failures.append("RESUME_UNKNOWN_POSITION")
        elif not 0 <= quantity <= allowed[conid]:
            failures.append("RESUME_STATE_MISMATCH")

    own = _own_ids(journal, settings.run_id)
    evidence = port.evidence(None)
    for order in evidence.get("orders") or []:
        if order.get("status") not in ("Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel", "ApiPending"):
            continue
        group = order.get("order_group_id") or ""
        if order.get("conid") not in allowed or not any(i in group for i in own):
            failures.append("RESUME_UNKNOWN_ORDER")

    for command in reading.get("unresolved_commands") or []:
        if not any(i in command for i in own):
            failures.append("RESUME_STATE_MISMATCH")
    if reading.get("breaker_tripped") is not False:
        failures.append("BREAKER_TRIPPED")
    experiment = (port.supervisor("get_experiment", {}).get("experiment") or {})
    if experiment.get("state") != "ARMED":
        failures.append("EXPERIMENT_NOT_ARMED")

    expected = _expected_after_last_step(journal, settings, sent)
    if expected is not None:
        conid, quantity = expected
        held = sum(float(r["quantity"]) for r in reading.get("positions") or [] if int(r["conid"]) == conid)
        if held != quantity:
            failures.append("RESUME_STATE_MISMATCH")
    unique = tuple(dict.fromkeys(failures))
    return PreflightResult(passed=not unique, failures=unique)


def _expected_after_last_step(journal: Any, settings: Any, sent: list[str]) -> Optional[tuple[int, float]]:
    """The last passed step fixes its conid's position, unless a later step on that conid was sent."""
    passed = [e["name"] for e in journal.of_kind("step") if e.get("passed")]
    if not passed or passed[-1] not in _AFTER_STEP:
        return None
    last = passed[-1]
    conid_field, quantity_of = _AFTER_STEP[last]
    later = sent[sent.index(last) + 1:] if last in sent else []
    if any(step in _AFTER_STEP and _AFTER_STEP[step][0] == conid_field for step in later):
        return None
    return getattr(settings, conid_field), float(quantity_of(settings))
