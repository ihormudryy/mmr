import datetime as dt
import json
import statistics

import pytest

from tests.scoreboard.test_report import full_inputs
from tests.automation.authored_fixtures import authored_world, stored
from trader.automation.ai_deployment_versions import (INITIAL, AiDeploymentVersionStore, DeploymentVersion,
                                                      apply_ai_deployment_version_migrations)
from trader.automation.ai_deployments import (AiDeployment, AiDeploymentStore, apply_ai_deployment_migration,
                                              deployment_digest)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.strategy_key import authored_strategy_path
from trader.scoreboard.authored_vs_catalogue import (NOTE, AuthoredScope, AuthoredScopeUnreadable, DeploymentWindow,
                                                     common_scope, compare_authored_vs_catalogue, pnl_drawdown_usd,
                                                     read_authored_scope, scope_or_warning)
from trader.scoreboard.report import build_report

DAYS = [dt.date(2026, 10, day) for day in (1, 2, 5, 6, 7, 8, 9, 13, 14, 15)]
AUTHORED, CATALOGUE = "sha256:" + "a" * 64, "sha256:" + "c" * 64
NOW = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)


def window(digest, conids, first=DAYS[0], last=DAYS[-1]):
    return DeploymentWindow("sha256:" + "9" * 63 + digest[-1], digest, frozenset(conids), first, last)


def scope(*windows, authored=(AUTHORED,)):
    return AuthoredScope(tuple(windows), frozenset(authored))


BOTH = scope(window(AUTHORED, {1, 2}), window(CATALOGUE, {2, 3}))


def trip(digest, conid, day, net, status="CLOSED"):
    return {"status": status, "conid": conid, "opened_session": day, "net_pnl_usd": net, "strategy_version": digest}


def side(digest, daily, conid=2):
    """Two closed trips a session, each half of that session's P&L: 20 trips over the ten sessions."""
    return [trip(digest, conid, day, pnl / 2) for day, pnl in zip(DAYS, daily) for _ in range(2)]


def compare(trips, sessions=DAYS, scope_=BOTH):
    return compare_authored_vs_catalogue(trips=trips, sessions=sessions, scope=scope_)


def test_yes_needs_more_sharpe_and_no_more_drawdown():                              # review focus 5
    steady = [22, 20, 21, 23, 20, 22, 21, 20, 23, 22]
    noisy = [30, -20, 25, -10, 40, -30, 20, 10, -5, 15]
    view = compare(side(AUTHORED, steady) + side(CATALOGUE, noisy))
    assert (view["verdict"], view["reason"], view["sessions"], view["conids"]) == ("yes", None, 10, [2])
    assert view["authored"]["closed_trips"] == view["catalogue"]["closed_trips"] == 20
    assert view["authored"]["max_drawdown_usd"] == 0.0 and view["catalogue"]["max_drawdown_usd"] > 0
    assert view["authored"]["sharpe_daily"] > view["catalogue"]["sharpe_daily"]
    assert (view["label"], view["note"]) == ("PAPER", NOTE) and view["authored"]["strategies"] == [AUTHORED]


def test_a_better_sharpe_with_a_deeper_drawdown_is_no():
    view = compare(side(AUTHORED, [10] * 9 + [-5]) + side(CATALOGUE, [1, -1] * 5))
    assert view["authored"]["sharpe_daily"] > view["catalogue"]["sharpe_daily"]
    assert (view["authored"]["max_drawdown_usd"], view["catalogue"]["max_drawdown_usd"]) == (5.0, 1.0)
    assert (view["verdict"], view["reason"]) == ("no", None)


def test_a_lower_sharpe_is_no():
    view = compare(side(AUTHORED, [1, -1] * 5) + side(CATALOGUE, [22, 20, 21, 23, 20, 22, 21, 20, 23, 22]))
    assert view["verdict"] == "no"


def test_nineteen_trips_on_a_side_is_not_enough_data():
    view = compare(side(AUTHORED, [5, 6] * 5)[:19] + side(CATALOGUE, [1, -1] * 5))
    assert (view["verdict"], view["reason"], view["authored"]["closed_trips"]) == (
        "not enough data", "TOO_FEW_TRIPS", 19)


def test_trips_outside_the_common_scope_do_not_count():                           # review focus 5
    inside = side(AUTHORED, [5, 6] * 5) + side(CATALOGUE, [1, -1] * 5)
    outside = [trip(AUTHORED, 1, DAYS[0], 999.0),                     # a conid only the authored side binds
               trip(CATALOGUE, 3, DAYS[0], -999.0),                   # a conid only the catalogue binds
               trip("discretionary", 2, DAYS[0], 50.0), trip(None, 2, DAYS[0], 50.0), trip("-", 2, DAYS[0], 5.0),
               trip(AUTHORED, 2, dt.date(2026, 11, 2), 999.0),         # a session outside both windows
               trip(AUTHORED, 2, DAYS[1], 999.0, status="OPEN")]
    assert compare(inside + outside) == compare(inside)


