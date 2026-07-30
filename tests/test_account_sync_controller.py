from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.account_sync_state import (
    BrokerAccountCandidate,
    accept_candidate,
    load_account_sync_state,
    project_account_sync_health,
    write_json_atomic,
)
from open_trader.account_sync_controller import (
    AccountSyncController,
    AccountSyncControllerConfig,
)
from open_trader.models import AssetClass, Market, Position


def test_sync_accounts_publishes_full_tiger_generation_before_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.account_sync_controller as controller_module

    data_dir = tmp_path / "data"
    portfolio_path = data_dir / "latest" / "portfolio.csv"
    _seed_state(data_dir, {"tiger": _candidate("tiger", 8, "OLD")})
    all_14_symbols = {f"NEW{index}" for index in range(14)}
    tiger_candidate = _candidate("tiger", 14, "NEW")
    _configure_sources(monkeypatch, controller_module, tiger_candidate=tiger_candidate)
    writes: list[Path] = []
    real_portfolio_write = controller_module.write_portfolio_atomic
    real_state_write = controller_module.write_json_atomic
    monkeypatch.setattr(
        controller_module,
        "write_portfolio_atomic",
        lambda path, rows: (writes.append(path), real_portfolio_write(path, rows))[1],
    )
    monkeypatch.setattr(
        controller_module,
        "write_json_atomic",
        lambda path, state: (writes.append(path), real_state_write(path, state))[1],
    )

    controller = AccountSyncController(_config(data_dir, portfolio_path))
    controller.sync_accounts_once()

    published = load_account_sync_state(data_dir / "latest/account_sync_state.json")
    assert published["brokers"]["tiger"]["summary"]["position_count"] == 14
    assert len(published["brokers"]["tiger"]["positions"]) == 14
    symbols = _portfolio_symbols(portfolio_path)
    assert symbols >= all_14_symbols
    assert "OLD0" not in symbols
    assert writes.index(portfolio_path) < writes.index(data_dir / "latest/account_sync_state.json")
    assert all("restore" not in str(path) for path in writes)
    assert not hasattr(controller_module, "_assert_preserves_other_brokers")


def test_sync_accounts_keeps_failed_source_data_and_later_clears_only_that_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.account_sync_controller as controller_module

    data_dir = tmp_path / "data"
    portfolio_path = data_dir / "latest" / "portfolio.csv"
    _seed_state(
        data_dir,
        {
            "futu": _candidate("futu", 1, "FUTU"),
            "tiger": _candidate("tiger", 1, "OLD"),
            "phillips": _candidate("phillips", 1, "PHILLIPS"),
            "eastmoney": _candidate("eastmoney", 1, "EAST"),
        },
    )
    before = load_account_sync_state(data_dir / "latest/account_sync_state.json")
    clock = [0.0]
    _configure_sources(
        monkeypatch,
        controller_module,
        futu_error=RuntimeError("Futu unavailable"),
        tiger_candidate=_candidate("tiger", 1, "TIGER"),
    )
    controller = AccountSyncController(
        _config(data_dir, portfolio_path),
        clock=lambda: clock[0],
        now_text=lambda: "2026-07-30T12:00:00+08:00",
    )

    controller.sync_accounts_once()

    failed = load_account_sync_state(data_dir / "latest/account_sync_state.json")
    futu_before = before["brokers"]["futu"]
    futu_after = failed["brokers"]["futu"]
    for field in ("last_success_at", "data_as_of", "period", "positions", "cash", "fx_rates", "summary"):
        assert futu_after[field] == futu_before[field]
    assert futu_after["status"] == "failed"
    assert failed["brokers"]["tiger"]["positions"][0]["symbol"] == "TIGER0"
    assert "OLD0" not in _portfolio_symbols(portfolio_path)
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    health = project_account_sync_health(
        failed,
        _controller_status(now),
        {"status": "ok", "last_success_at": now.isoformat(), "stale": False},
        now=now,
    )
    assert health["status"] == "abnormal"
    assert health["reason"] == "broker_futu_failed"

    reloaded = AccountSyncController(
        _config(data_dir, portfolio_path),
        clock=lambda: clock[0],
        now_text=lambda: "2026-07-30T12:00:00+08:00",
    )
    assert reloaded.sync_accounts_once()["status"] == "partial"
    assert load_account_sync_state(data_dir / "latest/account_sync_state.json") == failed
    assert controller.sync_accounts_once()["status"] == "skipped"
    assert load_account_sync_state(data_dir / "latest/account_sync_state.json") == failed

    clock[0] = 61.0
    _configure_sources(
        monkeypatch,
        controller_module,
        tiger_candidate=_candidate("tiger", 1, "TIGER"),
    )
    controller.sync_accounts_once()
    recovered = load_account_sync_state(data_dir / "latest/account_sync_state.json")
    assert recovered["brokers"]["futu"]["status"] == "ok"
    assert recovered["brokers"]["tiger"]["status"] == "ok"
    assert recovered["brokers"]["phillips"]["status"] == "ok"
    assert recovered["brokers"]["eastmoney"]["status"] == "ok"


