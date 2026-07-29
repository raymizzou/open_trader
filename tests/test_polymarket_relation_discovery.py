from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from open_trader.polymarket_relation_discovery import (
    CODEX_PROMPT_VERSION,
    CodexRelationValidator,
    codex_relation_cache_key,
    discover_threshold_relations,
)
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


RULES = (
    'This market resolves "Yes" if the Binance BTC/USDT close at 12:00 ET '
    "is higher than the price specified in the title. Otherwise it resolves "
    '"No". The resolution source is Binance.'
)


def market(
    market_id: str,
    *,
    question: str,
    rules: str = RULES,
    source: str = "Binance",
    end_date: str = "2026-12-31T17:00:00Z",
    group_item_threshold: str = "0",
    condition_id: str | None = None,
    active: bool = True,
    closed: bool = False,
    accepting_orders: bool = True,
    enable_order_book: bool = True,
    neg_risk: bool = False,
    outcomes: object = '["Yes", "No"]',
    token_ids: object | None = None,
) -> dict[str, object]:
    return {
        "id": market_id,
        "conditionId": condition_id or f"condition-{market_id}",
        "question": question,
        "description": rules,
        "resolutionSource": source,
        "endDate": end_date,
        "groupItemThreshold": group_item_threshold,
        "active": active,
        "closed": closed,
        "acceptingOrders": accepting_orders,
        "enableOrderBook": enable_order_book,
        "negRisk": neg_risk,
        "outcomes": outcomes,
        "clobTokenIds": token_ids or f'["yes-{market_id}", "no-{market_id}"]',
        "feesEnabled": True,
        "feeSchedule": {
            "rate": 0.07,
            "exponent": 1,
            "takerOnly": True,
            "rebateRate": 0.2,
        },
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.001,
    }


def event(
    *markets: dict[str, object],
    event_id: str = "event-1",
    active: bool = True,
    closed: bool = False,
    ended: bool = False,
    neg_risk: bool = False,
) -> dict[str, object]:
    return {
        "id": event_id,
        "title": "Bitcoin above ___ on December 31?",
        "active": active,
        "closed": closed,
        "ended": ended,
        "negRisk": neg_risk,
        "markets": list(markets),
    }


def test_exact_above_template_builds_the_logical_relation_and_buy_legs() -> None:
    relations = discover_threshold_relations(
        [
            event(
                market("lower", question="Will Bitcoin be above $90,000 on December 31?"),
                market("higher", question="Will Bitcoin be above $100,000 on December 31?"),
            )
        ]
    )

    assert len(relations) == 1
    relation = relations[0]
    assert relation.market_a.threshold == Decimal("90000")
    assert relation.market_b.threshold == Decimal("100000")
    assert relation.market_a.operator == ">"
    assert relation.market_b.operator == ">"
    assert relation.relation == "B_IMPLIES_A"
    assert (relation.buy_leg_a.outcome, relation.buy_leg_a.token_id) == (
        "YES",
        "yes-lower",
    )
    assert (relation.buy_leg_b.outcome, relation.buy_leg_b.token_id) == (
        "NO",
        "no-higher",
    )


def test_below_template_reverses_the_outcomes_but_keeps_low_high_order() -> None:
    rules = RULES.replace("higher than", "lower than")
    relation = discover_threshold_relations(
        [
            event(
                market(
                    "lower",
                    question="Will Bitcoin be below $90,000 on December 31?",
                    rules=rules,
                ),
                market(
                    "higher",
                    question="Will Bitcoin be below $100,000 on December 31?",
                    rules=rules,
                ),
            )
        ]
    )[0]

    assert relation.relation == "A_IMPLIES_B"
    assert relation.buy_leg_a.outcome == "NO"
    assert relation.buy_leg_a.token_id == "no-lower"
    assert relation.buy_leg_b.outcome == "YES"
    assert relation.buy_leg_b.token_id == "yes-higher"


