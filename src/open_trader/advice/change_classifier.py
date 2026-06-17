from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import ChangeClassification, PortfolioInputRow, TradingAdvice


PROMPT_PATH = Path(__file__).parent / "prompts" / "change_classifier.md"
CHANGE_TYPES = {
    "new_signal",
    "action_changed",
    "risk_changed",
    "trigger_changed",
    "no_material_change",
}
SEVERITIES = {"low", "medium", "high"}
REQUIRED_CLASSIFICATION_FIELDS = {
    "include_in_report",
    "change_type",
    "severity",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
}
STRING_CLASSIFICATION_FIELDS = {
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
}
DEFAULT_CLASSIFIER_MODEL = "deepseek-v4-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

CLASSIFICATION_JSON_SCHEMA = {
    "name": "premarket_change_classification",
    "schema": {
        "type": "object",
        "properties": {
            "include_in_report": {"type": "boolean"},
            "change_type": {"type": "string", "enum": sorted(CHANGE_TYPES)},
            "severity": {"type": "string", "enum": sorted(SEVERITIES)},
            "suggested_action": {"type": "string"},
            "summary": {"type": "string"},
            "rationale": {"type": "string"},
            "watch_trigger": {"type": "string"},
        },
        "required": [
            "include_in_report",
            "change_type",
            "severity",
            "suggested_action",
            "summary",
            "rationale",
            "watch_trigger",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}


class InvalidClassificationError(ValueError):
    pass


class ClassifierClient(Protocol):
    def classify(self, prompt: str, payload: dict[str, object]) -> str:
        pass


class ChangeClassifier:
    def __init__(self, client: ClassifierClient) -> None:
        self._client = client
        self._prompt = load_prompt()

    def classify(
        self,
        *,
        run_date: str,
        portfolio_row: PortfolioInputRow,
        previous_advice: dict[str, str] | None,
        latest_advice: TradingAdvice,
    ) -> ChangeClassification:
        if latest_advice.status != "ok":
            return _error_classification(
                run_date=run_date,
                symbol=latest_advice.symbol,
                error=latest_advice.error,
            )

        try:
            payload = build_classifier_payload(
                run_date=run_date,
                portfolio_row=portfolio_row,
                previous_advice=previous_advice,
                latest_advice=latest_advice,
            )
            parsed = validate_classifier_output(
                self._client.classify(self._prompt, payload)
            )
        except Exception as exc:
            return _error_classification(
                run_date=run_date,
                symbol=latest_advice.symbol,
                error=str(exc),
            )

        return ChangeClassification(
            run_date=run_date,
            symbol=latest_advice.symbol,
            include_in_report=parsed.include_in_report,
            change_type=parsed.change_type,
            severity=parsed.severity,
            suggested_action=parsed.suggested_action,
            summary=parsed.summary,
            rationale=parsed.rationale,
            watch_trigger=parsed.watch_trigger,
            status="ok",
            error="",
        )


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_classifier_payload(
    *,
    run_date: str,
    portfolio_row: PortfolioInputRow,
    previous_advice: dict[str, str] | None,
    latest_advice: TradingAdvice,
) -> dict[str, object]:
    return {
        "run_date": run_date,
        "portfolio": {
            "symbol": portfolio_row.symbol,
            "market": portfolio_row.market,
            "asset_class": portfolio_row.asset_class,
            "name": portfolio_row.name,
            "portfolio_weight_hkd": portfolio_row.portfolio_weight_hkd,
            "market_value_hkd": portfolio_row.market_value_hkd,
            "risk_flag": portfolio_row.risk_flag,
        },
        "previous_advice": previous_advice,
        "latest_advice": latest_advice.to_row(),
    }


def validate_classifier_output(raw: str) -> _ParsedClassification:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidClassificationError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidClassificationError("classification output must be an object")

    fields = set(data)
    missing = sorted(REQUIRED_CLASSIFICATION_FIELDS - fields)
    if missing:
        raise InvalidClassificationError(f"missing field(s): {', '.join(missing)}")
    extra = sorted(fields - REQUIRED_CLASSIFICATION_FIELDS)
    if extra:
        raise InvalidClassificationError(f"unexpected field(s): {', '.join(extra)}")
    if not isinstance(data["include_in_report"], bool):
        raise InvalidClassificationError("include_in_report must be boolean")
    if data["change_type"] not in CHANGE_TYPES:
        raise InvalidClassificationError(f"invalid change_type: {data['change_type']}")
    if data["severity"] not in SEVERITIES:
        raise InvalidClassificationError(f"invalid severity: {data['severity']}")
    for field in sorted(STRING_CLASSIFICATION_FIELDS):
        if not isinstance(data[field], str):
            raise InvalidClassificationError(f"{field} must be string")

    return _ParsedClassification(
        include_in_report=data["include_in_report"],
        change_type=data["change_type"],
        severity=data["severity"],
        suggested_action=data["suggested_action"],
        summary=data["summary"],
        rationale=data["rationale"],
        watch_trigger=data["watch_trigger"],
    )


class OpenAIClassifierClient:
    def __init__(self, *, model: str = DEFAULT_CLASSIFIER_MODEL) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=os.environ.get("DEEPSEEK_API_KEY"),
            base_url=DEEPSEEK_BASE_URL,
        )
        self._model = model

    def classify(self, prompt: str, payload: dict[str, object]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise InvalidClassificationError("model returned empty content")
        return content


def _error_classification(
    *,
    run_date: str,
    symbol: str,
    error: str,
) -> ChangeClassification:
    return ChangeClassification(
        run_date=run_date,
        symbol=symbol,
        include_in_report=False,
        change_type="no_material_change",
        severity="low",
        suggested_action="",
        summary="",
        rationale="",
        watch_trigger="",
        status="error",
        error=error,
    )


@dataclass(frozen=True)
class _ParsedClassification:
    include_in_report: bool
    change_type: str
    severity: str
    suggested_action: str
    summary: str
    rationale: str
    watch_trigger: str
