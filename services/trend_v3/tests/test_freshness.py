from datetime import date

from trend_v3.freshness import is_opening_data_fresh


def test_stale_data_blocks_opening():
    assert not is_opening_data_fresh(date(2026, 11, 19), date(2026, 11, 20))


def test_expected_date_passes_freshness_gate():
    assert is_opening_data_fresh(date(2026, 11, 20), date(2026, 11, 20))


def test_unknown_date_blocks_opening():
    assert not is_opening_data_fresh(None, date(2026, 11, 20))


def test_future_date_blocks_opening():
    assert not is_opening_data_fresh(date(2026, 11, 21), date(2026, 11, 20))
