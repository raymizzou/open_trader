from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from open_trader.trend_allocation import (
    ROOT_FIELDS,
    build_allocation_snapshot,
    fetch_allocation_roots,
    load_allocation_reference,
    write_allocation_snapshot,
)
from open_trader.daily_premarket import DailyPremarketConfig, NotificationAttempt
import open_trader.trend_allocation as trend_allocation
from open_trader.trend_animals import TrendAnimalsError


def root_rows(
    *,
    cn: tuple[str, str] = ("62.7", "58.3"),
    hk: tuple[str, str] = ("78.4", "75.0"),
    us: tuple[str, str] = ("80.0", "95.2"),
) -> dict[str, object]:
    return {
        "CN": {
            "stock": {"asset": "A股", "tm_id": 1, "as_of_date": "2026-08-03", "global_strength": cn[0]},
            "etf": {"asset": "ETF基金", "tm_id": 2, "as_of_date": "2026-08-03", "global_strength": cn[1]},
        },
        "HK": {
            "stock": {"asset": "港股", "tm_id": 3, "as_of_date": "2026-08-03", "global_strength": hk[0]},
            "etf": {"asset": "香港ETF", "tm_id": 4, "as_of_date": "2026-08-03", "global_strength": hk[1]},
        },
        "US": {
            "stock": {"asset": "美股", "tm_id": 5, "as_of_date": "2026-08-02", "global_strength": us[0]},
            "etf": {"asset": "美国ETF", "tm_id": 6, "as_of_date": "2026-08-02", "global_strength": us[1]},
        },
    }


def snapshot(*, roots: dict[str, object] | None = None, previous: dict[str, object] | None = None) -> dict[str, object]:
    return build_allocation_snapshot(
        allocation_date="2026-08-03",
        generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40,
        roots=roots or root_rows(),
        previous=previous,
    )


def test_build_allocation_snapshot_ranks_by_the_stronger_root() -> None:
    result = snapshot()

    assert result["markets"] == {
        "US": {"rank": 1, "score": "95.2", "score_source": "美国ETF", "entry_weight": "0.06", "nominal_weight": "0.60"},
        "HK": {"rank": 2, "score": "78.4", "score_source": "港股", "entry_weight": "0.04", "nominal_weight": "0.40"},
        "CN": {"rank": 3, "score": "62.7", "score_source": "A股", "entry_weight": "0.02", "nominal_weight": "0.20"},
    }


def test_build_allocation_snapshot_uses_secondary_root_then_previous_order() -> None:
    roots = root_rows(cn=("90", "70"), hk=("90", "80"), us=("30", "20"))
    assert list(snapshot(roots=roots)["markets"]) == ["HK", "CN", "US"]

    tied = root_rows(cn=("90", "80"), hk=("90", "80"), us=("30", "20"))
    previous = snapshot(roots=root_rows(cn=("80", "70"), hk=("90", "60"), us=("30", "20")))
    assert list(snapshot(roots=tied, previous=previous)["markets"]) == ["HK", "CN", "US"]
    with pytest.raises(TrendAnimalsError, match="tie"):
        snapshot(roots=tied)
    with pytest.raises(TrendAnimalsError, match="schema|mapping"):
        snapshot(roots=tied, previous={"markets": {"CN": {"rank": 1}}})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda roots: roots.pop("CN"),
        lambda roots: roots["CN"].__setitem__("duplicate", roots["CN"]["stock"]),
        lambda roots: roots["CN"]["stock"].__setitem__("global_strength", "NaN"),
        lambda roots: roots["CN"]["stock"].__setitem__("global_strength", "invalid"),
    ],
)
def test_build_allocation_snapshot_rejects_invalid_six_root_input(mutate: object) -> None:
    roots = root_rows()
    mutate(roots)  # type: ignore[operator]
    with pytest.raises(TrendAnimalsError):
        snapshot(roots=roots)


