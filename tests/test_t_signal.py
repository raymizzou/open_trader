from __future__ import annotations

import json
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

from open_trader.t_signal import (
    TMarketFacts,
    TPortfolioBaseline,
    TSignal,
    TSignalEvidence,
    TSignalHardGate,
    TSignalLiquidity,
    TSignalNotification,
    TSignalPrice,
    TSignalTechnical,
    TSignalTimelineEvent,
    OpenAITSignalInterpreterClient,
    TSignalInterpreter,
    apply_ai_interpretation,
    build_t_signal_from_facts,
    build_ai_interpretation_payload,
    ratio_from_score,
    to_futu_symbol,
    validate_ai_interpretation_output,
    validate_t_signal,
)


def sample_signal(**overrides: object) -> TSignal:
    signal = TSignal(
        schema_version="open_trader.t_signal.v1",
        run_date="2026-07-02",
        market="HK",
        symbol="00700",
        futu_symbol="HK.00700",
        name="腾讯控股",
        session_phase="regular",
        updated_at="2026-07-02T14:23:08+08:00",
        action="BUY_T",
        suggested_ratio="10",
        current_status="BUY_T 已通知，等待 SELL_T 信号",
        signal_summary_zh="价格低于 VWAP 后回收。",
        price=TSignalPrice(
            last_price="376.40",
            day_change_pct="-1.20",
            vwap="378.10",
            ma_1m="376.55",
            ma_5m="376.85",
            day_low="374.80",
            day_high="382.20",
        ),
        liquidity=TSignalLiquidity(
            bid="376.35",
            ask="376.40",
            spread_pct="0.013",
            bid_depth="52000",
            ask_depth="47000",
            depth_status="pass",
        ),
        technical=TSignalTechnical(
            rsi_5m="34",
            volume_ratio_5m="1.30",
            price_position="below_vwap_reclaim",
            trend_state="range_rebound",
        ),
        hard_gates=[
            TSignalHardGate(
                name="session_phase",
                status="pass",
                message_zh="当前处于盘中交易时段。",
            )
        ],
        evidence=[
            TSignalEvidence(
                name="vwap_reclaim",
                direction="buy",
                strength="medium",
                message_zh="价格低于 VWAP 后回收。",
            )
        ],
        timeline=[
            TSignalTimelineEvent(
                event_at="2026-07-02T14:21:36+08:00",
                event_type="notification_sent",
                action="BUY_T",
                suggested_ratio="10",
                message_zh="已发送 BUY_T 通知。",
            )
        ],
        notification=TSignalNotification(
            should_notify=False,
            notified=True,
            dedupe_key="2026-07-02|HK.00700|cycle-1|BUY_T",
            last_notified_at="2026-07-02T14:21:36+08:00",
        ),
        status="ok",
        error="",
    )
    for key, value in overrides.items():
        signal = signal.with_field(key, value)
    return signal


def test_valid_signal_serializes_fixed_schema() -> None:
    payload = sample_signal().to_dict()

    assert list(payload) == [
        "schema_version",
        "run_date",
        "market",
        "symbol",
        "futu_symbol",
        "name",
        "session_phase",
        "updated_at",
        "action",
        "suggested_ratio",
        "current_status",
        "signal_summary_zh",
        "price",
        "liquidity",
        "technical",
        "hard_gates",
        "evidence",
        "timeline",
        "notification",
        "status",
        "error",
    ]
    assert payload["schema_version"] == "open_trader.t_signal.v1"
    assert payload["action"] == "BUY_T"
    assert payload["suggested_ratio"] == "10"
    assert payload["price"]["vwap"] == "378.10"


@pytest.mark.parametrize("action", ["BUY", "SELL", "小T", ""])
def test_validate_rejects_illegal_action(action: str) -> None:
    with pytest.raises(ValueError, match="invalid action"):
        validate_t_signal(sample_signal(action=action))


@pytest.mark.parametrize("ratio", ["5", "12", "20%", "small"])
def test_validate_rejects_illegal_ratio(ratio: str) -> None:
    with pytest.raises(ValueError, match="invalid suggested_ratio"):
        validate_t_signal(sample_signal(suggested_ratio=ratio))


