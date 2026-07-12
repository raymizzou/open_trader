from open_trader.dashboard_acceptance import (
    classify_result,
    dashboard_signature,
    validate_dashboard_payload,
)


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