def test_fetch_allocation_roots_uses_exact_assets_dates_and_minimal_batched_fields() -> None:
    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[list[int], tuple[str, ...], str]] = []

        def get_update_status(self) -> list[dict[str, object]]:
            return [
                {"asset": asset, "asOfDate": date}
                for asset, date in (("A股", "2026-08-03"), ("ETF基金", "2026-08-03"), ("港股", "2026-08-03"), ("香港ETF", "2026-08-03"), ("美股", "2026-08-02"), ("美国ETF", "2026-08-02"))
            ]

        def get_favorites_tickers(self) -> list[dict[str, object]]:
            return [
                {"tmId": index, "tickername": asset, "asset": asset}
                for index, asset in enumerate(("A股", "ETF基金", "港股", "香港ETF", "美股", "美国ETF"), 1)
            ] + [
                {"tmId": 98, "tickername": "平安银行", "asset": "A股"},
                {"tmId": 99, "tickername": "ignore", "asset": "行业"},
            ]

        def get_snapshots(self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str) -> list[dict[str, object]]:
            self.calls.append((tm_ids, fields, expected_date))
            assets = ("A股", "ETF基金", "港股", "香港ETF", "美股", "美国ETF")
            return [{"tmId": ident, "tickerName": assets[ident - 1], "asset": assets[ident - 1], "asOfDate": expected_date, "trendStrengthGlobalCurr": str(ident)} for ident in tm_ids]

    api = Api()
    roots = fetch_allocation_roots(api)

    assert roots["CN"]["stock"]["tm_id"] == 1
    assert api.calls == [([1, 2, 3, 4], ROOT_FIELDS, "2026-08-03"), ([5, 6], ROOT_FIELDS, "2026-08-02")]


def test_write_load_is_idempotent_and_reports_stale_metadata(tmp_path: Path) -> None:
    first = snapshot()
    reference = write_allocation_snapshot(tmp_path, first)
    assert write_allocation_snapshot(tmp_path, first) == reference
    assert reference["daily_path"] == "data/trend_allocation/daily/2026-08-03.json"
    assert load_allocation_reference(
        tmp_path, allocation_date="2026-08-05", a_trading_days=["2026-08-04", "2026-08-05"]
    ) == {
        "daily_path": reference["daily_path"], "sha256": reference["sha256"], "snapshot": first,
        "reused": True, "stale_a_trading_days": 2, "failure_reason": None,
    }


def test_write_revisions_are_immutable_and_locked_batches_fail_closed(tmp_path: Path) -> None:
    first = snapshot()
    write_allocation_snapshot(tmp_path, first)
    changed = snapshot(roots=root_rows(cn=("99", "58.3")))
    with pytest.raises(TrendAnimalsError, match="immutable"):
        write_allocation_snapshot(tmp_path, changed)
    revised = write_allocation_snapshot(tmp_path, changed, revision=True)
    assert revised["daily_path"] == "data/trend_allocation/daily/2026-08-03-r1.json"
    assert write_allocation_snapshot(tmp_path, changed, revision=True) == revised

    report = tmp_path / "reports/CN/2026-08-03.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"as_of_date": "2026-08-03"}), encoding="utf-8")
    batch = tmp_path / "trend_review/ledgers/CN/batches/2026-08-04.json"
    batch.parent.mkdir(parents=True)
    batch.write_text(json.dumps({"report_path": str(report)}), encoding="utf-8")
    with pytest.raises(TrendAnimalsError, match="locked"):
        write_allocation_snapshot(tmp_path, snapshot(roots=root_rows(cn=("98", "58.3"))), revision=True)
    with pytest.raises(TrendAnimalsError, match="locked"):
        write_allocation_snapshot(tmp_path, changed, revision=True)


def test_load_rejects_bad_pointer_hash_schema_and_returns_none_when_cold(tmp_path: Path) -> None:
    assert load_allocation_reference(tmp_path, allocation_date="2026-08-03", a_trading_days=[]) is None
    snapshot_path = tmp_path / "trend_allocation/daily/2026-08-03.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text("{}", encoding="utf-8")
    latest = tmp_path / "trend_allocation/latest.json"
    latest.write_text(json.dumps({"daily_path": "data/trend_allocation/daily/2026-08-03.json", "sha256": hashlib.sha256(b"{}").hexdigest()}), encoding="utf-8")
    with pytest.raises(TrendAnimalsError):
        load_allocation_reference(tmp_path, allocation_date="2026-08-03", a_trading_days=[])


