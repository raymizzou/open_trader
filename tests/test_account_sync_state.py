from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.account_sync_state import (
    ACCOUNT_STATE_VERSION,
    REQUIRED_BROKERS,
    BrokerAccountCandidate,
    accept_candidate,
    accepted_portfolio_rows,
    effective_source_status,
    load_account_sync_state,
    project_account_sync_health,
    record_source_failure,
    write_json_atomic,
    write_portfolio_atomic,
)
from open_trader.models import AssetClass, CashBalance, Market, Position
from open_trader.portfolio import PORTFOLIO_FIELDNAMES, PortfolioBuildError


def test_load_missing_or_malformed_state_is_all_unknown(tmp_path) -> None:
    state = load_account_sync_state(tmp_path / "missing.json")

    assert state["version"] == ACCOUNT_STATE_VERSION == 1
    assert set(state["brokers"]) == set(REQUIRED_BROKERS)
    assert {item["status"] for item in state["brokers"].values()} == {"unknown"}
    assert all(not item["positions"] and not item["cash"] for item in state["brokers"].values())

    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("{not json", encoding="utf-8")
    assert load_account_sync_state(malformed_path) == state


def test_accepted_candidate_round_trips_normalized_detail_rows(tmp_path) -> None:
    position = Position(
        statement_id="snapshot-1",
        broker="futu",
        account_alias="main",
        market=Market.US,
        asset_class=AssetClass.STOCK,
        symbol="AAPL",
        name="Apple",
        currency="USD",
        quantity=Decimal("10.50"),
        cost_price=Decimal("175.25"),
        last_price=Decimal("190.75"),
        market_value=Decimal("2002.875"),
        cost_value=Decimal("1840.125"),
        unrealized_pnl=Decimal("162.750"),
        confidence="high",
        notes="complete",
    )
    cash = CashBalance(
        statement_id="snapshot-1",
        broker="futu",
        account_alias="main",
        currency="USD",
        cash_balance=Decimal("1234.50"),
        available_balance=Decimal("1200.00"),
        confidence="high",
        notes="available",
    )
    candidate = BrokerAccountCandidate(
        broker="futu",
        source_kind="live",
        data_as_of="2026-07-30T11:56:54+08:00",
        period="2026-07",
        positions=(position,),
        cash=(cash,),
        fx_rates=(
            {
                "account_alias": "main",
                "currency": "USD",
                "rate_to_hkd": "7.8123",
            },
        ),
        summary={"position_count": 1, "cash_count": 1},
    )

    state = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        candidate,
        attempted_at="2026-07-30T12:00:00+08:00",
    )
    path = tmp_path / "account_sync_state.json"
    write_json_atomic(path, state)
    loaded = load_account_sync_state(path)
    source = loaded["brokers"]["futu"]

    assert source["positions"] == [
        {
            "statement_id": "snapshot-1",
            "broker": "futu",
            "account_alias": "main",
            "market": "US",
            "asset_class": "stock",
            "symbol": "AAPL",
            "name": "Apple",
            "currency": "USD",
            "quantity": "10.50",
            "cost_price": "175.25",
            "last_price": "190.75",
            "market_value": "2002.875",
            "cost_value": "1840.125",
            "unrealized_pnl": "162.750",
            "confidence": "high",
            "notes": "complete",
        }
    ]
    assert source["cash"] == [
        {
            "statement_id": "snapshot-1",
            "broker": "futu",
            "account_alias": "main",
            "currency": "USD",
            "cash_balance": "1234.50",
            "available_balance": "1200.00",
            "confidence": "high",
            "notes": "available",
        }
    ]
    assert source["fx_rates"] == [
        {"account_alias": "main", "currency": "USD", "rate_to_hkd": "7.8123"}
    ]
    assert source["summary"] == {"position_count": 1, "cash_count": 1}
    assert "account_id" not in source["positions"][0]
    assert "account_id" not in source["cash"][0]


def test_load_rejects_unknown_versions_and_invalid_broker_payloads(tmp_path) -> None:
    path = tmp_path / "state.json"
    write_json_atomic(
        path,
        {
            "version": 2,
            "generation": "2026-07-30T12:00:00+08:00",
            "brokers": {},
        },
    )
    assert load_account_sync_state(path) == load_account_sync_state(tmp_path / "missing.json")

    accepted = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-30T12:00:00+08:00",
    )
    accepted["brokers"]["futu"]["positions"][0].pop("market_value")
    write_json_atomic(path, accepted)
    assert load_account_sync_state(path) == load_account_sync_state(tmp_path / "missing.json")

    write_json_atomic(
        path,
        {
            "version": 1,
            "generation": "2026-07-30T12:00:00+08:00",
            "brokers": {broker: {} for broker in REQUIRED_BROKERS},
        },
    )
    assert load_account_sync_state(path) == load_account_sync_state(tmp_path / "missing.json")


