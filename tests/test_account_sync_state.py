from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.account_sync_state import (
    ACCOUNT_STATE_VERSION,
    LIVE_BROKERS,
    REQUIRED_BROKERS,
    BrokerAccountCandidate,
    accept_candidate,
    accepted_portfolio_rows,
    build_dashboard_projection,
    dashboard_projection_from_state,
    effective_source_status,
    load_account_sync_state,
    load_latest_statement_candidate,
    project_account_sync_health,
    record_source_failure,
    write_json_atomic,
    write_portfolio_atomic,
)
from open_trader.csv_io import write_rows
from open_trader.models import AssetClass, CashBalance, Market, Position
from open_trader.pipeline import (
    CASH_FIELDNAMES,
    MANIFEST_FIELDNAMES,
    POSITION_FIELDNAMES,
    _cash_to_row,
    _position_to_row,
)
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


def test_legacy_state_keeps_accepted_sources_and_exposes_no_projection(
    tmp_path: Path,
) -> None:
    legacy = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-31T08:00:00+08:00",
    )
    legacy.pop("dashboard_projection", None)
    path = tmp_path / "state.json"
    write_json_atomic(path, legacy)

    loaded = load_account_sync_state(path)

    assert loaded["brokers"]["futu"] == legacy["brokers"]["futu"]
    assert loaded["dashboard_projection"] == {}
    assert dashboard_projection_from_state(loaded) is None


def test_invalid_projection_is_dropped_without_discarding_accepted_sources(
    tmp_path: Path,
) -> None:
    accepted = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at="2026-07-31T08:00:00+08:00",
    )
    accepted["dashboard_projection"] = {
        "generated_at": "2026-07-31T08:00:00+08:00",
        "quote_as_of": "",
        "summary": {},
        "broker_summaries": [],
        "broker_positions": [{"broker": "futu"}],
        "cash_details": [],
    }
    path = tmp_path / "state.json"
    write_json_atomic(path, accepted)

    loaded = load_account_sync_state(path)

    assert loaded["brokers"]["futu"]["positions"]
    assert loaded["dashboard_projection"] == {}
    assert dashboard_projection_from_state(loaded) is None

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


def test_failure_sanitizes_unlisted_credential_values(tmp_path) -> None:
    failed = record_source_failure(
        load_account_sync_state(tmp_path / "missing.json"),
        "futu",
        attempted_at="2026-07-30T12:03:00+08:00",
        message=(
            "Authorization: Bearer bearer-secret password=plain-password "
            "api_key: api-key-secret client_secret=client-secret"
        ),
    )

    message = failed["brokers"]["futu"]["message"]
    for credential in (
        "bearer-secret",
        "plain-password",
        "api-key-secret",
        "client-secret",
    ):
        assert credential not in message


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


def test_invalid_source_kind_is_untrusted_and_cannot_be_accepted(tmp_path) -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    invalid_source = _source(
        source_kind="unexpected",
        last_success_at=(now - timedelta(days=1)).isoformat(),
    )

    assert effective_source_status(invalid_source, now=now) == "unknown"
    with pytest.raises(ValueError, match="invalid source_kind"):
        accept_candidate(
            load_account_sync_state(tmp_path / "missing.json"),
            replace(_candidate(), source_kind="unexpected"),
            attempted_at=now.isoformat(),
        )

    accepted = accept_candidate(
        load_account_sync_state(tmp_path / "missing.json"),
        _candidate(),
        attempted_at=now.isoformat(),
    )
    accepted["brokers"]["futu"]["source_kind"] = "unexpected"
    path = tmp_path / "state.json"
    write_json_atomic(path, accepted)
    assert load_account_sync_state(path) == load_account_sync_state(tmp_path / "missing.json")


