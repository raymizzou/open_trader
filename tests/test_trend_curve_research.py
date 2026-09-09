from __future__ import annotations

import json
import multiprocessing
from queue import Empty
import subprocess
import sqlite3
from datetime import datetime, timezone
from base64 import b64encode
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


def _encrypted_curve_payload(payload: dict[str, object]) -> str:
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-ecb",
            "-K",
            "41464433303434323736393838413830",
            "-nosalt",
        ],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    return b64encode(completed.stdout).decode("ascii")


def _supplier_timestamp(value: str) -> int:
    return int(
        datetime.fromisoformat(f"{value}T00:00:00+08:00")
        .astimezone(timezone.utc)
        .timestamp()
        * 1000
    )


def _supplier_payload(
    *,
    labels: list[str] | None = None,
    snapshot_date: str = "2026-09-02",
    trend: str = "立秋\n右侧第7天",
    include_snapshot: bool = True,
    history: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    points = history or [
        {
            "rq": _supplier_timestamp("2026-09-01"),
            "px": "1.11",
            "rps": "11.1",
            "temperature": "温",
            "mom": "1",
            "yoy": "2",
            "bar": "3",
            "momDelta": "u",
            "yoyDelta": "d",
            "yield": "0.031",
        },
        {
            "rq": _supplier_timestamp("2026-09-02"),
            "px": "1.22",
            "rps": "12.2",
            "temperature": "热",
            "mom": "4",
            "yoy": "5",
            "bar": "6",
            "momDelta": "u",
            "yoyDelta": "d",
            "yield": "0.031",
        },
    ]
    if not include_snapshot:
        return {"code": "00000", "data": [{}, {}, points, {}]}
    return {
        "code": "00000",
        "data": [
            [
                {"labelName": value}
                for value in (
                    ["开香槟", "危险信号", "右侧启动", "温转热"]
                    if labels is None
                    else labels
                )
            ],
            [{"rq": _supplier_timestamp(snapshot_date), "下行趋势": trend}],
            points,
            {},
        ],
    }


def _run_blocking_collection(
    watchlist: str,
    database: str,
    batch_id: str,
    release_connection: object,
    events: object,
    response: str,
) -> None:
    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        events.put(("request", target_id))
        if target_id == 102:
            events.put(("blocked", target_id))
            release_connection.recv()  # type: ignore[attr-defined]
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        }

    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=transport,
        batch_id=batch_id,
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    events.put(("result", result.status, result.completed_count, result.pending_count))


def test_collect_migrates_and_preserves_supplier_fields(tmp_path: Path) -> None:
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
    old_row = (
        "US",
        "ESTC",
        "2026-08-31",
        "9.75",
        "温",
        "89.1",
        "1.2",
        "-0.3",
        "0.04",
        10002,
        332171,
        334101,
        101,
        "keep-me",
    )
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
                user_extension TEXT,
                PRIMARY KEY (market, symbol, curve_date)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO trend_curve_points
            (market, symbol, curve_date, price, temperature, strength, mom, yoy, bar,
             asset_id, group_id, tm_id, ccy_id, user_extension)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            old_row,
        )

    response = _encrypted_curve_payload(_supplier_payload())

    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=lambda *_args: {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        },
    )

    with sqlite3.connect(database) as connection:
        old_row_after = connection.execute(
            """
            SELECT market, symbol, curve_date, price, temperature, strength, mom, yoy, bar,
                   asset_id, group_id, tm_id, ccy_id, user_extension
            FROM trend_curve_points WHERE curve_date = '2026-08-31'
            """
        ).fetchone()
        new_row = connection.execute(
            """
            SELECT curve_date, mom_delta, yoy_delta, yield_value
            FROM trend_curve_points WHERE curve_date = '2026-09-02'
            """
        ).fetchone()
        snapshot = connection.execute(
            """
            SELECT market, symbol, snapshot_date, solar_term, right_side_day,
                   right_side_state, labels_json
            FROM trend_curve_daily_snapshots
            """
        ).fetchone()
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]

    assert (
        result.snapshot_count,
        old_row_after,
        new_row,
        snapshot,
        table_names,
    ) == (
        1,
        old_row,
        ("2026-09-02", "u", "d", "0.031"),
        (
            "US",
            "ESTC",
            "2026-09-02",
            "立秋",
            7,
            None,
            '["开香槟","危险信号","右侧启动"]',
        ),
        [
            "trend_curve_batch_items",
            "trend_curve_batches",
            "trend_curve_daily_snapshots",
            "trend_curve_points",
        ],
    )


