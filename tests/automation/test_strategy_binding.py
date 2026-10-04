import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tests.automation.fixture_bundle import export_fixture_paper_eligible_bundle
from tests.research.evaluation_fixtures import FIXED_NOW, export_eligible_bundle
from tests.test_strategy_paper_arm import _make_runtime
from trader.automation.artifact_verifier import ArtifactVerifier
from trader.automation.strategy_binding import (
    AttestedStrategy, StrategyBindingError, check_strategy_binding)
from trader.data.backtest_store import compute_strategy_hash
from trader.research.signing import AttestationSigner
from trader.strategy.intent_emitter import IntentEmitter
from trader.strategy.strategy_runtime import PaperAutomationArmError


@pytest.fixture(scope='module')
def bound(tmp_path_factory):
    repo = tmp_path_factory.mktemp('binding')
    exported = export_eligible_bundle(repo, str(repo / 'market.duckdb'))
    verified = ArtifactVerifier([exported.signer.public_key]).verify(
        exported.bundle_path, 'paper', exported.artifact_id, FIXED_NOW)
    return repo, exported, verified


def _entry(bound, **changes):
    repo, exported, _ = bound
    entry = dict(module_file=repo / 'strategies' / 'time_of_day.py', class_name='TimeOfDay',
                 params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660,
                         'artifact_bundle_path': str(exported.bundle_path)},
                 conids=list(exported.spec.conids), bar_size='15 mins')
    entry.update(changes)
    return entry


@pytest.mark.timeout(240)
def test_matching_strategy_passes(bound):
    _, _, verified = bound
    assert verified.attested_strategy.order_notional == 1900.0
    check_strategy_binding(verified.attested_strategy, **_entry(bound))


@pytest.mark.timeout(240)
@pytest.mark.parametrize('change, message', [
    (lambda e: {'params': {**e['params'], 'ENTRY_MINUTE': 615}}, 'params'),
    (lambda e: {'conids': e['conids'][:-1]}, 'conids'),
    (lambda e: {'bar_size': '1 min'}, 'bar size'),
    (lambda e: {'class_name': 'Other'}, 'class'),
])
def test_any_difference_is_refused(bound, change, message):
    _, _, verified = bound
    entry = _entry(bound)
    with pytest.raises(StrategyBindingError, match=message):
        check_strategy_binding(verified.attested_strategy, **{**entry, **change(entry)})


@pytest.mark.timeout(240)
def test_edited_strategy_file_is_refused(bound, tmp_path):
    _, _, verified = bound
    original = _entry(bound)['module_file']
    edited = tmp_path / 'strategies' / 'time_of_day.py'
    edited.parent.mkdir()
    edited.write_text(original.read_text() + '\n# edited\n')
    with pytest.raises(StrategyBindingError, match='changed since attestation'):
        check_strategy_binding(verified.attested_strategy, **_entry(bound, module_file=edited))


@pytest.mark.timeout(240)
def test_old_fixture_bundle_is_refused(bound, tmp_path):
    signer = AttestationSigner.generate()
    artifact_id = export_fixture_paper_eligible_bundle(signer=signer, artifacts_root=tmp_path / 'fx')
    fixture = ArtifactVerifier([signer.public_key]).verify(
        tmp_path / 'fx' / artifact_id, 'paper', artifact_id, FIXED_NOW)
    with pytest.raises(StrategyBindingError) as refusal:
        check_strategy_binding(fixture.attested_strategy, **_entry(bound))
    message = str(refusal.value)
    assert 'is not the attested strategies/orb.py' in message
    assert "class 'TimeOfDay' is not the attested 'OpeningRangeBreakout'" in message
    assert 'changed since attestation' in message
    assert 'params' in message
    assert 'conids' in message
    assert 'bar size' in message


@pytest.mark.timeout(240)
def test_missing_attested_strategy_is_refused(bound):
    with pytest.raises(StrategyBindingError, match='no attested strategy'):
        check_strategy_binding(None, **_entry(bound))


