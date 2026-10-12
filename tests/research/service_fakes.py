"""An in-memory trader for the research service tests: Plan 1's claim and judgment shapes, lost replies."""
from types import SimpleNamespace

from trader.research.trader_port import TraderUnavailable


class FakeTrader:
    def __init__(self, *, limit=10, cooling=()):
        self.claims, self.updates, self.limit, self.cooling = {}, [], limit, set(cooling)
        self.lose_next_reply = False
        self.lose_update = {}                 # state -> "request" (never arrives) or "reply" (applied, answer lost)
        self.judgments, self.shadow_rows = {}, {}
        self.shadow_calls, self.refuse_shadow = [], {}    # (judgment_id, session_date) -> (code, retryable)
        self.forward, self.forward_reads = {}, []
        self.authored_reports, self.judged_count = [], 0
        self.lose_authored_report, self.refuse_authored_report = False, None
        self.lose_authored_reply = False       # the report is applied, then its reply is lost (once)
        self.reported_after_close = False      # Plan 4 ruling 10: an end must reach the trader before FAILED

    def claim(self, request_id, body):
        existing = self.claims.get(request_id)
        if existing is not None:
            return {"status": "EXISTING", "claim": dict(existing), "code": None, "detail": None, "retryable": False}
        if body["strategy_key"] in self.cooling:
            return {"status": "REFUSED", "claim": None, "code": "FAMILY_COOLING_DOWN", "detail": "cooling",
                    "retryable": False}
        if len(self.claims) >= self.limit:
            return {"status": "REFUSED", "claim": None, "code": "EVALUATION_LIMIT_REACHED", "detail": "limit",
                    "retryable": False}
        self.claims[request_id] = {"request_id": request_id, "strategy_key": body["strategy_key"],
                                   "ny_day": body["research_day"], "state": "QUEUED", "body": dict(body),
                                   "claimed_at": "2024-03-29T14:00:00+00:00",
                                   "updated_at": "2024-03-29T14:00:00+00:00"}
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise TraderUnavailable("reply lost")
        return {"status": "ACCEPTED", "claim": dict(self.claims[request_id]), "code": None, "detail": None,
                "retryable": False}

    def claim_readback(self, request_id):
        claim = self.claims.get(request_id)
        return None if claim is None else dict(claim)

    def update_claim(self, request_id, state):
        self.updates.append((request_id, state))
        lost = self.lose_update.pop(state, None)
        if lost == "request":
            raise TraderUnavailable("update_claim: request lost")
        claim = self.claims[request_id]
        status = "UNCHANGED" if claim["state"] == state else "UPDATED"
        claim["state"] = state
        if lost == "reply":
            raise TraderUnavailable("update_claim: reply lost")
        return {"status": status, "code": None, "detail": None, "retryable": False}

    def judgment(self, *, judgment_id=None, case_digest=None):
        found = [j for j in self.judgments.values()
                 if j["judgment_id"] == judgment_id or j["case_digest"] == case_digest]
        return dict(found[0]) if found else None

    def forward_evidence(self, version_digest):
        """Plan 1's get_deployment_forward_evidence reply; Plan 5 owns the evidence shape."""
        self.forward_reads.append(version_digest)
        evidence = self.forward.get(version_digest)
        if evidence is None:
            return {"status": "REFUSED", "code": "DEPLOYMENT_VERSION_UNKNOWN", "detail": version_digest,
                    "evidence": None}
        return {"status": "FOUND", "code": None, "detail": None, "evidence": evidence}

    def record_shadow(self, body):
        key = (body["judgment_id"], body["session_date"])
        self.shadow_calls.append(key)
        if key in self.refuse_shadow:
            code, retryable = self.refuse_shadow[key]
            return {"status": "REFUSED", "code": code, "detail": "refused by the fake", "retryable": retryable}
        if key in self.shadow_rows and self.shadow_rows[key] != body:
            return {"status": "REFUSED", "code": "CONFLICTING_DUPLICATE", "retryable": False}
        status = "DUPLICATE" if key in self.shadow_rows else "INSERTED"
        self.shadow_rows[key] = dict(body)
        return {"status": status, "code": None, "retryable": False}

    def report_authored_evaluation(self, body):
        """Plan 3's report_authored_evaluation: UPDATED once per body, then UNCHANGED; judged_count rides along."""
        claim = self.claims.get(body["request_id"])
        if body["event"] != "STARTED" and claim is not None and claim["state"] == "FAILED":
            self.reported_after_close = True
        if self.lose_authored_report:
            self.lose_authored_report = False
            raise TraderUnavailable("report_authored_evaluation: request lost")
        if self.refuse_authored_report is not None:
            code, retryable = self.refuse_authored_report
            return {"status": "REFUSED", "code": code, "detail": "refused by the fake", "retryable": retryable,
                    "record": None, "judged_count": None}
        status = "UNCHANGED" if body in self.authored_reports else "UPDATED"
        if status == "UPDATED":
            self.authored_reports.append(dict(body))
        if self.lose_authored_reply:
            self.lose_authored_reply = False
            raise TraderUnavailable("report_authored_evaluation: reply lost")
        return {"status": status, "code": None, "detail": None, "retryable": False, "record": None,
                "judged_count": self.judged_count}


def judgment_view(judgment_id, case_digest, verdict, *, decided_at="2024-03-15T21:00:00+00:00",
                  narrative=None, binding=None, kind="INITIAL", jev_model="openrouter/jev-1"):
    """Plan 1's get_backtest_judgment view."""
    return {"judgment_id": judgment_id, "case_digest": case_digest, "request_id": None, "kind": kind,
            "verdict": verdict, "strategy_key": "strategies/time_of_day.py:TimeOfDay",
            "body": {"judgment_id": judgment_id, "case_digest": case_digest, "kind": kind, "verdict": verdict,
                     "jev_model": jev_model, "decided_at": decided_at, "narrative": narrative},
            "binding": binding or {}, "cooldown_until_session": None, "recorded_at": decided_at}


AI = SimpleNamespace(principal="ai_research")
CLI = SimpleNamespace(principal="cli")
