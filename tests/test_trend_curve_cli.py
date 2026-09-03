from __future__ import annotations

from pathlib import Path

import pytest

from open_trader.cli import build_parser


def test_trend_curve_cli_exposes_collect_only() -> None:
    parser = build_parser()
    collect_args = parser.parse_args(
        ["trend-curve", "collect", "--watchlist", "watchlist.json"]
    )

    assert collect_args.command == "trend-curve"
    assert collect_args.trend_curve_command == "collect"
    assert collect_args.watchlist == Path("watchlist.json")
    assert collect_args.database == Path("data/trend_curve/history.sqlite3")
    assert collect_args.mmkv_helper is None

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "trend-curve",
                "backtest",
                "--market",
                "US",
                "--symbol",
                "SLB",
                "--entry-transition",
                "平转温",
                "--exit-transition",
                "热→温",
            ]
        )
    assert exc_info.value.code == 2


def test_trend_curve_cli_accepts_explicit_mmkv_snapshot() -> None:
    parser = build_parser()
    explicit_args = parser.parse_args(
        [
            "trend-curve",
            "collect",
            "--watchlist",
            "watchlist.json",
            "--mmkv-path",
            "copied/wx64e4edbab5e14356",
        ]
    )
    default_args = parser.parse_args(
        ["trend-curve", "collect", "--watchlist", "watchlist.json"]
    )

    assert (explicit_args.mmkv_path, default_args.mmkv_path) == (
        Path("copied/wx64e4edbab5e14356"),
        None,
    )
