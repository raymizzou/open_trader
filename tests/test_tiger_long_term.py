from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from open_trader.kline_technical_facts import DailyKlineBar
from open_trader.standard_strategies import StrategyBar
from open_trader.tiger_long_term import (
    allocate_target_weights,
    generate_tiger_long_term_strategy,
    load_tiger_long_term_strategy,
    load_tiger_long_term_config,
    rebalance_reasons,
    sma200_state,
)


def bars(closes: list[str]) -> list[StrategyBar]:
    start = date(2020, 1, 1)
    return [
        StrategyBar(
            start + timedelta(days=index),
            Decimal(close),
            Decimal(close),
            Decimal(close),
            Decimal(close),
            Decimal("100"),
        )
        for index, close in enumerate(closes)
    ]


def test_loads_fixed_tiger_pool(tmp_path) -> None:
    path = tmp_path / "strategy.json"
    path.write_text(
        '{"strategy_id":"tiger_sma200_equal_weight/v1",'
        '"account_alias":"tiger_5683",'
        '"members":{"QQQ":"broad_us_growth"}}',
        encoding="utf-8",
    )

    config = load_tiger_long_term_config(path)

    assert config.members == {"QQQ": "broad_us_growth"}


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"strategy_id":"wrong","account_alias":"tiger_5683","members":{"QQQ":"broad"}}',
        '{"strategy_id":"tiger_sma200_equal_weight/v1","account_alias":"","members":{"QQQ":"broad"}}',
        '{"strategy_id":"tiger_sma200_equal_weight/v1","account_alias":"tiger_5683","members":{}}',
    ],
)
def test_rejects_invalid_tiger_pool(tmp_path, payload: str) -> None:
    path = tmp_path / "strategy.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="Tiger 长线策略配置无效"):
        load_tiger_long_term_config(path)


def test_sma200_uses_only_completed_closes() -> None:
    assert sma200_state(bars(["100"] * 199)) == "INELIGIBLE"
    assert sma200_state(bars(["100"] * 200 + ["101"])) == "LONG"
    assert sma200_state(bars(["100"] * 200 + ["100"])) == "CASH"


def test_allocation_scales_concentrated_risk_group() -> None:
    semiconductor = ["DRAM", "SOXX", "EUV", "TSM"]
    states = {symbol: "LONG" for symbol in [*semiconductor, "QQQ"]}
    groups = {symbol: "semiconductor" for symbol in semiconductor} | {
        "QQQ": "broad"
    }

    weights = allocate_target_weights(states, groups)

    assert sum((weights[symbol] for symbol in semiconductor), Decimal("0")) == Decimal(
        "0.30"
    )
    assert weights["QQQ"] == Decimal("0.10")


def test_allocation_equal_weights_more_than_ten_long_members() -> None:
    states = {f"S{index}": "LONG" for index in range(20)}
    groups = {symbol: symbol for symbol in states}

    weights = allocate_target_weights(states, groups)

    assert set(weights.values()) == {Decimal("0.05")}
    assert sum(weights.values(), Decimal("0")) == Decimal("1")


def test_rebalance_ignores_two_point_drift_but_reports_larger_drift() -> None:
    assert rebalance_reasons(
        {"QQQ": Decimal("0.08")},
        {"QQQ": Decimal("0.10")},
        {"QQQ": "LONG"},
        {"QQQ": "LONG"},
        {"QQQ": "broad"},
    ) == {}
    assert rebalance_reasons(
        {"QQQ": Decimal("0.079")},
        {"QQQ": Decimal("0.10")},
        {"QQQ": "LONG"},
        {"QQQ": "LONG"},
        {"QQQ": "broad"},
    ) == {"QQQ": "drift"}