def test_allocation_once_writes_terminal_snapshot_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / ".venv/bin/python", timezone="Asia/Shanghai",
        deadline="21:10", futu_host="127.0.0.1", futu_port=11111,
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs", portfolio=tmp_path / "data/latest/portfolio.csv",
        trend_executor_host="executor", trend_animals_api_key="test-key",
    )

    class Quote:
        def get_cn_trading_days(self, **_kwargs: object) -> list[str]:
            return ["2026-08-03"]

        def close(self) -> None:
            pass

    class Api:
        def get_update_status(self) -> list[dict[str, object]]:
            return [
                {"asset": asset, "asOfDate": "2026-08-03"}
                for asset in ("A股", "ETF基金", "港股", "香港ETF", "美股", "美国ETF")
            ]

        def get_favorites_tickers(self) -> list[dict[str, object]]:
            return [
                {"tmId": index, "tickerName": asset, "asset": asset}
                for index, asset in enumerate(("A股", "ETF基金", "港股", "香港ETF", "美股", "美国ETF"), 1)
            ]

        def get_snapshots(self, *, tm_ids: list[int], fields: tuple[str, ...], expected_date: str) -> list[dict[str, object]]:
            del fields
            assets = ("A股", "ETF基金", "港股", "香港ETF", "美股", "美国ETF")
            return [{"tmId": item, "tickerName": assets[item - 1], "asset": assets[item - 1], "asOfDate": expected_date, "trendStrengthGlobalCurr": str(item)} for item in tm_ids]

    monkeypatch.setattr(trend_allocation, "require_trend_executor", lambda *_args, **_kwargs: None)
    monkeypatch.chdir(tmp_path)
    status = trend_allocation.run_trend_allocation_controller(
        config, once=True, allocation_date="2026-08-03",
        now_fn=lambda: datetime.fromisoformat("2026-08-03T16:21:00+08:00"),
        quote_factory=lambda **_kwargs: Quote(), api_factory=lambda **_kwargs: Api(),
    )

    assert status | {
        "schema_version": "open_trader.trend_allocation.status.v1",
        "pid": status["pid"], "working_directory": str(tmp_path),
        "git_sha": status["git_sha"], "phase": "ready", "attempted_for": "2026-08-03",
        "latest_daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "latest_sha256": status["latest_sha256"], "blocker": None,
    } == status


