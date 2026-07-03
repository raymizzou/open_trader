from __future__ import annotations

from pathlib import Path

from open_trader.t_signal import (
    TSignal,
    TSignalEvidence,
    TSignalHardGate,
    TSignalLiquidity,
    TSignalNotification,
    TSignalPrice,
    TSignalTechnical,
    TSignalTimelineEvent,
)
from open_trader.t_signal_store import (
    T_SIGNALS_CACHE_SCHEMA_VERSION,
    index_t_signals_by_market_symbol,
    load_t_signals_cache,
    t_signals_latest_path,
    t_signals_run_path,
    write_t_signals_artifact,
)


def sample_signal() -> TSignal:
    return TSignal(
        schema_version="open_trader.t_signal.v1",
        run_date="2026-07-02",
        market="US",
        symbol="VIXY",
        futu_symbol="US.VIXY",
        name="Volatility ETF",
        session_phase="regular",
        updated_at="2026-07-02T22:31:00+08:00",
        action="BUY_T",
        suggested_ratio="10",
        current_status="BUY_T 条件满足，等待执行确认。",
        signal_summary_zh="价格低于 VWAP 后回收，适合按 10% 底仓比例低吸买回。",
        price=TSignalPrice(
            last_price="48.50",
            day_change_pct="-1.20",
            vwap="49.10",
            ma_1m="48.55",
            ma_5m="48.85",
            day_low="48.00",
            day_high="50.20",
        ),
        liquidity=TSignalLiquidity(
            bid="48.49",
            ask="48.50",
            spread_pct="0.021",
            bid_depth="5000",
            ask_depth="4700",
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
                event_at="2026-07-02T22:31:00+08:00",
                event_type="signal_created",
                action="BUY_T",
                suggested_ratio="10",
                message_zh="生成 BUY_T 信号，建议比例 10%。",
            )
        ],
        notification=TSignalNotification(
            should_notify=True,
            notified=False,
            dedupe_key="2026-07-02|US.VIXY|BUY_T|10",
            last_notified_at="",
            last_notified_dedupe_key="",
            last_attempted_dedupe_key="",
        ),
        status="ok",
        error="",
    )


def test_t_signal_store_writes_run_and_latest_artifacts(tmp_path: Path) -> None:
    result = write_t_signals_artifact(
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        signals=[sample_signal()],
        generated_at="2026-07-02T22:32:00+08:00",
    )

    assert result.run_path == tmp_path / "data/runs/2026-07-02/US/t_signals.json"
    assert result.latest_path == tmp_path / "data/latest/US/t_signals.json"
    cache = load_t_signals_cache(result.latest_path)
    assert cache["schema_version"] == T_SIGNALS_CACHE_SCHEMA_VERSION
    assert cache["generated_at"] == "2026-07-02T22:32:00+08:00"
    assert cache["records"][0]["symbol"] == "VIXY"


def test_t_signal_store_indexes_records_by_market_symbol(tmp_path: Path) -> None:
    write_t_signals_artifact(
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        signals=[sample_signal()],
        generated_at="2026-07-02T22:32:00+08:00",
    )

    indexed = index_t_signals_by_market_symbol(
        load_t_signals_cache(t_signals_latest_path(tmp_path / "data", "US"))
    )

    assert indexed[("US", "VIXY")]["action"] == "BUY_T"
    assert indexed[("US", "VIXY")]["suggested_ratio"] == "10"


def test_t_signal_paths_are_market_scoped(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    assert t_signals_run_path(data_dir, "2026-07-02", "HK") == (
        data_dir / "runs/2026-07-02/HK/t_signals.json"
    )
    assert t_signals_latest_path(data_dir, "HK") == (
        data_dir / "latest/HK/t_signals.json"
    )