def test_collect_checkpoints_survive_auth_failure(tmp_path: Path) -> None:
    targets = [
        {
            "market": "US",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 332171,
            "tm_id": tm_id,
            "ccy_id": 101,
        }
        for symbol, tm_id in (("A", 101), ("B", 102), ("C", 103))
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    success = _encrypted_curve_payload(_supplier_payload())
    inner_auth = _encrypted_curve_payload({"code": "A00004"})
    requests: list[int] = []

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        requests.append(target_id)
        encrypted = success if target_id == 101 else inner_auth
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": encrypted},
        }

    database = tmp_path / "history.sqlite3"
    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=transport,
        batch_id="batch-auth",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
        observed_at=datetime(2026, 9, 2, 8, tzinfo=timezone.utc),
    )

    with sqlite3.connect(database) as connection:
        point_count = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_points"
        ).fetchone()[0]
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_daily_snapshots"
        ).fetchone()[0]
        completed = connection.execute(
            "SELECT market, symbol FROM trend_curve_batch_items "
            "WHERE batch_id = ? AND completed_at IS NOT NULL ORDER BY symbol",
            ("batch-auth",),
        ).fetchall()
        database_bytes = database.read_bytes()

    assert (
        requests,
        result.batch_id,
        result.status,
        result.point_count,
        result.snapshot_count,
        result.completed_count,
        result.pending_count,
        [(issue["market"], issue["symbol"], issue["reason"]) for issue in result.issues],
        point_count,
        snapshot_count,
        completed,
        b"fake-token" in database_bytes,
        b"123456789" in database_bytes,
    ) == (
        [101, 102],
        "batch-auth",
        "auth_blocked",
        2,
        1,
        1,
        2,
        [("US", "B", "auth_blocked")],
        2,
        1,
        [("US", "A")],
        False,
        False,
    )

    outer_requests: list[int] = []

    def outer_auth_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        outer_requests.append(target_id)
        if target_id == 101:
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": success},
            }
        return {"success": False, "code": "A00004", "data": None}

    outer_database = tmp_path / "outer-auth.sqlite3"
    outer_result = collect_trend_curves(
        watchlist,
        database=outer_database,
        credentials=("fake-token", 123456789),
        transport=outer_auth_transport,
        batch_id="batch-outer-auth",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    assert (
        outer_requests,
        outer_result.status,
        outer_result.completed_count,
        outer_result.pending_count,
    ) == ([101, 102], "auth_blocked", 1, 2)


def test_collect_resumes_only_unfinished_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = [
        {
            "market": "US",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 332171,
            "tm_id": tm_id,
            "ccy_id": 101,
        }
        for symbol, tm_id in (("A", 101), ("B", 102), ("C", 103))
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    success = _encrypted_curve_payload(_supplier_payload())
    auth = _encrypted_curve_payload({"code": "A00004"})
    database = tmp_path / "history.sqlite3"
    first_requests: list[int] = []

    def first_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        first_requests.append(target_id)
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": success if target_id == 101 else auth},
        }

    collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=first_transport,
        batch_id="batch-resume",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )

    resumed_requests: list[int] = []

    def healthy_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        resumed_requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": success},
        }

    resumed = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=healthy_transport,
        batch_id="batch-resume",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    assert (first_requests, resumed_requests, resumed.status, resumed.completed_count, resumed.pending_count) == (
        [101, 102],
        [102, 103],
        "complete",
        3,
        0,
    )

    def no_credentials() -> object:
        raise AssertionError("completed batch must not read credentials")

    monkeypatch.setattr(trend_curve_research, "read_wechat_mini_credentials", no_credentials)

    def no_transport(*_args: object) -> object:
        raise AssertionError("completed batch must not request HTTP")

    completed = collect_trend_curves(
        watchlist,
        database=database,
        credentials=None,
        transport=no_transport,
        batch_id="batch-resume",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    assert (completed.status, completed.completed_count, completed.pending_count) == (
        "complete",
        3,
        0,
    )

    new_batch_requests: list[int] = []

    def new_batch_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        new_batch_requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": success},
        }

    new_batch = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("new-token", 123456789),
        transport=new_batch_transport,
        batch_id="batch-new",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    assert (new_batch_requests, new_batch.status, new_batch.completed_count) == (
        [101, 102, 103],
        "complete",
        3,
    )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM trend_curve_points WHERE market = 'US' AND symbol = 'A'"
        )

    repaired_requests: list[int] = []

    def repair_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        repaired_requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": success},
        }

    repaired = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=repair_transport,
        batch_id="batch-resume",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    with sqlite3.connect(database) as connection:
        counts = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_points"
        ).fetchone()[0]
        snapshots = connection.execute(
            "SELECT COUNT(*) FROM trend_curve_daily_snapshots"
        ).fetchone()[0]
    assert (
        repaired_requests,
        repaired.status,
        repaired.completed_count,
        repaired.pending_count,
        counts,
        snapshots,
    ) == ([101], "complete", 3, 0, 6, 3)


