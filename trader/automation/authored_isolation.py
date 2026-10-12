"""Isolation proofs of the running sandbox image (SP3 spec 7.9; Plan 3 rulings 12, 21, 22).

``mmr ai-author prove-isolation`` (operator, host) records one row per run: the image digest, pass or
fail, and each check. Rows are insert-only. A rebuilt image has another digest and needs its own run.
Rows hold no secret and no strategy code.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.authored_wire import RecordIsolationProofRequest
from trader.automation.evaluation_claims import utc
from trader.research.canonical import canonical_json_bytes, sha256_digest

PROOF_DOMAIN = "mmr.authored.isolation-proof.v1"
_COLUMNS = "proof_id, image_digest, passed, checks_json, ran_at, recorded_at, principal"
# Among runs recorded at the same moment a failing one counts as the newest: a tie never proves an image.
_NEWEST_FIRST = "recorded_at DESC, passed ASC, proof_id DESC"


@dataclass(frozen=True)
class IsolationProof:
    proof_id: str
    image_digest: str
    passed: bool
    checks: tuple[dict, ...]
    ran_at: dt.datetime
    recorded_at: dt.datetime
    principal: str

    def to_json(self) -> dict:
        return {"proof_id": self.proof_id, "image_digest": self.image_digest, "passed": self.passed,
                "checks": [dict(check) for check in self.checks], "ran_at": self.ran_at.isoformat(),
                "recorded_at": self.recorded_at.isoformat()}


def _proof(row: tuple) -> IsolationProof:
    return IsolationProof(row[0], row[1], bool(row[2]), tuple(json.loads(row[3])), utc(row[4]), utc(row[5]), row[6])


class IsolationProofs:
    def __init__(self, db: Any, *, now: Callable[[], dt.datetime], journal: Any = None):
        """``journal`` is the trader's DomainJournal over the same database. With it a proof is written under the
        journal's write lock, as the registration seal is, so a failing run either commits before the seal (and
        the seal sees it) or after it; never unseen beside it. Without it only the database's own lock is taken."""
        self._db = db
        self._now = now
        self._journal = journal

    def record(self, request: RecordIsolationProofRequest, *, principal: str) -> dict:
        body = request.model_dump()
        proof_id = "sha256:" + sha256_digest(PROOF_DOMAIN, body)
        checks = tuple(body["checks"])

        def write(conn: Any) -> dict:
            row = conn.execute(f"SELECT {_COLUMNS} FROM authored_isolation_proofs WHERE proof_id = ?",
                               [proof_id]).fetchone()
            if row is not None:                    # the same run sent twice
                return {"status": "EXISTING", "proof": _proof(row).to_json()}
            proof = IsolationProof(proof_id, request.image_digest, request.passed, checks, request.ran_at_utc(),
                                   utc(self._now()), principal)
            conn.execute(f"INSERT INTO authored_isolation_proofs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [proof.proof_id, proof.image_digest, proof.passed,
                          canonical_json_bytes(list(checks)).decode("utf-8"), proof.ran_at, proof.recorded_at,
                          principal])
            return {"status": "RECORDED", "proof": proof.to_json()}
        if self._journal is None:
            return self._db.transaction(write)
        return self._journal.mutate_batch_work(self._journal.connect(), lambda conn, _append: write(conn))

    def latest_in_tx(self, conn: Any, image_digest: Optional[str]) -> Optional[IsolationProof]:
        """The newest proof of ``image_digest`` (by recording time), or the newest of all when None."""
        if image_digest is None:
            row = conn.execute(f"SELECT {_COLUMNS} FROM authored_isolation_proofs "
                               f"ORDER BY {_NEWEST_FIRST} LIMIT 1").fetchone()
        else:
            row = conn.execute(f"SELECT {_COLUMNS} FROM authored_isolation_proofs WHERE image_digest = ? "
                               f"ORDER BY {_NEWEST_FIRST} LIMIT 1", [image_digest]).fetchone()
        return None if row is None else _proof(row)

    def latest(self, image_digest: Optional[str]) -> Optional[IsolationProof]:
        return self._db.transaction(lambda conn: self.latest_in_tx(conn, image_digest))

    def passing_for_in_tx(self, conn: Any, image_digest: Optional[str]) -> bool:
        """True only when the newest run of exactly this image passed: a later failing run withdraws it. Read
        inside the registration's seal transaction too (review r1 #5)."""
        if image_digest is None:
            return False
        latest = self.latest_in_tx(conn, image_digest)
        return latest is not None and latest.passed

    def passing_for(self, image_digest: Optional[str]) -> bool:
        return self._db.transaction(lambda conn: self.passing_for_in_tx(conn, image_digest))
