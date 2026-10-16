"""Resolver-cache must honour exchange/sec_type/universe filters on a HIT.

Regression for the precedence bug where a cache hit skipped the exchange check
when a universe was given and skipped sec_type entirely.
"""
import types

from trader.data.universe import UniverseAccessor


def _accessor_with_cached_asx_stk():
    acc = object.__new__(UniverseAccessor)
    sd = types.SimpleNamespace(conId=1, exchange='ASX', primaryExchange='ASX',
                               secType='STK', symbol='BHP')
    u = types.SimpleNamespace(name='mine', security_definitions=[sd])
    acc._resolver_cache = {1: (u, sd)}
    # A miss falls through to iteration; keep it empty so we can tell a hit from
    # a fall-through (fall-through returns []).
    acc.get_all = lambda **_: []
    acc.get = lambda name: types.SimpleNamespace(name=name, security_definitions=[])
    return acc, u, sd


class TestResolverCacheHit:
    def test_matching_exchange_hits(self):
        acc, u, sd = _accessor_with_cached_asx_stk()
        assert acc.resolve_universe(1, exchange='ASX') == [(u, sd)]

    def test_wrong_exchange_does_not_hit(self):
        acc, u, sd = _accessor_with_cached_asx_stk()
        assert acc.resolve_universe(1, exchange='NYSE') == []

    def test_wrong_sectype_does_not_hit(self):
        acc, u, sd = _accessor_with_cached_asx_stk()
        assert acc.resolve_universe(1, sec_type='OPT') == []

    def test_matching_sectype_hits(self):
        acc, u, sd = _accessor_with_cached_asx_stk()
        assert acc.resolve_universe(1, sec_type='STK') == [(u, sd)]

    def test_universe_filter_respected(self):
        acc, u, sd = _accessor_with_cached_asx_stk()
        assert acc.resolve_universe(1, universe='mine') == [(u, sd)]
        assert acc.resolve_universe(1, universe='other') == []


def test_invalidate_clears_cache():
    acc, u, sd = _accessor_with_cached_asx_stk()
    acc.invalidate_resolver_cache()
    assert acc._resolver_cache == {}


class TestHiddenUniverses:
    """#202: the trader's `_instruments` cache must be found by a lookup, yet stay out of listings."""

    def _accessor(self, tmp_path):
        from tests.test_execution_costs import _definition
        acc = UniverseAccessor(str(tmp_path / "mmr.duckdb"), "Universes")
        acc.insert("downloads", _definition(265598, "AAPL", "NASDAQ"))
        acc.insert("_instruments", _definition(51529211, "GLD", "ARCA"))
        return acc

    def test_a_conid_stored_only_in_the_hidden_cache_resolves_locally(self, tmp_path):
        assert [d.conId for d in self._accessor(tmp_path).resolve_symbol(51529211)] == [51529211]

    def test_a_ticker_stored_only_in_the_hidden_cache_resolves_locally(self, tmp_path):
        assert [d.primaryExchange for d in self._accessor(tmp_path).resolve_symbol("GLD")] == ["ARCA"]

    def test_listings_still_hide_the_cache(self, tmp_path):
        acc = self._accessor(tmp_path)
        assert "_instruments" not in acc.list_universes()
        assert [u.name for u in acc.get_all()] == ["downloads"]
        assert "_instruments" in acc.list_universes(include_hidden=True)