def test_collect_write_failure_rolls_back_only_current_target(tmp_path: Path) -> None:
    targets = [
        {
            "market": "US",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 332171,
            "tm_id": tm_id,
            "ccy_id": 101,
        }
        for symbol, tm_id in (("A", 101), ("B", 102), ("C", 103))
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    response = _encrypted_curve_payload(_supplier_payload())
    requests: list[int] = []
    database = tmp_path / "history.sqlite3"

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        target_id = json.loads(body)["id"]
        requests.append(target_id)
        if target_id == 102:
            with sqlite3.connect(database) as trigger_connection:
                trigger_connection.execute(
                    """
                    CREATE TRIGGER reject_b_snapshot
                    BEFORE INSERT ON trend_curve_daily_snapshots
                    WHEN NEW.symbol = 'B'
                    BEGIN
                        SELECT RAISE(ABORT, 'reject B snapshot');
                    END
                    """
                )
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        }

    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=transport,
        batch_id="batch-write-failure",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT market, symbol, curve_date FROM trend_curve_points "
            "ORDER BY symbol, curve_date"
        ).fetchall()
        snapshots = connection.execute(
            "SELECT market, symbol, snapshot_date FROM trend_curve_daily_snapshots"
        ).fetchall()
        items = connection.execute(
            "SELECT symbol, completed_at, issue_reason FROM trend_curve_batch_items "
            "WHERE batch_id = ? ORDER BY symbol",
            ("batch-write-failure",),
        ).fetchall()
        database_bytes = database.read_bytes()

    assert (
        requests,
        result.status,
        result.point_count,
        result.snapshot_count,
        result.completed_count,
        result.pending_count,
        [(issue["symbol"], issue["reason"]) for issue in result.issues],
        rows,
        snapshots,
        [(symbol, completed_at is not None, issue_reason) for symbol, completed_at, issue_reason in items],
        b"fake-token" in database_bytes,
        b"123456789" in database_bytes,
    ) == (
        [101, 102],
        "partial",
        2,
        1,
        1,
        2,
        [("B", "persistence_failed")],
        [
            ("US", "A", "2026-09-01"),
            ("US", "A", "2026-09-02"),
        ],
        [("US", "A", "2026-09-02")],
        [("A", True, None), ("B", False, None), ("C", False, None)],
        False,
        False,
    )


def test_collect_resume_rejects_changed_frozen_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = [
        {
            "market": "US",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 332171,
            "tm_id": tm_id,
            "ccy_id": 101,
        }
        for symbol, tm_id in (("A", 101), ("B", 102))
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    response = _encrypted_curve_payload(_supplier_payload())
    database = tmp_path / "history.sqlite3"

    def transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        }

    collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=transport,
        batch_id="frozen-batch",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    baseline = database.read_bytes()

    def no_credentials() -> object:
        raise AssertionError("frozen request mismatch must precede credentials")

    monkeypatch.setattr(trend_curve_research, "read_wechat_mini_credentials", no_credentials)

    def no_transport(*_args: object) -> object:
        raise AssertionError("frozen request mismatch must precede HTTP")

    variants = [
        (
            "target-set",
            [targets[0]],
            True,
            {"US": "2026-09-02"},
        ),
        (
            "provider-id",
            [{**target, "tm_id": target["tm_id"] + 1000} for target in targets],
            True,
            {"US": "2026-09-02"},
        ),
        (
            "currency",
            [{**target, "ccy_id": 104} for target in targets],
            True,
            {"US": "2026-09-02"},
        ),
        (
            "snapshot-mode",
            targets,
            False,
            {"US": "2026-09-02"},
        ),
        (
            "expected-date",
            targets,
            True,
            {"US": "2026-09-03"},
        ),
    ]
    for name, changed_targets, mode, dates in variants:
        changed = tmp_path / f"{name}.json"
        changed.write_text(json.dumps(changed_targets), encoding="utf-8")
        with pytest.raises(ValueError):
            collect_trend_curves(
                changed,
                database=database,
                credentials=None,
                transport=no_transport,
                batch_id="frozen-batch",
                require_snapshot=mode,
                expected_dates=dates,
            )
        assert database.read_bytes() == baseline

    reordered = tmp_path / "reordered.json"
    reordered.write_text(json.dumps(list(reversed(targets))), encoding="utf-8")
    allowed = collect_trend_curves(
        reordered,
        database=database,
        credentials=None,
        transport=no_transport,
        batch_id="frozen-batch",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    assert (allowed.status, allowed.completed_count, allowed.pending_count) == (
        "complete",
        2,
        0,
    )

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps([targets[0], targets[0]]), encoding="utf-8")
    with pytest.raises(ValueError):
        collect_trend_curves(
            duplicate,
            database=database,
            credentials=None,
            transport=no_transport,
            batch_id="duplicate-batch",
            expected_dates={"US": "2026-09-02"},
        )
    assert database.read_bytes() == baseline

    for suffix, supplied_dates in (
        ("missing-date", {"CN": "2026-09-02"}),
        ("invalid-date", {"US": "2026-9-2"}),
    ):
        invalid = tmp_path / f"{suffix}.json"
        invalid.write_text(json.dumps(targets), encoding="utf-8")
        with pytest.raises(ValueError):
            collect_trend_curves(
                invalid,
                database=database,
                credentials=None,
                transport=no_transport,
                batch_id=f"{suffix}-batch",
                expected_dates=supplied_dates,
            )
        assert database.read_bytes() == baseline