def test_group_item_threshold_is_never_used_as_the_economic_threshold() -> None:
    relation = discover_threshold_relations(
        [
            event(
                market(
                    "lower",
                    question="Will Bitcoin be above $90k on December 31?",
                    group_item_threshold="0",
                ),
                market(
                    "higher",
                    question="Will Bitcoin be above $100k on December 31?",
                    group_item_threshold="1",
                ),
            )
        ]
    )[0]

    assert relation.market_a.threshold == Decimal("90000")
    assert relation.market_b.threshold == Decimal("100000")
    assert relation.market_a.group_item_threshold == "0"
    assert relation.market_b.group_item_threshold == "1"


def test_identical_rules_or_rules_differing_only_by_threshold_are_certified() -> None:
    embedded = RULES.replace(
        "the price specified in the title",
        "the stated threshold of $90,000",
    )
    higher = embedded.replace("$90,000", "$100,000")

    identical_rules = discover_threshold_relations(
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market("higher", question="BTC above $100,000?"),
            )
        ]
    )
    threshold_rules = discover_threshold_relations(
        [
            event(
                market("lower", question="BTC above $90,000?", rules=embedded),
                market("higher", question="BTC above $100,000?", rules=higher),
            )
        ]
    )

    assert len(identical_rules) == 1
    assert len(threshold_rules) == 1


@pytest.mark.parametrize(
    "events",
    [
        [
            event(market("lower", question="BTC above $90,000?"), event_id="one"),
            event(market("higher", question="BTC above $100,000?"), event_id="two"),
        ],
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market(
                    "higher",
                    question="BTC above $100,000?",
                    rules=RULES + " A disputed source resolves 50-50.",
                ),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market("higher", question="BTC above $100,000?", source="Coinbase"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?"),
                market(
                    "higher",
                    question="BTC above $100,000?",
                    end_date="2027-01-01T17:00:00Z",
                ),
            )
        ],
        [
            event(
                market("lower", question="Will Bitcoin hit $90,000?"),
                market("higher", question="Will Bitcoin hit $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", neg_risk=True),
                market("higher", question="BTC above $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", closed=True),
                market("higher", question="BTC above $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", outcomes='["Up", "Down"]'),
                market("higher", question="BTC above $100,000?"),
            )
        ],
        [
            event(
                market("lower", question="BTC above $90,000?", token_ids='["same", "same"]'),
                market("higher", question="BTC above $100,000?"),
            )
        ],
    ],
)
def test_non_machine_provable_pairs_are_rejected(
    events: list[dict[str, object]],
) -> None:
    assert discover_threshold_relations(events) == ()


@pytest.mark.parametrize(
    "event_state",
    [
        {"active": False},
        {"closed": True},
        {"ended": True},
        {"neg_risk": True},
    ],
)
def test_ineligible_event_state_rejects_the_whole_family(
    event_state: dict[str, bool],
) -> None:
    value = event(
        market("lower", question="BTC above $90,000?"),
        market("higher", question="BTC above $100,000?"),
        **event_state,
    )

    assert discover_threshold_relations([value]) == ()


def threshold_relation():
    return discover_threshold_relations(
        [
            event(
                market(
                    "lower",
                    question="Will Bitcoin be above $90,000 on December 31?",
                    condition_id="condition-lower",
                ),
                market(
                    "higher",
                    question="Will Bitcoin be above $100,000 on December 31?",
                    condition_id="condition-higher",
                ),
            )
        ]
    )[0]


def codex_market_result(
    *,
    condition_id: str,
    threshold: str,
) -> dict[str, object]:
    return {
        "condition_id": condition_id,
        "subject": "Bitcoin",
        "metric": "Binance BTC/USDT close",
        "operator": ">",
        "threshold": threshold,
        "unit": "USD",
        "currency": "USD",
        "observation_start": "2026-12-31T17:00:00Z",
        "observation_end": "2026-12-31T17:00:00Z",
        "timezone": "America/New_York",
        "resolution_source": "Binance",
        "special_settlement": None,
    }


