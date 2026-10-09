"""Synthetic market for research-evaluation tests: 8 NASDAQ conids, 15-minute
bars on real XNYS sessions, a price that drifts by ``drift`` every bar, and a
strategy that buys at 10:00 ET and sells at 11:00 ET."""
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import yaml

from tests.test_execution_costs import CONFIG, _definition
from trader.data.duckdb_store import DuckDBConnection, DuckDBDataStore
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.research.attest_export import attest_and_export
from trader.research.eligibility import Ruleset
from trader.research.evaluation import EvaluationPaths, EvaluationResult, evaluate
from trader.research.evaluation_spec import load_evaluation_spec
from trader.research.review import OperatorReview, OperatorReviewRepository
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.research.signing import AttestationSigner
from trader.simulation.execution_costs import load_execution_costs_config

CONIDS = list(range(1001, 1009))
SPY_CONID = 756733
PERIOD = ('2024-02-01', '2024-03-28')
HOLDOUT_RULES = {'expectancy_baseline_positive', 'holdout_drawdown_within_canary',
                 'deterministic_replay', 'holdout_opened_once'}
FIXED_NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
TIME_OF_DAY_STRATEGY = '''
import pandas as pd

from trader.objects import Action
from trader.trading.strategy import Signal, Strategy


class TimeOfDay(Strategy):
    ENTRY_MINUTE = 600
    EXIT_MINUTE = 660

    def on_prices(self, prices):
        ts = pd.Timestamp(prices.index[-1])
        ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts
        local = ts.tz_convert('America/New_York')
        minute = local.hour * 60 + local.minute
        if minute == self.ENTRY_MINUTE:
            return Signal(source_name='time_of_day', action=Action.BUY, probability=0.5, risk=0.5)
        if minute == self.EXIT_MINUTE:
            return Signal(source_name='time_of_day', action=Action.SELL, probability=0.5, risk=0.5)
        return None
'''


def write_costs_config(path: Path) -> Path:
    venues = {'us': {**CONFIG['venues']['us'], 'calendar': 'XNYS'},
              'asx': {**CONFIG['venues']['asx'], 'calendar': 'XASX'}}
    path.write_text(yaml.safe_dump({**CONFIG, 'venues': venues}))
    return path


def write_universe(duckdb_path: str) -> UniverseAccessor:
    universes = UniverseAccessor(duckdb_path, 'Universes')
    for conid in CONIDS:
        universes.insert('evaluation', _definition(conid, f'S{conid}', 'NASDAQ'))
    return universes


def write_trend_bars(duckdb_path: str, *, drift: float, start=PERIOD[0], end=PERIOD[1],
                     conids=CONIDS, holdout_drift: Optional[float] = None,
                     holdout_sessions: int = 5) -> None:
    """``holdout_drift`` replaces ``drift`` over the last ``holdout_sessions``
    sessions, so a strategy can pass walk-forward and then fail its holdout."""
    calendar = xcals.get_calendar('XNYS')
    sessions = calendar.sessions_in_range(start, end)
    stamps = []
    for session in sessions:
        session_open = calendar.session_open(session)
        session_close = calendar.session_close(session)
        stamps.extend(pd.date_range(session_open, session_close - pd.Timedelta(minutes=15), freq='15min'))
    index = pd.DatetimeIndex(stamps).tz_convert('UTC')
    if holdout_drift is None:
        growth = (1 + drift) ** pd.RangeIndex(len(index)).to_numpy()
    else:
        holdout_start = sessions[-holdout_sessions].date()
        in_holdout = np.array([day >= holdout_start
                               for day in index.tz_convert('America/New_York').date])
        steps = np.where(in_holdout[1:], holdout_drift, drift)
        growth = np.concatenate([[1.0], np.cumprod(1 + steps)])
    store = DuckDBDataStore(duckdb_path)
    for offset, conid in enumerate(conids):
        price = (100.0 + 10 * offset) * growth
        frame = pd.DataFrame({'open': price, 'high': price * 1.0005, 'low': price * 0.9995,
                              'close': price, 'volume': 50_000.0, 'bar_size': '15 mins'},
                             index=index)
        frame.index.name = 'date'
        store.write(str(conid), frame)
    write_benchmark_bars(duckdb_path, end=end)


def write_benchmark_bars(duckdb_path: str, *, start='2023-01-03', end=PERIOD[1],
                         drift: float = 0.0002) -> None:
    """SPY daily closes over real XNYS sessions, enough lookback for regimes.

    Daily returns alternate drift +/- 0.5%: volatile enough for a real
    volatility-matched benchmark, still one low-volatility bull regime."""
    calendar = xcals.get_calendar('XNYS')
    sessions = calendar.sessions_in_range(start, end)
    index = pd.DatetimeIndex([pd.Timestamp(s.date(), tz='UTC') for s in sessions])
    swing = np.where(np.arange(1, len(index)) % 2 == 1, 0.005, -0.005)
    price = 400.0 * np.concatenate([[1.0], np.cumprod(1 + drift + swing)])
    frame = pd.DataFrame({'open': price, 'high': price, 'low': price, 'close': price,
                          'volume': 1_000_000.0, 'bar_size': '1 day'}, index=index)
    frame.index.name = 'date'
    DuckDBDataStore(duckdb_path).write(str(SPY_CONID), frame)