def test_health_is_ok_only_with_current_controller_sources_quotes_and_generation() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    state = {
        "version": 1,
        "generation": "2026-07-30T11:59:59+08:00",
        "dashboard_projection": _valid_dashboard_projection(),
        "brokers": {
            broker: _source(
                source_kind="live" if broker in LIVE_BROKERS else "statement",
                last_success_at=(now - timedelta(seconds=1)).isoformat(),
            )
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


def test_health_marks_generated_state_without_projection_abnormal() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    state = {
        "version": 1,
        "generation": "2026-07-30T11:59:59+08:00",
        "brokers": {
            broker: _source(
                source_kind="live" if broker in LIVE_BROKERS else "statement",
                last_success_at=(now - timedelta(seconds=1)).isoformat(),
            )
            for broker in REQUIRED_BROKERS
        },
    }

    health = project_account_sync_health(
        state,
        _controller_status(now),
        {"status": "ok", "last_success_at": now.isoformat(), "stale": False},
        now=now,
    )

    assert health["status"] == "abnormal"
    assert health["reason"] == "dashboard_projection_missing"


def test_health_marks_failed_projection_loop_abnormal() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    state = {
        "version": 1,
        "generation": "2026-07-30T11:59:59+08:00",
        "dashboard_projection": _valid_dashboard_projection(),
        "brokers": {
            broker: _source(
                source_kind="live" if broker in LIVE_BROKERS else "statement",
                last_success_at=(now - timedelta(seconds=1)).isoformat(),
            )
            for broker in REQUIRED_BROKERS
        },
    }
    controller = _controller_status(now)
    controller["quote_loop"] = {"status": "failed", "blocker": "dashboard_projection_failed"}

    health = project_account_sync_health(
        state,
        controller,
        {"status": "ok", "last_success_at": now.isoformat(), "stale": False},
        now=now,
    )

    assert health["status"] == "abnormal"
    assert health["reason"] == "quote_loop_failed"


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


def test_dashboard_projection_publishes_complete_live_and_statement_fields(
    tmp_path: Path,
) -> None:
    projection = build_dashboard_projection(
        _projection_state(tmp_path),
        {
            "status": "ok",
            "last_success_at": "2026-07-31T08:30:05+08:00",
            "stale": False,
            "quotes": {},
        },
        generated_at="2026-07-31T08:30:05+08:00",
    )

    rows = {(row["broker"], row["symbol"]): row for row in projection["broker_positions"]}
    tiger = rows[("tiger", "ADP")]
    phillips = rows[("phillips", "00200")]
    eastmoney = rows[("eastmoney", "000001")]

    assert tiger["market_value_hkd"] == "22640.05"
    assert tiger["price_kind"] == "account_snapshot"
    assert phillips["market_value_hkd"] == "1973.16"
    assert phillips["price_kind"] == "statement"
    assert eastmoney["market_value_hkd"] == "1080.00"
    assert all(row["account_weight_hkd"] for row in rows.values())
    assert all(row["portfolio_weight_hkd"] for row in rows.values())
    assert Decimal(projection["summary"]["portfolio_value_hkd"]) == sum(
        Decimal(row["portfolio_value_hkd"])
        for row in projection["broker_summaries"]
    )


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


def test_load_latest_statement_candidate_uses_statement_period_not_run_mtime(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_statement_run(
        data_dir / "runs" / "newer-directory",
        broker="phillips",
        statement_id="2026-07-10-phillips",
        symbol="OLD",
    )
    _write_statement_run(
        data_dir / "runs" / "older-directory",
        broker="phillips",
        statement_id="2026-07-15-phillips",
        symbol="LATEST",
    )
    _write_statement_run(
        data_dir / "runs" / "eastmoney-older-directory",
        broker="eastmoney",
        statement_id="2026-08-01-eastmoney",
        symbol="600001",
    )
    _write_statement_run(
        data_dir / "runs" / "eastmoney-newer-directory",
        broker="eastmoney",
        statement_id="2026-07-31-eastmoney",
        symbol="600002",
    )
    _write_statement_run(
        data_dir / "runs" / "failed",
        broker="phillips",
        statement_id="2026-08-01-phillips",
        symbol="IGNORED",
        manifest_status="failed",
    )
    _write_statement_run(
        data_dir / "runs" / "malformed",
        broker="eastmoney",
        statement_id="2026-09-01-eastmoney",
        symbol="IGNORED",
        quantity="not-a-number",
    )
    _write_truncated_statement_run(
        data_dir / "runs" / "truncated",
        broker="phillips",
        statement_id="2026-08-01-phillips",
    )
    _write_statement_run(
        data_dir / "runs" / "live-only",
        broker="futu",
        statement_id="2026-10-01-futu",
        symbol="IGNORED",
    )

    phillips = load_latest_statement_candidate(data_dir, "phillips")
    eastmoney = load_latest_statement_candidate(data_dir, "eastmoney")

    assert phillips is not None
    assert phillips.source_kind == "statement"
    assert phillips.period == "2026-07-15"
    assert phillips.data_as_of == "2026-07-15"
    assert [position.symbol for position in phillips.positions] == ["LATEST"]
    assert phillips.summary == {
        "position_count": 1,
        "cash_count": 1,
        "is_real_time": False,
    }
    assert eastmoney is not None
    assert eastmoney.source_kind == "statement"
    assert eastmoney.period == "2026-08"
    assert eastmoney.data_as_of == "2026-08-01"
    assert [position.symbol for position in eastmoney.positions] == ["600001"]
    assert eastmoney.summary["is_real_time"] is False


def test_load_latest_statement_candidate_never_selects_live_broker_artifacts(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_statement_run(
        data_dir / "runs" / "live",
        broker="tiger",
        statement_id="2026-07-30-tiger",
        symbol="AAPL",
    )

    assert load_latest_statement_candidate(data_dir, "phillips") is None
    assert load_latest_statement_candidate(data_dir, "eastmoney") is None


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


def _projection_state(tmp_path: Path) -> dict[str, object]:
    state = load_account_sync_state(tmp_path / "missing.json")
    candidates = (
        _candidate(),
        _projection_candidate(
            broker="tiger",
            market=Market.US,
            symbol="ADP",
            currency="USD",
            quantity="11",
            market_value="2902.57",
            cost_value="3067.9",
            fx_rate="7.8",
        ),
        _projection_candidate(
            broker="phillips",
            market=Market.HK,
            symbol="00200",
            currency="HKD",
            quantity="522",
            market_value="1973.16",
            cost_value="1800",
        ),
        _projection_candidate(
            broker="eastmoney",
            market=Market.CN,
            symbol="000001",
            currency="CNY",
            quantity="100",
            market_value="1000",
            cost_value="900",
        ),
    )
    for candidate in candidates:
        state = accept_candidate(
            state,
            candidate,
            attempted_at="2026-07-31T08:00:00+08:00",
        )
    return state


def _projection_candidate(
    *,
    broker: str,
    market: Market,
    symbol: str,
    currency: str,
    quantity: str,
    market_value: str,
    cost_value: str,
    fx_rate: str = "",
) -> BrokerAccountCandidate:
    source_kind = "live" if broker in LIVE_BROKERS else "statement"
    account_alias = f"{broker}_main"
    statement_id = (
        "2026-07-31-tiger-live"
        if broker == "tiger"
        else f"2026-07-31-{broker}"
    )
    return BrokerAccountCandidate(
        broker=broker,
        source_kind=source_kind,
        data_as_of="2026-07-31T08:00:00+08:00",
        period="2026-07",
        positions=(
            Position(
                statement_id=statement_id,
                broker=broker,
                account_alias=account_alias,
                market=market,
                asset_class=AssetClass.STOCK,
                symbol=symbol,
                name=symbol,
                currency=currency,
                quantity=Decimal(quantity),
                cost_price=Decimal(cost_value) / Decimal(quantity),
                last_price=Decimal(market_value) / Decimal(quantity),
                market_value=Decimal(market_value),
                cost_value=Decimal(cost_value),
                unrealized_pnl=Decimal(market_value) - Decimal(cost_value),
                confidence="high",
                notes="",
            ),
        ),
        cash=(
            CashBalance(
                statement_id=statement_id,
                broker=broker,
                account_alias=account_alias,
                currency=currency,
                cash_balance=Decimal("100"),
                available_balance=Decimal("100"),
                confidence="high",
                notes="",
            ),
        ),
        fx_rates=(
            ({"account_alias": account_alias, "currency": currency, "rate_to_hkd": fx_rate},)
            if fx_rate
            else ()
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


def _valid_dashboard_projection() -> dict[str, object]:
    return {
        "generated_at": "2026-07-30T11:59:59+08:00",
        "quote_as_of": "",
        "summary": {
            "holding_value_hkd": "0.00",
            "cash_like_value_hkd": "0.00",
            "portfolio_value_hkd": "0.00",
            "holding_weight_hkd": "0.00%",
            "cash_like_weight_hkd": "0.00%",
            "holding_count": 0,
            "broker_count": 4,
        },
        "broker_summaries": [
            {
                "broker": broker,
                "label": broker,
                "source_kind": "live" if broker in LIVE_BROKERS else "statement",
                "detail_available": True,
                "holding_value_hkd": "0.00",
                "cash_like_value_hkd": "0.00",
                "portfolio_value_hkd": "0.00",
                "holding_count": 0,
            }
            for broker in REQUIRED_BROKERS
        ],
        "broker_positions": [],
        "cash_details": [],
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


def _write_statement_run(
    run_dir: Path,
    *,
    broker: str,
    statement_id: str,
    symbol: str,
    manifest_status: str = "parsed",
    quantity: str = "1",
) -> None:
    period = statement_id[:10] if broker == "phillips" else statement_id[:7]
    position = Position(
        statement_id=statement_id,
        broker=broker,
        account_alias=f"{broker}_main",
        market=Market.HK if broker == "phillips" else Market.CN,
        asset_class=AssetClass.STOCK,
        symbol=symbol,
        name=symbol,
        currency="HKD" if broker == "phillips" else "CNY",
        quantity=Decimal(quantity) if quantity != "not-a-number" else Decimal("1"),
        cost_price=Decimal("10"),
        last_price=Decimal("11"),
        market_value=Decimal("11"),
        cost_value=Decimal("10"),
        unrealized_pnl=Decimal("1"),
        confidence="high",
        notes="statement",
    )
    cash = CashBalance(
        statement_id=statement_id,
        broker=broker,
        account_alias=f"{broker}_main",
        currency=position.currency,
        cash_balance=Decimal("100"),
        available_balance=Decimal("100"),
        confidence="high",
        notes="statement",
    )
    position_row = _position_to_row(position)
    position_row["quantity"] = quantity
    write_rows(
        run_dir / "manifest.csv",
        MANIFEST_FIELDNAMES,
        [{
            "month": period[:7],
            "broker": broker,
            "source_file": "statement.pdf",
            "source_sha256": "hash",
            "parsed_at": "2026-07-30T12:00:00+00:00",
            "page_count": "1",
            "parser_version": "test",
            "status": manifest_status,
        }],
    )
    write_rows(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [position_row],
    )
    write_rows(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [_cash_to_row(cash)],
    )


def _write_truncated_statement_run(
    run_dir: Path, *, broker: str, statement_id: str
) -> None:
    write_rows(
        run_dir / "manifest.csv",
        MANIFEST_FIELDNAMES,
        [{
            "month": statement_id[:7],
            "broker": broker,
            "source_file": "statement.pdf",
            "source_sha256": "hash",
            "parsed_at": "2026-07-30T12:00:00+00:00",
            "page_count": "1",
            "parser_version": "test",
            "status": "parsed",
        }],
    )
    write_rows(
        run_dir / "extracted_positions.csv",
        ["statement_id", "broker"],
        [{"statement_id": statement_id, "broker": broker}],
    )
    write_rows(run_dir / "extracted_cash.csv", CASH_FIELDNAMES, [])