STRATEGY_SOURCE = 'class TimeOfDay:\n    ENTRY_MINUTE = 600\n'


def _synthetic_attested(strategy_file) -> AttestedStrategy:
    return AttestedStrategy(
        strategy_path='strategies/time_of_day.py', class_name='TimeOfDay',
        source_digest=compute_strategy_hash(str(strategy_file)),
        parameters={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660},
        instruments=frozenset({'1001', '1002'}), bar_size='15 mins', order_notional=1900.0)


def _synthetic_entry(module_file, **changes):
    entry = dict(module_file=module_file, class_name='TimeOfDay',
                 params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660},
                 conids=[1001, 1002], bar_size='15 mins')
    entry.update(changes)
    return entry


@pytest.fixture
def strategy_file(tmp_path):
    path = tmp_path / 'strategies' / 'time_of_day.py'
    path.parent.mkdir()
    path.write_text(STRATEGY_SOURCE)
    return path


def test_synthetic_attestation_matches_its_own_file(strategy_file):
    check_strategy_binding(_synthetic_attested(strategy_file), **_synthetic_entry(strategy_file))


def test_a_different_strategy_path_is_refused_even_with_the_same_source(strategy_file):
    other = strategy_file.with_name('other.py')
    other.write_text(STRATEGY_SOURCE)
    with pytest.raises(StrategyBindingError) as refusal:
        check_strategy_binding(_synthetic_attested(strategy_file), **_synthetic_entry(other))
    assert 'is not the attested strategies/time_of_day.py' in str(refusal.value)
    assert 'changed since attestation' not in str(refusal.value)


def test_an_unreadable_strategy_file_is_refused_as_unreadable(strategy_file):
    attested = _synthetic_attested(strategy_file)
    strategy_file.unlink()
    with pytest.raises(StrategyBindingError, match='cannot be read') as refusal:
        check_strategy_binding(attested, **_synthetic_entry(strategy_file))
    assert 'changed since attestation' not in str(refusal.value)


def test_an_extra_tunable_param_is_refused(strategy_file):
    params = {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660, 'STOP_PCT': 2}
    with pytest.raises(StrategyBindingError, match='params .*STOP_PCT'):
        check_strategy_binding(_synthetic_attested(strategy_file),
                               **_synthetic_entry(strategy_file, params=params))


def test_a_missing_tunable_param_is_refused(strategy_file):
    with pytest.raises(StrategyBindingError, match='params'):
        check_strategy_binding(_synthetic_attested(strategy_file),
                               **_synthetic_entry(strategy_file, params={'ENTRY_MINUTE': 600}))


def test_the_bundle_path_param_is_transport_not_behaviour(strategy_file):
    params = {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660, 'artifact_bundle_path': '/bundle'}
    check_strategy_binding(_synthetic_attested(strategy_file),
                           **_synthetic_entry(strategy_file, params=params))


def test_an_unattested_lower_case_param_is_refused(strategy_file):
    # `self.params.get('key')` strategies read lower-case params; they change behaviour too.
    params = {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660, 'unresearched_override': True}
    with pytest.raises(StrategyBindingError, match='params .*unresearched_override'):
        check_strategy_binding(_synthetic_attested(strategy_file),
                               **_synthetic_entry(strategy_file, params=params))


@pytest.mark.parametrize('params', [
    {'ENTRY_MINUTE': 600.0, 'EXIT_MINUTE': 660},
    {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660.0},
])
def test_a_param_of_another_type_is_refused(strategy_file, params):
    # Python `==` treats 600 and 600.0 as equal; the attested value is exact.
    with pytest.raises(StrategyBindingError, match='params'):
        check_strategy_binding(_synthetic_attested(strategy_file),
                               **_synthetic_entry(strategy_file, params=params))


def test_an_extra_conid_is_refused(strategy_file):
    with pytest.raises(StrategyBindingError, match="conids .*'1003'"):
        check_strategy_binding(_synthetic_attested(strategy_file),
                               **_synthetic_entry(strategy_file, conids=[1001, 1002, 1003]))


