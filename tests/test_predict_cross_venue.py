from __future__ import annotations

import asyncio
import json
import hashlib
import subprocess
import threading
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import open_trader.predict_cross_venue as predict_cross_venue
from open_trader.predict_cross_venue import (
    CROSS_EXCHANGE_YES_NO_EQUIVALENCE_PROMPT_VERSION,
    CodexCrossVenueEquivalenceValidator,
    CrossVenueValidation,
    ExplicitMarketPair,
    PredictCrossVenueMonitor,
    build_cross_venue_intents,
    resolve_explicit_market_pairs,
)
from open_trader.predict_source import PredictBook, PredictMarket
from open_trader.predict_trading import PredictBuyQuote
from open_trader.prediction_arbitrage import BookLevel, ThresholdOrderBook
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def predict_market(*, external_ids: tuple[str, ...]) -> PredictMarket:
    return PredictMarket(
        market_id="predict-market-1",
        condition_id="predict-native-condition-1",
        question="Will the public test event resolve Yes?",
        rules="This public test event resolves from the named source.",
        category_slug="public-test",
        event_start_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_end_at=datetime(2026, 1, 11, tzinfo=UTC),
        resolution_provider="Public Test Oracle",
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
    pair = resolve_explicit_market_pairs(
        (predict_market(external_ids=("poly-condition",)),),
        gamma_lookup=lambda *args, **kwargs: [polymarket_row("poly-condition")],
        clob_lookup=lambda condition_id: None,
    ).pairs[0]
    cutoff = "at 23:59 UTC on December 31, 2026"
    return replace(
        pair,
        predict=replace(
            pair.predict,
            rules=f"This contract closes {cutoff} and resolves from the named source.",
            event_end_at=datetime(2026, 12, 31, 23, 58, tzinfo=UTC),
        ),
        polymarket=replace(
            pair.polymarket,
            rules=f"This contract closes {cutoff} and resolves from the named source.",
            close_at=datetime(2027, 1, 2, 4, 59, tzinfo=UTC),
            settlement_at=datetime(2027, 1, 2, 5, tzinfo=UTC),
        ),
    )


def equivalence_result(pair: ExplicitMarketPair) -> dict[str, object]:
    return {
        "schema_version": 2,
        "decision": "APPROVE",
        "summary": "The supplied rules exclude both divergent settlement states.",
        "contract_shape": "BINARY",
        "predict": {
            "exchange": "predict.fun",
            "market_id": pair.predict.market_id,
            "condition_id": pair.predict.condition_id,
            "rules_fingerprint": pair.predict.rules_fingerprint,
        },
        "polymarket": {
            "exchange": "polymarket",
            "market_id": pair.polymarket.market_id,
            "condition_id": pair.polymarket.condition_id,
            "rules_fingerprint": pair.polymarket.rules_fingerprint,
        },
        "direct_outcome_mapping": {
            "predict_yes": "YES", "predict_no": "NO",
            "polymarket_yes": "YES", "polymarket_no": "NO",
        },
        "canonical_cutoff": "2026-12-31T23:59:00Z",
        "divergent_states": {
            "PREDICT_YES_POLYMARKET_NO": {"possible": False, "reason": "same rule"},
            "POLYMARKET_YES_PREDICT_NO": {"possible": False, "reason": "same rule"},
        },
        "evidence": [
            {"exchange": "predict.fun", "field": "cutoff", "quote": "This contract closes at 23:59 UTC on December 31, 2026"},
            {"exchange": "polymarket", "field": "cutoff", "quote": "This contract closes at 23:59 UTC on December 31, 2026"},
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
    assert first.prompt_version == "cross-exchange-yes-no-equivalence-v2"
    assert first.canonical_cutoff == datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert first.direct_outcome_mapping == {
        "predict_yes": "YES", "predict_no": "NO",
        "polymarket_yes": "YES", "polymarket_no": "NO",
    }
    assert first.cache_key == predict_cross_venue.cross_exchange_equivalence_cache_key(
        pair, model="gpt-test"
    )
    assert second.approved is True
    assert len(calls) == 1
    assert CROSS_EXCHANGE_YES_NO_EQUIVALENCE_PROMPT_VERSION == "cross-exchange-yes-no-equivalence-v2"
    assert Path(calls[0][calls[0].index("--output-schema") + 1]).name == "cross_exchange_yes_no_equivalence.json"
    assert store.llm_usage_24h()["cache_hits"] == 1


def test_equivalence_approval_accepts_one_minute_raw_metadata_difference(tmp_path: Path) -> None:
    pair = explicit_pair()
    pair = replace(
        pair,
        predict=replace(pair.predict, event_end_at=datetime(2026, 12, 31, 23, 58, tzinfo=UTC)),
        polymarket=replace(pair.polymarket, close_at=datetime(2026, 12, 31, 23, 59, tzinfo=UTC)),
    )
    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path / "data"), model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=codex_jsonl(equivalence_result(pair)), stderr=""
        ),
    )

    result = validator.validate(pair)

    assert result.approved is True
    assert result.canonical_cutoff == datetime(2026, 12, 31, 23, 59, tzinfo=UTC)


def test_equivalence_approval_accepts_twenty_nine_hour_raw_metadata_difference(tmp_path: Path) -> None:
    pair = explicit_pair()
    pair = replace(
        pair,
        predict=replace(pair.predict, event_end_at=datetime(2027, 1, 1, 23, 59, tzinfo=UTC)),
        polymarket=replace(pair.polymarket, close_at=datetime(2027, 1, 3, 4, 59, tzinfo=UTC)),
    )
    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path / "data"), model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=codex_jsonl(equivalence_result(pair)), stderr=""
        ),
    )

    assert validator.validate(pair).approved is True