def test_rebalance_prioritizes_state_and_hard_caps() -> None:
    assert rebalance_reasons(
        {"QQQ": Decimal("0.10")},
        {"QQQ": Decimal("0")},
        {"QQQ": "LONG"},
        {"QQQ": "CASH"},
        {"QQQ": "broad"},
    ) == {"QQQ": "state_change"}
    assert rebalance_reasons(
        {"QQQ": Decimal("0.101")},
        {"QQQ": Decimal("0.10")},
        {"QQQ": "LONG"},
        {"QQQ": "LONG"},
        {"QQQ": "broad"},
    ) == {"QQQ": "symbol_cap"}
    assert rebalance_reasons(
        {symbol: Decimal("0.08") for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {symbol: Decimal("0.075") for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {symbol: "LONG" for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {symbol: "LONG" for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {
            symbol: "semiconductor"
            for symbol in ["DRAM", "SOXX", "EUV", "TSM"]
        },
    ) == {
        symbol: "risk_group_cap"
        for symbol in ["DRAM", "SOXX", "EUV", "TSM"]
    }


class FakeTigerStrategyPriceProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.rehab_calls: list[str] = []

    def get_daily_kline(
        self,
        futu_symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[DailyKlineBar]:
        self.calls.append((futu_symbol, start, end))
        current = date(2020, 1, 6)
        final = date(2026, 1, 6)
        rows: list[DailyKlineBar] = []
        index = 0
        while current <= final:
            if current.weekday() < 5:
                close = 100 + index * 0.02
                rows.append(DailyKlineBar(
                    date=current.isoformat(),
                    open=close - 0.01,
                    high=close + 1,
                    low=close - 1,
                    close=close,
                    volume=1_000_000,
                ))
                index += 1
            current += timedelta(days=1)
        return rows

    def get_rehab_rows(self, futu_symbol: str) -> list[dict[str, str]]:
        self.rehab_calls.append(futu_symbol)
        return [{"symbol": futu_symbol, "time": "2025-12-15", "dividend": "0.5"}]


def _write_strategy_fixture(data_dir: Path, *, include_account_total: bool = True) -> Path:
    config_path = data_dir.parent / "tiger_strategy.json"
    config_path.write_text(
        '{"strategy_id":"tiger_sma200_equal_weight/v1",'
        '"account_alias":"tiger_5683",'
        '"members":{"QQQ":"broad_us_growth"}}',
        encoding="utf-8",
    )
    run_dir = data_dir / "runs" / "2026-01-06"
    run_dir.mkdir(parents=True)
    cash_records = [{
        "record_type": "account_total",
        "account_alias": "other_account",
        "currency": "USD",
        "account_total": "99999999",
    }]
    if include_account_total:
        cash_records.append({
            "record_type": "account_total",
            "account_alias": "tiger_5683",
            "currency": "USD",
            "account_total": "1000",
        })
    (run_dir / "tiger_account_snapshot.json").write_text(json.dumps({
        "accounts": [],
        "cash_records": cash_records,
        "position_records": [
            {
                "account_alias": "tiger_5683",
                "market": "US",
                "symbol": "QQQ",
                "market_value": "100",
            },
            {
                "account_alias": "other_account",
                "market": "US",
                "symbol": "QQQ",
                "market_value": "99999999",
            },
        ],
    }), encoding="utf-8")
    latest = data_dir / "latest"
    latest.mkdir(parents=True)
    (latest / "portfolio.csv").write_text(
        "broker,account_alias,symbol,market_value\n"
        "futu,futu_main,QQQ,88888888\n"
        "phillips,phillips_main,QQQ,77777777\n",
        encoding="utf-8",
    )
    rates = data_dir / "rates"
    rates.mkdir()
    (rates / "DGS3MO.csv").write_text(
        "DATE,DGS3MO\n2020-01-01,4.00\n2026-01-07,4.00\n",
        encoding="utf-8",
    )
    return config_path


def _copy_snapshot_to_next_day(data_dir: Path) -> None:
    source = data_dir / "runs" / "2026-01-06" / "tiger_account_snapshot.json"
    target = data_dir / "runs" / "2026-01-07" / "tiger_account_snapshot.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())


def test_generate_tiger_strategy_uses_only_tiger_snapshot_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    config_path = _write_strategy_fixture(data_dir)
    provider = FakeTigerStrategyPriceProvider()

    result = generate_tiger_long_term_strategy(
        "2026-01-06",
        data_dir,
        config_path,
        provider,
        update_latest=True,
    )

    assert result.status == "shadow"
    assert result.member_count == result.eligible_count == 1
    assert result.latest_path == data_dir / "latest" / "US" / "tiger_long_term_strategy.json"
    payload = load_tiger_long_term_strategy(result.run_path)
    assert payload["account_alias"] == "tiger_5683"
    assert payload["nav"] == "1000"
    assert payload["members"][0]["actual_weight"] == "0.1"
    assert payload["gate"]["reasons"][-1] == "calibration_required"
    assert payload["order_requests"] == []
    assert result.latest_path.read_bytes() == result.run_path.read_bytes()
    assert set(provider.rehab_calls) == {"US.QQQ", "US.SPY"}


def test_generate_tiger_strategy_failure_keeps_previous_latest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_path = _write_strategy_fixture(data_dir, include_account_total=False)
    latest_path = data_dir / "latest" / "US" / "tiger_long_term_strategy.json"
    latest_path.parent.mkdir(parents=True)
    latest_path.write_text('{"sentinel":true}', encoding="utf-8")

    result = generate_tiger_long_term_strategy(
        "2026-01-06",
        data_dir,
        config_path,
        FakeTigerStrategyPriceProvider(),
        update_latest=True,
    )

    assert result.status == "failed"
    assert latest_path.read_text(encoding="utf-8") == '{"sentinel":true}'
    failure = json.loads(result.run_path.read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert "account_total" in failure["error"]


def test_generate_tiger_strategy_reuses_same_month_matching_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    config_path = _write_strategy_fixture(data_dir)
    provider = FakeTigerStrategyPriceProvider()
    first = generate_tiger_long_term_strategy(
        "2026-01-06", data_dir, config_path, provider, update_latest=True,
    )
    assert first.status == "shadow"
    _copy_snapshot_to_next_day(data_dir)

    monkeypatch.setattr(
        "open_trader.tiger_long_term_backtest.run_tiger_long_term_backtest",
        lambda *args, **kwargs: pytest.fail("matching validation must be reused"),
    )
    second = generate_tiger_long_term_strategy(
        "2026-01-07", data_dir, config_path, provider, update_latest=True,
    )

    payload = load_tiger_long_term_strategy(second.run_path)
    assert payload["validation_reused"] is True
    assert payload["run_date"] == "2026-01-07"


def test_generate_tiger_strategy_config_change_forces_new_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_trader.tiger_long_term_backtest as backtest_module

    data_dir = tmp_path / "data"
    config_path = _write_strategy_fixture(data_dir)
    provider = FakeTigerStrategyPriceProvider()
    first = generate_tiger_long_term_strategy(
        "2026-01-06", data_dir, config_path, provider, update_latest=True,
    )
    assert first.status == "shadow"
    _copy_snapshot_to_next_day(data_dir)
    config_path.write_text(
        '{"strategy_id":"tiger_sma200_equal_weight/v1",'
        '"account_alias":"tiger_5683",'
        '"members":{"QQQ":"changed_group"}}',
        encoding="utf-8",
    )
    real_runner = backtest_module.run_tiger_long_term_backtest
    calls = 0

    def counting_runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_runner(*args, **kwargs)

    monkeypatch.setattr(backtest_module, "run_tiger_long_term_backtest", counting_runner)
    second = generate_tiger_long_term_strategy(
        "2026-01-07", data_dir, config_path, provider, update_latest=True,
    )

    payload = load_tiger_long_term_strategy(second.run_path)
    assert payload["validation_reused"] is False
    assert calls == 1