class _FixedClockVerifier:
    """The real verifier, asked at the instant the fixture bundle was attested,
    so the 90-day attestation never expires under the test."""

    def __init__(self, verifier):
        self._verifier = verifier

    def verify(self, bundle_path, expected_mode, expected_artifact_id, now):
        return self._verifier.verify(bundle_path, expected_mode, expected_artifact_id, FIXED_NOW)


def _runtime_with_loaded_strategy(bound, tmp_path, monkeypatch, *, bar_size_str='15 mins',
                                  params=None, conids=None, strategies_dir=None):
    repo, exported, _ = bound
    strategies_dir = strategies_dir or repo / 'strategies'
    runtime = _make_runtime(tmp_path, strategies_directory=str(strategies_dir))
    runtime._trader_command_client = MagicMock()
    runtime.storage = MagicMock()
    runtime.universe_accessor = MagicMock()
    verifier = _FixedClockVerifier(ArtifactVerifier([exported.signer.public_key]))
    monkeypatch.setattr(runtime, '_get_artifact_verifier', lambda: verifier)
    runtime.load_strategy(
        name='time_of_day', bar_size_str=bar_size_str,
        conids=list(exported.spec.conids) if conids is None else conids,
        universe=None, historical_days_prior=5, module=str(strategies_dir / 'time_of_day.py'),
        class_name='TimeOfDay', description='attested candidate',
        params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660,
                'artifact_bundle_path': str(exported.bundle_path)} if params is None else params)
    assert runtime.get_strategy('time_of_day') is not None
    return runtime


def _arm(runtime, exported):
    return runtime.arm_paper_automation(
        strategy_name='time_of_day', artifact_bundle_path=str(exported.bundle_path),
        public_key_ring_path='unused-the-verifier-is-stubbed', expected_artifact_id=exported.artifact_id)


@pytest.mark.timeout(240)
def test_arm_accepts_the_loaded_strategy_the_bundle_attests(bound, tmp_path, monkeypatch):
    _, exported, _ = bound
    runtime = _runtime_with_loaded_strategy(bound, tmp_path, monkeypatch)

    assert _arm(runtime, exported)['armed'] is True
    assert isinstance(runtime.intent_emitter, IntentEmitter)


@pytest.mark.timeout(240)
@pytest.mark.parametrize('loaded, cause', [
    (dict(bar_size_str='5 mins'), "bar size '5 mins' differs from attested '15 mins'"),
    (dict(params={'ENTRY_MINUTE': 615, 'EXIT_MINUTE': 660}), "'ENTRY_MINUTE': 615"),
    (dict(params={'ENTRY_MINUTE': 600}), "params {'ENTRY_MINUTE': 600} differ"),
])
def test_arm_refuses_a_loaded_strategy_that_differs_from_the_bundle(
        bound, tmp_path, monkeypatch, loaded, cause):
    _, exported, _ = bound
    runtime = _runtime_with_loaded_strategy(bound, tmp_path, monkeypatch, **loaded)

    with pytest.raises(PaperAutomationArmError) as refusal:
        _arm(runtime, exported)

    assert refusal.value.code == 'ARM_FAILED'
    assert cause in str(refusal.value)
    assert runtime.intent_emitter is None
    assert runtime.get_paper_automation_arm()['armed'] is False


@pytest.mark.timeout(240)
def test_arm_refuses_loaded_conids_that_differ_from_the_bundle(bound, tmp_path, monkeypatch):
    _, exported, _ = bound
    loaded_conids = list(exported.spec.conids)[:-1]
    runtime = _runtime_with_loaded_strategy(bound, tmp_path, monkeypatch, conids=loaded_conids)

    with pytest.raises(PaperAutomationArmError) as refusal:
        _arm(runtime, exported)

    assert refusal.value.code == 'ARM_FAILED'
    assert f"conids {sorted(str(c) for c in loaded_conids)} differ" in str(refusal.value)