@pytest.mark.parametrize(
    ("phase", "blocker", "corrupt"),
    [
        ("ready", None, None),
        ("fallback", "offline", None),
        ("holiday", None, None),
        ("fallback", 123, {"blocker": 123}),
        ("ready", None, {"latest_sha256": None}),
    ],
)
def test_terminal_allocation_refreshes_runtime_status_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
    blocker: object, corrupt: dict[str, object] | None,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
        trend_animals_api_key="test-key",
    )
    reference = write_allocation_snapshot(config.data_dir, snapshot())
    write_allocation_snapshot(
        config.data_dir, snapshot(roots=root_rows(cn=("99", "58.3"))), revision=True,
    )
    status_path = config.data_dir / "trend_allocation/controller_status.json"
    old_status = {
        "schema_version": "open_trader.trend_allocation.status.v1",
        "effective_mode": "execute",
        "executor_host": "executor",
        "local_host": "executor",
        "pid": -1,
        "working_directory": "/old/worktree",
        "git_sha": "a" * 40,
        "phase": phase,
        "heartbeat_at": "2026-08-03T15:05:00+08:00",
        "attempted_for": "2026-08-03",
        "latest_daily_path": reference["daily_path"],
        "latest_sha256": reference["sha256"],
        "blocker": blocker,
        "next_check_at": "2026-08-04T15:05:00+08:00",
    }
    old_status.update(corrupt or {})
    status_path.write_text(json.dumps(old_status))

    class StopLoop(Exception):
        pass

    monkeypatch.setattr(trend_allocation, "require_trend_executor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(trend_allocation, "_process_version", lambda _repo: "b" * 40)
    monkeypatch.chdir(tmp_path)
    expected_error = TrendAnimalsError if corrupt else StopLoop
    with pytest.raises(expected_error, match="invalid" if corrupt else None):
        trend_allocation.run_trend_allocation_controller(
            config,
            now_fn=lambda: datetime.fromisoformat("2026-08-03T16:21:00+08:00"),
            sleep_fn=lambda _seconds: (_ for _ in ()).throw(StopLoop()),
            quote_factory=lambda **_kwargs: pytest.fail("terminal allocation fetched the calendar"),
            api_factory=lambda **_kwargs: pytest.fail("terminal allocation was rebuilt"),
        )

    if corrupt:
        return
    status = json.loads(status_path.read_text())
    assert status["phase"] == phase
    assert status["attempted_for"] == "2026-08-03"
    assert status["pid"] != -1
    assert status["working_directory"] == str(tmp_path)
    assert status["git_sha"] == "b" * 40
    assert status["heartbeat_at"] == "2026-08-03T16:21:00+08:00"
    assert status["latest_daily_path"] == reference["daily_path"]
    assert status["latest_sha256"] == reference["sha256"]
    assert status["blocker"] == blocker


def test_existing_requested_day_snapshot_restores_ready_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
        trend_animals_api_key="test-key",
    )
    reference = write_allocation_snapshot(config.data_dir, snapshot())
    daily = config.data_dir / "trend_allocation/daily/2026-08-03.json"
    original = daily.read_bytes()
    status_path = config.data_dir / "trend_allocation/controller_status.json"
    status_path.write_text(json.dumps({
        "schema_version": "open_trader.trend_allocation.status.v1",
        "phase": "retrying",
        "attempted_for": "2026-08-03",
        "latest_daily_path": reference["daily_path"],
        "latest_sha256": reference["sha256"],
        "blocker": "immutable allocation snapshot collision",
    }), encoding="utf-8")

    class StopLoop(Exception):
        pass

    def unexpected_external_call(**_kwargs: object) -> object:
        raise AssertionError("existing allocation triggered an external call")

    monkeypatch.setattr(trend_allocation, "require_trend_executor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(trend_allocation, "_process_version", lambda _repo: "b" * 40)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(StopLoop):
        trend_allocation.run_trend_allocation_controller(
            config,
            now_fn=lambda: datetime.fromisoformat("2026-08-03T16:19:00+08:00"),
            sleep_fn=lambda _seconds: (_ for _ in ()).throw(StopLoop()),
            quote_factory=unexpected_external_call,
            api_factory=unexpected_external_call,
        )

    status = json.loads(status_path.read_text())
    assert status["phase"] == "ready"
    assert status["attempted_for"] == "2026-08-03"
    assert status["latest_daily_path"] == reference["daily_path"]
    assert status["latest_sha256"] == reference["sha256"]
    assert status["blocker"] is None
    assert daily.read_bytes() == original


def test_allocation_reference_requires_current_terminal_attempt(tmp_path: Path) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )

    with pytest.raises(TrendAnimalsError, match="terminal"):
        trend_allocation.allocation_reference_for_report(
            config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
        )