def test_validate_rejects_buy_signal_without_ratio() -> None:
    with pytest.raises(ValueError, match="BUY_T requires suggested_ratio"):
        validate_t_signal(sample_signal(action="BUY_T", suggested_ratio=""))


def test_validate_allows_hold_without_ratio() -> None:
    validate_t_signal(sample_signal(action="HOLD", suggested_ratio="", status="ok"))


def regular_facts(**overrides: object) -> TMarketFacts:
    facts = TMarketFacts(
        run_date="2026-07-02",
        market="HK",
        symbol="00700",
        futu_symbol="HK.00700",
        name="腾讯控股",
        session_phase="regular",
        updated_at="2026-07-02T14:23:08+08:00",
        last_price=Decimal("376.40"),
        day_change_pct=Decimal("-1.20"),
        vwap=Decimal("378.10"),
        ma_1m=Decimal("376.55"),
        ma_5m=Decimal("376.85"),
        day_low=Decimal("374.80"),
        day_high=Decimal("382.20"),
        bid=Decimal("376.35"),
        ask=Decimal("376.40"),
        bid_depth=Decimal("52000"),
        ask_depth=Decimal("47000"),
        rsi_5m=Decimal("34"),
        volume_ratio_5m=Decimal("1.30"),
    )
    for key, value in overrides.items():
        facts = facts.with_field(key, value)
    return facts


def test_to_futu_symbol_normalizes_hk_numeric_symbol() -> None:
    assert to_futu_symbol("HK", "700") == "HK.00700"
    assert to_futu_symbol("US", "msft") == "US.MSFT"
    assert to_futu_symbol("US", "BRK.B") == "US.BRK.B"
    assert to_futu_symbol("HK", "HK.700") == "HK.00700"
    assert to_futu_symbol("HK", "HK.00700") == "HK.00700"
    assert to_futu_symbol("US", "us.msft") == "US.MSFT"
    assert to_futu_symbol("US", "US.MSFT") == "US.MSFT"


@pytest.mark.parametrize(
    ("market", "symbol"),
    [("HK", "US.MSFT"), ("CN", "600000"), ("US", "")],
)
def test_to_futu_symbol_rejects_invalid_market_or_prefix(
    market: str,
    symbol: str,
) -> None:
    with pytest.raises(ValueError):
        to_futu_symbol(market, symbol)


@pytest.mark.parametrize(
    ("score", "ratio"),
    [(0, ""), (1, "6"), (2, "10"), (3, "15"), (4, "20"), (8, "20")],
)
def test_ratio_from_score_maps_fixed_ratio(score: int, ratio: str) -> None:
    assert ratio_from_score(score) == ratio


def test_regular_rebound_builds_buy_signal_with_ratio() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="价格低于 VWAP 后回收，接近支撑。",
    )

    assert signal.action == "BUY_T"
    assert signal.suggested_ratio == "15"
    assert signal.technical.price_position == "below_vwap_reclaim"
    assert any(item.name == "vwap_reclaim" for item in signal.evidence)
    assert signal.status == "ok"


def test_regular_reject_builds_sell_signal_with_ratio() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(
            last_price=Decimal("380"),
            vwap=Decimal("378.10"),
            rsi_5m=Decimal("66"),
        ),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="价格高于 VWAP 后回落，接近压力。",
    )

    assert signal.action == "SELL_T"
    assert signal.suggested_ratio == "15"
    assert signal.technical.price_position == "above_vwap_reject"
    assert any(item.name == "vwap_reject" for item in signal.evidence)
    assert signal.status == "ok"


def test_sell_signal_ratio_ignores_buy_side_evidence() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(
            last_price=Decimal("380"),
            vwap=Decimal("378.10"),
            rsi_5m=Decimal("34"),
        ),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="价格高于 VWAP 但 RSI 偏低，信号冲突。",
    )

    assert signal.action == "SELL_T"
    assert signal.suggested_ratio == "10"
    assert any(item.name == "vwap_reject" for item in signal.evidence)
    assert any(item.name == "rsi_rebound_zone" for item in signal.evidence)


def test_build_t_signal_canonicalizes_blank_futu_symbol() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(symbol="700", futu_symbol=""),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="使用标的代码规范化。",
    )

    assert signal.futu_symbol == "HK.00700"
    assert signal.notification.dedupe_key.startswith("2026-07-02|HK.00700|")


