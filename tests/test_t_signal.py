from __future__ import annotations

from decimal import Decimal

import pytest

from open_trader.t_signal import (
    TSignal,
    TSignalEvidence,
    TSignalHardGate,
    TSignalLiquidity,
    TSignalNotification,
    TSignalPrice,
    TSignalTechnical,
    TSignalTimelineEvent,
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
