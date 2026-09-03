from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from open_trader import trend_curve_research
from open_trader.trend_curve_research import (
    collect_trend_curves,
    read_wechat_mini_credentials,
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
                "selected": 5,
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