def test_daily_collection_keeps_successes_and_reports_signal_gaps(tmp_path: Path) -> None:
    targets = [
        {
            "market": market,
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": group_id,
            "tm_id": tm_id,
            "ccy_id": ccy_id,
        }
        for market, symbol, group_id, tm_id, ccy_id in (
            ("CN", "A", 303121, 101, 100),
            ("HK", "B", 329480, 102, 104),
            ("US", "C", 332171, 103, 101),
        )
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    expected_dates = {
        "CN": "2026-09-02",
        "HK": "2026-09-02",
        "US": "2026-09-03",
    }
    observed_at = datetime(2026, 9, 3, 8, 30, tzinfo=timezone.utc)

    scenarios = (
        ("missing", None, "snapshot_missing"),
        (
            "wrong-day",
            _supplier_payload(
                snapshot_date="2026-09-03",
                history=[
                    {
                        "rq": _supplier_timestamp("2026-09-02"),
                        "px": "1.11",
                        "rps": "11.1",
                        "temperature": "温",
                    },
                    {
                        "rq": _supplier_timestamp("2026-09-03"),
                        "px": "1.22",
                        "rps": "12.2",
                        "temperature": "热",
                    }
                ],
            ),
            "snapshot_date_mismatch",
        ),
        ("malformed", {"code": "00000", "data": [{}, {}, [], {}]}, "data_gap"),
    )

    for scenario, b_payload, expected_reason in scenarios:
        database = tmp_path / f"{scenario}.sqlite3"
        a_response = _encrypted_curve_payload(
            _supplier_payload(labels=[])
        )
        c_response = _encrypted_curve_payload(
            _supplier_payload(
                snapshot_date="2026-09-03",
                history=[
                    {
                        "rq": _supplier_timestamp("2026-09-02"),
                        "px": "1.11",
                        "rps": "11.1",
                        "temperature": "温",
                    },
                    {
                        "rq": _supplier_timestamp("2026-09-03"),
                        "px": "1.22",
                        "rps": "12.2",
                        "temperature": "热",
                    },
                ],
            )
        )
        b_response = (
            _encrypted_curve_payload({"code": "00000", "data": [{}, {}, [], {}]})
            if scenario == "malformed"
            else (
                _encrypted_curve_payload(_supplier_payload(include_snapshot=False))
                if b_payload is None
                else _encrypted_curve_payload(b_payload)
            )
        )
        requests: list[int] = []

        def transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            target_id = json.loads(body)["id"]
            requests.append(target_id)
            response_by_id = {101: a_response, 102: b_response, 103: c_response}
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": response_by_id[target_id]},
            }

        result = collect_trend_curves(
            watchlist,
            database=database,
            credentials=("fake-token", 123456789),
            transport=transport,
            batch_id=f"daily-{scenario}",
            require_snapshot=True,
            expected_dates=expected_dates,
            observed_at=observed_at,
        )

        with sqlite3.connect(database) as connection:
            point_counts = connection.execute(
                "SELECT market, symbol, COUNT(*) FROM trend_curve_points "
                "GROUP BY market, symbol ORDER BY market"
            ).fetchall()
            snapshots = connection.execute(
                "SELECT market, symbol, snapshot_date, labels_json, observed_at "
                "FROM trend_curve_daily_snapshots ORDER BY market"
            ).fetchall()
            items = connection.execute(
                "SELECT market, symbol, expected_date, completed_at, observed_at "
                "FROM trend_curve_batch_items WHERE batch_id = ? ORDER BY market",
                (f"daily-{scenario}",),
            ).fetchall()

        assert requests == [101, 102, 103]
        assert (
            result.status,
            result.completed_count,
            result.pending_count,
            [(issue["symbol"], issue["reason"]) for issue in result.issues],
            point_counts,
        ) == (
            "partial",
            2,
            1,
            [("B", expected_reason)],
            [
                ("CN", "A", 2),
                ("US", "C", 2),
            ]
            if scenario == "malformed"
            else [
                ("CN", "A", 2),
                ("HK", "B", 2),
                ("US", "C", 2),
            ],
        )
        assert [(market, symbol, expected_date) for market, symbol, expected_date, *_ in items] == [
            ("CN", "A", "2026-09-02"),
            ("HK", "B", "2026-09-02"),
            ("US", "C", "2026-09-03"),
        ]
        assert all(
            observed == observed_at.isoformat()
            for _, _, _, completed, observed in items
            if completed is not None
        )
        assert snapshots[0][3] == "[]"
        assert all(snapshot[4] == observed_at.isoformat() for snapshot in snapshots)
        if scenario == "missing":
            assert [(row[0], row[1], row[2]) for row in snapshots] == [
                ("CN", "A", "2026-09-02"),
                ("US", "C", "2026-09-03"),
            ]
        elif scenario == "wrong-day":
            assert ("HK", "B", "2026-09-02") not in [
                (row[0], row[1], row[2]) for row in snapshots
            ]
            assert ("HK", "B", "2026-09-03") in [
                (row[0], row[1], row[2]) for row in snapshots
            ]