def test_build_t_signal_canonicalizes_uncanonical_futu_symbol() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(futu_symbol="HK.700"),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="使用富途代码规范化。",
    )

    assert signal.futu_symbol == "HK.00700"
    assert signal.notification.dedupe_key.startswith("2026-07-02|HK.00700|")


def test_build_t_signal_blocks_mismatched_futu_symbol_without_raising() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(futu_symbol="US.MSFT"),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="市场和代码前缀不匹配。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "symbol" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_build_t_signal_blocks_unsupported_market_without_raising() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(market="CN", symbol="600000", futu_symbol=""),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="暂不支持该市场。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "symbol" and gate.status == "block"
        for gate in signal.hard_gates
    )


@pytest.mark.parametrize(
    "facts",
    [
        regular_facts(symbol="123456", futu_symbol=""),
        regular_facts(symbol="123456", futu_symbol="HK.123456"),
    ],
)
def test_build_t_signal_blocks_malformed_long_hk_symbol(
    facts: TMarketFacts,
) -> None:
    signal = build_t_signal_from_facts(
        facts=facts,
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="港股代码格式异常。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "symbol" and gate.status == "block"
        for gate in signal.hard_gates
    )


@pytest.mark.parametrize(
    "facts",
    [
        regular_facts(symbol="00700", futu_symbol="HK.00701"),
        regular_facts(
            market="US",
            symbol="MSFT",
            futu_symbol="US.AAPL",
        ),
    ],
)
def test_build_t_signal_blocks_symbol_identity_mismatch(
    facts: TMarketFacts,
) -> None:
    signal = build_t_signal_from_facts(
        facts=facts,
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="标的代码与富途代码不一致。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "symbol" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_unsupported_session_phase_returns_review_without_raising() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(session_phase="auction"),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="集合竞价只观察。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.session_phase == "unknown"
    assert signal.status == "review"
    assert any(
        gate.name == "session_phase" and gate.status == "block"
        for gate in signal.hard_gates
    )
    assert any(item.name == "unsupported_session_phase" for item in signal.evidence)


@pytest.mark.parametrize("phase", ["pre_market", "post_market", "closed", "unknown"])
def test_non_regular_session_blocks_buy_sell(phase: str) -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(session_phase=phase),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="盘前只观察。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "session_phase" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_wide_spread_blocks_action() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(bid=Decimal("375"), ask=Decimal("376.40")),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="价差过大。",
    )

    assert signal.action == "REVIEW"
    assert signal.liquidity.depth_status == "wide_spread"
    assert signal.suggested_ratio == ""


def test_middle_range_high_volume_does_not_emit_directional_evidence() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(
            last_price=Decimal("378.10"),
            vwap=Decimal("378.10"),
            rsi_5m=Decimal("50"),
            volume_ratio_5m=Decimal("1.30"),
        ),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="价格位于 VWAP 附近，量能放大但方向不明确。",
    )

    assert signal.action == "HOLD"
    assert signal.suggested_ratio == ""
    assert signal.technical.price_position == "middle_range"
    assert not any(item.direction in {"buy", "sell"} for item in signal.evidence)