def test_allocation_reference_must_match_the_terminal_status(
    tmp_path: Path,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )
    reference = write_allocation_snapshot(config.data_dir, snapshot())
    status_path = config.data_dir / "trend_allocation/controller_status.json"
    status = {
        "schema_version": "open_trader.trend_allocation.status.v1",
        "phase": "ready",
        "attempted_for": "2026-08-03",
        "latest_daily_path": reference["daily_path"],
        "latest_sha256": reference["sha256"],
    }
    status_path.write_text(json.dumps(status), encoding="utf-8")

    loaded = trend_allocation.allocation_reference_for_report(
        config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
    )
    assert loaded is not None
    assert (loaded["daily_path"], loaded["sha256"]) == (
        reference["daily_path"], reference["sha256"],
    )

    write_allocation_snapshot(
        config.data_dir,
        snapshot(roots=root_rows(cn=("99", "58.3"))),
        revision=True,
    )
    with pytest.raises(TrendAnimalsError, match="terminal status"):
        trend_allocation.allocation_reference_for_report(
            config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
        )

    (config.data_dir / "trend_allocation/latest.json").unlink()
    status["latest_daily_path"] = None
    status_path.write_text(json.dumps(status), encoding="utf-8")
    with pytest.raises(TrendAnimalsError, match="terminal status"):
        trend_allocation.allocation_reference_for_report(
            config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
        )

    status["latest_sha256"] = None
    status_path.write_text(json.dumps(status), encoding="utf-8")
    with pytest.raises(TrendAnimalsError, match="terminal status"):
        trend_allocation.allocation_reference_for_report(
            config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
        )


@pytest.mark.parametrize(
    ("requested_date", "error"),
    [("2026-08-02", "future snapshot"), ("2026-08-04", "requested-day snapshot")],
)
def test_allocation_ready_status_requires_requested_day_snapshot(
    tmp_path: Path, requested_date: str, error: str,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )
    reference = write_allocation_snapshot(config.data_dir, snapshot())
    (config.data_dir / "trend_allocation/controller_status.json").write_text(json.dumps({
        "schema_version": "open_trader.trend_allocation.status.v1",
        "phase": "ready",
        "attempted_for": requested_date,
        "latest_daily_path": reference["daily_path"],
        "latest_sha256": reference["sha256"],
        "blocker": None,
    }), encoding="utf-8")

    with pytest.raises(TrendAnimalsError, match=error):
        trend_allocation.allocation_reference_for_report(
            config,
            allocation_date=requested_date,
            a_trading_days=["2026-08-02", "2026-08-03", "2026-08-04"],
        )


@pytest.mark.parametrize(
    ("phase", "blocker"),
    [("fallback", "Trend Animals unavailable"), ("holiday", None)],
)
def test_allocation_terminal_status_rejects_future_snapshot(
    tmp_path: Path, phase: str, blocker: str | None,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )
    reference = write_allocation_snapshot(config.data_dir, snapshot())
    (config.data_dir / "trend_allocation/controller_status.json").write_text(json.dumps({
        "schema_version": "open_trader.trend_allocation.status.v1",
        "phase": phase,
        "attempted_for": "2026-08-02",
        "latest_daily_path": reference["daily_path"],
        "latest_sha256": reference["sha256"],
        "blocker": blocker,
    }), encoding="utf-8")

    with pytest.raises(TrendAnimalsError, match="future snapshot"):
        trend_allocation.allocation_reference_for_report(
            config,
            allocation_date="2026-08-02",
            a_trading_days=["2026-08-02", "2026-08-03"],
        )


@pytest.mark.parametrize(
    ("phase", "blocker", "valid"),
    [
        ("fallback", "Trend Animals unavailable", True),
        ("holiday", None, True),
        ("fallback", None, False),
        ("fallback", "", False),
        ("holiday", "unexpected blocker", False),
        ("ready", "unexpected blocker", False),
    ],
)
def test_allocation_reference_binds_terminal_phase_to_blocker(
    tmp_path: Path, phase: str, blocker: str | None, valid: bool,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )
    status_path = config.data_dir / "trend_allocation/controller_status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "schema_version": "open_trader.trend_allocation.status.v1",
        "phase": phase,
        "attempted_for": "2026-08-03",
        "latest_daily_path": None,
        "latest_sha256": None,
        "blocker": blocker,
    }), encoding="utf-8")

    if valid:
        assert trend_allocation.allocation_reference_for_report(
            config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
        ) is None
    else:
        with pytest.raises(TrendAnimalsError, match="phase and blocker"):
            trend_allocation.allocation_reference_for_report(
                config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
            )


