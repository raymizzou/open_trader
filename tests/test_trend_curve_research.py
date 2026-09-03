from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from open_trader import trend_curve_research
from open_trader.futu_symbols import from_trend_animals_symbol, to_futu_symbol
from open_trader.trend_curve_research import (
    collect_trend_curves,
    read_wechat_mini_credentials,
)


FOUR_SECTION_ENCRYPTED = (
    "ehtRChN4vTYXmnU0XeI1jROyn46BO1bnfGp5zD3cGvgQuIY7Z/UlaEGb/heZWgb2OTBwAywGoxu/W9hc3m6wlEqU08aJongByGXl1KgrWW269RssHjerZRumWavSRvAVptahDUKx6yqrYkkDpjcCqg=="
)
THREE_SECTION_ENCRYPTED = (
    "ehtRChN4vTYXmnU0XeI1jROyn46BO1bnfGp5zD3cGvgQuIY7Z/UlaEGb/heZWgb2OTBwAywGoxu/W9hc3m6wlEqU08aJongByGXl1KgrWW269RssHjerZRumWavSRvAV9Iz+b7bj5CCzRqIuXlT8og=="
)


def test_collect_stores_curve_rows_for_future_database_use(
    tmp_path: Path, capsys
) -> None:
    expected_rows = [
        ("US", "SLB", "2026-08-25", "53.01", "平", "77.3", None, None, None, 10002, 332171, 337127, 101),
        ("US", "SLB", "2026-08-26", "53.32", "平", "80.9", None, None, None, 10002, 332171, 337127, 101),
        ("US", "SLB", "2026-08-27", "54.73", "温", "87.3", None, None, None, 10002, 332171, 337127, 101),
        ("US", "SLB", "2026-08-28", "57.03", "温", "93.9", None, None, None, 10002, 332171, 337127, 101),
        ("US", "SLB", "2026-08-31", "59.79", "热", "96.5", None, None, None, 10002, 332171, 337127, 101),
        ("US", "SLB", "2026-09-01", "56.85", "温", "86.8", None, None, None, 10002, 332171, 337127, 101),
        ("US", "SLB", "2026-09-02", "58.13", "温", "90.8", None, None, None, 10002, 332171, 337127, 101),
    ]
    encrypted = (
        "ehtRChN4vTYXmnU0XeI1jUK90sU2F7krL05NDT98YL8UTnyA+ZPzfFhGtc16tZucbQiHicdZb6zASL09TNBfXk9jMxyga1yBUjoxwZmQV9f6VhEKsU0ASQEFlmLlF9Drr5Dhe3ZG0O3JuK/ZhV319o/zMC9iziANRRZEbU3zeZltSCoRpfm/2nkUUxlYReMvaHhaE5njUaXGc4yXvmKKB/DB+3KE/phKeRKYP/zZ2mB7dEvRW7nppyiVRq35neeyP0EMKv28Jvjf5VVzhukl+JrtrR6YsHnyPcDOTcf3qT+vPyEvieKpK9oVsMc0dcvRHmRw5vxii3b0k0L5VaXOPd4II3UiPVt8hIeQwSE5BvTLaqbOYAZgIdb8VFR0sVfGUR0b8XqS9i1f1LUB7XEaSmB/OknT2hbVGtJcsN96W0GYUMgCwgxHQxmJspmlFlZ9zJ2DCMGc0XKeQL/ztER2WCvYidySWZe7Il/lPCsc6UPgwHQw2n+SVAG7E0zG7UoqK8FPcneVJd77KoFVcj8vx8woK3cTAbqZEzmyNPg9t6SdN01c0qONGzC6qFBivXyw6R0OEGWI9UYbPc4r3WcbmTiBKYdVxn+31ioLr1lG8w3NElYsMBArmln2uBWUonOzeme6spz0p3d1qgAfGsyve6Ixu/sMagxMIXR6ZUizW+UmgKmbEQVx8b3hiGK4JFp6"
    )
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
    database = tmp_path / "history.sqlite3"
    requests: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def transport(
        url: str, body: bytes, headers: dict[str, str]
    ) -> dict[str, object]:
        requests.append((url, json.loads(body), dict(headers)))
        return {"success": True, "code": "00000", "data": {"encryptedData": encrypted}}

    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token-123", 998877665544),
        transport=transport,
    )

    assert result.target_count == 1
    assert result.point_count == 7
    assert requests == [
        (
            "https://www.trendtrader.cn/mall4cloud_breed/breed/getVarietyCurve_V3",
            {
                "assetId": 10002,
                "groupId": 332171,
                "id": 337127,
                "userId": 998877665544,
                "selected": 0,
                "ccyId": 101,
                "code": "998877665544",
            },
            {"Authorization": "fake-token-123", "Content-Type": "application/json"},
        )
    ]
    with sqlite3.connect(database) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(trend_curve_points)")] == [
            "market", "symbol", "curve_date", "price", "temperature", "strength",
            "mom", "yoy", "bar", "asset_id", "group_id", "tm_id", "ccy_id",
        ]
        assert connection.execute(
            "SELECT market, symbol, curve_date, price, temperature, strength, mom, yoy, bar, "
            "asset_id, group_id, tm_id, ccy_id FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall() == expected_rows
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("trend_curve_points",)]
    database_bytes = database.read_bytes()
    assert b"fake-token-123" not in database_bytes
    assert b"998877665544" not in database_bytes
    captured = capsys.readouterr()
    assert "fake-token-123" not in captured.out
    assert "998877665544" not in captured.out