def codex_result(*, decision: str = "APPROVE") -> dict[str, object]:
    approved = decision == "APPROVE"
    return {
        "schema_version": 1,
        "decision": decision,
        "relation": "B_IMPLIES_A" if approved else "NONE",
        "market_a": codex_market_result(
            condition_id="condition-lower",
            threshold="90000",
        ),
        "market_b": codex_market_result(
            condition_id="condition-higher",
            threshold="100000",
        ),
        "proof": {
            "excluded_state": "A=NO,B=YES" if approved else None,
            "why_excluded": (
                "A has the lower threshold, so B=YES requires A=YES."
                if approved
                else None
            ),
        },
        "reason_codes": [] if approved else ["AMBIGUOUS_RULES"],
        "summary": (
            "高阈值合约为 YES 时，低阈值合约必为 YES。"
            if approved
            else "完整规则仍有歧义，不能证明蕴含关系。"
        ),
        "evidence": (
            [
                {
                    "market": "A",
                    "field": "metric",
                    "quote": "Binance BTC/USDT close at 12:00 ET",
                },
                {
                    "market": "B",
                    "field": "metric",
                    "quote": "Binance BTC/USDT close at 12:00 ET",
                },
            ]
            if approved
            else []
        ),
        "uncertainties": [] if approved else ["规则存在无法消除的歧义"],
    }


def codex_jsonl(
    result: dict[str, object],
    *,
    usage: dict[str, int] | None = None,
) -> str:
    return "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(result),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": usage
                    or {
                        "input_tokens": 100,
                        "cached_input_tokens": 60,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                    },
                }
            ),
        )
    )