def test_collect_process_restart_releases_lock_and_resumes(tmp_path: Path) -> None:
    targets = [
        {
            "market": "US",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 332171,
            "tm_id": tm_id,
            "ccy_id": 101,
        }
        for symbol, tm_id in (("A", 101), ("B", 102))
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    database = tmp_path / "history.sqlite3"
    response = _encrypted_curve_payload(_supplier_payload())
    release_parent, release_child = multiprocessing.Pipe()
    events = multiprocessing.Queue()
    child = multiprocessing.get_context("fork").Process(
        target=_run_blocking_collection,
        args=(
            str(watchlist),
            str(database),
            "restart-batch",
            release_child,
            events,
            response,
        ),
    )
    child.start()
    try:
        assert events.get(timeout=5) == ("request", 101)
        assert events.get(timeout=5) == ("request", 102)
        assert events.get(timeout=5) == ("blocked", 102)

        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM trend_curve_points WHERE symbol = 'A'"
            ).fetchone()[0] == 2

        second_requests: list[int] = []

        def concurrent_transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            second_requests.append(json.loads(body)["id"])
            raise AssertionError("concurrent collector must fail before HTTP")

        with pytest.raises(ValueError, match="already running"):
            collect_trend_curves(
                watchlist,
                database=database,
                credentials=("fake-token", 123456789),
                transport=concurrent_transport,
                batch_id="restart-batch",
                require_snapshot=True,
                expected_dates={"US": "2026-09-02"},
            )
        assert second_requests == []
    finally:
        child.terminate()
        child.join(timeout=5)
        release_parent.close()
        release_child.close()
        events.close()
        events.join_thread()

    assert not child.is_alive()
    resumed_requests: list[int] = []

    def resumed_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        resumed_requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        }

    result = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("fake-token", 123456789),
        transport=resumed_transport,
        batch_id="restart-batch",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    with sqlite3.connect(database) as connection:
        counts = connection.execute(
            "SELECT symbol, COUNT(*) FROM trend_curve_points GROUP BY symbol ORDER BY symbol"
        ).fetchall()
        snapshots = connection.execute(
            "SELECT symbol, COUNT(*) FROM trend_curve_daily_snapshots GROUP BY symbol ORDER BY symbol"
        ).fetchall()
    assert (
        resumed_requests,
        result.status,
        result.completed_count,
        result.pending_count,
        counts,
        snapshots,
    ) == ([102], "complete", 2, 0, [("A", 2), ("B", 2)], [("A", 1), ("B", 1)])


