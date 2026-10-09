"""What Plan 2 reads from Plan 1: durable judgments (joined with their verified case) and cooldowns.

Every Plan 1 name this plan relies on is used only here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from trader.automation.backtest_judge_schema import cooling_until_in_tx
from trader.automation.evaluation_claims import ny_day
from trader.research.strategy_key import split_strategy_key


@dataclass(frozen=True)
class JudgmentFacts:
    judgment_id: str
    verdict: str                   # DEPLOY | SHADOW | REJECT | NO_VERDICT
    kind: str                      # INITIAL | RENEWAL
    renews_version: Optional[str]
    model_id: str
    strategy_path: str
    class_name: str
    file_hash: str                 # sha256:<hex> of the evaluated bytes
    params: Mapping[str, Any]
    conids: tuple[int, ...]
    bar_size: str
    artifact_id: Optional[str]     # None for a RENEWAL case
    family_id: Optional[str]


class JudgmentReader(Protocol):
    def get(self, judgment_id: str) -> Optional[JudgmentFacts]: ...
    def renewal_verdicts(self, version_digest: str) -> tuple[str, ...]: ...


class CooldownReader(Protocol):
    def cooling_down(self, strategy_key: str, now: dt.datetime) -> bool: ...
    def cooling_down_in_tx(self, conn: Any, strategy_key: str, now: dt.datetime) -> bool: ...


def strategy_key(strategy_path: str, class_name: str) -> str:
    return f"{strategy_path}:{class_name}"


def _no_renewals(version_digest: str) -> Iterable[Any]:
    """Plan 1 refuses every RENEWAL judgment until SP2c Plan 5, so none can name a version."""
    return ()


class Plan1Judgments:
    def __init__(self, store: Any, *, read_case: Callable[[str], Any],
                 renewals_of: Callable[[str], Iterable[Any]] = _no_renewals):
        self._store = store
        self._read_case = read_case
        self._renewals_of = renewals_of

    def get(self, judgment_id: str) -> Optional[JudgmentFacts]:
        judgment = self._store.get(judgment_id)        # BacktestJudgment; a tampered row raises JudgmentRefused
        if judgment is None:
            return None
        case = self._read_case(judgment.case_digest)   # EvaluationCase; signature checked by Plan 1's verifier
        strategy_path, class_name = split_strategy_key(case.strategy_key)
        return JudgmentFacts(
            judgment_id=judgment.judgment_id, verdict=judgment.verdict, kind=judgment.kind,
            renews_version=judgment.body["renewal_of_version"], model_id=judgment.body["jev_model"],
            strategy_path=strategy_path, class_name=class_name, file_hash=case.strategy_file_hash,
            params=dict(case.selected_params or {}), conids=tuple(sorted(int(c) for c in case.conids)),
            bar_size=case.bar_size, artifact_id=case.artifact_id, family_id=case.family_id)

    def renewal_verdicts(self, version_digest: str) -> tuple[str, ...]:
        return tuple(judgment.verdict for judgment in self._renewals_of(version_digest))


class Plan1Cooldowns:
    def __init__(self, db: Any):
        self._db = db

    def cooling_down(self, strategy_key: str, now: dt.datetime) -> bool:
        return self._db.transaction(lambda conn: self.cooling_down_in_tx(conn, strategy_key, now))

    def cooling_down_in_tx(self, conn: Any, strategy_key: str, now: dt.datetime) -> bool:
        """On the caller's journal transaction: a REJECT committed before it is seen."""
        return cooling_until_in_tx(conn, strategy_key, ny_day(now)) is not None


def judgment_reader_for(judgments: Any, *, cases_dir: Path, verify_dir: Path) -> JudgmentReader:
    from trader.research.evaluation_case import load_case_verify_keys, load_verified_case

    def read_case(digest: str) -> Any:
        # Keys are loaded per read: nothing touches the disk at trader start, and a rotated key is seen.
        return load_verified_case(cases_dir, digest, load_case_verify_keys(verify_dir))
    return Plan1Judgments(judgments, read_case=read_case)


def cooldown_reader_for(db: Any) -> CooldownReader:
    return Plan1Cooldowns(db)