def test_sessions_need_both_sides_active():
    late_catalogue = scope(window(AUTHORED, {2}), window(CATALOGUE, {2}, first=DAYS[5]))
    view = compare(side(AUTHORED, [5, 6] * 5) + side(CATALOGUE, [1, -1] * 5), scope_=late_catalogue)
    assert view["sessions"] == 5 and view["authored"]["closed_trips"] == 10


def test_a_session_without_trips_counts_as_zero():
    view = compare([trip(AUTHORED, 2, DAYS[0], 10.0)] + side(CATALOGUE, [1, -1] * 5))
    assert (view["authored"]["pnl_usd"], view["authored"]["max_drawdown_usd"]) == (10.0, 0.0)
    assert view["authored"]["sharpe_daily"] == pytest.approx(1.0 / statistics.stdev([10.0] + [0.0] * 9))


def test_pending_fees_are_not_enough_data():                                      # review focus 5
    trips = side(AUTHORED, [5, 6] * 5) + side(CATALOGUE, [1, -1] * 5)
    trips[0] = {**trips[0], "net_pnl_usd": None}
    view = compare(trips)
    assert (view["verdict"], view["reason"]) == ("not enough data", "FEES_PENDING")
    assert (view["authored"]["pnl_usd"], view["authored"]["unresolved_fee_trips"]) == (None, 1)


def test_an_unknown_sharpe_is_not_enough_data():
    view = compare(side(AUTHORED, [5] * 10) + side(CATALOGUE, [1, -1] * 5))      # flat P&L: zero deviation
    assert (view["verdict"], view["reason"], view["authored"]["sharpe_daily"]) == (
        "not enough data", "SHARPE_UNKNOWN", None)


def test_no_scope_or_no_common_session_is_not_enough_data():
    assert compare([], scope_=None) == {
        "label": "PAPER", "note": NOTE, "verdict": "not enough data", "reason": "SCOPE_UNKNOWN",
        "min_closed_trips": 20, "sessions": 0, "conids": [], "authored": None, "catalogue": None}
    assert compare([], scope_=scope(window(AUTHORED, {2})))["reason"] == "NO_COMMON_SESSIONS"


def test_pnl_drawdown_usd():
    assert pnl_drawdown_usd([]) is None
    assert pnl_drawdown_usd([10, -5, -10, 20]) == 15.0
    assert pnl_drawdown_usd([-3, -2]) == 5.0                                      # the curve starts at 0


def test_the_report_carries_the_view():
    with_scope = build_report(full_inputs(authored_scope=BOTH))["authored_vs_catalogue"]
    assert (with_scope["label"], with_scope["reason"], with_scope["sessions"]) == ("PAPER", "TOO_FEW_TRIPS", 2)
    assert build_report(full_inputs())["authored_vs_catalogue"]["reason"] == "SCOPE_UNKNOWN"


def test_a_failing_reader_is_unknown_with_a_warning():
    def broken():
        raise RuntimeError("authored_strategies does not exist")
    assert scope_or_warning(None) == (None, [])
    scope_, warnings = scope_or_warning(broken)
    assert scope_ is None and warnings[0]["code"] == "AUTHORED_SCOPE_UNKNOWN"
    assert "authored_strategies does not exist" in warnings[0]["detail"]