def test_failure_preserves_accepted_data_and_sanitizes_sensitive_error(tmp_path) -> None:
    accepted = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-30T12:00:00+08:00",
    )
    failed = record_source_failure(
        accepted,
        "futu",
        attempted_at="2026-07-30T12:03:00+08:00",
        message=(
            "token=sekret account=123456789 /Users/ray/.config/tiger/config.json "
            "/Users/ray/projects/open_trader/data"
        ),
        sensitive_values=("sekret",),
        sensitive_roots=(Path("/Users/ray/projects/open_trader"),),
    )

    before = accepted["brokers"]["futu"]
    after = failed["brokers"]["futu"]
    assert after["status"] == "failed"
    assert after["attempted_at"] == "2026-07-30T12:03:00+08:00"
    assert failed["generation"] == "2026-07-30T12:03:00+08:00"
    for field in (
        "last_success_at",
        "data_as_of",
        "period",
        "positions",
        "cash",
        "fx_rates",
        "summary",
    ):
        assert after[field] == before[field]
    assert "sekret" not in after["message"]
    assert "123456789" not in after["message"]
    assert "/Users/ray" not in after["message"]
    assert "tiger/config.json" not in after["message"]
    assert "/Users/ray/projects/open_trader" not in after["message"]


def test_effective_source_status_uses_live_freshness_only() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    live_ok_179_seconds_old = _source(
        source_kind="live", last_success_at=(now - timedelta(seconds=179)).isoformat()
    )
    live_ok_181_seconds_old = _source(
        source_kind="live", last_success_at=(now - timedelta(seconds=181)).isoformat()
    )
    statement_ok_two_months_old = _source(
        source_kind="statement", last_success_at="2026-05-30T12:00:00+08:00"
    )
    failed_source = _source(status="failed")
    unknown_source = _source(status="unknown")

    assert effective_source_status(live_ok_179_seconds_old, now=now) == "ok"
    assert effective_source_status(live_ok_181_seconds_old, now=now) == "stale"
    assert effective_source_status(statement_ok_two_months_old, now=now) == "ok"
    assert effective_source_status(failed_source, now=now) == "failed"
    assert effective_source_status(unknown_source, now=now) == "unknown"


def test_health_is_ok_only_with_current_controller_sources_quotes_and_generation() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    state = {
        "version": 1,
        "generation": "2026-07-30T11:59:59+08:00",
        "brokers": {
            broker: _source(last_success_at=(now - timedelta(seconds=1)).isoformat())
            for broker in REQUIRED_BROKERS
        },
    }
    controller = _controller_status(now)
    quotes = {"status": "ok", "last_success_at": now.isoformat(), "stale": False}

    healthy = project_account_sync_health(state, controller, quotes, now=now)
    assert healthy["status"] == "ok"
    assert healthy["label"] == "同步正常"
    assert healthy["reason"] == ""
    assert healthy["brokers"]["futu"]["display"] == "同步正常"

    stale_controller = _controller_status(now - timedelta(seconds=16))
    abnormal = project_account_sync_health(state, stale_controller, quotes, now=now)
    assert abnormal["status"] == "abnormal"
    assert abnormal["reason"] == "controller_stale"

    failed_state = record_source_failure(
        state,
        "tiger",
        attempted_at=now.isoformat(),
        message="failed",
    )
    abnormal = project_account_sync_health(failed_state, controller, quotes, now=now)
    assert abnormal["status"] == "abnormal"
    assert abnormal["reason"] == "broker_tiger_failed"

    stale_quotes = {"status": "ok", "last_success_at": (now - timedelta(seconds=16)).isoformat()}
    abnormal = project_account_sync_health(state, controller, stale_quotes, now=now)
    assert abnormal["status"] == "abnormal"
    assert abnormal["reason"] == "quotes_stale"

    state_without_generation = {**state, "generation": ""}
    abnormal = project_account_sync_health(state_without_generation, controller, quotes, now=now)
    assert abnormal["status"] == "abnormal"
    assert abnormal["reason"] == "portfolio_missing"


def test_health_rejects_invalid_controller_status_and_keeps_unknown_display() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    health = project_account_sync_health(
        load_account_sync_state(Path("/missing/account_sync_state.json")),
        {"pid": "not-an-int"},
        {},
        now=now,
    )

    assert health["status"] == "abnormal"
    assert health["reason"] == "controller_unknown"
    assert health["controller"]["status"] == "unknown"
    assert health["brokers"]["futu"] == {
        "status": "unknown",
        "data_as_of": "",
        "last_success_at": "",
        "message": "",
        "display": "同步状态未知 · 数据未验证",
    }