def test_equivalence_rejects_opening_time_quote_as_cutoff_evidence(tmp_path: Path) -> None:
    opening = "the market opens at 23:59 UTC on December 31, 2026"
    pair = explicit_pair()
    pair = replace(
        pair,
        predict=replace(pair.predict, rules=opening),
        polymarket=replace(pair.polymarket, rules=opening),
    )
    structured = {
        **equivalence_result(pair),
        "evidence": [
            {"exchange": "predict.fun", "field": "cutoff", "quote": opening},
            {"exchange": "polymarket", "field": "cutoff", "quote": opening},
        ],
    }
    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path / "data"), model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=codex_jsonl(structured), stderr=""
        ),
    )

    result = validator.validate(pair)

    assert result.approved is False
    assert result.reason == "CUTOFF_EVIDENCE_MISMATCH"


def test_equivalence_rejects_ambiguous_opening_and_closing_time_quote(tmp_path: Path) -> None:
    quote = (
        "This market opens at 23:59 UTC on December 31, 2026 and closes at "
        "00:00 UTC on January 1, 2027"
    )
    pair = explicit_pair()
    pair = replace(
        pair,
        predict=replace(pair.predict, rules=quote),
        polymarket=replace(pair.polymarket, rules=quote),
    )
    structured = {
        **equivalence_result(pair),
        "evidence": [
            {"exchange": "predict.fun", "field": "cutoff", "quote": quote},
            {"exchange": "polymarket", "field": "cutoff", "quote": quote},
        ],
    }
    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path / "data"), model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=codex_jsonl(structured), stderr=""
        ),
    )

    result = validator.validate(pair)

    assert result.approved is False
    assert result.reason == "CUTOFF_EVIDENCE_MISMATCH"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: {**value, "predict": {**value["predict"], "condition_id": "wrong"}}, "IDENTITY_MISMATCH"),
        (lambda value: {**value, "polymarket": {**value["polymarket"], "exchange": "predict.fun"}}, "IDENTITY_MISMATCH"),
        (lambda value: {**value, "predict": {**value["predict"], "rules_fingerprint": "wrong"}}, "FINGERPRINT_MISMATCH"),
        (lambda value: {**value, "direct_outcome_mapping": {**value["direct_outcome_mapping"], "predict_yes": "NO"}}, "OUTCOME_MAPPING_MISMATCH"),
        (lambda value: {**value, "canonical_cutoff": "2026-12-31T23:59:00"}, "CUTOFF_INVALID"),
        (lambda value: {**value, "canonical_cutoff": "not-a-date"}, "CUTOFF_INVALID"),
        (lambda value: {**value, "canonical_cutoff": "2027-01-01T00:00:00Z"}, "CUTOFF_EVIDENCE_MISMATCH"),
        (lambda value: {**value, "contract_shape": "COMPOUND"}, "COMPOUND_CONTRACT"),
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
    assert schema["properties"]["schema_version"]["const"] == 2
    assert {"predict", "polymarket", "direct_outcome_mapping", "canonical_cutoff", "contract_shape", "divergent_states", "evidence", "uncertainties"} <= set(schema["required"])
    assert set(schema["properties"]["divergent_states"]["required"]) == {"PREDICT_YES_POLYMARKET_NO", "POLYMARKET_YES_PREDICT_NO"}
    assert set(schema["$defs"]["venue_market"]["required"]) == {"exchange", "market_id", "condition_id", "rules_fingerprint"}
    assert any(
        clause.get("if", {}).get("properties", {}).get("contract_shape", {}).get("const") == "COMPOUND"
        and clause.get("then", {}).get("properties", {}).get("decision", {}).get("const") == "REJECT"
        for clause in schema["allOf"]
    )


def test_equivalence_cache_and_hot_pool_invalidate_every_admission_input() -> None:
    pair = explicit_pair()
    baseline = predict_cross_venue.cross_exchange_equivalence_cache_key(pair, model="gpt-test")
    variants = (
        replace(pair, predict=replace(pair.predict, rules="changed rules")),
        replace(pair, predict=replace(pair.predict, event_end_at=datetime(2027, 1, 1, tzinfo=UTC))),
        replace(pair, predict=replace(pair.predict, yes_token_id="changed-token")),
        replace(pair, polymarket=replace(pair.polymarket, condition_id="changed-candidate")),
    )
    assert baseline is not None
    assert all(
        predict_cross_venue.cross_exchange_equivalence_cache_key(variant, model="gpt-test") != baseline
        for variant in variants
    )
    assert predict_cross_venue.cross_exchange_equivalence_cache_key(
        pair, model="gpt-test", prompt_version="changed-prompt"
    ) != baseline

    monitor = PredictCrossVenueMonitor(
        predict_source=FakeCrossVenuePredict(()), polymarket_monitor=FakeCrossVenuePolymarket(),
        validator=FakeCrossVenueValidator(), gamma_lookup=lambda *args, **kwargs: [],
        clob_lookup=lambda condition_id: None,
        predict_quote_fn=predict_quote(),
    )
    monitor._set_approved({pair.pair_id: pair})
    for variant in variants:
        assert monitor._same_fingerprints(pair, variant) is False
        monitor._set_approved({})
        assert monitor.snapshot()["funnel"]["codex_approved_pairs"] == 0


def test_threshold_validator_schema_is_unchanged() -> None:
    unchanged = subprocess.run(
        [
            "git", "diff", "--quiet", "HEAD", "--",
            "src/open_trader/schemas/polymarket_threshold_relation.json",
        ],
        check=False,
    )

    assert unchanged.returncode == 0


def test_incomplete_predict_canonical_metadata_fails_closed_before_payload() -> None:
    pair = explicit_pair()

    for field, value in (
        ("category_slug", ""),
        ("event_start_at", None),
        ("event_end_at", None),
        ("resolution_provider", ""),
    ):
        malformed = replace(pair.predict, **{field: value})
        assert predict_cross_venue._valid_market_pair(replace(pair, predict=malformed)) is False


def test_direct_validator_and_cache_key_fail_closed_for_malformed_pair(tmp_path: Path) -> None:
    pair = explicit_pair()
    malformed = replace(pair, predict=replace(pair.predict, event_start_at=None))

    assert predict_cross_venue.cross_exchange_equivalence_cache_key(
        malformed, model="gpt-test"
    ) is None
    validator = CodexCrossVenueEquivalenceValidator(
        PredictionArbitrageStore(tmp_path / "data"),
        model="gpt-test",
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Codex must not run for malformed metadata")
        ),
    )

    result = validator.validate(malformed)

    assert result.approved is False
    assert result.reason == "MARKET_INVALID"


