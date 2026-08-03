from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from open_trader.account_snapshot import load_account_snapshot
from open_trader.account_sync_state import (
    LIVE_BROKERS,
    REQUIRED_BROKERS,
    BrokerAccountCandidate,
    accept_candidate,
    empty_account_sync_state,
    with_dashboard_projection,
    write_json_atomic,
)
from open_trader.models import AssetClass, CashBalance, Market, Position


SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime.fromisoformat("2026-08-03T12:00:05+08:00")


def _contract_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _write_publication(data_dir: Path, *, worker_sha: str = SHA) -> None:
    account_as_of = "2026-08-03T12:00:00+08:00"
    quote_as_of = "2026-08-03T12:00:04+08:00"
    state = empty_account_sync_state()
    for index, broker in enumerate(REQUIRED_BROKERS):
        live = broker in LIVE_BROKERS
        alias = f"{broker}_main"
        state = accept_candidate(
            state,
            BrokerAccountCandidate(
                broker=broker,
                source_kind="live" if live else "statement",
                data_as_of=account_as_of if live else "2026-07-31",
                period="2026-08" if live else "2026-07",
                positions=(
                    Position(
                        statement_id="" if live else f"2026-07-31-{broker}",
                        broker=broker,
                        account_alias=alias,
                        market=Market.US,
                        asset_class=AssetClass.STOCK,
                        symbol=f"TEST{index}",
                        name=f"Test {index}",
                        currency="USD",
                        quantity=Decimal("1"),
                        cost_price=Decimal("10"),
                        last_price=Decimal("11"),
                        market_value=Decimal("11"),
                        cost_value=Decimal("10"),
                        unrealized_pnl=Decimal("1"),
                        confidence="high",
                        notes="",
                    ),
                ),
                cash=(
                    CashBalance(
                        statement_id="" if live else f"2026-07-31-{broker}",
                        broker=broker,
                        account_alias=alias,
                        currency="USD",
                        cash_balance=Decimal("5"),
                        available_balance=Decimal("4"),
                        confidence="high",
                        notes="",
                    ),
                ),
                fx_rates=(
                    {"account_alias": alias, "currency": "USD", "rate_to_hkd": "7.8"},
                ) if live else (),
                summary={"position_count": 1, "cash_count": 1},
            ),
            attempted_at=account_as_of,
        )
    quotes = {
        "status": "ok",
        "requested_count": 2,
        "quote_count": 2,
        "missing_count": 0,
        "fetched_at": quote_as_of,
        "last_success_at": quote_as_of,
        "stale": False,
        "quotes": {
            f"US.TEST{index}": {
                "market": "US",
                "symbol": f"TEST{index}",
                "status": "ok",
                "last_price": "11",
                "price_session": "regular",
                "price_time": quote_as_of,
                "fetched_at": quote_as_of,
                "stale": False,
            }
            for index in (0, 1)
        },
        "diagnostic": {},
    }
    state = with_dashboard_projection(state, quotes, generated_at=quote_as_of)
    write_json_atomic(data_dir / "latest/account_sync_state.json", state)
    write_json_atomic(data_dir / "latest/quotes.json", quotes)
    write_json_atomic(
        data_dir / "account_sync/controller_status.json",
        {
            "schema_version": "open_trader.account_sync.controller.v1",
            "pid": 123,
            "started_at": account_as_of,
            "working_directory": "/tmp/open-trader",
            "git_sha": worker_sha,
            "heartbeat_at": quote_as_of,
            "phase": "idle",
            "account_loop": {"status": "ok"},
            "quote_loop": {"status": "ok"},
            "blocker": None,
        },
    )


def test_snapshot_maps_current_publication_to_frozen_v1_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["schema_version"] == 1
    assert result.payload["status"] == "healthy"
    assert result.payload["stale"] is False
    assert result.payload["generated_at"] == "2026-08-03T12:00:04+08:00"
    assert result.payload["quote_as_of"] == "2026-08-03T12:00:04+08:00"
    assert result.payload["release"] == {"api_git_sha": SHA, "worker_git_sha": SHA}
    assert result.payload["sources"]["account"]["as_of"] == "2026-08-03T12:00:00+08:00"
    assert [row["broker"] for row in result.payload["broker_summaries"]] == sorted(REQUIRED_BROKERS)
    assert result.payload["positions"] == sorted(
        result.payload["positions"],
        key=lambda row: (
            row["broker"], row["account_alias"], row["market"],
            row["asset_class"], row["symbol"], row["position_id"],
        ),
    )
    assert result.payload["cash_balances"] == sorted(
        result.payload["cash_balances"],
        key=lambda row: (row["broker"], row["account_alias"], row["currency"]),
    )
    position = next(row for row in result.payload["positions"] if row["broker"] == "futu")
    canonical = json.dumps(["US", "stock", "TEST0"], ensure_ascii=False, separators=(",", ":"))
    instrument_id = "ins_" + hashlib.sha256(canonical.encode()).hexdigest()
    assert position["instrument_id"] == instrument_id
    assert position["position_id"] == "pos_" + hashlib.sha256(
        json.dumps(["futu", "futu_main", instrument_id], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert result.payload["account_generation"].startswith("sha256:")
    assert result.payload["snapshot_generation"].startswith("sha256:")
    assert result.etag == f'"account-v1-{result.payload["snapshot_generation"].removeprefix("sha256:")}"'
    account_input = {
        "summary": result.payload["summary"],
        "broker_summaries": result.payload["broker_summaries"],
        "positions": result.payload["positions"],
        "cash_balances": result.payload["cash_balances"],
        "accepted_account_as_of": result.payload["sources"]["account"]["as_of"],
        "accepted_broker_data_as_of": {
            broker: result.payload["sources"]["account"]["brokers"][broker]["data_as_of"]
            for broker in sorted(REQUIRED_BROKERS)
        },
    }
    assert result.payload["account_generation"] == _contract_sha(account_input)
    visible = dict(result.payload)
    visible.pop("snapshot_generation")
    assert result.payload["snapshot_generation"] == _contract_sha(visible)
    assert not ({"risk_flag", "actionable", "decision_plan"} & result.payload.keys())


def test_snapshot_whitelists_public_fields_and_requires_quote_publication_time(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    state_path = data_dir / "latest/account_sync_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    projection = state["dashboard_projection"]
    projection["summary"]["risk_flag"] = "overweight"
    projection["broker_summaries"][0]["actionable"] = True
    projection["broker_positions"][0]["decision_plan"] = {"action": "buy"}
    projection["cash_details"][0]["research"] = "private"
    write_json_atomic(state_path, state)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert "risk_flag" not in result.payload["summary"]
    assert "actionable" not in result.payload["broker_summaries"][0]
    assert "decision_plan" not in result.payload["positions"][0]
    assert "research" not in result.payload["cash_balances"][0]

    quotes_path = data_dir / "latest/quotes.json"
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    quotes["last_success_at"] = "2026-08-03T12:00:03+08:00"
    write_json_atomic(quotes_path, quotes)

    with pytest.raises(ValueError, match="quote publication time"):
        load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)
