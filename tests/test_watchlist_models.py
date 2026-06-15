from open_trader.advice.models import WATCHLIST_FIELDNAMES, WatchlistRow


def test_watchlist_row_to_row_has_stable_csv_fields() -> None:
    row = WatchlistRow(
        run_date="2026-06-16",
        symbol="VIXY",
        market="US",
        suggested_action="reduce",
        severity="high",
        portfolio_weight_hkd="3.05%",
        trigger_type="price",
        operator="<=",
        trigger_price="95",
        trigger_text="below 95",
        status="active",
        error="",
    )

    serialized = row.to_row()

    assert list(serialized) == WATCHLIST_FIELDNAMES
    assert serialized == {
        "run_date": "2026-06-16",
        "symbol": "VIXY",
        "market": "US",
        "suggested_action": "reduce",
        "severity": "high",
        "portfolio_weight_hkd": "3.05%",
        "trigger_type": "price",
        "operator": "<=",
        "trigger_price": "95",
        "trigger_text": "below 95",
        "status": "active",
        "error": "",
    }