def test_collect_damaged_completion_stays_pending_after_failed_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    targets = [
        {
            "market": "US",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 332171,
            "tm_id": tm_id,
            "ccy_id": 101,
        }
        for symbol, tm_id in (("A", 101), ("B", 102), ("C", 103))
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    response = _encrypted_curve_payload(_supplier_payload())
    auth_response = _encrypted_curve_payload({"code": "A00004"})

    def successful_transport(
        _url: str, _body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": response},
        }

    def no_credentials(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verified batch must not read credentials")

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", no_credentials
    )

    for variant in ("corrupt-point", "missing-item"):
        database = tmp_path / f"{variant}.sqlite3"
        batch_id = f"damaged-{variant}"
        collect_trend_curves(
            watchlist,
            database=database,
            credentials=("fake-token", 123456789),
            transport=successful_transport,
            batch_id=batch_id,
            require_snapshot=True,
            expected_dates={"US": "2026-09-02"},
        )
        with sqlite3.connect(database) as connection:
            bc_rows_before = connection.execute(
                "SELECT market, symbol, curve_date, price, temperature, strength "
                "FROM trend_curve_points WHERE symbol IN ('B', 'C') "
                "ORDER BY symbol, curve_date"
            ).fetchall()
            if variant == "corrupt-point":
                connection.execute(
                    "UPDATE trend_curve_points SET price = 'corrupted' "
                    "WHERE market = 'US' AND symbol = 'A' AND curve_date = '2026-09-01'"
                )
            else:
                connection.execute(
                    "DELETE FROM trend_curve_batch_items "
                    "WHERE batch_id = ? AND market = 'US' AND symbol = 'A'",
                    (batch_id,),
                )

        auth_requests: list[int] = []

        def auth_transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            target_id = json.loads(body)["id"]
            auth_requests.append(target_id)
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": auth_response},
            }

        blocked = collect_trend_curves(
            watchlist,
            database=database,
            credentials=("fake-token", 123456789),
            transport=auth_transport,
            batch_id=batch_id,
            require_snapshot=True,
            expected_dates={"US": "2026-09-02"},
        )
        with sqlite3.connect(database) as connection:
            bc_rows_after_block = connection.execute(
                "SELECT market, symbol, curve_date, price, temperature, strength "
                "FROM trend_curve_points WHERE symbol IN ('B', 'C') "
                "ORDER BY symbol, curve_date"
            ).fetchall()
        assert (
            auth_requests,
            blocked.status,
            blocked.target_count,
            blocked.completed_count,
            blocked.pending_count,
            [(issue["symbol"], issue["reason"]) for issue in blocked.issues],
            bc_rows_after_block,
        ) == (
            [101],
            "auth_blocked",
            3,
            2,
            1,
            [("A", "auth_blocked")],
            bc_rows_before,
        )

        repaired_requests: list[int] = []

        def repair_transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            repaired_requests.append(json.loads(body)["id"])
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": response},
            }

        repaired = collect_trend_curves(
            watchlist,
            database=database,
            credentials=("fake-token", 123456789),
            transport=repair_transport,
            batch_id=batch_id,
            require_snapshot=True,
            expected_dates={"US": "2026-09-02"},
        )
        with sqlite3.connect(database) as connection:
            item_count = connection.execute(
                "SELECT COUNT(*) FROM trend_curve_batch_items WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0]
            repaired_a = connection.execute(
                "SELECT price FROM trend_curve_points "
                "WHERE market = 'US' AND symbol = 'A' AND curve_date = '2026-09-01'"
            ).fetchone()[0]
            bc_rows_after_repair = connection.execute(
                "SELECT market, symbol, curve_date, price, temperature, strength "
                "FROM trend_curve_points WHERE symbol IN ('B', 'C') "
                "ORDER BY symbol, curve_date"
            ).fetchall()
        assert (
            repaired_requests,
            repaired.status,
            repaired.completed_count,
            repaired.pending_count,
            item_count,
            repaired_a,
            bc_rows_after_repair,
        ) == ([101], "complete", 3, 0, 3, "1.11", bc_rows_before)

        def no_transport(*_args: object) -> object:
            raise AssertionError("verified batch must not request HTTP")

        completed = collect_trend_curves(
            watchlist,
            database=database,
            credentials=None,
            transport=no_transport,
            batch_id=batch_id,
            require_snapshot=True,
            expected_dates={"US": "2026-09-02"},
        )
        assert (completed.status, completed.completed_count, completed.pending_count) == (
            "complete",
            3,
            0,
        )
        database_bytes = database.read_bytes()
        captured = capsys.readouterr()
        assert not any(
            secret in database_bytes
            for secret in (b"fake-token", b"123456789", b"A00004")
        )
        assert all(
            secret not in captured.out
            for secret in ("fake-token", "123456789", "A00004")
        )


def test_collect_old_batch_ignores_later_unrelated_curve_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = {
        "market": "US",
        "symbol": "A",
        "asset_id": 10002,
        "group_id": 332171,
        "tm_id": 101,
        "ccy_id": 101,
    }
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps([target]), encoding="utf-8")
    old_history = [
        {
            "rq": _supplier_timestamp("2026-09-01"),
            "px": "1.11",
            "rps": "11.1",
            "temperature": "温",
            "mom": "1",
            "yoy": "2",
            "bar": "3",
            "momDelta": "u",
            "yoyDelta": "d",
            "yield": "0.031",
        },
        {
            "rq": _supplier_timestamp("2026-09-02"),
            "px": "1.22",
            "rps": "12.2",
            "temperature": "热",
            "mom": "4",
            "yoy": "5",
            "bar": "6",
            "momDelta": "u",
            "yoyDelta": "d",
            "yield": "0.031",
        },
    ]
    later_history = [
        dict(old_history[1]),
        {
            "rq": _supplier_timestamp("2026-09-03"),
            "px": "1.33",
            "rps": "13.3",
            "temperature": "平",
            "mom": "7",
            "yoy": "8",
            "bar": "9",
            "momDelta": "d",
            "yoyDelta": "u",
            "yield": "0.032",
        },
    ]
    old_response = _encrypted_curve_payload(
        _supplier_payload(history=old_history, snapshot_date="2026-09-02")
    )
    later_response = _encrypted_curve_payload(
        _supplier_payload(history=later_history, snapshot_date="2026-09-03")
    )
    database = tmp_path / "history.sqlite3"

    def collect_response(response: str):
        def transport(
            _url: str, _body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": response},
            }

        return transport

    collect_trend_curves(
        watchlist,
        database=database,
        credentials=("old-token", 123456789),
        transport=collect_response(old_response),
        batch_id="old-batch",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    collect_trend_curves(
        watchlist,
        database=database,
        credentials=("later-token", 123456789),
        transport=collect_response(later_response),
        batch_id="later-batch",
        require_snapshot=True,
        expected_dates={"US": "2026-09-03"},
    )
    before_replay = database.read_bytes()

    def no_credentials(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("intact old batch must not read credentials")

    monkeypatch.setattr(
        trend_curve_research, "read_wechat_mini_credentials", no_credentials
    )

    def no_transport(*_args: object) -> object:
        raise AssertionError("intact old batch must not request HTTP")

    replayed = collect_trend_curves(
        watchlist,
        database=database,
        credentials=None,
        transport=no_transport,
        batch_id="old-batch",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    with sqlite3.connect(database) as connection:
        point_dates = connection.execute(
            "SELECT curve_date FROM trend_curve_points "
            "WHERE market = 'US' AND symbol = 'A' ORDER BY curve_date"
        ).fetchall()
        snapshot_dates = connection.execute(
            "SELECT snapshot_date FROM trend_curve_daily_snapshots "
            "WHERE market = 'US' AND symbol = 'A' ORDER BY snapshot_date"
        ).fetchall()
    assert (
        replayed.status,
        replayed.completed_count,
        replayed.pending_count,
        point_dates,
        snapshot_dates,
        database.read_bytes() == before_replay,
    ) == (
        "complete",
        1,
        0,
        [("2026-09-01",), ("2026-09-02",), ("2026-09-03",)],
        [("2026-09-02",), ("2026-09-03",)],
        True,
    )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE trend_curve_points SET price = 'corrupted' "
            "WHERE market = 'US' AND symbol = 'A' AND curve_date = '2026-09-01'"
        )
    repair_requests: list[int] = []

    def repair_transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        repair_requests.append(json.loads(body)["id"])
        return {
            "success": True,
            "code": "00000",
            "data": {"encryptedData": old_response},
        }

    repaired = collect_trend_curves(
        watchlist,
        database=database,
        credentials=("old-token", 123456789),
        transport=repair_transport,
        batch_id="old-batch",
        require_snapshot=True,
        expected_dates={"US": "2026-09-02"},
    )
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT curve_date, price FROM trend_curve_points "
            "WHERE market = 'US' AND symbol = 'A' ORDER BY curve_date"
        ).fetchall()
        snapshot_dates_after_repair = connection.execute(
            "SELECT snapshot_date FROM trend_curve_daily_snapshots "
            "WHERE market = 'US' AND symbol = 'A' ORDER BY snapshot_date"
        ).fetchall()
    assert (
        repair_requests,
        repaired.status,
        repaired.completed_count,
        repaired.pending_count,
        rows,
        snapshot_dates_after_repair,
    ) == (
        [101],
        "complete",
        1,
        0,
        [("2026-09-01", "1.11"), ("2026-09-02", "1.22"), ("2026-09-03", "1.33")],
        [("2026-09-02",), ("2026-09-03",)],
    )


def test_collect_requires_snapshot_without_batch(tmp_path: Path) -> None:
    targets = [
        {
            "market": "US",
            "symbol": symbol,
            "asset_id": 10002,
            "group_id": 332171,
            "tm_id": tm_id,
            "ccy_id": 101,
        }
        for symbol, tm_id in (("A", 101), ("B", 102))
    ]
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps(targets), encoding="utf-8")
    response_a = _encrypted_curve_payload(_supplier_payload())
    response_missing = _encrypted_curve_payload(
        _supplier_payload(include_snapshot=False)
    )
    response_wrong_day = _encrypted_curve_payload(
        _supplier_payload(
            history=[
                {
                    "rq": _supplier_timestamp("2026-09-02"),
                    "px": "1.22",
                    "rps": "12.2",
                    "temperature": "热",
                    "mom": "4",
                    "yoy": "5",
                    "bar": "6",
                    "momDelta": "u",
                    "yoyDelta": "d",
                    "yield": "0.031",
                },
                {
                    "rq": _supplier_timestamp("2026-09-03"),
                    "px": "1.33",
                    "rps": "13.3",
                    "temperature": "平",
                    "mom": "7",
                    "yoy": "8",
                    "bar": "9",
                    "momDelta": "d",
                    "yoyDelta": "u",
                    "yield": "0.032",
                },
            ],
            snapshot_date="2026-09-03",
        )
    )

    for variant, response_b in (
        ("missing", response_missing),
        ("wrong-day", response_wrong_day),
    ):
        database = tmp_path / f"no-batch-{variant}.sqlite3"
        requests: list[int] = []

        def transport(
            _url: str, body: bytes, _headers: dict[str, str]
        ) -> dict[str, object]:
            target_id = json.loads(body)["id"]
            requests.append(target_id)
            response = response_a if target_id == 101 else response_b
            return {
                "success": True,
                "code": "00000",
                "data": {"encryptedData": response},
            }

        with pytest.raises(ValueError, match="snapshot"):
            collect_trend_curves(
                watchlist,
                database=database,
                credentials=("fake-token", 123456789),
                transport=transport,
                require_snapshot=True,
                expected_dates={"US": "2026-09-02"},
            )

        with sqlite3.connect(database) as connection:
            a_points = connection.execute(
                "SELECT COUNT(*) FROM trend_curve_points "
                "WHERE market = 'US' AND symbol = 'A'"
            ).fetchone()[0]
            a_snapshot = connection.execute(
                "SELECT snapshot_date FROM trend_curve_daily_snapshots "
                "WHERE market = 'US' AND symbol = 'A'"
            ).fetchone()
            b_expected_snapshot = connection.execute(
                "SELECT COUNT(*) FROM trend_curve_daily_snapshots "
                "WHERE market = 'US' AND symbol = 'B' AND snapshot_date = '2026-09-02'"
            ).fetchone()[0]
        assert (requests, a_points, a_snapshot, b_expected_snapshot) == (
            [101, 102],
            2,
            ("2026-09-02",),
            0,
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
            "mom_delta", "yoy_delta", "yield_value",
        ]
        assert connection.execute(
            "SELECT market, symbol, curve_date, price, temperature, strength, mom, yoy, bar, "
            "asset_id, group_id, tm_id, ccy_id FROM trend_curve_points "
            "ORDER BY market, symbol, curve_date"
        ).fetchall() == expected_rows
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [
            ("trend_curve_batch_items",),
            ("trend_curve_batches",),
            ("trend_curve_daily_snapshots",),
            ("trend_curve_points",),
        ]
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    exclusion_file = tmp_path / "config" / "trend_curve_portfolio_exclusions.json"
    exclusion_file.parent.mkdir(parents=True)
    exclusion_file.write_text(
        json.dumps(
            {
                "US.AGRZ": (
                    "Trend Animals history begins with a point missing rps; "
                    "the user chose exclusion on 2026-09-03."
                )
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
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


def test_collect_portfolio_fails_closed_when_exclusion_file_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    exclusion_file = tmp_path / "config" / "trend_curve_portfolio_exclusions.json"
    exclusion_file.parent.mkdir(parents=True)
    exclusion_file.write_text("{not-json", encoding="utf-8")
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
                "trend_animals_symbol": "ESTC",
                "trend_animals_tm_id": 334101,
            }
        ),
        encoding="utf-8",
    )
    calls: list[bytes] = []

    def transport(
        _url: str, body: bytes, _headers: dict[str, str]
    ) -> dict[str, object]:
        calls.append(body)
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

    assert (str(raised.value), calls, database.exists()) == (
        "portfolio trend-curve exclusions are unreadable or malformed",
        [],
        False,
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