def test_allocation_reference_uses_one_terminal_status_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )
    reference = write_allocation_snapshot(config.data_dir, snapshot())
    status_path = config.data_dir / "trend_allocation/controller_status.json"
    status_path.write_text(json.dumps({
        "schema_version": "open_trader.trend_allocation.status.v1",
        "phase": "ready",
        "attempted_for": "2026-08-03",
        "latest_daily_path": reference["daily_path"],
        "latest_sha256": reference["sha256"],
        "blocker": None,
    }), encoding="utf-8")
    monkeypatch.setattr(
        trend_allocation,
        "_status_failure_reason",
        lambda _data_dir: pytest.fail("controller status was read twice"),
    )

    loaded = trend_allocation.allocation_reference_for_report(
        config, allocation_date="2026-08-03", a_trading_days=["2026-08-03"]
    )
    assert loaded is not None
    assert loaded["failure_reason"] is None


def test_holiday_waits_for_the_post_close_attempt_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )

    class StopLoop(Exception):
        pass

    def unexpected_external_call(**_kwargs: object) -> object:
        raise AssertionError("waiting allocation fetched the calendar")

    monkeypatch.setattr(trend_allocation, "require_trend_executor", lambda *_args, **_kwargs: None)
    with pytest.raises(StopLoop):
        trend_allocation.run_trend_allocation_controller(
            config,
            now_fn=lambda: datetime.fromisoformat("2026-08-03T16:19:00+08:00"),
            sleep_fn=lambda _seconds: (_ for _ in ()).throw(StopLoop()),
            quote_factory=unexpected_external_call,
        )

    status = json.loads(
        (config.data_dir / "trend_allocation/controller_status.json").read_text()
    )
    assert status["phase"] == "waiting"
    assert status["attempted_for"] is None


def test_failure_marker_requires_delivery_and_recovery_is_not_normal_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )
    outcomes = [
        [NotificationAttempt(channel="feishu", success=False, error="offline")],
        [NotificationAttempt(channel="feishu", success=True)],
        [NotificationAttempt(channel="feishu", success=True)],
    ]
    monkeypatch.setattr(trend_allocation, "build_notifier", lambda _config: object())
    monkeypatch.setattr(
        trend_allocation,
        "send_notification_with_results",
        lambda *_args, **_kwargs: outcomes.pop(0),
    )

    assert not trend_allocation._notify_allocation_failure_once(
        config, allocation_date="2026-08-03", reason="offline"
    )
    marker = config.data_dir / "trend_allocation/notifications/2026-08-03.json"
    assert not marker.exists()
    assert trend_allocation._notify_allocation_failure_once(
        config, allocation_date="2026-08-03", reason="offline"
    )
    assert json.loads(marker.read_text())["delivered"] is True
    assert trend_allocation._notify_allocation_recovery(
        config, allocation_date="2026-08-03"
    )
    assert json.loads(marker.read_text())["recovered"] is True


def test_recovery_aggregates_outstanding_failure_markers_into_one_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DailyPremarketConfig(
        repo=tmp_path, python=tmp_path / "python", timezone="Asia/Shanghai", deadline="21:10",
        futu_host="127.0.0.1", futu_port=11111, data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports", logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv", trend_executor_host="executor",
    )
    notifications = config.data_dir / "trend_allocation/notifications"
    notifications.mkdir(parents=True)
    markers = []
    for allocation_date in ("2026-08-01", "2026-08-02"):
        marker = notifications / f"{allocation_date}.json"
        marker.write_text(json.dumps({
            "allocation_date": allocation_date,
            "reason": "offline",
            "delivered": True,
        }))
        markers.append(marker)

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(trend_allocation, "build_notifier", lambda _config: object())
    monkeypatch.setattr(
        trend_allocation,
        "send_notification_with_results",
        lambda *args, **_kwargs: (calls.append(args) or [NotificationAttempt(channel="feishu", success=True)]),
    )

    assert trend_allocation._notify_allocation_recovery(
        config, allocation_date="2026-08-03"
    )
    assert len(calls) == 1
    assert "2026-08-01" in calls[0][2]
    assert "2026-08-02" in calls[0][2]
    assert all(json.loads(marker.read_text())["recovered"] is True for marker in markers)
