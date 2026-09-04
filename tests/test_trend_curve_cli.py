from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import open_trader.trend_animals as trend_animals
import open_trader.cli as cli
import open_trader.notifications as notifications
import open_trader.trend_curve_research as trend_curve_research
from open_trader.cli import build_parser


SLB_CURVE_ENCRYPTED = (
    "ehtRChN4vTYXmnU0XeI1jUK90sU2F7krL05NDT98YL8UTnyA+ZPzfFhGtc16tZucbQiHicdZb6zASL09TNBfXk9jMxyga1yBUjoxwZmQV9f6VhEKsU0ASQEFlmLlF9Drr5Dhe3ZG0O3JuK/ZhV319o/zMC9iziANRRZEbU3zeZltSCoRpfm/2nkUUxlYReMvaHhaE5njUaXGc4yXvmKKB/DB+3KE/phKeRKYP/zZ2mB7dEvRW7nppyiVRq35neeyP0EMKv28Jvjf5VVzhukl+JrtrR6YsHnyPcDOTcf3qT+vPyEvieKpK9oVsMc0dcvRHmRw5vxii3b0k0L5VaXOPd4II3UiPVt8hIeQwSE5BvTLaqbOYAZgIdb8VFR0sVfGUR0b8XqS9i1f1LUB7XEaSmB/OknT2hbVGtJcsN96W0GYUMgCwgxHQxmJspmlFlZ9zJ2DCMGc0XKeQL/ztER2WCvYidySWZe7Il/lPCsc6UPgwHQw2n+SVAG7E0zG7UoqK8FPcneVJd77KoFVcj8vx8woK3cTAbqZEzmyNPg9t6SdN01c0qONGzC6qFBivXyw6R0OEGWI9UYbPc4r3WcbmTiBKYdVxn+31ioLr1lG8w3NElYsMBArmln2uBWUonOzeme6spz0p3d1qgAfGsyve6Ixu/sMagxMIXR6ZUizW+UmgKmbEQVx8b3hiGK4JFp6"
)


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