def cross_venue_pair() -> ExplicitMarketPair:
    pair = explicit_pair()
    return ExplicitMarketPair(
        pair_id=pair.pair_id,
        predict=replace(pair.predict, event_end_at=datetime(2026, 1, 11, tzinfo=UTC)),
        polymarket=replace(pair.polymarket, settlement_at=datetime(2026, 1, 21, tzinfo=UTC)),
        canonical_cutoff=datetime(2026, 1, 21, tzinfo=UTC),
    )


def cross_venue_books() -> tuple[PredictBook, dict[str, ThresholdOrderBook]]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    predict = PredictBook(
        market_id="predict-market-1",
        yes_asks=(
            BookLevel(Decimal("0.40"), Decimal("5")),
            BookLevel(Decimal("0.41"), Decimal("5")),
        ),
        no_asks=(BookLevel(Decimal("0.60"), Decimal("10")),),
        source_timestamp=now,
        received_at=now,
    )
    books = {
        "poly-yes-1": ThresholdOrderBook(
            token_id="poly-yes-1",
            asks=(BookLevel(Decimal("0.80"), Decimal("10")),),
            bids=(BookLevel(Decimal("0.79"), Decimal("10")),),
            confirmed_at=now,
        ),
        "poly-no-1": ThresholdOrderBook(
            token_id="poly-no-1",
            asks=(
                BookLevel(Decimal("0.50"), Decimal("5")),
                BookLevel(Decimal("0.51"), Decimal("5")),
            ),
            bids=(BookLevel(Decimal("0.49"), Decimal("10")),),
            confirmed_at=now,
        ),
    }
    return predict, books


def predict_quote(
    *, net_quantity: Decimal | None = None, all_in_price: Decimal = Decimal("0.408")
):
    def quote(market_id: str, token_id: str, requested_units: int) -> PredictBuyQuote:
        net_units = (
            requested_units
            if net_quantity is None
            else int(net_quantity * Decimal(10**18))
        )
        debit = int(
            Decimal(net_units) / Decimal(10**18) * all_in_price * Decimal(10**6)
        )
        return PredictBuyQuote(
            market_id, token_id, 400_000, debit, net_units
        )

    return quote


def test_cross_venue_intent_uses_decimal_depth_fees_later_settlement_and_venue_ids() -> None:
    pair = cross_venue_pair()
    predict, polymarket = cross_venue_books()

    intents = build_cross_venue_intents(
        pair, predict, polymarket, now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(),
    )

    assert len(intents) == 1
    intent = intents[0]
    assert intent.direction == "PREDICT_YES_POLYMARKET_NO"
    assert intent.quantity == Decimal("10")
    assert intent.total_max_cost == Decimal("9.22998")
    assert intent.maximum_fee == Decimal("0.12998")
    assert intent.minimum_payout == Decimal("10")
    assert intent.minimum_profit == Decimal("0.77002")
    assert intent.resolution_at == datetime(2026, 1, 21, tzinfo=UTC)
    assert intent.annualized_yield == Decimal("0.77002") / Decimal("9.22998") * Decimal("365") / Decimal("20")
    assert [(leg.exchange, leg.outcome, leg.max_price, leg.max_cost, leg.maximum_fee) for leg in intent.legs] == [
        ("predict.fun", "YES", Decimal("0.4"), Decimal("4.08"), Decimal("0.08")),
        ("polymarket", "NO", Decimal("0.51"), Decimal("5.14998"), Decimal("0.04998")),
    ]
    assert [(leg.settlement_asset, leg.market_id, leg.condition_id, leg.token_id) for leg in intent.legs] == [
        ("USDT", "predict-market-1", "predict-native-condition-1", "predict-yes-1"),
        ("USDC", "poly-market-1", "poly-condition", "poly-no-1"),
    ]
    assert set(asdict(intent.legs[0])) == {
        "exchange", "market_id", "condition_id", "outcome", "token_id",
        "settlement_asset", "requested_quantity", "net_quantity", "max_price", "max_cost", "maximum_fee", "fee_asset",
        "book_timestamp", "settlement_at",
    }


def test_cross_venue_intent_calculates_only_the_polymarket_yes_predict_no_direction() -> None:
    pair = cross_venue_pair()
    predict, polymarket = cross_venue_books()
    predict = replace(
        predict,
        yes_asks=(BookLevel(Decimal("0.70"), Decimal("10")),),
        no_asks=(BookLevel(Decimal("0.30"), Decimal("10")),),
    )
    polymarket["poly-yes-1"] = replace(
        polymarket["poly-yes-1"],
        asks=(BookLevel(Decimal("0.50"), Decimal("10")),),
        bids=(BookLevel(Decimal("0.49"), Decimal("10")),),
    )

    intents = build_cross_venue_intents(
        pair, predict, polymarket, now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(),
    )

    assert [intent.direction for intent in intents] == ["POLYMARKET_YES_PREDICT_NO"]
    assert [(leg.exchange, leg.outcome) for leg in intents[0].legs] == [
        ("predict.fun", "NO"),
        ("polymarket", "YES"),
    ]