def test_collect_stores_four_section_curve_history(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            [
                {
                    "market": "US",
                    "symbol": "ESTC",
                    "asset_id": 10002,
                    "group_id": 332171,
                    "tm_id": 334101,
                    "ccy_id": 101,
                }
            ]
        ),
        encoding="utf-8",
    )
    database = tmp_path / "history.sqlite3"

    def transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": FOUR_SECTION_ENCRYPTED},
        }

    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=transport,
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT market, symbol, curve_date, price, temperature, strength, "
            "asset_id, group_id, tm_id, ccy_id FROM trend_curve_points"
        ).fetchall()
    assert (result.target_count, result.point_count, rows) == (
        1,
        1,
        [("US", "ESTC", "2026-09-02", "0.22", "凉", "13.2", 10002, 332171, 334101, 101)],
    )


def test_collect_portfolio_uses_every_eligible_holding_and_local_mapping(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,ai_eligible\n"
        "US,stock,ESTC,ESTC,true\n"
        "CN,etf,515450,,true\n"
        "US,cash,CASH,,false\n"
        "US,fund,MONEY,,false\n",
        encoding="utf-8",
    )
    mappings_root = tmp_path / "symbol_mappings"
    (mappings_root / "US").mkdir(parents=True)
    (mappings_root / "CN").mkdir(parents=True)
    (mappings_root / "US" / "US.ESTC.json").write_text(
        json.dumps(
            {
                "asset": "美股",
                "futu_symbol": "US.ESTC",
                "market": "US",
                "schema_version": "open_trader.trend_symbol_mapping.v1",
                "trend_animals_symbol": "ESTC",
                "trend_animals_tm_id": 334101,
                "provenance": "local-cache",
            }
        ),
        encoding="utf-8",
    )
    (mappings_root / "CN" / "SH.515450.json").write_text(
        json.dumps(
            {
                "asset": "ETF基金",
                "futu_symbol": "SH.515450",
                "market": "CN",
                "schema_version": "open_trader.trend_symbol_mapping.v1",
                "trend_animals_symbol": "515450.SH",
                "trend_animals_tm_id": 328879,
            }
        ),
        encoding="utf-8",
    )
    requests: list[dict[str, object]] = []

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        requests.append(json.loads(body))
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": FOUR_SECTION_ENCRYPTED},
        }

    result = collect_trend_curves(
        portfolio=portfolio,
        mappings_root=mappings_root,
        database=tmp_path / "history.sqlite3",
        credentials=("fake-token", 123456789),
        transport=transport,
    )

    assert (result.target_count, requests) == (
        2,
        [
            {
                "assetId": 10002,
                "groupId": 377042,
                "id": 328879,
                "userId": 123456789,
                "selected": 0,
                "ccyId": 100,
                "code": "123456789",
            },
            {
                "assetId": 10002,
                "groupId": 332171,
                "id": 334101,
                "userId": 123456789,
                "selected": 0,
                "ccyId": 101,
                "code": "123456789",
            },
        ],
    )


