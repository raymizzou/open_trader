from __future__ import annotations

import json

import pytest

from open_trader.advice.change_classifier import (
    ChangeClassifier,
    InvalidClassificationError,
    build_classifier_payload,
    load_prompt,
    validate_classifier_output,
)
from open_trader.advice.models import PortfolioInputRow, TradingAdvice


def portfolio_row() -> PortfolioInputRow:
    return PortfolioInputRow(
        symbol="VIXY",
        market="US",
        asset_class="etf",
        name="Volatility ETF",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        analysis_symbol="VIXY",
    )


def latest_advice(action: str = "reduce") -> TradingAdvice:
    return TradingAdvice(
        run_date="2026-06-16",
        symbol="VIXY",
        market="US",
        asset_class="etf",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        source="tradingagents",
        advice_action=action,
        advice_summary=f"Latest action is {action}.",
        raw_decision='{"action":"reduce"}',
        status="ok",
        error="",
    )


def test_load_prompt_reads_version_controlled_prompt() -> None:
    prompt = load_prompt()

    assert "include_in_report" in prompt
    assert "previous advice" in prompt.lower()
    assert "latest tradingagents advice" in prompt.lower()


def test_build_classifier_payload_includes_previous_and_latest_advice() -> None:
    payload = build_classifier_payload(
        run_date="2026-06-16",
        portfolio_row=portfolio_row(),
        previous_advice={"advice_action": "hold", "advice_summary": "Old hold."},
        latest_advice=latest_advice(),
    )

    assert payload["portfolio"]["symbol"] == "VIXY"
    assert payload["previous_advice"]["advice_action"] == "hold"
    assert payload["latest_advice"]["advice_action"] == "reduce"


def test_validate_classifier_output_accepts_valid_json() -> None:
    output = validate_classifier_output(
        json.dumps(
            {
                "include_in_report": True,
                "change_type": "action_changed",
                "severity": "high",
                "suggested_action": "reduce",
                "summary": "VIXY changed from hold to reduce.",
                "rationale": "Latest advice materially changed.",
                "watch_trigger": "If price loses prior support.",
            }
        )
    )

    assert output.include_in_report is True
    assert output.change_type == "action_changed"
    assert output.severity == "high"


def test_validate_classifier_output_rejects_invalid_enum() -> None:
    with pytest.raises(InvalidClassificationError, match="change_type"):
        validate_classifier_output(
            json.dumps(
                {
                    "include_in_report": True,
                    "change_type": "urgent",
                    "severity": "high",
                    "suggested_action": "reduce",
                    "summary": "summary",
                    "rationale": "rationale",
                    "watch_trigger": "",
                }
            )
        )


def test_change_classifier_uses_client_response() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def classify(self, prompt: str, payload: dict[str, object]) -> str:
            self.payloads.append(payload)
            return json.dumps(
                {
                    "include_in_report": True,
                    "change_type": "new_signal",
                    "severity": "medium",
                    "suggested_action": "watch",
                    "summary": "New watch item.",
                    "rationale": "No previous advice exists.",
                    "watch_trigger": "",
                }
            )

    client = FakeClient()
    classifier = ChangeClassifier(client=client)

    result = classifier.classify(
        run_date="2026-06-16",
        portfolio_row=portfolio_row(),
        previous_advice=None,
        latest_advice=latest_advice("watch"),
    )

    assert result.symbol == "VIXY"
    assert result.status == "ok"
    assert result.include_in_report is True
    assert client.payloads[0]["previous_advice"] is None
