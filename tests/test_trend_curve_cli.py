from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

import open_trader.cli as cli
import open_trader.notifications as notifications
from open_trader.cli import build_parser


def _write_cli_inputs(database: Path, prices: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE trend_curve_points (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                curve_date TEXT NOT NULL,
                price TEXT NOT NULL,
                temperature TEXT NOT NULL,
                strength TEXT NOT NULL,
                mom TEXT,
                yoy TEXT,
                bar TEXT,
                asset_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                tm_id INTEGER NOT NULL,
                ccy_id INTEGER NOT NULL,
                PRIMARY KEY (market, symbol, curve_date)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO trend_curve_points
            (market, symbol, curve_date, price, temperature, strength,
             mom, yoy, bar, asset_id, group_id, tm_id, ccy_id)
            VALUES ('US', 'TEST', ?, '10', ?, '80', NULL, NULL, NULL,
                    1, 2, 3, 4)
            """,
            [("2026-01-01", "温"), ("2026-01-02", "热")],
        )
    with prices.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", "open", "high", "low", "close"))
        writer.writeheader()
        writer.writerows(
            [
                {"date": "2026-01-01", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-01-02", "open": "10", "high": "10", "low": "10", "close": "10"},
                {"date": "2026-01-03", "open": "11", "high": "11", "low": "11", "close": "11"},
            ]
        )


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


def test_trend_curve_failure_notifies_feishu_and_keeps_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    mappings_root = tmp_path / "mappings"
    config_path = tmp_path / "daily.env"
    mmkv_path = tmp_path / "mmkv-snapshot"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,ai_eligible\n"
        "US,stock,ESTC,ESTC,true\n",
        encoding="utf-8",
    )
    mapping_directory = mappings_root / "US"
    mapping_directory.mkdir(parents=True)
    (mapping_directory / "US.ESTC.json").write_text(
        (
            '{"asset":"美股","futu_symbol":"US.ESTC","market":"US",'
            '"schema_version":"open_trader.trend_symbol_mapping.v1",'
            '"trend_animals_symbol":"ESTC","trend_animals_tm_id":334101}'
        ),
        encoding="utf-8",
    )
    mmkv_path.write_bytes(b"snapshot")
    Path(f"{mmkv_path}.crc").write_bytes(b"crc")
    config_path.write_text(
        "\n".join(
            (
                f"OPEN_TRADER_REPO={tmp_path}",
                f"OPEN_TRADER_PYTHON={sys.executable}",
                "OPEN_TRADER_TIMEZONE=Asia/Shanghai",
                "OPEN_TRADER_DEADLINE=23:59",
                "OPEN_TRADER_FUTU_HOST=127.0.0.1",
                "OPEN_TRADER_FUTU_PORT=11111",
                "DEEPSEEK_API_KEY=test-key",
                "OPEN_TRADER_NOTIFIERS=feishu,macos",
                "OPEN_TRADER_FEISHU_WEBHOOK_URL=https://example.invalid/hook",
            )
        ),
        encoding="utf-8",
    )
    captured_feishu: list[tuple[str, str]] = []
    captured_non_feishu: list[tuple[str, str]] = []

    def capture_feishu(self: object, title: str, message: str) -> None:
        captured_feishu.append((title, message))

    def capture_non_feishu(self: object, title: str, message: str) -> None:
        captured_non_feishu.append((title, message))

    monkeypatch.setattr(notifications.FeishuWebhookNotifier, "notify", capture_feishu)
    monkeypatch.setattr(notifications.MacOSNotifier, "notify", capture_non_feishu)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "trend-curve",
                "collect",
                "--portfolio",
                str(portfolio),
                "--mappings-root",
                str(mappings_root),
                "--database",
                str(tmp_path / "history.sqlite3"),
                "--mmkv-path",
                str(mmkv_path),
                "--mmkv-helper",
                str(tmp_path / "missing-mmkv-helper"),
                "--notify-failure",
                "--config",
                str(config_path),
            ]
        )

    assert (exc_info.value.code, captured_feishu, captured_non_feishu) == (
        2,
        [("趋势曲线采集失败", "trend-curve collect 失败：MMKV helper is unavailable")],
        [],
    )


def test_trend_curve_backtest_cli_emits_versioned_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "history.sqlite3"
    prices = tmp_path / "prices.csv"
    _write_cli_inputs(database, prices)

    exit_code = cli.main(
        [
            "trend-curve",
            "backtest",
            "--database",
            str(database),
            "--prices",
            str(prices),
            "--market",
            "US",
            "--symbol",
            "TEST",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-03",
            "--initial-cash",
            "1000",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert {
        "exit_code": exit_code,
        "stdout_line_count": len(captured.out.strip().splitlines()),
        "stderr": captured.err,
        "schema": payload["schema"],
        "strategy_id": payload["strategy_id"],
        "database_hash": payload["source_hashes"]["trend_curve_database"],
        "prices_hash": payload["source_hashes"]["ohlc_csv"],
        "commission_bps": payload["assumptions"]["commission_bps"],
        "slippage_bps": payload["assumptions"]["slippage_bps"],
        "sections_present": all(
            key in payload
            for key in (
                "decisions",
                "trades",
                "equity_curve",
                "completed_rounds",
                "metrics",
                "buy_and_hold",
            )
        ),
    } == {
        "exit_code": 0,
        "stdout_line_count": 1,
        "stderr": "",
        "schema": "open_trader.trend_curve_backtest.v1",
        "strategy_id": "trend_curve_warm_to_hot_flat_exit/US/v1",
        "database_hash": hashlib.sha256(database.read_bytes()).hexdigest(),
        "prices_hash": hashlib.sha256(prices.read_bytes()).hexdigest(),
        "commission_bps": "10",
        "slippage_bps": "5",
        "sections_present": True,
    }