def test_collect_portfolio_excludes_blacklisted_holding_before_mapping(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,ai_eligible\n"
        "US,stock,ESTC,ESTC,true\n"
        "US,stock,AGRZ,AGRZ,true\n",
        encoding="utf-8",
    )
    mappings_root = tmp_path / "symbol_mappings"
    mapping_directory = mappings_root / "US"
    mapping_directory.mkdir(parents=True)
    (mapping_directory / "US.ESTC.json").write_text(
        json.dumps(
            {
                "asset": "美股",
                "futu_symbol": "US.ESTC",
                "market": "US",
                "schema_version": "open_trader.trend_symbol_mapping.v1",
                "trend_animals_symbol": "ESTC",
                "trend_animals_tm_id": 334101,
            }
        ),
        encoding="utf-8",
    )
    requests: list[dict[str, object]] = []

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        requests.append(json.loads(body))
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": FOUR_SECTION_ENCRYPTED},
        }

    database = tmp_path / "history.sqlite3"
    result = collect_trend_curves(
        portfolio=portfolio,
        mappings_root=mappings_root,
        database=database,
        credentials=("fake-token", 123456789),
        transport=transport,
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points"
        ).fetchall()
    assert (result.target_count, requests, rows) == (
        1,
        [
            {
                "assetId": 10002,
                "groupId": 332171,
                "id": 334101,
                "userId": 123456789,
                "selected": 0,
                "ccyId": 101,
                "code": "123456789",
            }
        ],
        [("US", "ESTC", "2026-09-02")],
    )


def test_collect_portfolio_rejects_mismatched_mapping_before_network(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,ai_eligible\n"
        "US,stock,ESTC,ESTC,true\n",
        encoding="utf-8",
    )
    mappings_root = tmp_path / "symbol_mappings"
    mapping_directory = mappings_root / "US"
    mapping_directory.mkdir(parents=True)
    (mapping_directory / "US.ESTC.json").write_text(
        json.dumps(
            {
                "asset": "美股",
                "futu_symbol": "US.ESTC",
                "market": "US",
                "schema_version": "open_trader.trend_symbol_mapping.v1",
                "trend_animals_symbol": "MSFT",
                "trend_animals_tm_id": 334101,
            }
        ),
        encoding="utf-8",
    )
    expected_futu = to_futu_symbol("US", "US.ESTC")
    assert from_trend_animals_symbol("US", "MSFT") != expected_futu
    requests: list[bytes] = []

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        requests.append(body)
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": FOUR_SECTION_ENCRYPTED},
        }

    database = tmp_path / "history.sqlite3"
    with pytest.raises(ValueError) as raised:
        collect_trend_curves(
            portfolio=portfolio,
            mappings_root=mappings_root,
            database=database,
            credentials=("fake-token", 123456789),
            transport=transport,
        )

    assert (str(raised.value), requests, database.exists()) == (
        "symbol mapping cache is malformed",
        [],
        False,
    )


