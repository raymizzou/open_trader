from decimal import Decimal

from open_trader.dashboard_acceptance import (
    _is_actionable_console_error,
    classify_result,
    dashboard_signature,
    validate_dashboard_payload,
)


def test_browser_ignores_chrome_unattributed_404_but_not_app_errors() -> None:
    assert not _is_actionable_console_error(
        "Failed to load resource: the server responded with a status of 404 (Not Found)"
    )
    assert _is_actionable_console_error("Uncaught TypeError: failed")


def valid_payload() -> dict[str, object]:
    cn = [
        {"market": "CN", "symbol": str(index), "portfolio_weight_hkd": "10.00%"}
        for index in range(5)
    ]
    other = [{"market": "US", "symbol": "MSFT", "portfolio_weight_hkd": "50.00%"}]
    return {
        "holdings": cn + other,
        "cash_rows": [],
        "backtest_universe": {"holdings": [
            {"market": "CN", "symbol": row["symbol"]} for row in cn
        ]},
    }


def test_validate_dashboard_payload_accepts_real_contract() -> None:
    assert validate_dashboard_payload(valid_payload(), expected_cn=5) == []


def test_validate_dashboard_payload_rejects_bad_counts_and_weights() -> None:
    payload = valid_payload()
    payload["holdings"][0]["portfolio_weight_hkd"] = "9.99%"  # type: ignore[index]
    payload["backtest_universe"] = {"holdings": []}

    errors = validate_dashboard_payload(payload, expected_cn=5)

    assert "组合权重合计不是 100.00%：99.99%" in errors
    assert "A 股回测标的数量不是 5：0" in errors


def test_validate_dashboard_payload_checks_eastmoney_statement_total_assets() -> None:
    payload = valid_payload()
    for row in payload["holdings"][:5]:  # type: ignore[index]
        row.update({"brokers": "eastmoney", "currency": "CNY", "market_value": "10"})
    payload["cash_rows"] = [{
        "market": "CASH", "symbol": "CNY_CASH", "brokers": "eastmoney",
        "currency": "CNY", "market_value": "50", "portfolio_weight_hkd": "0.00%",
    }]

    assert validate_dashboard_payload(
        payload, expected_cn=5, expected_eastmoney_cny=Decimal("100")
    ) == []

    errors = validate_dashboard_payload(
        payload, expected_cn=5, expected_eastmoney_cny=Decimal("101")
    )
    assert "东方财富总资产不匹配：100 != 101 CNY" in errors


def test_validate_dashboard_payload_checks_full_portfolio_preservation() -> None:
    payload = valid_payload()
    payload["holdings"][0]["brokers"] = "eastmoney;phillips"  # type: ignore[index]

    errors = validate_dashboard_payload(
        payload, expected_cn=5, expected_rows=33, expected_phillips_rows=7
    )

    assert "组合总行数不是 33：6" in errors
    assert "辉立关联持仓行数不是 7：1" in errors


def test_validate_dashboard_payload_rejects_empty_phillips_account_card() -> None:
    payload = valid_payload()
    payload["broker_summaries"] = [{
        "broker": "phillips", "detail_available": False, "portfolio_value_hkd": ""
    }]
    payload["source_statuses"] = [{
        "broker": "phillips", "display_text": "暂无月结单明细"
    }]

    errors = validate_dashboard_payload(
        payload, expected_cn=5, expected_phillips_rows=0
    )

    assert "辉立账户卡没有可用月结单资产" in errors


def test_classify_result_has_only_three_states() -> None:
    assert classify_result([], browser_blocker=None) == "PASS"
    assert classify_result(["API failed"], browser_blocker=None) == "FAIL"
    assert classify_result([], browser_blocker="Chrome unavailable") == "BLOCKED"
    assert classify_result(["API failed"], browser_blocker="Chrome unavailable") == "FAIL"


def test_dashboard_signature_ignores_refresh_metadata_but_detects_data_change() -> None:
    first = valid_payload()
    second = valid_payload()
    first["last_refresh"] = "one"
    second["last_refresh"] = "two"
    assert dashboard_signature(first) == dashboard_signature(second)

    second["holdings"][0]["portfolio_weight_hkd"] = "9.99%"  # type: ignore[index]
    assert dashboard_signature(first) != dashboard_signature(second)