@pytest.mark.parametrize(
    ("predict_asks", "polymarket_token", "polymarket_asks", "direction"),
    (
        (
            (BookLevel(Decimal("0.40"), Decimal("10")),),
            "poly-no-1",
            (BookLevel(Decimal("0.50"), Decimal("10")),),
            "PREDICT_YES_POLYMARKET_NO",
        ),
        (
            (BookLevel(Decimal("0.30"), Decimal("10")),),
            "poly-yes-1",
            (BookLevel(Decimal("0.50"), Decimal("10")),),
            "POLYMARKET_YES_PREDICT_NO",
        ),
    ),
)
def test_cross_venue_intent_sizes_both_directions_to_predict_net_units(
    predict_asks, polymarket_token, polymarket_asks, direction
) -> None:
    pair = cross_venue_pair()
    predict, polymarket = cross_venue_books()
    predict = replace(
        predict,
        yes_asks=predict_asks if direction.startswith("PREDICT") else (BookLevel(Decimal("0.70"), Decimal("10")),),
        no_asks=predict_asks if direction.startswith("POLYMARKET") else (BookLevel(Decimal("0.60"), Decimal("10")),),
    )
    polymarket[polymarket_token] = replace(
        polymarket[polymarket_token],
        asks=polymarket_asks,
        bids=(BookLevel(Decimal("0.49"), Decimal("10")),),
    )

    intents = build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(net_quantity=Decimal("9")),
    )

    intent = next(item for item in intents if item.direction == direction)
    assert intent.quantity == Decimal("9")
    assert [leg.requested_quantity for leg in intent.legs] == [Decimal("10"), Decimal("9")]
    assert [leg.net_quantity for leg in intent.legs] == [Decimal("9"), Decimal("9")]


def test_cross_venue_keeps_positive_stage_four_observation_when_quote_is_unavailable() -> None:
    pair = cross_venue_pair()
    predict, polymarket = cross_venue_books()

    def unavailable_quote(*args: object) -> PredictBuyQuote:
        raise RuntimeError("unavailable")

    observation = predict_cross_venue._build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        require_annualized_gate=False,
        predict_quote_fn=unavailable_quote,
    )

    assert observation
    assert observation[0].actionable is False
    assert build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=unavailable_quote,
    ) == ()


@pytest.mark.parametrize(
    ("annualized", "actionable"),
    ((Decimal("0.149999"), False), (Decimal("0.15"), True)),
)
def test_cross_venue_uses_the_inclusive_fifteen_percent_annualized_gate(
    monkeypatch: pytest.MonkeyPatch, annualized: Decimal, actionable: bool
) -> None:
    monkeypatch.setattr(
        predict_cross_venue,
        "simple_annualized_yield_from_values",
        lambda *args, **kwargs: annualized,
    )
    pair = cross_venue_pair()
    predict, polymarket = cross_venue_books()

    observation = predict_cross_venue._build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        require_annualized_gate=False,
        predict_quote_fn=predict_quote(),
    )
    intents = build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(),
    )

    assert observation[0].actionable is actionable
    assert bool(intents) is actionable


def test_cross_venue_descends_when_largest_quote_exceeds_twenty_usdt() -> None:
    pair = cross_venue_pair()
    predict, polymarket = cross_venue_books()

    def capped_quote(market_id: str, token_id: str, requested_units: int) -> PredictBuyQuote:
        quantity = Decimal(requested_units) / Decimal(10**18)
        all_in_price = Decimal("3") if quantity > Decimal("5") else Decimal("0.408")
        return PredictBuyQuote(
            market_id,
            token_id,
            400_000,
            int(quantity * all_in_price * Decimal(10**6)),
            requested_units,
        )

    intents = build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=capped_quote,
    )

    assert intents[0].quantity == Decimal("5")
    assert intents[0].total_max_cost <= Decimal("20")


def test_cross_venue_treats_usdt_and_pusd_as_one_to_one_without_fx() -> None:
    pair = replace(
        cross_venue_pair(),
        polymarket=replace(cross_venue_pair().polymarket, settlement_asset="pUSD"),
    )
    predict, polymarket = cross_venue_books()

    intent = build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(),
    )[0]

    assert [leg.settlement_asset for leg in intent.legs] == ["USDT", "pUSD"]
    assert intent.total_max_cost == Decimal("9.22998")
    assert intent.calculable_gas == Decimal("0")


def test_cross_venue_rejects_fee_inclusive_zero_profit_quotes() -> None:
    pair = replace(
        cross_venue_pair(),
        predict=replace(cross_venue_pair().predict, fee_rate_bps=Decimal("2500")),
    )
    predict, polymarket = cross_venue_books()

    assert build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(all_in_price=Decimal("0.5")),
    ) == ()


@pytest.mark.parametrize(
    "canonical_cutoff",
    (None, datetime(2025, 12, 31, tzinfo=UTC)),
)
def test_cross_venue_missing_or_past_canonical_cutoff_is_observation_only(
    canonical_cutoff: datetime | None,
) -> None:
    pair = replace(cross_venue_pair(), canonical_cutoff=canonical_cutoff)
    predict, polymarket = cross_venue_books()

    observation = predict_cross_venue._build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        require_annualized_gate=False,
        predict_quote_fn=predict_quote(),
    )

    assert observation[0].actionable is False
    assert build_cross_venue_intents(
        pair,
        predict,
        polymarket,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(),
    ) == ()


def test_cross_venue_intent_uses_shared_scalar_annualization_with_fee_inclusive_capital(monkeypatch) -> None:
    calls: list[tuple[Decimal, Decimal, datetime, datetime]] = []

    def annualized(
        minimum_profit: Decimal,
        total_max_cost: Decimal,
        *,
        now: datetime,
        resolution_at: datetime,
    ) -> Decimal:
        calls.append((minimum_profit, total_max_cost, now, resolution_at))
        return Decimal("1")

    monkeypatch.setattr(
        predict_cross_venue, "simple_annualized_yield_from_values", annualized
    )
    pair = cross_venue_pair()
    predict, polymarket = cross_venue_books()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    intents = build_cross_venue_intents(
        pair, predict, polymarket, now=now, predict_quote_fn=predict_quote()
    )

    assert len(intents) == 1
    assert calls == [
        (Decimal("0.77002"), Decimal("9.22998"), now, pair.canonical_cutoff)
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pair, predict, books: (replace(pair, predict=replace(pair.predict, minimum_order_size=Decimal("11"))), predict, books),
        lambda pair, predict, books: (replace(pair, predict=replace(pair.predict, tick_size=Decimal("0.03"))), predict, books),
        lambda pair, predict, books: (replace(pair, predict=replace(pair.predict, fee_rate_bps=Decimal("1000"))), predict, books),
        lambda pair, predict, books: (pair, replace(predict, received_at=datetime(2026, 1, 1, tzinfo=UTC) - timedelta(seconds=11)), books),
        lambda pair, predict, books: (pair, replace(predict, yes_asks=(BookLevel(0.40, Decimal("10")),)), books),
        lambda pair, predict, books: (pair, replace(predict, yes_asks=(BookLevel(Decimal("0.40"), Decimal("-1")),)), books),
        lambda pair, predict, books: (pair, replace(predict, yes_asks=(BookLevel(Decimal("NaN"), Decimal("10")),)), books),
        lambda pair, predict, books: (pair, replace(predict, no_asks=(BookLevel(Decimal("0.59"), Decimal("10")),)), books),
        lambda pair, predict, books: (pair, predict, {**books, "poly-no-1": replace(books["poly-no-1"], bids=(BookLevel(Decimal("0.51"), Decimal("10")),))}),
        lambda pair, predict, books: (pair, predict, {**books, "poly-no-1": replace(books["poly-no-1"], confirmed_at=datetime(2026, 1, 1, tzinfo=UTC) - timedelta(seconds=11))}),
    ],
)
def test_cross_venue_intent_fails_closed_for_invalid_depth_fees_and_freshness(mutate) -> None:
    pair, predict, polymarket = mutate(cross_venue_pair(), *cross_venue_books())

    assert build_cross_venue_intents(
        pair, predict, polymarket, now=datetime(2026, 1, 1, tzinfo=UTC),
        predict_quote_fn=predict_quote(),
    ) == ()


