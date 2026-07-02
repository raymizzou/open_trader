from __future__ import annotations

from decimal import Decimal

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
    build_t_signal_from_facts,
    ratio_from_score,
    to_futu_symbol,
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
    assert to_futu_symbol("HK", "HK.700") == "HK.00700"
    assert to_futu_symbol("HK", "HK.00700") == "HK.00700"
    assert to_futu_symbol("US", "us.msft") == "US.MSFT"
    assert to_futu_symbol("US", "US.MSFT") == "US.MSFT"


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