@pytest.mark.timeout(240)
def test_arm_refuses_code_loaded_before_the_file_became_the_attested_one(
        bound, tmp_path, monkeypatch):
    repo, exported, _ = bound
    attested_source = (repo / 'strategies' / 'time_of_day.py').read_text()
    strategy_file = tmp_path / 'strategies' / 'time_of_day.py'
    strategy_file.parent.mkdir()
    strategy_file.write_text(attested_source + '\n# the code that was loaded\n')
    runtime = _runtime_with_loaded_strategy(bound, tmp_path, monkeypatch,
                                            strategies_dir=strategy_file.parent)
    strategy_file.write_text(attested_source)

    with pytest.raises(PaperAutomationArmError) as refusal:
        _arm(runtime, exported)

    assert refusal.value.code == 'ARM_FAILED'
    assert ('loaded code of time_of_day.py differs from the attested file; reload the strategy'
            in str(refusal.value))
    assert runtime.intent_emitter is None


@pytest.mark.timeout(240)
def test_the_intent_carries_the_digest_of_the_loaded_code(bound, tmp_path, monkeypatch):
    repo, exported, _ = bound
    runtime = _runtime_with_loaded_strategy(bound, tmp_path, monkeypatch)
    _arm(runtime, exported)

    loaded = runtime.get_strategy('time_of_day').loaded_source_digest
    assert loaded == hashlib.sha256((repo / 'strategies' / 'time_of_day.py').read_bytes()).hexdigest()
    assert runtime.intent_emitter.context.strategy_source_digest == loaded


def _armed_runtime_with_config(bound, tmp_path, monkeypatch):
    repo, exported, _ = bound
    runtime = _runtime_with_loaded_strategy(bound, tmp_path, monkeypatch)
    Path(runtime.strategy_config_file).write_text(yaml.safe_dump({'strategies': [{
        'name': 'time_of_day', 'module': str(repo / 'strategies' / 'time_of_day.py'),
        'class_name': 'TimeOfDay', 'bar_size': '15 mins', 'conids': list(exported.spec.conids),
        'historical_days_prior': 5,
        'params': {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660,
                   'artifact_bundle_path': str(exported.bundle_path)}}]}))
    assert _arm(runtime, exported)['armed'] is True
    return runtime


@pytest.mark.timeout(240)
def test_reloading_the_armed_strategy_without_its_bundle_disarms(bound, tmp_path, monkeypatch):
    runtime = _armed_runtime_with_config(bound, tmp_path, monkeypatch)

    runtime.update_strategy_params('time_of_day', {'artifact_bundle_path': ''})

    assert runtime.get_strategy('time_of_day') is not None
    assert runtime.intent_emitter is None
    assert runtime.get_paper_automation_arm()['armed'] is False
    assert 'artifact_bundle_path' in runtime.automation_disarm_reason


@pytest.mark.timeout(240)
@pytest.mark.parametrize('edit, reason', [
    ({'ENTRY_MINUTE': 615}, "'ENTRY_MINUTE': 615"),
    ({'note': 'not attested'}, "'note': 'not attested'"),
])
def test_reloading_the_armed_strategy_with_params_that_no_longer_bind_disarms(
        bound, tmp_path, monkeypatch, edit, reason):
    runtime = _armed_runtime_with_config(bound, tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match='a restart would fail the same way'):
        runtime.update_strategy_params('time_of_day', edit)

    assert runtime.intent_emitter is None
    assert runtime.get_paper_automation_arm()['armed'] is False
    assert reason in runtime.automation_disarm_reason


@pytest.mark.timeout(240)
def test_a_reload_that_still_binds_stays_armed(bound, tmp_path, monkeypatch):
    runtime = _armed_runtime_with_config(bound, tmp_path, monkeypatch)
    before = runtime.intent_emitter

    runtime.update_strategy_params('time_of_day', {'ENTRY_MINUTE': 600})

    assert runtime.get_paper_automation_arm()['armed'] is True
    assert runtime.intent_emitter is not None and runtime.intent_emitter is not before
