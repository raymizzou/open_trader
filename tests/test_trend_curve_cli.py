from __future__ import annotations

import sys
from pathlib import Path

import pytest

import open_trader.cli as cli
import open_trader.notifications as notifications
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