def test_missing_technical_facts_blocks_action() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(
            ma_1m=None,
            ma_5m=None,
            rsi_5m=None,
            volume_ratio_5m=None,
        ),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="技术指标不完整。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "technical" and gate.status == "block"
        for gate in signal.hard_gates
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"day_change_pct": None},
        {"day_low": None},
        {"day_high": None},
        {"day_change_pct": Decimal("NaN")},
        {"day_low": Decimal("NaN")},
        {"day_high": Decimal("NaN")},
    ],
)
def test_incomplete_market_price_facts_block_action(
    overrides: dict[str, object],
) -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(**overrides),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="行情价格字段不完整。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "technical" and gate.status == "block"
        for gate in signal.hard_gates
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"last_price": Decimal("0")},
        {"last_price": Decimal("-1")},
        {"vwap": Decimal("0")},
        {"ma_1m": Decimal("0")},
        {"ma_5m": Decimal("0")},
        {"day_low": Decimal("0")},
        {"day_high": Decimal("0")},
        {"volume_ratio_5m": Decimal("0")},
    ],
)
def test_non_positive_required_price_facts_block_action(
    overrides: dict[str, object],
) -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(**overrides),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="行情价格字段非正数。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "technical" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_non_finite_last_price_blocks_action_without_raising() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(last_price=Decimal("NaN")),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="价格数据异常。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "technical" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_crossed_bid_ask_blocks_action() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(bid=Decimal("376.40"), ask=Decimal("376.35")),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="买卖盘倒挂。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.liquidity.depth_status == "missing"
    assert any(
        gate.name == "liquidity" and gate.status == "block"
        for gate in signal.hard_gates
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"bid": None},
        {"ask": None},
        {"bid_depth": None},
        {"ask_depth": None},
    ],
)
def test_missing_liquidity_blocks_action(overrides: dict[str, object]) -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(**overrides),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="流动性数据缺失。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.liquidity.depth_status == "missing"
    assert any(
        gate.name == "liquidity" and gate.status == "block"
        for gate in signal.hard_gates
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"bid_depth": Decimal("0")},
        {"ask_depth": Decimal("0")},
        {"bid_depth": Decimal("-1")},
        {"ask_depth": Decimal("-1")},
    ],
)
def test_non_positive_depth_blocks_action(overrides: dict[str, object]) -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(**overrides),
        baseline=TPortfolioBaseline(total_quantity=Decimal("300")),
        previous=None,
        ai_summary_zh="买卖盘深度不足。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.liquidity.depth_status == "thin"
    assert any(
        gate.name == "liquidity" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_missing_baseline_blocks_action() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(),
        baseline=TPortfolioBaseline(total_quantity=Decimal("0")),
        previous=None,
        ai_summary_zh="无底仓。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert any(
        gate.name == "baseline" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_non_finite_baseline_blocks_action_without_raising() -> None:
    signal = build_t_signal_from_facts(
        facts=regular_facts(),
        baseline=TPortfolioBaseline(total_quantity=Decimal("NaN")),
        previous=None,
        ai_summary_zh="底仓数据异常。",
    )

    assert signal.action == "REVIEW"
    assert signal.suggested_ratio == ""
    assert signal.status == "review"
    assert any(
        gate.name == "baseline" and gate.status == "block"
        for gate in signal.hard_gates
    )


def test_build_ai_interpretation_payload_uses_structured_signal_fields() -> None:
    signal = sample_signal()

    payload = build_ai_interpretation_payload(signal)

    assert payload["action"] == "BUY_T"
    assert payload["suggested_ratio"] == "10"
    assert payload["price"]["last_price"] == "376.40"
    assert payload["evidence"] == [
        {
            "name": "vwap_reclaim",
            "direction": "buy",
            "strength": "medium",
            "message_zh": "价格低于 VWAP 后回收。",
        }
    ]
    assert "notification" not in payload


def test_validate_ai_interpretation_accepts_matching_chinese_output() -> None:
    parsed = validate_ai_interpretation_output(
        json.dumps(
            {
                "action": "BUY_T",
                "suggested_ratio": "10",
                "signal_summary_zh": "价格低于 VWAP 后回收，短线反弹条件成立。",
                "ratio_rationale_zh": "10% 来自规则层评分，且硬性条件均通过。",
                "evidence_refs": ["vwap_reclaim"],
            },
            ensure_ascii=False,
        ),
        sample_signal(),
    )

    assert parsed.action == "BUY_T"
    assert parsed.suggested_ratio == "10"
    assert parsed.evidence_refs == ["vwap_reclaim"]


def test_apply_ai_interpretation_updates_summary_without_changing_rule_action() -> None:
    signal = sample_signal()

    interpreted = apply_ai_interpretation(
        signal,
        json.dumps(
            {
                "action": "BUY_T",
                "suggested_ratio": "10",
                "signal_summary_zh": "价格低于 VWAP 后回收，短线反弹条件成立。",
                "ratio_rationale_zh": "10% 来自规则层评分，且硬性条件均通过。",
                "evidence_refs": ["vwap_reclaim"],
            },
            ensure_ascii=False,
        ),
    )

    assert interpreted.action == "BUY_T"
    assert interpreted.suggested_ratio == "10"
    assert "短线反弹条件成立" in interpreted.signal_summary_zh
    assert "比例依据" in interpreted.signal_summary_zh
    assert interpreted.error == ""


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(
            {
                "action": "SELL_T",
                "suggested_ratio": "10",
                "signal_summary_zh": "价格低于 VWAP 后回收，短线反弹条件成立。",
                "ratio_rationale_zh": "10% 来自规则层评分，且硬性条件均通过。",
                "evidence_refs": ["vwap_reclaim"],
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "action": "BUY_T",
                "suggested_ratio": "20",
                "signal_summary_zh": "价格低于 VWAP 后回收，短线反弹条件成立。",
                "ratio_rationale_zh": "20% 来自模型判断。",
                "evidence_refs": ["vwap_reclaim"],
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "action": "BUY_T",
                "suggested_ratio": "10",
                "signal_summary_zh": "Price reclaimed VWAP, buy now.",
                "ratio_rationale_zh": "Rule score supports 10%.",
                "evidence_refs": ["vwap_reclaim"],
            }
        ),
        json.dumps(
            {
                "action": "BUY_T",
                "suggested_ratio": "10",
                "signal_summary_zh": "价格低于 VWAP 后回收，短线反弹条件成立。",
                "ratio_rationale_zh": "10% 来自规则层评分，且硬性条件均通过。",
                "evidence_refs": ["invented_signal"],
            },
            ensure_ascii=False,
        ),
    ],
)
def test_apply_ai_interpretation_degrades_invalid_ai_output_to_review(raw: str) -> None:
    signal = sample_signal()

    interpreted = apply_ai_interpretation(signal, raw)

    assert interpreted.action == "REVIEW"
    assert interpreted.suggested_ratio == ""
    assert interpreted.status == "review"
    assert interpreted.notification.should_notify is False
    assert interpreted.evidence == signal.evidence
    assert interpreted.timeline[-1].event_type == "review_required"
    assert interpreted.timeline[-1].action == "REVIEW"
    assert "AI 解读未通过验证" in interpreted.signal_summary_zh
    assert "AI interpretation invalid" in interpreted.error