def test_accepted_portfolio_uses_source_fx_and_rejects_duplicate_identities(tmp_path) -> None:
    state = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-30T12:00:00+08:00",
    )

    rows = accepted_portfolio_rows(state)
    assert rows[0]["brokers"] == "futu"
    assert rows[0]["fx_to_hkd"] == "7.8123"
    assert rows[0]["market_value_hkd"] == "15640.22"

    source = state["brokers"]["futu"]
    duplicate_position_state = {
        **state,
        "brokers": {**state["brokers"], "futu": {**source, "positions": [*source["positions"], source["positions"][0]]}},
    }
    with pytest.raises(PortfolioBuildError, match="duplicate position identity"):
        accepted_portfolio_rows(duplicate_position_state)

    duplicate_cash_state = {
        **state,
        "brokers": {**state["brokers"], "futu": {**source, "cash": [*source["cash"], source["cash"][0]]}},
    }
    with pytest.raises(PortfolioBuildError, match="duplicate cash identity"):
        accepted_portfolio_rows(duplicate_cash_state)


def test_mixed_broker_portfolio_rows_keep_the_deterministic_fx_fallback(tmp_path) -> None:
    state = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-30T12:00:00+08:00",
    )
    candidate = _candidate()
    tiger_position = replace(candidate.positions[0], broker="tiger")
    tiger_cash = replace(candidate.cash[0], broker="tiger")
    state = accept_candidate(
        state,
        BrokerAccountCandidate(
            broker="tiger",
            source_kind="live",
            data_as_of="2026-07-30T11:56:54+08:00",
            period="2026-07",
            positions=(tiger_position,),
            cash=(tiger_cash,),
            fx_rates=(
                {"account_alias": "main", "currency": "USD", "rate_to_hkd": "7.5000"},
            ),
            summary={"position_count": 1, "cash_count": 1},
        ),
        attempted_at="2026-07-30T12:00:01+08:00",
    )

    row = accepted_portfolio_rows(state)[0]
    assert row["brokers"] == "futu;tiger"
    assert row["fx_to_hkd"] == "7.8"
    assert row["market_value_hkd"] == "31231.20"


def test_write_portfolio_atomic_uses_the_existing_portfolio_columns(tmp_path) -> None:
    path = tmp_path / "latest" / "portfolio.csv"
    write_portfolio_atomic(path, [{field: "value" for field in PORTFOLIO_FIELDNAMES}])

    with path.open(encoding="utf-8", newline="") as handle:
        assert list(csv.DictReader(handle)) == [{field: "value" for field in PORTFOLIO_FIELDNAMES}]


def _candidate() -> BrokerAccountCandidate:
    return BrokerAccountCandidate(
        broker="futu",
        source_kind="live",
        data_as_of="2026-07-30T11:56:54+08:00",
        period="2026-07",
        positions=(
            Position(
                statement_id="snapshot-1",
                broker="futu",
                account_alias="main",
                market=Market.US,
                asset_class=AssetClass.STOCK,
                symbol="AAPL",
                name="Apple",
                currency="USD",
                quantity=Decimal("10"),
                cost_price=Decimal("175"),
                last_price=Decimal("200"),
                market_value=Decimal("2002"),
                cost_value=Decimal("1750"),
                unrealized_pnl=Decimal("252"),
                confidence="high",
                notes="",
            ),
        ),
        cash=(
            CashBalance(
                statement_id="snapshot-1",
                broker="futu",
                account_alias="main",
                currency="USD",
                cash_balance=Decimal("100"),
                available_balance=Decimal("100"),
                confidence="high",
                notes="",
            ),
        ),
        fx_rates=(
            {"account_alias": "main", "currency": "USD", "rate_to_hkd": "7.8123"},
        ),
        summary={"position_count": 1, "cash_count": 1},
    )


def _source(
    *,
    source_kind: str = "live",
    status: str = "ok",
    last_success_at: str = "2026-07-30T12:00:00+08:00",
) -> dict[str, object]:
    return {
        "source_kind": source_kind,
        "status": status,
        "attempted_at": last_success_at,
        "last_success_at": last_success_at,
        "data_as_of": last_success_at,
        "period": "2026-07",
        "message": "",
        "positions": [],
        "cash": [],
        "fx_rates": [],
        "summary": {},
    }


def _controller_status(heartbeat_at: datetime) -> dict[str, object]:
    return {
        "schema_version": "open_trader.account_sync.controller.v1",
        "pid": 123,
        "started_at": "2026-07-30T11:00:00+08:00",
        "working_directory": "/Users/ray/projects/open_trader",
        "git_sha": "abc123",
        "heartbeat_at": heartbeat_at.isoformat(),
        "phase": "idle",
        "account_loop": {},
        "quote_loop": {},
        "blocker": None,
    }