def test_collect_portfolio_rejects_conflicting_mapping_ids_before_network(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,ai_eligible\n"
        "US,stock,ESTC,ESTC,true\n",
        encoding="utf-8",
    )
    mappings_root = tmp_path / "symbol_mappings"
    mapping_directory = mappings_root / "US"
    mapping_directory.mkdir(parents=True)
    for futu_symbol, trend_symbol in (("US.ESTC", "ESTC"), ("US.MSFT", "MSFT")):
        (mapping_directory / f"{futu_symbol}.json").write_text(
            json.dumps(
                {
                    "asset": "美股",
                    "futu_symbol": futu_symbol,
                    "market": "US",
                    "schema_version": "open_trader.trend_symbol_mapping.v1",
                    "trend_animals_symbol": trend_symbol,
                    "trend_animals_tm_id": 334101,
                }
            ),
            encoding="utf-8",
        )
    requests: list[bytes] = []

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        requests.append(body)
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": FOUR_SECTION_ENCRYPTED},
        }

    database = tmp_path / "history.sqlite3"
    with pytest.raises(ValueError) as raised:
        collect_trend_curves(
            portfolio=portfolio,
            mappings_root=mappings_root,
            database=database,
            credentials=("fake-token", 123456789),
            transport=transport,
        )

    assert (str(raised.value), requests, database.exists()) == (
        "symbol mapping conflict",
        [],
        False,
    )


def test_collect_rejects_unapproved_curve_section_count(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            [
                {
                    "market": "US",
                    "symbol": "ESTC",
                    "asset_id": 10002,
                    "group_id": 332171,
                    "tm_id": 334101,
                    "ccy_id": 101,
                }
            ]
        ),
        encoding="utf-8",
    )
    database = tmp_path / "history.sqlite3"

    def transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": THREE_SECTION_ENCRYPTED},
        }

    with pytest.raises(ValueError) as raised:
        collect_trend_curves(
            watchlist,
            database=database,
            credentials=("fake-token", 123456789),
            transport=transport,
        )

    with sqlite3.connect(database) as connection:
        point_count = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_points"
        ).fetchone()[0]
    assert (str(raised.value), point_count) == (
        "Trend Animals curve payload is malformed",
        0,
    )


def test_collect_portfolio_fails_closed_before_network_when_mapping_missing(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.csv"
    portfolio.write_text(
        "market,asset_class,symbol,analysis_symbol,ai_eligible\n"
        "US,stock,ESTC,ESTC,true\n",
        encoding="utf-8",
    )
    database = tmp_path / "history.sqlite3"
    calls: list[bytes] = []

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        calls.append(body)
        raise AssertionError("transport must not be called")

    with pytest.raises(ValueError) as raised:
        collect_trend_curves(
            portfolio=portfolio,
            mappings_root=tmp_path / "empty-mappings",
            database=database,
            credentials=("fake-token", 123456789),
            transport=transport,
        )

    assert (str(raised.value), calls, database.exists()) == (
        "portfolio mapping unavailable: US.ESTC",
        [],
        False,
    )


def test_collect_stores_current_direct_curve_history(tmp_path: Path) -> None:
    encrypted = (
        "ehtRChN4vTYXmnU0XeI1jaN2N5o2ThNiIfS+zXGJzox7bRvAZYuv9MUA387xXPpIjQhnGOmqz3UhuXUsXaQcGGh4WhOZ41GlxnOMl75iigfXh5PagA1P1WsFVK6u40mtBXgXDTsY3WweVDJUwEIYLEI2Cc9IY5A0/qGhtK9W2Oe7syGf5m0TDbnSOMiiLX9QbDrkgfWb//Fthrz4Yhp8f2HQLuLubZlG/Nov6V4MjXcNwcWzRtBl3SjX1ClgR8Cr"
    )
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
    database = tmp_path / "history.sqlite3"

    def transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {"success": True, "code": "00000", "data": {"encryptedData": encrypted}}

    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=transport,
    )

    assert result.point_count == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT curve_date, price, temperature, strength FROM trend_curve_points "
            "ORDER BY curve_date"
        ).fetchall() == [
            ("2026-09-01", "53.21", "平", "71.1"),
            ("2026-09-02", "54.32", "温", "82.2"),
        ]


