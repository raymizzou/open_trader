from __future__ import annotations

import json
import hashlib
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.predict_cross_venue import (
    CROSS_EXCHANGE_YES_NO_EQUIVALENCE_PROMPT_VERSION,
    CodexCrossVenueEquivalenceValidator,
    ExplicitMarketPair,
    resolve_explicit_market_pairs,
)
from open_trader.predict_source import PredictMarket
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def predict_market(*, external_ids: tuple[str, ...]) -> PredictMarket:
    return PredictMarket(
        market_id="predict-market-1",
        condition_id="predict-native-condition-1",
        question="Will the public test event resolve Yes?",
        rules="This public test event resolves from the named source.",
        resolution_source="Public Test Oracle",
        close_at=datetime(2026, 12, 31, tzinfo=UTC),
        settlement_at=datetime(2027, 1, 1, tzinfo=UTC),
        yes_token_id="predict-yes-1",
        no_token_id="predict-no-1",
        settlement_asset="USDT",
        minimum_order_size=Decimal("5"),
        tick_size=Decimal("0.01"),
        fee_rate_bps=Decimal("200"),
        polymarket_condition_ids=external_ids,
        rules_fingerprint="predict-fingerprint-1",
    )


def polymarket_row(condition_id: str) -> dict[str, object]:
    return {
        "id": "poly-market-1",
        "conditionId": condition_id,
        "question": "Will the public test event resolve Yes?",
        "description": "This public test event resolves from the named source.",
        "resolutionSource": "Public Test Oracle",
        "endDate": "2026-12-31T00:00:00Z",
        "resolutionDate": "2027-01-01T00:00:00Z",
        "clobTokenIds": '["poly-yes-1", "poly-no-1"]',
        "outcomes": '["Yes", "No"]',
        "orderMinSize": "5",
        "orderPriceMinTickSize": "0.01",
        "feesEnabled": True,
        "feeSchedule": {"rate": "0.02"},
    }


def test_mapping_requests_every_external_id_checks_both_gamma_states_and_uses_exact_clob_fallback() -> None:
    gamma_calls: list[tuple[tuple[str, ...], bool]] = []
    clob_calls: list[str] = []

    def gamma(condition_ids: tuple[str, ...], *, closed: bool) -> list[object]:
        gamma_calls.append((condition_ids, closed))
        return [polymarket_row("poly-a")] if not closed else []

    def clob(condition_id: str) -> object:
        clob_calls.append(condition_id)
        return polymarket_row(condition_id) if condition_id == "poly-b" else None

    result = resolve_explicit_market_pairs(
        (predict_market(external_ids=("poly-a", "", "poly-b")),),
        gamma_lookup=gamma,
        clob_lookup=clob,
    )

    assert gamma_calls == [(("poly-a", "poly-b"), False), (("poly-a", "poly-b"), True)]
    assert clob_calls == ["poly-b"]
    assert [pair.polymarket.condition_id for pair in result.pairs] == ["poly-a", "poly-b"]
    assert result.skipped_empty_mappings == 1
    assert result.skipped_unresolved_mappings == 0


def test_mapping_uses_only_external_polymarket_ids_and_counts_unresolved_values() -> None:
    clob_calls: list[str] = []

    def clob(condition_id: str) -> object:
        clob_calls.append(condition_id)
        return polymarket_row("poly-external") if condition_id == "poly-external" else None

    market = replace(
        predict_market(external_ids=("poly-external", "poly-missing", "")),
        condition_id="poly-native-but-not-external",
    )

    result = resolve_explicit_market_pairs(
        (market,),
        gamma_lookup=lambda *args, **kwargs: [polymarket_row("poly-native-but-not-external")],
        clob_lookup=clob,
    )

    assert [pair.polymarket.condition_id for pair in result.pairs] == ["poly-external"]
    assert clob_calls == ["poly-external", "poly-missing"]
    assert result.skipped_empty_mappings == 1
    assert result.skipped_unresolved_mappings == 1


def test_mapping_skips_polymarket_rows_without_actual_resolution_time() -> None:
    result = resolve_explicit_market_pairs(
        (predict_market(external_ids=("poly-no-resolution",)),),
        gamma_lookup=lambda *args, **kwargs: [
            {key: value for key, value in polymarket_row("poly-no-resolution").items() if key != "resolutionDate"}
        ],
        clob_lookup=lambda condition_id: None,
    )

    assert result.pairs == ()
    assert result.skipped_unresolved_mappings == 1


def test_mapping_counts_normalized_empty_external_id_tuples() -> None:
    result = resolve_explicit_market_pairs(
        (predict_market(external_ids=()),),
        gamma_lookup=lambda *args, **kwargs: pytest.fail("Gamma called"),
        clob_lookup=lambda condition_id: pytest.fail("CLOB called"),
    )

    assert result.pairs == ()
    assert result.skipped_empty_mappings == 1
    assert result.skipped_unresolved_mappings == 0


def test_pair_id_is_deterministic_from_venue_qualified_native_condition_ids() -> None:
    pair = explicit_pair()

    assert pair.pair_id == hashlib.sha256(
        b'{"polymarket":"polymarket:poly-condition","predict":"predict.fun:predict-native-condition-1"}'
    ).hexdigest()


