from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from open_trader.a_share_trend import load_real_holding_input


BROKERS = ("eastmoney", "futu", "phillips", "tiger")


def account_snapshot() -> dict[str, object]:
    positions = []
    cash_balances = []
    for market, broker, currency, symbol, instrument_id in (
        ("CN", "eastmoney", "CNY", "SH.600001", "ins_cn"),
        ("HK", "phillips", "HKD", "700", "ins_hk"),
        ("US", "tiger", "USD", "AAPL", "ins_us"),
    ):
        positions.append({
            "broker": broker,
            "account_alias": f"{broker}_main",
            "market": market,
            "asset_class": "stock",
            "symbol": symbol,
            "name": f"{market} holding",
            "currency": currency,
            "quantity": "100",
            "cost_price": "9.5",
            "market_value": "1234.5",
            "instrument_id": instrument_id,
            "position_id": f"pos_{market.lower()}",
        })
        cash_balances.append({
            "broker": broker,
            "account_alias": f"{broker}_main",
            "currency": currency,
            "cash_balance": "20",
            "available_balance": "10",
        })
    return {
        "snapshot_generation": "sha256:" + "a" * 64,
        "account_generation": "sha256:" + "b" * 64,
        "status": "healthy",
        "sources": {
            "account": {
                "status": "healthy",
                "as_of": "2026-08-04T12:00:00+08:00",
                "reason": None,
                "brokers": {
                    broker: {
                        "source_kind": "live" if broker == "tiger" else "statement",
                        "data_as_of": "2026-08-04T12:00:00+08:00",
                        "last_success_at": "2026-08-04T12:00:00+08:00",
                        "status": "healthy",
                        "reason": None,
                    }
                    for broker in BROKERS
                },
            },
            "quotes": {
                "status": "healthy",
                "as_of": "2026-08-04T12:00:00+08:00",
                "reason": None,
            },
        },
        "positions": positions,
        "cash_balances": cash_balances,
    }