def codex_store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def test_codex_fingerprint_uses_only_versioned_semantic_payload() -> None:
    relation = threshold_relation()
    expected = codex_relation_cache_key(
        relation,
        model="gpt-test",
        prompt_version=CODEX_PROMPT_VERSION,
    )
    payload = {
        "market_a": {
            "condition_id": "condition-lower",
            "question": "Will Bitcoin be above $90,000 on December 31?",
            "rules": RULES,
            "resolution_source": "Binance",
            "end_date": "2026-12-31T17:00:00Z",
            "updated_at": "",
        },
        "market_b": {
            "condition_id": "condition-higher",
            "question": "Will Bitcoin be above $100,000 on December 31?",
            "rules": RULES,
            "resolution_source": "Binance",
            "end_date": "2026-12-31T17:00:00Z",
            "updated_at": "",
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert expected == hashlib.sha256(
        f"gpt-test{CODEX_PROMPT_VERSION}{canonical}".encode()
    ).hexdigest()
    price_only_change = replace(
        relation,
        market_a=replace(relation.market_a, fee_rate=Decimal("0.99")),
    )
    rules_change = replace(
        relation,
        market_a=replace(relation.market_a, rules=relation.market_a.rules + " Extra."),
    )
    condition_change = replace(
        relation,
        market_a=replace(relation.market_a, condition_id="condition-new"),
    )

    assert (
        codex_relation_cache_key(
            price_only_change,
            model="gpt-test",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        == expected
    )
    assert (
        codex_relation_cache_key(
            rules_change,
            model="gpt-test",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        != expected
    )
    assert (
        codex_relation_cache_key(
            condition_change,
            model="gpt-test",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        != expected
    )
    assert (
        codex_relation_cache_key(
            relation,
            model="different-model",
            prompt_version=CODEX_PROMPT_VERSION,
        )
        != expected
    )
    assert (
        codex_relation_cache_key(
            relation,
            model="gpt-test",
            prompt_version="polymarket-threshold-relation-v2",
        )
        != expected
    )


def test_codex_approve_uses_isolated_structured_command_and_records_usage(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(codex_result()),
            stderr="",
        )

    db = codex_store(tmp_path)
    result = CodexRelationValidator(
        db,
        model="gpt-test",
        runner=runner,
    ).validate(threshold_relation())

    assert result.status == "approved"
    assert result.decision == "APPROVE"
    assert result.relation == "B_IMPLIES_A"
    assert result.cached is False
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--output-schema" in command
    assert "--json" in command
    assert kwargs["input"].endswith("\n")
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 45.0
    assert Path(str(kwargs["cwd"])) != Path.cwd()
    assert db.llm_usage_24h() == {
        "calls": 1,
        "successes": 1,
        "failures": 0,
        "cache_hits": 0,
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }


def test_codex_cache_survives_validator_restart_without_new_process(
    tmp_path: Path,
) -> None:
    db = codex_store(tmp_path)
    relation = threshold_relation()
    first = CodexRelationValidator(
        db,
        model="gpt-test",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(codex_result()),
            stderr="",
        ),
    ).validate(relation)

    def must_not_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("persistent cache miss")

    second = CodexRelationValidator(
        PredictionArbitrageStore(db.data_dir),
        model="gpt-test",
        runner=must_not_run,
    ).validate(relation)

    assert first.status == second.status == "approved"
    assert second.cached is True
    assert db.llm_usage_24h()["calls"] == 1
    assert db.llm_usage_24h()["cache_hits"] == 1


def test_codex_reject_is_cached_with_operator_visible_reason(
    tmp_path: Path,
) -> None:
    db = codex_store(tmp_path)
    relation = threshold_relation()
    runner = lambda command, **kwargs: subprocess.CompletedProcess(
        command,
        0,
        stdout=codex_jsonl(codex_result(decision="REJECT")),
        stderr="",
    )

    first = CodexRelationValidator(
        db, model="gpt-test", runner=runner
    ).validate(relation)
    second = CodexRelationValidator(
        db, model="gpt-test", runner=runner
    ).validate(relation)

    assert first.status == "llm_rejected"
    assert first.reason_codes == ("AMBIGUOUS_RULES",)
    assert "歧义" in first.summary
    assert second.cached is True
    assert db.llm_usage_24h()["calls"] == 1


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (subprocess.TimeoutExpired(["codex"], 45), "CODEX_TIMEOUT"),
        (
            subprocess.CompletedProcess(["codex"], 1, stdout="", stderr="secret"),
            "CODEX_FAILED",
        ),
        (
            subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout=json.dumps({"type": "turn.completed", "usage": {}}),
                stderr="",
            ),
            "CODEX_OUTPUT_INVALID",
        ),
        (
            subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout=codex_jsonl({**codex_result(), "unknown": True}),
                stderr="",
            ),
            "CODEX_OUTPUT_INVALID",
        ),
    ],
)
def test_codex_unavailable_results_are_not_cached(
    tmp_path: Path,
    response: object,
    expected_reason: str,
) -> None:
    calls = 0

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, subprocess.CompletedProcess)
        return response

    validator = CodexRelationValidator(
        codex_store(tmp_path),
        model="gpt-test",
        runner=runner,
    )
    first = validator.validate(threshold_relation())
    second = validator.validate(threshold_relation())

    assert first.status == second.status == "llm_unavailable"
    assert first.reason_codes == (expected_reason,)
    assert calls == 2
    assert validator.store.llm_usage_24h()["failures"] == 2
    assert validator.store.llm_usage_24h()["cache_hits"] == 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda value: {
                **value,
                "market_a": {
                    **value["market_a"],
                    "condition_id": "forged-condition",
                },
            },
            "CONDITION_ID_MISMATCH",
        ),
        (
            lambda value: {
                **value,
                "evidence": [
                    {
                        "market": "A",
                        "field": "rules",
                        "quote": "text not present in either rule",
                    },
                    value["evidence"][1],
                ],
            },
            "EVIDENCE_NOT_FOUND",
        ),
        (
            lambda value: {
                **value,
                "uncertainties": ["仍可能存在特殊结算"],
            },
            "UNRESOLVED_UNCERTAINTY",
        ),
        (
            lambda value: {**value, "relation": "A_IMPLIES_B"},
            "RELATION_MISMATCH",
        ),
    ],
)
def test_codex_approve_must_pass_deterministic_post_validation(
    tmp_path: Path,
    mutate,
    reason: str,
) -> None:
    structured = mutate(codex_result())
    calls = 0

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_jsonl(structured),
            stderr="",
        )

    validator = CodexRelationValidator(
        codex_store(tmp_path),
        model="gpt-test",
        runner=runner,
    )
    first = validator.validate(threshold_relation())
    second = validator.validate(threshold_relation())

    assert first.status == second.status == "deterministic_rejected"
    assert first.reason_codes == (reason,)
    assert calls == 2
    assert validator.store.llm_usage_24h()["cache_hits"] == 0