def test_state_publication_failure_stops_without_false_source_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.account_sync_controller as controller_module

    data_dir = tmp_path / "data"
    portfolio_path = data_dir / "latest" / "portfolio.csv"
    old_candidate = _candidate("futu", 1, "OLD")
    new_candidate = _candidate("futu", 1, "NEW")
    _seed_state(data_dir, {"futu": old_candidate})
    state_path = data_dir / "latest" / "account_sync_state.json"
    before = state_path.read_bytes()
    _configure_sources(monkeypatch, controller_module, futu_candidate=new_candidate)
    real_write = controller_module.write_json_atomic
    state_writes = 0

    def fail_state_write(path: Path, payload: object) -> None:
        nonlocal state_writes
        if path == state_path:
            state_writes += 1
            raise OSError("state storage unavailable")
        real_write(path, payload)

    monkeypatch.setattr(controller_module, "write_json_atomic", fail_state_write)
    monkeypatch.setattr(
        controller_module,
        "record_source_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("false source failure")),
    )

    result = AccountSyncController(_config(data_dir, portfolio_path)).sync_accounts_once()

    assert result == {
        "status": "publication_failed",
        "blocker": "account_state_publish_failed: futu",
        "brokers": {"futu": {"status": "publication_failed"}},
    }
    assert state_writes == 2
    assert state_path.read_bytes() == before
    assert _portfolio_symbols(portfolio_path) == {"NEW0"}
    assert not hasattr(controller_module, "_verify_or_restore_portfolio")


def test_validation_failure_writes_diagnostic_without_replacing_accepted_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.account_sync_controller as controller_module

    data_dir = tmp_path / "data"
    portfolio_path = data_dir / "latest" / "portfolio.csv"
    original = _candidate("futu", 1, "OLD")
    _seed_state(data_dir, {"futu": original})
    invalid = BrokerAccountCandidate(
        **{**original.__dict__, "source_kind": "statement"}
    )
    _configure_sources(monkeypatch, controller_module, futu_candidate=invalid)

    AccountSyncController(_config(data_dir, portfolio_path)).sync_accounts_once()

    published = load_account_sync_state(data_dir / "latest/account_sync_state.json")
    assert published["brokers"]["futu"]["positions"][0]["symbol"] == "OLD0"
    assert published["brokers"]["futu"]["status"] == "failed"
    diagnostics = list((data_dir / "account_sync" / "runs").glob("*/futu.json"))
    assert len(diagnostics) == 1


def _config(data_dir: Path, portfolio_path: Path) -> AccountSyncControllerConfig:
    return AccountSyncControllerConfig(
        data_dir=data_dir,
        reports_dir=data_dir / "reports",
        portfolio_path=portfolio_path,
        futu_host="127.0.0.1",
        futu_port=11111,
        tiger_config_dir=data_dir / "tiger",
        tiger_account=None,
    )


def _configure_sources(
    monkeypatch: pytest.MonkeyPatch,
    controller_module: object,
    *,
    futu_candidate: BrokerAccountCandidate | None = None,
    tiger_candidate: BrokerAccountCandidate | None = None,
    futu_error: Exception | None = None,
) -> None:
    class FutuClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def fetch_snapshot(self) -> object:
            if futu_error is not None:
                raise futu_error
            return object()

        def close(self) -> None:
            pass

    class TigerClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def fetch_snapshot(self) -> object:
            return object()

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller_module, "FutuAccountClient", FutuClient)
    monkeypatch.setattr(controller_module, "TigerAccountClient", TigerClient)
    monkeypatch.setattr(controller_module, "load_tiger_account_config", lambda **_kwargs: object())
    monkeypatch.setattr(
        controller_module,
        "build_futu_account_candidate",
        lambda *_args, **_kwargs: futu_candidate or _candidate("futu", 1, "FUTU"),
    )
    monkeypatch.setattr(
        controller_module,
        "build_tiger_account_candidate",
        lambda *_args, **_kwargs: tiger_candidate or _candidate("tiger", 1, "TIGER"),
    )
    monkeypatch.setattr(
        controller_module,
        "load_latest_statement_candidate",
        lambda _data_dir, broker: _candidate(broker, 1, broker.upper()),
    )


def _seed_state(data_dir: Path, candidates: dict[str, BrokerAccountCandidate]) -> None:
    state = load_account_sync_state(data_dir / "latest/account_sync_state.json")
    for broker, candidate in candidates.items():
        assert candidate.broker == broker
        state = accept_candidate(state, candidate, attempted_at="2026-07-30T11:00:00+08:00")
    write_json_atomic(data_dir / "latest/account_sync_state.json", state)


def _candidate(broker: str, count: int, prefix: str) -> BrokerAccountCandidate:
    source_kind = "live" if broker in {"futu", "tiger"} else "statement"
    period = "2026-07" if broker != "phillips" else "2026-07-30"
    return BrokerAccountCandidate(
        broker=broker,
        source_kind=source_kind,
        data_as_of="2026-07-30T12:00:00+08:00" if source_kind == "live" else "2026-07-30",
        period=period,
        positions=tuple(
            Position(
                statement_id=f"{period}-{broker}",
                broker=broker,
                account_alias=f"{broker}_main",
                market=Market.US,
                asset_class=AssetClass.STOCK,
                symbol=f"{prefix}{index}",
                name=f"{prefix}{index}",
                currency="USD",
                quantity=Decimal("1"),
                cost_price=Decimal("10"),
                last_price=Decimal("11"),
                market_value=Decimal("11"),
                cost_value=Decimal("10"),
                unrealized_pnl=Decimal("1"),
                confidence="high",
                notes="test",
            )
            for index in range(count)
        ),
        cash=(),
        fx_rates=(),
        summary={"position_count": count, "cash_count": 0, "is_real_time": source_kind == "live"},
    )


def _portfolio_symbols(path: Path) -> set[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["symbol"] for row in csv.DictReader(handle)}


def _controller_status(heartbeat_at: datetime) -> dict[str, object]:
    return {
        "schema_version": "open_trader.account_sync.controller.v1",
        "pid": 123,
        "started_at": "2026-07-30T11:00:00+08:00",
        "working_directory": "/tmp",
        "git_sha": "test",
        "heartbeat_at": heartbeat_at.isoformat(),
        "phase": "idle",
        "account_loop": {},
        "quote_loop": {},
        "blocker": None,
    }