@pytest.mark.parametrize(
    ("market", "broker", "currency", "symbol", "instrument_id"),
    [
        ("CN", "eastmoney", "CNY", "600001", "ins_cn"),
        ("HK", "phillips", "HKD", "00700", "ins_hk"),
        ("US", "tiger", "USD", "AAPL", "ins_us"),
    ],
)
def test_real_input_projects_only_the_market_trend_broker(
    tmp_path: Path,
    market: str,
    broker: str,
    currency: str,
    symbol: str,
    instrument_id: str,
) -> None:
    snapshot = account_snapshot()

    loaded = load_real_holding_input(
        snapshot,
        market,
        state_path=tmp_path / "real_protection_state.json",
    )

    assert loaded.status == "available"
    assert [(position.symbol, position.quantity, position.market_value) for position in loaded.positions] == [
        (symbol, 100, pytest.approx(1234.5))
    ]
    assert loaded.available_cash == 10
    assert loaded.net_value == pytest.approx(1244.5)
    assert loaded.source["broker"] == broker
    assert loaded.source["snapshot_period"] == "2026-08-04T12:00:00+08:00"
    assert loaded.instrument_ids_by_symbol == {symbol: instrument_id}
    assert loaded.blocked_instrument_ids == {}
    assert currency in {
        row["currency"] for row in snapshot["cash_balances"]  # type: ignore[index]
        if row["broker"] == broker
    }


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("quantity", "bad", "数量或标的"),
        ("market_value", "-1", "市值"),
        ("currency", "USD", "币种"),
    ],
)
def test_real_input_fails_closed_for_invalid_account_position(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    snapshot = account_snapshot()
    row = next(
        item for item in snapshot["positions"]  # type: ignore[index]
        if item["broker"] == "phillips"
    )
    row[field] = value

    loaded = load_real_holding_input(
        snapshot,
        "HK",
        state_path=tmp_path / "real_protection_state.json",
    )

    assert loaded.status == "unavailable"
    assert loaded.positions == ()
    assert reason in loaded.reason


@pytest.mark.parametrize(
    ("source", "reason"),
    [("broker", "account_broker_stale:phillips"), ("quotes", "account_quotes_stale")],
)
def test_real_input_blocks_instruments_from_unhealthy_required_sources(
    tmp_path: Path, source: str, reason: str
) -> None:
    snapshot = account_snapshot()
    if source == "broker":
        broker = snapshot["sources"]["account"]["brokers"]["phillips"]  # type: ignore[index]
        broker.update(status="stale", reason="broker_refresh_failed")
    else:
        quotes = snapshot["sources"]["quotes"]  # type: ignore[index]
        quotes.update(status="stale", reason="quotes_refresh_failed")
    snapshot["status"] = "stale"

    loaded = load_real_holding_input(
        snapshot,
        "HK",
        state_path=tmp_path / "real_protection_state.json",
    )

    assert loaded.status == "available"
    assert [position.symbol for position in loaded.positions] == ["00700"]
    assert loaded.blocked_instrument_ids == {"ins_hk": reason}


def test_real_input_applies_one_block_to_every_row_of_an_instrument(
    tmp_path: Path,
) -> None:
    snapshot = account_snapshot()
    original = next(
        item for item in snapshot["positions"]  # type: ignore[index]
        if item["broker"] == "tiger"
    )
    second = deepcopy(original)
    second.update(account_alias="tiger_secondary", position_id="pos_us_secondary")
    snapshot["positions"].append(second)  # type: ignore[union-attr]
    broker = snapshot["sources"]["account"]["brokers"]["tiger"]  # type: ignore[index]
    broker.update(status="stale", reason="broker_refresh_failed")
    snapshot["status"] = "stale"

    loaded = load_real_holding_input(
        snapshot,
        "US",
        state_path=tmp_path / "real_protection_state.json",
    )

    assert len(loaded.positions) == 2
    assert loaded.instrument_ids_by_symbol == {"AAPL": "ins_us"}
    assert loaded.blocked_instrument_ids == {
        "ins_us": "account_broker_stale:tiger"
    }


def test_real_input_never_scans_run_csvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "runs/2026-08-04").mkdir(parents=True)
    for name in ("extracted_positions.csv", "extracted_cash.csv"):
        (data_dir / "runs/2026-08-04" / name).write_text("forbidden", encoding="utf-8")
    monkeypatch.chdir(data_dir)

    loaded = load_real_holding_input(
        account_snapshot(),
        "CN",
        state_path=data_dir / "real_protection_state.json",
    )

    assert loaded.status == "available"


def test_real_input_tolerates_multi_currency_and_negative_cash(
    tmp_path: Path,
) -> None:
    snapshot = account_snapshot()
    tiger_position = next(
        row for row in snapshot["positions"]  # type: ignore[index]
        if row["broker"] == "tiger"
    )
    tiger_position["current_valuation"] = {
        "market_value_usd": "1234.5",
        "market_value_hkd": "12345",
    }
    tiger_cash = next(
        row for row in snapshot["cash_balances"]  # type: ignore[index]
        if row["broker"] == "tiger"
    )
    tiger_cash.update(
        currency="HKD",
        cash_balance="1000",
        available_balance="1000",
        cash_balance_hkd="1000",
        available_balance_hkd="1000",
    )
    snapshot["cash_balances"].append({  # type: ignore[union-attr]
        "broker": "tiger",
        "account_alias": "tiger_main",
        "currency": "USD",
        "cash_balance": "-200",
        "available_balance": "-200",
        "cash_balance_hkd": "-1560",
        "available_balance_hkd": "-1560",
    })

    loaded = load_real_holding_input(
        snapshot,
        "US",
        state_path=tmp_path / "real_protection_state.json",
    )

    assert loaded.status == "available"
    assert [position.symbol for position in loaded.positions] == ["AAPL"]
    assert loaded.available_cash == pytest.approx(-100)
    assert loaded.net_value == pytest.approx(1134.5)


def test_real_input_fails_closed_for_malformed_cash(tmp_path: Path) -> None:
    snapshot = account_snapshot()
    tiger_cash = next(
        row for row in snapshot["cash_balances"]  # type: ignore[index]
        if row["broker"] == "tiger"
    )
    tiger_cash["available_balance"] = "bad"

    loaded = load_real_holding_input(
        snapshot,
        "US",
        state_path=tmp_path / "real_protection_state.json",
    )

    assert loaded.status == "unavailable"
    assert "可用现金" in loaded.reason