def explicit_pair() -> ExplicitMarketPair:
    return resolve_explicit_market_pairs(
        (predict_market(external_ids=("poly-condition",)),),
        gamma_lookup=lambda *args, **kwargs: [polymarket_row("poly-condition")],
        clob_lookup=lambda condition_id: None,
    ).pairs[0]


def equivalence_result(pair: ExplicitMarketPair) -> dict[str, object]:
    return {
        "schema_version": 1,
        "decision": "APPROVE",
        "summary": "The supplied rules exclude both divergent settlement states.",
        "predict": {
            "exchange": "predict.fun",
            "condition_id": pair.predict.condition_id,
            "rules_fingerprint": pair.predict.rules_fingerprint,
        },
        "polymarket": {
            "exchange": "polymarket",
            "condition_id": pair.polymarket.condition_id,
            "rules_fingerprint": pair.polymarket.rules_fingerprint,
        },
        "divergent_states": {
            "PREDICT_YES_POLYMARKET_NO": {"possible": False, "reason": "same rule"},
            "POLYMARKET_YES_PREDICT_NO": {"possible": False, "reason": "same rule"},
        },
        "evidence": [
            {"exchange": "predict.fun", "field": "rules", "quote": pair.predict.rules},
            {"exchange": "polymarket", "field": "rules", "quote": pair.polymarket.rules},
        ],
        "uncertainties": [],
    }


def codex_jsonl(result: dict[str, object]) -> str:
    return "\n".join(
        (
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
        )
    )


def test_equivalence_approval_uses_required_namespace_schema_and_cache(tmp_path: Path) -> None:
    pair = explicit_pair()
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=codex_jsonl(equivalence_result(pair)), stderr="")

    store = PredictionArbitrageStore(tmp_path / "data")
    validator = CodexCrossVenueEquivalenceValidator(store, model="gpt-test", runner=runner)

    first = validator.validate(pair)
    second = validator.validate(pair)

    assert first.approved is True
    assert first.prompt_version == "cross-exchange-yes-no-equivalence-v1"
    assert second.approved is True
    assert len(calls) == 1
    assert CROSS_EXCHANGE_YES_NO_EQUIVALENCE_PROMPT_VERSION == "cross-exchange-yes-no-equivalence-v1"
    assert Path(calls[0][calls[0].index("--output-schema") + 1]).name == "cross_exchange_yes_no_equivalence.json"
    assert store.llm_usage_24h()["cache_hits"] == 1


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: {**value, "predict": {**value["predict"], "condition_id": "wrong"}}, "IDENTITY_MISMATCH"),
        (lambda value: {**value, "polymarket": {**value["polymarket"], "exchange": "predict.fun"}}, "IDENTITY_MISMATCH"),
        (lambda value: {**value, "predict": {**value["predict"], "rules_fingerprint": "wrong"}}, "FINGERPRINT_MISMATCH"),
        (lambda value: {**value, "divergent_states": {**value["divergent_states"], "PREDICT_YES_POLYMARKET_NO": {"possible": True, "reason": "possible"}}}, "DIVERGENT_STATE_POSSIBLE"),
        (lambda value: {**value, "evidence": []}, "MISSING_EVIDENCE"),
        (lambda value: {**value, "evidence": [{"exchange": "predict.fun", "field": "rules", "quote": "not supplied"}, value["evidence"][1]]}, "EVIDENCE_NOT_FOUND"),
        (lambda value: {**value, "uncertainties": ["unresolved"]}, "UNRESOLVED_UNCERTAINTY"),
    ],
)
def test_equivalence_approval_fails_closed_for_all_post_check_mismatches(tmp_path: Path, mutate, reason: str) -> None:
    pair = explicit_pair()
    structured = mutate(equivalence_result(pair))
    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path / "data"),
        model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=codex_jsonl(structured), stderr=""),
    )

    result = validator.validate(pair)

    assert result.approved is False
    assert result.reason == reason


def test_equivalence_schema_requires_explicit_exchange_evidence_and_divergent_checks() -> None:
    schema_path = Path(__file__).parents[1] / "src/open_trader/schemas/cross_exchange_yes_no_equivalence.json"
    schema = json.loads(schema_path.read_text())

    assert schema["properties"]["decision"]["enum"] == ["APPROVE", "REJECT"]
    assert {"predict", "polymarket", "divergent_states", "evidence", "uncertainties"} <= set(schema["required"])
    assert set(schema["properties"]["divergent_states"]["required"]) == {"PREDICT_YES_POLYMARKET_NO", "POLYMARKET_YES_PREDICT_NO"}


def test_threshold_validator_assets_are_unchanged() -> None:
    unchanged = subprocess.run(
        [
            "git", "diff", "--quiet", "HEAD", "--",
            "src/open_trader/polymarket_relation_discovery.py",
            "src/open_trader/schemas/polymarket_threshold_relation.json",
        ],
        check=False,
    )

    assert unchanged.returncode == 0