def test_t_signal_interpreter_calls_client_with_structured_payload() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def interpret(self, prompt: str, payload: dict[str, object]) -> str:
            self.calls.append({"prompt": prompt, "payload": payload})
            return json.dumps(
                {
                    "action": "BUY_T",
                    "suggested_ratio": "10",
                    "signal_summary_zh": "价格低于 VWAP 后回收，短线反弹条件成立。",
                    "ratio_rationale_zh": "10% 来自规则层评分，且硬性条件均通过。",
                    "evidence_refs": ["vwap_reclaim"],
                },
                ensure_ascii=False,
            )

    client = FakeClient()
    interpreted = TSignalInterpreter(client=client).interpret(sample_signal())

    assert interpreted.action == "BUY_T"
    assert "短线反弹条件成立" in interpreted.signal_summary_zh
    assert len(client.calls) == 1
    assert "不得改写 action" in str(client.calls[0]["prompt"])
    assert client.calls[0]["payload"]["symbol"] == "00700"
    assert "notification" not in client.calls[0]["payload"]


def test_t_signal_interpreter_degrades_client_failure_to_review() -> None:
    class RaisingClient:
        def interpret(self, prompt: str, payload: dict[str, object]) -> str:
            raise RuntimeError("llm unavailable")

    interpreted = TSignalInterpreter(client=RaisingClient()).interpret(sample_signal())

    assert interpreted.action == "REVIEW"
    assert interpreted.suggested_ratio == ""
    assert interpreted.status == "review"
    assert interpreted.notification.should_notify is False
    assert "llm unavailable" in interpreted.error


def test_openai_t_signal_interpreter_client_uses_deepseek_json_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object):
            captured["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"action":"HOLD"}')
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(
            self,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            timeout: float | None = None,
        ) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["client_timeout"] = timeout
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    client = OpenAITSignalInterpreterClient(timeout_seconds=8.0)
    content = client.interpret("Return JSON.", {"symbol": "00700"})

    assert content == '{"action":"HOLD"}'
    assert captured["api_key"] == "deepseek-secret"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["client_timeout"] == 8.0
    request = captured["request"]
    assert request["model"] == "deepseek-v4-flash"
    assert request["response_format"] == {"type": "json_object"}
    assert request["timeout"] == 8.0