def test_read_authored_scope_reads_judged_versions_and_authored_digests(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    apply_ai_deployment_migration(migrator)
    apply_ai_deployment_version_migrations(migrator)
    # Plan 3 owns this table (migration 130); the reader uses its key column only (spec 7.3).
    db.execute("CREATE TABLE IF NOT EXISTS authored_strategies (digest VARCHAR PRIMARY KEY)")
    db.execute("INSERT INTO authored_strategies (digest) VALUES (?)", [AUTHORED])
    versions = {}
    for n, (digest, conids) in enumerate(((AUTHORED, [2, 1]), (CATALOGUE, [3, 2])), start=1):
        base, version = "sha256:" + str(n) * 64, "sha256:" + str(n + 4) * 64
        versions[digest] = version
        db.execute("INSERT INTO ai_deployments VALUES (?, ?, 'ai_research', 'c', ?, 'BUNDLE', 'strategy')",
                   [base, json.dumps({"strategy_digest": digest, "conids": conids}), NOW])
        db.execute("INSERT INTO ai_deployment_versions (digest, judgment_id, base_digest, prior_version, record_json, "
                   "request_digest, principal, command_id, sealed_at) VALUES (?, ?, ?, NULL, ?, 'r', 'ai_research', "
                   "'c', ?)", [version, f"jdg-{n}", base,
                               json.dumps({"first_session": "2026-10-01", "expiry_session": "2026-10-30"}), NOW])
    db.execute("INSERT INTO ai_deployment_withdrawals VALUES (?, 'operator', 'cli', 'c', ?)",
               [versions[CATALOGUE], dt.datetime(2026, 10, 9, 15, tzinfo=dt.timezone.utc)])
    read = read_authored_scope(db)
    by_strategy = {w.strategy_digest: w for w in read.windows}
    assert read.authored_digests == frozenset({AUTHORED})
    assert (by_strategy[AUTHORED].conids, by_strategy[AUTHORED].last_session) == (frozenset({1, 2}),
                                                                                  dt.date(2026, 10, 30))
    assert by_strategy[CATALOGUE].last_session == dt.date(2026, 10, 9)
    assert read.side_of(CATALOGUE) == "catalogue" and read.side_of("discretionary") is None


# -- the reader against rows written by the real stores (no hand-written SQL) ---------------------------------------

FIRST, EXPIRY = dt.date(2026, 10, 1), dt.date(2026, 10, 30)


def _deployment(path, digest, conids, class_name="RangeBreak"):
    return AiDeployment(strategy_path=path, strategy_digest=digest, class_name=class_name,
                        params={"RANGE_BARS": 15}, conids=conids, bar_size="5 mins", style="intraday_long",
                        decider="jev", decider_verdict="DEPLOY", evidence_ref="trial:1",
                        evidence_order_notional=2000.0)


def _real_journal(tmp_path):
    """Plan 3's store stores one authored file; SP2c's tables come from their own migrations."""
    world = authored_world(tmp_path)
    authored_digest = stored(world)
    migrator = SchemaMigrator(world.db)
    apply_ai_deployment_migration(migrator)
    apply_ai_deployment_version_migrations(migrator)
    return world.db, authored_digest


def _deploy(db, deployment, judgment_id):
    """Seal the base deployment (when given) and one INITIAL version, through the stores' own methods."""
    base = deployment_digest(deployment)
    if judgment_id != "jdg-no-base":
        AiDeploymentStore(db, lambda: NOW).register(deployment, principal="ai_research", command_id=judgment_id)
    versions = AiDeploymentVersionStore(DomainJournal(db), lambda: NOW)
    version = DeploymentVersion(base_digest=base, judgment_id=judgment_id, kind=INITIAL, prior_version=None,
                                first_session=FIRST, expiry_session=EXPIRY)
    return db.transaction(lambda conn: versions.seal_in_tx(conn, version, request_digest="sha256:" + "1" * 64,
                                                           principal="ai_research", command_id=judgment_id))[0]


def test_read_authored_scope_classifies_rows_of_the_real_stores(tmp_path):
    db, authored_digest = _real_journal(tmp_path)
    _deploy(db, _deployment(authored_strategy_path(authored_digest), authored_digest, [3, 1]), "jdg-a")
    _deploy(db, _deployment("strategies/opening_range_breakout.py", CATALOGUE, [5, 3], "OpeningRangeBreakout"),
            "jdg-c")
    read = read_authored_scope(db)
    by_strategy = {w.strategy_digest: w for w in read.windows}
    assert read.authored_digests == frozenset({authored_digest})
    assert (read.side_of(authored_digest), read.side_of(CATALOGUE), read.side_of("discretionary")) == (
        "authored", "catalogue", None)
    assert by_strategy[authored_digest].conids == frozenset({1, 3})
    assert (by_strategy[CATALOGUE].first_session, by_strategy[CATALOGUE].last_session) == (FIRST, EXPIRY)
    assert common_scope(read, [dt.date(2026, 10, 6)]) == {dt.date(2026, 10, 6): frozenset({3})}


@pytest.mark.parametrize("case", ["authored path, no authored record", "authored path, other digest",
                                  "catalogue path, authored digest", "version without its base deployment"])
def test_a_version_that_cannot_be_classified_makes_the_scope_unknown(tmp_path, case):
    db, authored_digest = _real_journal(tmp_path)
    unknown = "sha256:" + "e" * 64
    deployment = {
        "authored path, no authored record": _deployment(authored_strategy_path(unknown), unknown, [1]),
        "authored path, other digest": _deployment(authored_strategy_path(authored_digest), unknown, [1]),
        "catalogue path, authored digest": _deployment("strategies/opening_range_breakout.py", authored_digest,
                                                       [1], "OpeningRangeBreakout"),
        "version without its base deployment": _deployment(authored_strategy_path(authored_digest),
                                                           authored_digest, [1]),
    }[case]
    _deploy(db, deployment, "jdg-no-base" if case == "version without its base deployment" else "jdg-x")
    with pytest.raises(AuthoredScopeUnreadable):
        read_authored_scope(db)
    scope_, warnings = scope_or_warning(lambda: read_authored_scope(db))
    assert scope_ is None and [w["code"] for w in warnings] == ["AUTHORED_SCOPE_UNKNOWN"]
    view = compare_authored_vs_catalogue(trips=[trip(authored_digest, 1, DAYS[0], 5.0)], sessions=DAYS,
                                         scope=scope_)
    assert (view["reason"], view["authored"], view["catalogue"]) == ("SCOPE_UNKNOWN", None, None)