def monitor_predict_book() -> PredictBook:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return PredictBook(
        market_id="predict-market-1",
        yes_asks=(BookLevel(Decimal("0.55"), Decimal("10")),),
        no_asks=(BookLevel(Decimal("0.45"), Decimal("10")),),
        source_timestamp=now,
        received_at=now,
    )


def monitor_polymarket_books() -> dict[str, ThresholdOrderBook]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        token: ThresholdOrderBook(
            token_id=token,
            asks=(BookLevel(Decimal("0.40"), Decimal("10")),),
            bids=(BookLevel(Decimal("0.39"), Decimal("10")),),
            confirmed_at=now,
        )
        for token in ("poly-yes-1", "poly-no-1")
    }


def rejected_predict_market() -> PredictMarket:
    return replace(
        monitor_predict_market(external_ids=("poly-rejected",)),
        market_id="predict-market-rejected",
        condition_id="predict-condition-rejected",
        yes_token_id="predict-yes-rejected",
        no_token_id="predict-no-rejected",
        rules_fingerprint="predict-fingerprint-rejected",
    )


def monitor_predict_market(*, external_ids: tuple[str, ...]) -> PredictMarket:
    return replace(
        predict_market(external_ids=external_ids),
        event_end_at=datetime(2026, 1, 10, tzinfo=UTC),
        event_start_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def rejected_polymarket_row() -> dict[str, object]:
    return {
        **polymarket_row("poly-rejected"),
        "id": "poly-market-rejected",
        "clobTokenIds": '["poly-yes-rejected", "poly-no-rejected"]',
    }


async def wait_until(predicate, *, attempts: int = 500) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not reached")


class FakeCrossVenueValidator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def validate(self, pair: ExplicitMarketPair) -> CrossVenueValidation:
        self.calls.append(pair.pair_id)
        approved = pair.predict.market_id != "predict-market-rejected"
        return CrossVenueValidation(
            approved=approved,
            reason="APPROVED" if approved else "LLM_REJECTED",
            prompt_version="cross-exchange-yes-no-equivalence-v1",
            predict_fingerprint=pair.predict.rules_fingerprint,
            polymarket_fingerprint=pair.polymarket.rules_fingerprint,
            predict_event_start_at=pair.predict.event_start_at,
            predict_event_end_at=pair.predict.event_end_at,
            polymarket_close_at=pair.polymarket.close_at,
            polymarket_settlement_at=pair.polymarket.settlement_at,
            canonical_cutoff=datetime(2026, 1, 21, tzinfo=UTC) if approved else None,
            direct_outcome_mapping={
                "predict_yes": "YES",
                "predict_no": "NO",
                "polymarket_yes": "YES",
                "polymarket_no": "NO",
            } if approved else None,
        )


class FakeCrossVenuePolymarket:
    def __init__(self) -> None:
        self.books = monitor_polymarket_books()
        self.token_sets: list[tuple[str, ...]] = []
        self.confirm_calls = 0
        self.confirm_started = asyncio.Event()
        self.predict_started: asyncio.Event | None = None
        self.release = asyncio.Event()
        self.same_venue_running = True

    def set_cross_venue_tokens(self, token_ids) -> None:
        self.token_sets.append(tuple(sorted(token_ids)))

    def cross_venue_books(self, token_ids) -> dict[str, ThresholdOrderBook]:
        return {token: self.books[token] for token in token_ids if token in self.books}

    async def _confirm_cross_venue_books(self, token_ids) -> dict[str, ThresholdOrderBook]:
        self.confirm_calls += 1
        self.confirm_started.set()
        if self.predict_started is not None:
            await self.predict_started.wait()
        await self.release.wait()
        return self.cross_venue_books(token_ids)


def test_monitor_allows_task_six_to_defer_quote_wiring() -> None:
    monitor = PredictCrossVenueMonitor(
        predict_source=FakeCrossVenuePredict(()),
        polymarket_monitor=FakeCrossVenuePolymarket(),
        validator=FakeCrossVenueValidator(),
        gamma_lookup=lambda *args, **kwargs: [],
        clob_lookup=lambda condition_id: None,
    )

    assert monitor._predict_quote_fn is None


class FakeCrossVenuePredict:
    def __init__(self, markets: tuple[PredictMarket, ...]) -> None:
        self.markets = markets
        self.list_calls = 0
        self.subscriptions: list[tuple[str, ...]] = []
        self.active_subscriptions: set[tuple[str, ...]] = set()
        self.queue: asyncio.Queue[PredictBook | None] = asyncio.Queue()
        self.rest_calls = 0
        self.rest_started = asyncio.Event()
        self.polymarket_started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.health = {"rest": "ready", "ws": "ready"}
        self.ws_generation = 0

    async def list_open_markets(self) -> tuple[PredictMarket, ...]:
        self.list_calls += 1
        return self.markets

    async def stream_books(self, market_ids):
        subscription = tuple(sorted(market_ids))
        self.subscriptions.append(subscription)
        self.active_subscriptions.add(subscription)
        try:
            while True:
                book = await self.queue.get()
                if book is None:
                    return
                yield book
        finally:
            self.active_subscriptions.discard(subscription)

    async def get_order_book(self, market_id: str) -> PredictBook | None:
        self.rest_calls += 1
        self.rest_started.set()
        if self.polymarket_started is not None:
            await self.polymarket_started.wait()
        if self.release is not None:
            await self.release.wait()
        return monitor_predict_book() if market_id == "predict-market-1" else None

    def snapshot(self) -> dict[str, str | int]:
        return {
            "venue": "predict.fun",
            **self.health,
            "ws_generation": self.ws_generation,
        }


def monitor_gamma(condition_ids, *, closed: bool) -> list[dict[str, object]]:
    del closed
    rows = {
        "poly-condition": polymarket_row("poly-condition"),
        "poly-rejected": rejected_polymarket_row(),
    }
    rows = {
        condition_id: {
            **row,
            "endDate": "2026-01-20T00:00:00Z",
            "resolutionDate": "2026-01-21T00:00:00Z",
        }
        for condition_id, row in rows.items()
    }
    return [rows[condition_id] for condition_id in condition_ids if condition_id in rows]


@pytest.mark.parametrize(
    "mapping",
    (
        None,
        {
            "predict_yes": "NO",
            "predict_no": "YES",
            "polymarket_yes": "YES",
            "polymarket_no": "NO",
        },
    ),
)
def test_monitor_does_not_admit_validation_without_exact_direct_mapping(mapping) -> None:
    class InvalidMappingValidator(FakeCrossVenueValidator):
        def validate(self, pair: ExplicitMarketPair) -> CrossVenueValidation:
            return replace(super().validate(pair), direct_outcome_mapping=mapping)

    async def exercise() -> None:
        predict = FakeCrossVenuePredict(
            (monitor_predict_market(external_ids=("poly-condition",)),)
        )
        polymarket = FakeCrossVenuePolymarket()
        validator = InvalidMappingValidator()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=validator,
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
        )

        await monitor.start()
        await wait_until(lambda: bool(validator.calls))
        assert monitor.snapshot()["funnel"]["codex_approved_pairs"] == 0
        assert predict.subscriptions == []
        assert polymarket.token_sets[-1] == ()
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_validates_before_subscription_and_confirms_both_rest_books_concurrently() -> None:
    async def exercise() -> None:
        predict = FakeCrossVenuePredict(
            (
                monitor_predict_market(external_ids=("poly-condition",)),
                rejected_predict_market(),
            )
        )
        polymarket = FakeCrossVenuePolymarket()
        validator = FakeCrossVenueValidator()
        predict.polymarket_started = polymarket.confirm_started
        predict.release = polymarket.release
        polymarket.predict_started = predict.rest_started
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=validator,
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: bool(predict.subscriptions))
        assert predict.subscriptions == [("predict-market-1",)]
        assert polymarket.token_sets[-1] == ("poly-no-1", "poly-yes-1")
        assert len(validator.calls) == 2

        await predict.queue.put(monitor_predict_book())
        await wait_until(
            lambda: predict.rest_started.is_set() and polymarket.confirm_started.is_set()
        )
        assert monitor.snapshot()["opportunities"] == []
        assert polymarket.confirm_calls == 1
        polymarket.release.set()
        await wait_until(lambda: len(monitor.snapshot()["opportunities"]) == 2)

        snapshot = monitor.snapshot()
        assert snapshot["status"] == "ready"
        assert snapshot["mode"] == "observe_only"
        assert snapshot["funnel"] == {
            "matched_pairs": 2,
            "monitored_pairs": 2,
            "codex_approved_pairs": 1,
            "arbitrage_space_pairs": 1,
            "clear_signal_pairs": 1,
        }
        assert {row["direction"] for row in snapshot["opportunities"]} == {
            "PREDICT_YES_POLYMARKET_NO",
            "POLYMARKET_YES_PREDICT_NO",
        }
        assert all(
            row["market_type"] == "cross_venue_yes_no"
            and row["execution_mode"] == "observe_only"
            and row["actionable"] is False
            and row["clear_signal"] is True
            for row in snapshot["opportunities"]
        )

        await predict.queue.put(monitor_predict_book())
        await asyncio.sleep(0.01)
        assert len(validator.calls) == 2
        assert polymarket.confirm_calls == 1
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_closes_and_rearms_episode_without_touching_same_venue_state() -> None:
    async def exercise() -> None:
        predict = FakeCrossVenuePredict(
            (monitor_predict_market(external_ids=("poly-condition",)),)
        )
        polymarket = FakeCrossVenuePolymarket()
        polymarket.release.set()
        validator = FakeCrossVenueValidator()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=validator,
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: bool(predict.subscriptions))
        await predict.queue.put(monitor_predict_book())
        await wait_until(lambda: bool(monitor.snapshot()["opportunities"]))
        assert polymarket.confirm_calls == 1

        polymarket.books["poly-no-1"] = replace(
            polymarket.books["poly-no-1"],
            asks=(BookLevel(Decimal("0.80"), Decimal("10")),),
            bids=(BookLevel(Decimal("0.79"), Decimal("10")),),
        )
        await predict.queue.put(monitor_predict_book())
        await wait_until(lambda: len(monitor.snapshot()["opportunities"]) == 1)
        assert monitor.snapshot()["opportunities"][0]["direction"] == (
            "POLYMARKET_YES_PREDICT_NO"
        )

        polymarket.books = {}
        await predict.queue.put(monitor_predict_book())
        await wait_until(lambda: monitor.snapshot()["opportunities"] == [])
        polymarket.books = monitor_polymarket_books()
        await predict.queue.put(monitor_predict_book())
        await wait_until(lambda: polymarket.confirm_calls == 2)
        await wait_until(lambda: bool(monitor.snapshot()["opportunities"]))

        await predict.queue.put(None)
        await wait_until(lambda: polymarket.token_sets[-1] == ())
        assert polymarket.same_venue_running is True
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_persists_and_notifies_one_cross_venue_observation_episode(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        predict = FakeCrossVenuePredict(
            (monitor_predict_market(external_ids=("poly-condition",)),)
        )
        polymarket = FakeCrossVenuePolymarket()
        polymarket.release.set()
        store = PredictionArbitrageStore(tmp_path / "data")
        notifications: list[tuple[str, str]] = []
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=FakeCrossVenueValidator(),
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            store=store,
            ready_observer=lambda opportunity_id, signal_id: notifications.append(
                (opportunity_id, signal_id)
            ),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: bool(predict.subscriptions))
        await predict.queue.put(monitor_predict_book())
        await wait_until(lambda: bool(store.open_signal_history()))
        opportunity = next(
            row["opportunity_id"]
            for row in monitor.snapshot()["opportunities"]
            if row["direction"] == "PREDICT_YES_POLYMARKET_NO"
        )
        opportunity_id = str(opportunity)
        signal = next(
            row
            for row in store.open_signal_history()
            if row["opportunity_id"] == opportunity_id
        )
        assert signal["opportunity_id"] == opportunity_id
        assert signal["market_type"] == "cross_venue_yes_no"
        assert signal["execution_mode"] == "observe_only"
        assert signal["clear_signal"] is True
        assert signal["trigger_total_max_cost"] == "8.128000"
        assert signal["trigger_minimum_profit"] == "1.872000"
        assert signal["legs"][0]["token_id"] == "predict-yes-1"
        assert signal["legs"][1]["token_id"] == "poly-no-1"
        assert signal["question"] == "Will the public test event resolve Yes?"
        assert signal["predict_question"] == "Will the public test event resolve Yes?"
        assert signal["polymarket_question"] == "Will the public test event resolve Yes?"
        await wait_until(lambda: bool(notifications))
        assert (signal["opportunity_id"], signal["signal_id"]) in notifications

        await monitor.stop()
        assert store.signal(str(signal["signal_id"]))["ended_reason"] == "data_unavailable"  # type: ignore[index]

    asyncio.run(exercise())


