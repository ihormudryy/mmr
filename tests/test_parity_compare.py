"""[COMPAT] Task 3: field-level parity core (tolerances, keys, exit codes)."""
import json

import pytest

from scripts.parity_compare import (
    FieldDivergence, ParityReport, compare_keyed, compare_surfaces,
    normalize_center_proposal, normalize_legacy_proposal, values_match)


class TestValuesMatch:
    def test_float_within_tolerance(self):
        assert values_match(100.0000004, 100.0000009, 'float')

    def test_float_beyond_tolerance_diverges(self):
        assert not values_match(100.0, 100.000002, 'float')

    def test_timestamps_compare_at_second_resolution(self):
        assert values_match('2026-07-15T13:42:17.201Z',
                            '2026-07-15T13:42:17.899+00:00', 'timestamp')
        assert not values_match('2026-07-15T13:42:17Z',
                                '2026-07-15T13:42:18Z', 'timestamp')

    def test_naive_timestamp_is_treated_as_utc(self):
        assert values_match('2026-07-15T13:42:17', '2026-07-15T13:42:17Z', 'timestamp')

    def test_none_on_one_side_diverges(self):
        assert not values_match(None, 0.0, 'float')
        assert values_match(None, None, 'float')

    def test_loose_map_mixes_numeric_and_text(self):
        assert values_match({'RANGE_MINUTES': 45, 'TZ': 'Australia/Sydney'},
                            {'RANGE_MINUTES': '45', 'TZ': 'Australia/Sydney'},
                            'loose_map')
        assert not values_match({'RANGE_MINUTES': 45}, {'RANGE_MINUTES': 30},
                                'loose_map')


class TestProposalNormalization:
    def test_executed_maps_to_order_submitted_on_both_sides(self):
        legacy = normalize_legacy_proposal(
            {'id': 7, 'storage_status': 'EXECUTED', 'display_status': 'ORDER_SUBMITTED',
             'symbol': 'AMD', 'action': 'BUY', 'quantity': 10, 'amount': None,
             'confidence': 0.7})
        center = normalize_center_proposal(
            {'id': 7, 'status': 'EXECUTED', 'symbol': 'AMD', 'action': 'BUY',
             'quantity': 10, 'amount': None, 'confidence': 0.7})
        assert legacy['display_status'] == center['display_status'] == 'ORDER_SUBMITTED'
        assert legacy['storage_status'] == center['storage_status'] == 'EXECUTED'


class TestCompareKeyed:
    FIELDS = {'quantity': 'float'}

    def test_presence_divergence_when_row_missing(self):
        divs = compare_keyed('positions', [{'key': 5437, 'quantity': 100.0}], [],
                             self.FIELDS, allow=[])
        assert divs == [FieldDivergence('positions', '5437', '<presence>',
                                        True, False, False)]

    def test_allow_pattern_marks_explained(self):
        divs = compare_keyed('positions',
                             [{'key': 5437, 'quantity': 100.0}],
                             [{'key': 5437, 'quantity': 99.0}],
                             self.FIELDS, allow=['positions:5437:quantity'])
        assert divs[0].explained is True


class TestReport:
    def _report(self, explained):
        return ParityReport(generated_at='2026-07-15T14:00:00Z', counts={'positions': 1},
                            divergences=[FieldDivergence('positions', '5437', 'quantity',
                                                         100.0, 99.0, explained)])

    def test_unexplained_divergence_exits_one(self):
        assert self._report(explained=False).exit_code() == 1

    def test_explained_divergence_exits_zero_but_is_reported(self):
        report = self._report(explained=True)
        assert report.exit_code() == 0
        assert json.loads(report.to_json())['divergences'][0]['explained'] is True


def test_compare_surfaces_covers_every_required_section():
    empty = {s: [] for s in ('account', 'cash', 'positions', 'proposals',
                             'strategies', 'risk', 'orders', 'fills')}
    report = compare_surfaces(empty, empty, allow=[])
    assert set(report.counts) == set(empty)
    assert report.exit_code() == 0