def test_collecting_same_curve_twice_is_idempotent(tmp_path: Path) -> None:
    expected_rows = [
        ("2026-08-26", "53.32", "平", "80.9"),
        ("2026-08-27", "54.73", "温", "87.3"),
    ]
    encrypted = (
        "ehtRChN4vTYXmnU0XeI1jUK90sU2F7krL05NDT98YL8UTnyA+ZPzfFhGtc16tZucM9tF40/L85yOWiLeeXjztZffM1BPIDr6srhTg+gA+8z6VhEKsU0ASQEFlmLlF9Drvc0vsuhYCADC1pOAev/+Rs/dJogO/oY+OIr2/M7G5h5J5z0E6SiEOiNxJz77UDYFaHhaE5njUaXGc4yXvmKKB3VZQBVYPZICa/J+876JL+7mURSFnxtbZXQ81ZOfFkEV"
    )
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
    database = tmp_path / "history.sqlite3"

    def transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {"success": True, "code": "00000", "data": {"encryptedData": encrypted}}

    collect_trend_curves(
        watchlist, database=database, credentials=("fake-token", 123456789), transport=transport
    )
    collect_trend_curves(
        watchlist, database=database, credentials=("fake-token", 123456789), transport=transport
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM trend_curve_points").fetchone()[0] == 2
        assert connection.execute(
            "SELECT curve_date, price, temperature, strength FROM trend_curve_points "
            "ORDER BY curve_date"
        ).fetchall() == expected_rows


def test_recollect_overwrites_only_matching_curve_date(tmp_path: Path) -> None:
    expected_rows = [
        ("2026-08-26", "53.32", "平", "80.9"),
        ("2026-08-27", "55.01", "热", "91.2"),
    ]
    baseline = (
        "ehtRChN4vTYXmnU0XeI1jUK90sU2F7krL05NDT98YL8UTnyA+ZPzfFhGtc16tZucM9tF40/L85yOWiLeeXjztZffM1BPIDr6srhTg+gA+8z6VhEKsU0ASQEFlmLlF9Drvc0vsuhYCADC1pOAev/+Rs/dJogO/oY+OIr2/M7G5h5J5z0E6SiEOiNxJz77UDYFaHhaE5njUaXGc4yXvmKKB3VZQBVYPZICa/J+876JL+7mURSFnxtbZXQ81ZOfFkEV"
    )
    revised = (
        "ehtRChN4vTYXmnU0XeI1jUK90sU2F7krL05NDT98YL8UTnyA+ZPzfFhGtc16tZucM9tF40/L85yOWiLeeXjztZffM1BPIDr6srhTg+gA+8z6VhEKsU0ASQEFlmLlF9Drvc0vsuhYCADC1pOAev/+Rs/dJogO/oY+OIr2/M7G5h5bQ/f4AkVmeyJcZcrBVmEMaHhaE5njUaXGc4yXvmKKBwTbs8eWoqgX9OLmcAPGRmy+PanrSQcWsNwPFEtM39YW"
    )
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
    database = tmp_path / "history.sqlite3"
    responses = [baseline, revised]

    def transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": responses.pop(0)},
        }

    collect_trend_curves(
        watchlist, database=database, credentials=("fake-token", 123456789), transport=transport
    )
    collect_trend_curves(
        watchlist, database=database, credentials=("fake-token", 123456789), transport=transport
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM trend_curve_points").fetchone()[0] == 2
        assert connection.execute(
            "SELECT curve_date, price, temperature, strength FROM trend_curve_points "
            "ORDER BY curve_date"
        ).fetchall() == expected_rows


def test_collect_default_transport_refuses_redirect_before_credentials_can_leave_exact_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    connections: list[dict[str, object]] = []
    requests: list[dict[str, object]] = []

    class RedirectResponse:
        status = 302
        headers = {"Location": "https://evil.example/steal"}

        def read(self) -> bytes:
            return b""

    class FakeHTTPSConnection:
        def __init__(self, host: str, *, timeout: int) -> None:
            connections.append({"host": host, "timeout": timeout})

        def request(
            self, method: str, path: str, *, body: bytes, headers: dict[str, str]
        ) -> None:
            requests.append({"method": method, "path": path, "body": body, "headers": headers})

        def getresponse(self) -> RedirectResponse:
            return RedirectResponse()

        def close(self) -> None:
            pass

    def reject_redirecting_transport(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("redirecting transport must not be used")

    monkeypatch.setattr(trend_curve_research, "HTTPSConnection", FakeHTTPSConnection)
    monkeypatch.setattr(trend_curve_research, "urlopen", reject_redirecting_transport, raising=False)
    database = tmp_path / "history.sqlite3"
    with pytest.raises(ValueError) as raised:
        collect_trend_curves(
            watchlist,
            database=database,
            credentials=("redirect-secret-token", 123456789),
        )

    message = str(raised.value)
    assert message == "Trend Animals curve request failed"
    assert "redirect-secret-token" not in message
    assert "123456789" not in message
    assert connections == [{"host": "www.trendtrader.cn", "timeout": 30}]
    assert [(request["method"], request["path"]) for request in requests] == [
        ("POST", "/mall4cloud_breed/breed/getVarietyCurve_V3")
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM trend_curve_points"
        ).fetchone()[0] == 0


def test_wechat_auth_reader_uses_temporary_snapshot_and_returns_credentials_only_in_memory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mmkv = tmp_path / "wx64e4edbab5e14356"
    crc = Path(f"{mmkv}.crc")
    original_mmkv = b"encrypted-mmkv-bytes"
    original_crc = b"crc-bytes"
    mmkv.write_bytes(original_mmkv)
    crc.write_bytes(original_crc)

    seen = tmp_path / "helper-seen.json"
    helper = tmp_path / "open-trader-mmkv-dump"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "pathlib.Path(os.environ['SEEN']).write_text(json.dumps({\n"
        "    'root': str(root), 'files': sorted(p.name for p in root.iterdir())\n"
        "}), encoding='utf-8')\n"
        "print('other\\tignored')\n"
        "print('vuex\\t' + json.dumps({'user': {'token': 'fake-token-123', 'info': {'id': 456789}}}))\n",
        encoding="utf-8",
    )
    helper.chmod(helper.stat().st_mode | 0o111)
    monkeypatch.setenv("SEEN", str(seen))

    credentials = read_wechat_mini_credentials(
        mmkv, helper_path=helper, app_id="wx64e4edbab5e14356"
    )

    assert credentials.token == "fake-token-123"
    assert credentials.user_id == 456789
    observed = json.loads(seen.read_text(encoding="utf-8"))
    assert observed["root"] != str(tmp_path)
    assert observed["root"] != str(mmkv.parent)
    assert observed["files"] == [mmkv.name, crc.name]
    assert not Path(observed["root"]).exists()
    assert mmkv.read_bytes() == original_mmkv
    assert crc.read_bytes() == original_crc
    captured = capsys.readouterr()
    assert "fake-token-123" not in captured.out
    assert "456789" not in captured.out
    assert "fake-token-123" not in seen.read_text(encoding="utf-8")


def test_wechat_auth_reader_accepts_current_wrapped_vuex_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mmkv = tmp_path / "wx64e4edbab5e14356"
    Path(f"{mmkv}.crc").write_bytes(b"crc-bytes")
    mmkv.write_bytes(b"encrypted-mmkv-bytes")
    helper = tmp_path / "open-trader-mmkv-dump"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print('vuex\\t' + json.dumps({'data': json.dumps({'user': {'token': 'wrapped-token-456', 'info': {'id': 789012}}}), 'dataType': 'String'}))\n",
        encoding="utf-8",
    )
    helper.chmod(helper.stat().st_mode | 0o111)

    credentials = read_wechat_mini_credentials(mmkv, helper_path=helper)

    assert credentials.token == "wrapped-token-456"
    assert credentials.user_id == 789012
    captured = capsys.readouterr()
    assert "wrapped-token-456" not in captured.out
    assert "789012" not in captured.out
    assert "wrapped-token-456" not in captured.err
    assert "789012" not in captured.err