def test_monitor_snapshot_closes_episode_when_books_age_without_another_update() -> None:
    async def exercise() -> None:
        now = [datetime(2026, 1, 1, tzinfo=UTC)]
        predict = FakeCrossVenuePredict(
            (monitor_predict_market(external_ids=("poly-condition",)),)
        )
        polymarket = FakeCrossVenuePolymarket()
        polymarket.release.set()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=FakeCrossVenueValidator(),
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: now[0],
        )

        await monitor.start()
        await wait_until(lambda: bool(predict.subscriptions))
        await predict.queue.put(monitor_predict_book())
        await wait_until(lambda: bool(monitor.snapshot()["opportunities"]))

        now[0] += timedelta(seconds=11)

        assert monitor.snapshot()["opportunities"]
        await wait_until(lambda: monitor.snapshot()["opportunities"] == [])
        assert monitor.snapshot()["funnel"]["clear_signal_pairs"] == 0
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_missing_api_key_is_pending_and_has_zero_cross_subscriptions() -> None:
    async def exercise() -> None:
        predict = FakeCrossVenuePredict(())
        predict.health = {"rest": "pending", "ws": "pending"}
        polymarket = FakeCrossVenuePolymarket()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=FakeCrossVenueValidator(),
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: predict.list_calls == 1)
        await wait_until(lambda: bool(polymarket.token_sets))
        assert monitor.snapshot()["status"] == "pending"
        assert polymarket.token_sets[-1] == ()
        assert predict.subscriptions == []
        assert polymarket.same_venue_running is True
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_uses_fixed_fifteen_minute_discovery_and_invalidates_changed_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        from open_trader import predict_cross_venue as module

        monkeypatch.setattr(module, "CROSS_VENUE_DISCOVERY_SECONDS", 0.02)
        original = monitor_predict_market(external_ids=("poly-condition",))
        changed = replace(original, rules_fingerprint="changed-fingerprint")
        predict = FakeCrossVenuePredict((original,))
        polymarket = FakeCrossVenuePolymarket()

        class BlockingValidator(FakeCrossVenueValidator):
            def __init__(self) -> None:
                super().__init__()
                self.second_started = threading.Event()
                self.release_second = threading.Event()

            def validate(self, pair: ExplicitMarketPair) -> CrossVenueValidation:
                if self.calls:
                    self.second_started.set()
                    self.release_second.wait(timeout=1)
                return super().validate(pair)

        validator = BlockingValidator()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=validator,
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: bool(predict.subscriptions))
        assert len(validator.calls) == 1
        for _ in range(3):
            await predict.queue.put(monitor_predict_book())
        monitor.snapshot()
        await asyncio.sleep(0.005)
        assert len(validator.calls) == 1
        predict.markets = (changed,)
        await wait_until(validator.second_started.is_set)
        assert polymarket.token_sets[-1] == ()
        await wait_until(lambda: ("predict-market-1",) not in predict.active_subscriptions)
        assert validator.release_second.is_set() is False
        validator.release_second.set()
        await wait_until(lambda: len(validator.calls) == 2)
        await monitor.stop()

    assert predict_cross_venue.CROSS_VENUE_DISCOVERY_SECONDS == 15 * 60
    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda market: replace(market, rules="changed rules"),
        lambda market: replace(market, event_end_at=datetime(2026, 1, 11, tzinfo=UTC)),
        lambda market: replace(market, yes_token_id="changed-token"),
        lambda market: replace(market, polymarket_condition_ids=("changed-candidate",)),
    ],
)
def test_monitor_discovery_evicts_before_one_re_admission_for_changed_inputs(
    monkeypatch: pytest.MonkeyPatch, mutate,
) -> None:
    async def exercise() -> None:
        from open_trader import predict_cross_venue as module

        monkeypatch.setattr(module, "CROSS_VENUE_DISCOVERY_SECONDS", 0.02)
        original = monitor_predict_market(external_ids=("poly-condition",))
        predict = FakeCrossVenuePredict((original,))
        polymarket = FakeCrossVenuePolymarket()

        class BlockingValidator(FakeCrossVenueValidator):
            def __init__(self) -> None:
                super().__init__()
                self.second_started = threading.Event()
                self.release_second = threading.Event()

            def validate(self, pair: ExplicitMarketPair) -> CrossVenueValidation:
                if self.calls:
                    self.second_started.set()
                    self.release_second.wait(timeout=1)
                return super().validate(pair)

        validator = BlockingValidator()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict, polymarket_monitor=polymarket, validator=validator,
            gamma_lookup=lambda ids, **kwargs: [
                {**polymarket_row(condition_id), "endDate": "2026-01-20T00:00:00Z", "resolutionDate": "2026-01-21T00:00:00Z"}
                for condition_id in ids
            ],
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: len(validator.calls) == 1)
        predict.markets = (mutate(original),)
        await wait_until(validator.second_started.is_set)
        assert monitor.snapshot()["funnel"]["codex_approved_pairs"] == 0
        validator.release_second.set()
        await wait_until(lambda: len(validator.calls) == 2)
        assert monitor.snapshot()["funnel"]["codex_approved_pairs"] == 1
        await asyncio.sleep(0.005)
        assert len(validator.calls) == 2
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_discovery_re_admits_once_when_prompt_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        from open_trader import predict_cross_venue as module

        monkeypatch.setattr(module, "CROSS_VENUE_DISCOVERY_SECONDS", 0.02)
        predict = FakeCrossVenuePredict((monitor_predict_market(external_ids=("poly-condition",)),))
        polymarket = FakeCrossVenuePolymarket()

        class BlockingValidator(FakeCrossVenueValidator):
            def __init__(self) -> None:
                super().__init__()
                self.prompt_version = "v1"
                self.second_started = threading.Event()
                self.release_second = threading.Event()

            def validate(self, pair: ExplicitMarketPair) -> CrossVenueValidation:
                if self.calls:
                    self.second_started.set()
                    self.release_second.wait(timeout=1)
                return super().validate(pair)

        validator = BlockingValidator()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict, polymarket_monitor=polymarket, validator=validator,
            gamma_lookup=monitor_gamma, clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: len(validator.calls) == 1)
        validator.prompt_version = "v2"
        await wait_until(validator.second_started.is_set)
        assert monitor.snapshot()["funnel"]["codex_approved_pairs"] == 0
        validator.release_second.set()
        await wait_until(lambda: len(validator.calls) == 2)
        assert monitor.snapshot()["funnel"]["codex_approved_pairs"] == 1
        await asyncio.sleep(0.005)
        assert len(validator.calls) == 2
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_suspends_during_hidden_predict_reconnect_and_rearms_after_fresh_book() -> None:
    class ReconnectingPredict(FakeCrossVenuePredict):
        def __init__(self) -> None:
            super().__init__(
                (monitor_predict_market(external_ids=("poly-condition",)),)
            )
            self.disconnect = asyncio.Event()
            self.disconnected = asyncio.Event()
            self.reconnect = asyncio.Event()

        async def stream_books(self, market_ids):
            subscription = tuple(sorted(market_ids))
            self.subscriptions.append(subscription)
            self.active_subscriptions.add(subscription)
            try:
                yield monitor_predict_book()
                await self.disconnect.wait()
                self.health["ws"] = "stale"
                self.ws_generation += 1
                self.disconnected.set()
                await self.reconnect.wait()
                self.health["ws"] = "ready"
                yield monitor_predict_book()
                await asyncio.Event().wait()
            finally:
                self.active_subscriptions.discard(subscription)

    async def exercise() -> None:
        predict = ReconnectingPredict()
        polymarket = FakeCrossVenuePolymarket()
        polymarket.release.set()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=FakeCrossVenueValidator(),
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: bool(monitor.snapshot()["opportunities"]))
        predict.disconnect.set()
        await predict.disconnected.wait()

        await wait_until(lambda: polymarket.token_sets[-1] == ())
        assert monitor.snapshot()["opportunities"] == []
        assert predict.active_subscriptions == {("predict-market-1",)}

        predict.reconnect.set()
        await wait_until(lambda: polymarket.token_sets[-1] == ("poly-no-1", "poly-yes-1"))
        await wait_until(lambda: bool(monitor.snapshot()["opportunities"]))
        assert predict.subscriptions == [("predict-market-1",)]
        await monitor.stop()

    asyncio.run(exercise())


