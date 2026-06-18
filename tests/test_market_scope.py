from pathlib import Path

import pytest

from open_trader.market_scope import (
    MarketScope,
    market_report_path,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)


def test_parse_market_scope_accepts_us_and_hk_case_insensitively() -> None:
    assert parse_market_scope("us") is MarketScope.US
    assert parse_market_scope("HK") is MarketScope.HK


def test_parse_market_scope_rejects_blank_or_unknown_values() -> None:
    with pytest.raises(ValueError, match="market must be one of: HK, US"):
        parse_market_scope("")
    with pytest.raises(ValueError, match="market must be one of: HK, US"):
        parse_market_scope("CN")


def test_market_scoped_paths_are_separate_from_legacy_latest() -> None:
    data_dir = Path("data")
    reports_dir = Path("reports")

    assert market_run_dir(data_dir, "2026-06-19", MarketScope.HK) == Path(
        "data/runs/2026-06-19/HK"
    )
    assert market_scoped_latest_path(data_dir, MarketScope.HK, "trading_plan.csv") == Path(
        "data/latest/HK/trading_plan.csv"
    )
    assert market_report_path(
        reports_dir,
        "daily_runs",
        "2026-06-19",
        MarketScope.US,
    ) == Path("reports/daily_runs/2026-06-19-US.md")