def build_spec_file(repo: Path, **overrides) -> Path:
    (repo / 'strategies').mkdir(exist_ok=True)
    strategy = repo / 'strategies' / 'time_of_day.py'
    if not strategy.exists():
        strategy.write_text(TIME_OF_DAY_STRATEGY)
    spec = {
        'name': 'time_of_day_us',
        'strategy': 'strategies/time_of_day.py',
        'class': 'TimeOfDay',
        'params': {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660},
        'neighbourhood': {'ENTRY_MINUTE': [615, 630], 'EXIT_MINUTE': [645, 675]},
        'conids': CONIDS,
        'bar_size': '15 mins',
        'period': {'start': PERIOD[0], 'end': PERIOD[1]},
        'walk_forward': {'folds': 2, 'embargo_sessions': 1, 'holdout_sessions': 5},
        'sizing': {'order_notional': 1900, 'account_equity': 100000},
        'max_gross_allocation': 0.05,
    }
    spec.update(overrides)
    path = repo / f"{spec['name']}.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def holdout_ruleset() -> Ruleset:
    """A paper-v1 subset small enough for synthetic data to pass, so tests can
    exercise the holdout and export paths through public APIs."""
    return Ruleset(name='paper-v1-subset', version='test', source_digest='test',
                   rules=tuple(r for r in PAPER_V1.rules if r.code in HOLDOUT_RULES))


def judge_qualified_evidence_by_holdout_ruleset(monkeypatch) -> None:
    """Synthetic bundles pass only the holdout subset ruleset. Let the
    qualified-evidence gate (`require_qualified_research_evidence`) demand that
    subset instead of the full paper-v1 rules; every other check still runs."""
    import trader.automation.paper_materials as paper_materials

    monkeypatch.setattr(paper_materials, 'PAPER_V1', holdout_ruleset())


def submit_review(research_db, artifact_id: str, decision_digest: str, kind: str = 'llm') -> str:
    return OperatorReviewRepository(research_db).record(OperatorReview(
        artifact_id=artifact_id, eligibility_decision_digest=decision_digest,
        reviewer='claude-test', reviewed_at=FIXED_NOW, economic_rationale='test drift',
        edge_survives_costs='modeled', known_failure_regimes='flat days',
        data_and_survivorship_limits='synthetic', parameter_sensitivity='neighbours',
        operational_dependencies='none', capacity_and_decay='small',
        episode_dominance='none', holdout_opened_once_confirmed=True, reviewer_kind=kind))


@dataclass(frozen=True)
class EligibleBundleFixture:
    bundle_path: Path
    signer: AttestationSigner
    artifact_id: str
    spec: object
    research_db: object


@dataclass(frozen=True)
class SyntheticEvaluation:
    result: EvaluationResult
    research_db: object
    spec: object
    paths: EvaluationPaths


def evaluate_synthetic(repo: Path, duckdb_path: str, *, drift: float = 0.0006,
                       holdout_drift: Optional[float] = None) -> SyntheticEvaluation:
    """Evaluate the synthetic strategy under the holdout subset ruleset."""
    write_universe(duckdb_path)
    write_trend_bars(duckdb_path, drift=drift, holdout_drift=holdout_drift)
    costs = write_costs_config(repo / 'execution_costs.yaml')
    research_db = DuckDBConnection.get_instance(str(repo / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(research_db))
    spec = load_evaluation_spec(build_spec_file(repo),
                                universe_accessor=UniverseAccessor(duckdb_path, 'Universes'),
                                costs_config=load_execution_costs_config(str(costs)), repo_root=repo)
    paths = EvaluationPaths(duckdb_path, duckdb_path, 'Universes', str(costs),
                            repo, repo / 'reports', repo / 'artifacts' / 'evaluations')
    ticks = iter(range(10_000))
    result = evaluate(spec, research_db=research_db, paths=paths,
                      now=lambda: FIXED_NOW + dt.timedelta(seconds=next(ticks)),
                      ruleset=holdout_ruleset())
    return SyntheticEvaluation(result, research_db, spec, paths)


def export_eligible_bundle(repo: Path, duckdb_path: str) -> EligibleBundleFixture:
    """Evaluate the synthetic strategy under the holdout subset ruleset, review it
    as an LLM, and export the signed bundle to ``repo/artifacts``."""
    evaluation = evaluate_synthetic(repo, duckdb_path)
    result, research_db, spec = evaluation.result, evaluation.research_db, evaluation.spec
    submit_review(research_db, result.artifact_id, result.decision_digest)
    signer = AttestationSigner.generate()
    bundle_path = attest_and_export(research_db, artifact_id=result.artifact_id, signer=signer,
                                    artifacts_root=repo / 'artifacts', now=FIXED_NOW,
                                    ruleset=holdout_ruleset())
    return EligibleBundleFixture(bundle_path, signer, result.artifact_id, spec, research_db)


def write_alpaca_extended_hours_bar(duckdb_path: str, day: str, *, conids=CONIDS, ny_time=dt.time(8, 0)) -> None:
    """One SIP extended-hours 15-minute bar per conid, stored the way the Alpaca refresh stores it."""
    from trader.data.data_access import TickStorage
    from trader.data_providers.alpaca.history import _to_frame
    from trader.objects import BarSize

    stamp = pd.Timestamp(dt.datetime.combine(dt.date.fromisoformat(day), ny_time), tz='America/New_York')
    bar_size = BarSize.parse_str('15 mins')
    tickdata = TickStorage(duckdb_path).get_tickdata(bar_size)
    for conid in conids:
        frame = _to_frame([{'t': stamp.tz_convert('UTC').isoformat(), 'o': 100.0, 'h': 100.0, 'l': 100.0,
                            'c': 100.0, 'v': 10, 'vw': 100.0, 'n': 1}], bar_size, 'US/Eastern')
        tickdata.write_resolve_overlap(conid, frame)