def test_monitor_slow_codex_validation_does_not_pause_hot_books(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        from open_trader import predict_cross_venue as module

        monkeypatch.setattr(module, "CROSS_VENUE_DISCOVERY_SECONDS", 0.02)
        original = monitor_predict_market(external_ids=("poly-condition",))
        predict = FakeCrossVenuePredict((original,))
        polymarket = FakeCrossVenuePolymarket()
        polymarket.release.set()

        class BlockingValidator(FakeCrossVenueValidator):
            def __init__(self) -> None:
                super().__init__()
                self.second_started = threading.Event()
                self.release_second = threading.Event()

            def validate(self, pair: ExplicitMarketPair) -> CrossVenueValidation:
                if self.calls:
                    self.second_started.set()
                    self.release_second.wait(timeout=1)
                return super().validate(pair)

        validator = BlockingValidator()
        monitor = PredictCrossVenueMonitor(
            predict_source=predict,
            polymarket_monitor=polymarket,
            validator=validator,
            gamma_lookup=monitor_gamma,
            clob_lookup=lambda condition_id: None,
            predict_quote_fn=predict_quote(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        await monitor.start()
        await wait_until(lambda: bool(predict.subscriptions))
        predict.markets = (original, rejected_predict_market())
        await wait_until(validator.second_started.is_set)

        await predict.queue.put(monitor_predict_book())

        await wait_until(lambda: bool(monitor.snapshot()["opportunities"]))
        assert validator.release_second.is_set() is False
        validator.release_second.set()
        await monitor.stop()

    asyncio.run(exercise())