def _write_portfolio_cli_inputs(
    database: Path,
    prices_dir: Path,
    portfolio: Path,
    exclusions: Path,
) -> None:
    prices_dir.mkdir()
    _write_cli_inputs(database, prices_dir / "TEST.csv")
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,name,market_value_hkd,ai_eligible\n"
        "US,stock,TEST,TEST,测试标的,100,true\n"
        "US,stock,SKIP,SKIP,排除标的,100,true\n",
        encoding="utf-8",
    )
    exclusions.write_text(
        json.dumps({"US.SKIP": "configured exclusion"}), encoding="utf-8"
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


def _prepare_reconcile_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    paid_rows: list[dict[str, object]],
) -> tuple[list[str], list[tuple[str, float]], list[tuple[str, dict[str, object], float]], Path]:
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            [
                {
                    "market": "US",
                    "symbol": "SLB",
                    "asset_id": 10002,
                    "group_id": 332171,
                    "tm_id": 337127,
                    "ccy_id": 101,
                }
            ]
        ),
        encoding="utf-8",
    )
    mmkv_path = tmp_path / "wx64e4edbab5e14356"
    mmkv_path.write_bytes(b"snapshot")
    Path(f"{mmkv_path}.crc").write_bytes(b"crc")
    mmkv_helper = tmp_path / "open-trader-mmkv-dump"
    mmkv_helper.write_text(
        "#!/bin/sh\n"
        "printf '%s\\t%s\\n' other ignored\n"
        "printf '%s\\t%s\\n' vuex '{\"user\":{\"token\":\"mini-token\",\"info\":{\"id\":456789}}}'\n",
        encoding="utf-8",
    )
    mmkv_helper.chmod(mmkv_helper.stat().st_mode | 0o111)
    config_path = tmp_path / "daily.env"
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
                "TREND_ANIMALS_API_KEY=paid-key",
                "OPEN_TRADER_NOTIFIERS=feishu",
                "OPEN_TRADER_FEISHU_WEBHOOK_URL=https://example.invalid/hook",
            )
        ),
        encoding="utf-8",
    )

    def curve_transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": SLB_CURVE_ENCRYPTED},
        }

    monkeypatch.setattr(trend_curve_research, "_default_curve_transport", curve_transport)
    paid_requests: list[tuple[str, float]] = []

    class PaidResponse:
        def __enter__(self) -> "PaidResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"success": True, "code": "00000", "data": paid_rows}
            ).encode("utf-8")

    def paid_transport(url: str, timeout: float) -> PaidResponse:
        paid_requests.append((url, timeout))
        return PaidResponse()

    monkeypatch.setattr(trend_animals, "urlopen", paid_transport)
    deliveries: list[tuple[str, dict[str, object], float]] = []

    def feishu_transport(
        url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        deliveries.append((url, payload, timeout))
        return {"code": 0}

    monkeypatch.setattr(notifications, "_post_json", feishu_transport)
    database = tmp_path / "history.sqlite3"
    command = [
        "trend-curve",
        "collect",
        "--watchlist",
        str(watchlist),
        "--database",
        str(database),
        "--mmkv-path",
        str(mmkv_path),
        "--mmkv-helper",
        str(mmkv_helper),
        "--reconcile-and-notify",
        "--config",
        str(config_path),
    ]
    return command, paid_requests, deliveries, database


def test_trend_curve_collect_reconciles_same_day_snapshot_and_notifies_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (
        command,
        paid_requests,
        deliveries,
        _database,
    ) = _prepare_reconcile_cli(
        monkeypatch,
        tmp_path,
        [
            {
                "tmId": 337127,
                "asOfDate": "2026-09-02",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "温",
                "trendStrengthLocalCurr": "90.8",
            }
        ],
    )
    exit_code = cli.main(command)

    captured = capsys.readouterr()
    message = deliveries[0][1]["content"]["text"]
    requested_fields = parse_qs(
        urlparse(paid_requests[0][0]).query
    )["fields"][0].split(",")
    assert (
        exit_code,
        captured.err,
        "database:" in captured.out,
        "targets: 1" in captured.out,
        "points: 7" in captured.out,
        len(paid_requests),
        tuple(sorted(requested_fields)),
        len(deliveries),
        "趋势曲线采集对账一致" in message,
        "数据日期：US 2026-09-02" in message,
        "标的：1/1" in message,
        "对账字段：前一温度、当前温度、当前本地强度" in message,
        "结果：全部一致" in message,
    ) == (
        0,
        "",
        True,
        True,
        True,
        1,
        (
            "asOfDate",
            "tmId",
            "trendStrengthLocalCurr",
            "trendTemperatureCurr",
            "trendTemperaturePrev",
        ),
        1,
        True,
        True,
        True,
        True,
        True,
    )


def test_trend_curve_collect_reconciles_fresh_paid_snapshot_on_repeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paid_rows = [
        {
            "tmId": 337127,
            "asOfDate": "2026-09-02",
            "trendTemperaturePrev": "温",
            "trendTemperatureCurr": "温",
            "trendStrengthLocalCurr": "90.8",
        }
    ]
    command, paid_requests, deliveries, database = _prepare_reconcile_cli(
        monkeypatch, tmp_path, paid_rows
    )

    first_exit = cli.main(command)
    paid_rows[0]["trendTemperatureCurr"] = "热"
    try:
        second_exit = cli.main(command)
    except SystemExit as exc:
        second_exit = exc.code

    with sqlite3.connect(database) as connection:
        curve_rows = connection.execute(
            """
            SELECT market, symbol, curve_date, COUNT(*)
            FROM trend_curve_points
            WHERE market = 'US' AND symbol = 'SLB' AND curve_date = '2026-09-02'
            GROUP BY market, symbol, curve_date
            """
        ).fetchall()
    first_message = deliveries[0][1]["content"]["text"]
    second_message = deliveries[1][1]["content"]["text"]
    assert (
        first_exit,
        "趋势曲线采集对账一致" in first_message,
        second_exit != 0,
        "趋势曲线采集对账异常" in second_message,
        "US.SLB 2026-09-02 当前温度：曲线=温，API=热" in second_message,
        len(paid_requests),
        curve_rows,
    ) == (
        0,
        True,
        True,
        True,
        True,
        2,
        [("US", "SLB", "2026-09-02", 1)],
    )


def test_trend_curve_collect_notifies_field_mismatch_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command, _paid_requests, deliveries, database = _prepare_reconcile_cli(
        monkeypatch,
        tmp_path,
        [
            {
                "tmId": 337127,
                "asOfDate": "2026-09-02",
                "trendTemperaturePrev": "温",
                "trendTemperatureCurr": "热",
                "trendStrengthLocalCurr": "90.8",
            }
        ],
    )

    try:
        exit_code = cli.main(command)
    except SystemExit as exc:
        exit_code = exc.code

    with sqlite3.connect(database) as connection:
        curve_rows = connection.execute(
            """
            SELECT market, symbol, curve_date
            FROM trend_curve_points
            WHERE market = 'US' AND symbol = 'SLB' AND curve_date = '2026-09-02'
            """
        ).fetchall()
    message = deliveries[0][1]["content"]["text"]
    assert (
        exit_code != 0,
        curve_rows,
        len(deliveries),
        "趋势曲线采集对账异常" in message,
        "US.SLB 2026-09-02 当前温度：曲线=温，API=热" in message,
    ) == (
        True,
        [("US", "SLB", "2026-09-02")],
        1,
        True,
        True,
    )


def test_trend_curve_collect_notifies_missing_paid_snapshot_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command, _paid_requests, deliveries, database = _prepare_reconcile_cli(
        monkeypatch, tmp_path, []
    )

    try:
        exit_code = cli.main(command)
    except SystemExit as exc:
        exit_code = exc.code

    with sqlite3.connect(database) as connection:
        curve_rows = connection.execute(
            """
            SELECT market, symbol, curve_date
            FROM trend_curve_points
            WHERE market = 'US' AND symbol = 'SLB' AND curve_date = '2026-09-02'
            """
        ).fetchall()
    message = deliveries[0][1]["content"]["text"]
    assert (
        exit_code != 0,
        curve_rows,
        len(deliveries),
        "趋势曲线采集对账异常" in message,
        "US.SLB 2026-09-02：API 快照缺失" in message,
    ) == (
        True,
        [("US", "SLB", "2026-09-02")],
        1,
        True,
        True,
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


def test_trend_curve_portfolio_backtest_cli_emits_one_versioned_json_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "history.sqlite3"
    prices_dir = tmp_path / "prices"
    portfolio = tmp_path / "portfolio.csv"
    exclusions = tmp_path / "exclusions.json"
    _write_portfolio_cli_inputs(database, prices_dir, portfolio, exclusions)
    before_files = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    exit_code = cli.main(
        [
            "trend-curve",
            "portfolio-backtest",
            "--database",
            str(database),
            "--prices-dir",
            str(prices_dir),
            "--portfolio",
            str(portfolio),
            "--exclusions",
            str(exclusions),
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-02",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    after_files = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    per_symbol = payload["per_symbol"]
    fixed_sections = (
        "schema",
        "strategy_id",
        "requested_range",
        "assumptions",
        "preflight",
        "weights",
        "strategy",
        "buy_and_hold",
        "per_symbol",
        "source_hashes",
    )

    assert {
        "exit_code": exit_code,
        "stderr": captured.err,
        "stdout_line_count": len(captured.out.strip().splitlines()),
        "schema": payload["schema"],
        "strategy_id": payload["strategy_id"],
        "requested_range": payload["requested_range"],
        "initial_cash": payload["assumptions"]["initial_cash"],
        "caveats": payload["caveats"],
        "symbol": per_symbol[0]["symbol"],
        "name_zh": per_symbol[0]["name_zh"],
        "source_hashes": payload["source_hashes"],
        "sections_present": all(key in payload for key in fixed_sections),
        "files_unchanged": after_files == before_files,
    } == {
        "exit_code": 0,
        "stderr": "",
        "stdout_line_count": 1,
        "schema": "open_trader.trend_curve_portfolio_backtest.v1",
        "strategy_id": "trend_curve_warm_to_hot_flat_exit/US/v1",
        "requested_range": {"start": "2026-01-01", "end": "2026-01-02"},
        "initial_cash": "1000000",
        "caveats": [
            "Current holdings and weights are applied retrospectively, so results include survivorship and lookahead bias and do not reconstruct the historical account."
        ],
        "symbol": "TEST",
        "name_zh": "测试标的",
        "source_hashes": {
            "portfolio_csv": hashlib.sha256(portfolio.read_bytes()).hexdigest(),
            "exclusions_json": hashlib.sha256(exclusions.read_bytes()).hexdigest(),
            "trend_curve_database": hashlib.sha256(database.read_bytes()).hexdigest(),
            "ohlc_csvs": {
                "TEST": hashlib.sha256(
                    (prices_dir / "TEST.csv").read_bytes()
                ).hexdigest()
            },
        },
        "sections_present": True,
        "files_unchanged": True,
    }
