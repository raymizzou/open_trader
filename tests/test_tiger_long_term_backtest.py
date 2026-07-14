from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from open_trader.tiger_long_term_backtest import (
    TigerUsFeeModel,
    build_validation_gate,
    cash_growth,
    ensure_dgs3mo_rates,
    load_dgs3mo_csv,
    run_tiger_long_term_backtest,
)
from open_trader.standard_strategies import StrategyBar


def test_tiger_fee_model_applies_minimums_and_sell_fees() -> None:
    model = TigerUsFeeModel()

    assert model.fee("BUY", Decimal("1"), Decimal("100")) == Decimal("1.99")
    assert model.fee("SELL", Decimal("1"), Decimal("100")) == Decimal("2.01")
    assert model.fee("BUY", Decimal("0.5"), Decimal("100")) == Decimal("0.50")


def test_tiger_fee_model_applies_per_share_fees_for_larger_order() -> None:
    model = TigerUsFeeModel()

    assert model.fee("BUY", Decimal("100"), Decimal("100")) == Decimal("2.29")


@pytest.mark.parametrize(
    ("side", "quantity", "price"),
    [
        ("HOLD", Decimal("1"), Decimal("100")),
        ("BUY", Decimal("0"), Decimal("100")),
        ("BUY", Decimal("1"), Decimal("0")),
    ],
)
def test_tiger_fee_model_rejects_invalid_orders(
    side: str,
    quantity: Decimal,
    price: Decimal,
) -> None:
    with pytest.raises(ValueError):
        TigerUsFeeModel().fee(side, quantity, price)


def test_dgs3mo_loader_skips_missing_observations(tmp_path: Path) -> None:
    path = tmp_path / "rates.csv"
    path.write_text(
        "DATE,DGS3MO\n2026-01-02,4.00\n2026-01-05,.\n2026-01-06,3.90\n",
        encoding="utf-8",
    )

    rates = load_dgs3mo_csv(path)

    assert rates == {
        date(2026, 1, 2): Decimal("4.00"),
        date(2026, 1, 6): Decimal("3.90"),
    }
    assert cash_growth(Decimal("4"), 365) == Decimal("0.04")


@pytest.mark.parametrize(
    "csv_text",
    [
        "DATE,DGS3MO\n2026-01-02,-0.01\n",
        "DATE,DGS3MO\n2026-01-02,NaN\n",
        "DATE,DGS3MO\n2026-01-02,4.0\n2026-01-02,4.1\n",
    ],
)
def test_dgs3mo_loader_rejects_invalid_series(tmp_path: Path, csv_text: str) -> None:
    path = tmp_path / "rates.csv"
    path.write_text(csv_text, encoding="utf-8")

    with pytest.raises(ValueError):
        load_dgs3mo_csv(path)


def test_cash_growth_rejects_negative_rate_or_days() -> None:
    with pytest.raises(ValueError):
        cash_growth(Decimal("-0.01"), 1)
    with pytest.raises(ValueError):
        cash_growth(Decimal("4"), -1)


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_ensure_dgs3mo_rates_downloads_atomically_and_returns_hash(
    tmp_path: Path,
) -> None:
    body = b"DATE,DGS3MO\n2026-01-02,4.00\n2026-01-06,3.90\n"
    requested_urls: list[str] = []

    def opener(url: str) -> FakeHttpResponse:
        requested_urls.append(url)
        return FakeHttpResponse(body)

    rates, digest = ensure_dgs3mo_rates(
        tmp_path,
        date(2026, 1, 6),
        opener=opener,
    )

    rate_path = tmp_path / "rates" / "DGS3MO.csv"
    assert rates[date(2026, 1, 6)] == Decimal("3.90")
    assert digest == hashlib.sha256(body).hexdigest()
    assert rate_path.read_bytes() == body
    assert requested_urls and "id=DGS3MO" in requested_urls[0]
    assert not list(rate_path.parent.glob("*.tmp"))

    cached_rates, cached_digest = ensure_dgs3mo_rates(
        tmp_path,
        date(2026, 1, 6),
        opener=lambda url: pytest.fail("fresh cache must not download"),
    )
    assert cached_rates == rates
    assert cached_digest == digest


def test_ensure_dgs3mo_rates_rejects_empty_download(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        ensure_dgs3mo_rates(
            tmp_path,
            date(2026, 1, 6),
            opener=lambda url: FakeHttpResponse(b""),
        )


def _six_year_bars(*, crossing: bool) -> list[StrategyBar]:
    current = date(2020, 1, 1)
    end = date(2025, 12, 31)
    index = 0
    bars: list[StrategyBar] = []
    while current <= end:
        if current.weekday() < 5:
            if not crossing:
                close = Decimal("100") + Decimal(index) * Decimal("0.05")
            elif index < 500:
                close = Decimal("100") + Decimal(index) * Decimal("0.10")
            elif index < 800:
                close = Decimal("150") - Decimal(index - 500) * Decimal("0.20")
            else:
                close = Decimal("90") + Decimal(index - 800) * Decimal("0.20")
            open_price = close - Decimal("0.02")
            bars.append(StrategyBar(
                date=current,
                open=open_price,
                high=close + Decimal("1"),
                low=open_price - Decimal("1"),
                close=close,
                volume=Decimal("1000000"),
            ))
            index += 1
        current += timedelta(days=1)
    return bars


def test_portfolio_backtest_uses_next_open_and_respects_caps() -> None:
    result = run_tiger_long_term_backtest(
        bars_by_symbol={
            "QQQ": _six_year_bars(crossing=False),
            "SOXX": _six_year_bars(crossing=True),
        },
        risk_groups={"QQQ": "broad", "SOXX": "semiconductor"},
        rates={date(2019, 12, 31): Decimal("4.0")},
        initial_cash=Decimal("100000"),
    )

    first_order = result["strategy"]["orders"][0]
    assert first_order["decision_date"] < first_order["execution_date"]
    assert max(
        Decimal(row["target_weight"])
        for row in result["strategy"]["member_weights"]
    ) <= Decimal("0.10")
    assert any(
        order["reason"] == "symbol_cap"
        for order in result["strategy"]["orders"]
    )
    assert Decimal(result["strategy"]["cash_interest"]) > 0
    assert any(
        order["reason"] == "sma200_exit"
        for order in result["strategy"]["orders"]
    )
    assert all(
        order["reason"] != "sma200_exit"
        for order in result["benchmark"]["orders"]
    )


def test_portfolio_backtest_emits_metrics_and_ten_segments() -> None:
    result = run_tiger_long_term_backtest(
        bars_by_symbol={"QQQ": _six_year_bars(crossing=False)},
        risk_groups={"QQQ": "broad"},
        rates={date(2019, 12, 31): Decimal("4.0")},
        initial_cash=Decimal("100000"),
    )

    for portfolio in (result["strategy"], result["benchmark"]):
        assert len(portfolio["segments"]) == 10
        assert Decimal(portfolio["annualized_return_pct"]).is_finite()
        assert Decimal(portfolio["max_drawdown_pct"]) >= 0
        assert "sharpe_ratio" in portfolio
        assert "calmar_ratio" in portfolio


def test_gate_requires_risk_adjusted_floors_and_calibration() -> None:
    gate = build_validation_gate(
        strategy={
            "sharpe_ratio": "1.1",
            "calmar_ratio": "1.0",
            "annualized_return_pct": "7",
            "max_drawdown_pct": "8",
        },
        benchmark={
            "sharpe_ratio": "0.9",
            "calmar_ratio": "0.8",
            "annualized_return_pct": "8",
            "max_drawdown_pct": "10",
        },
        cash_annualized_return_pct=Decimal("4"),
        provenance_ok=True,
    )

    assert gate == {
        "passed": False,
        "policy_id": "tiger_risk_adjusted/v1",
        "reasons": ["calibration_required"],
    }


def test_gate_reports_every_fixed_failure() -> None:
    gate = build_validation_gate(
        strategy={
            "sharpe_ratio": "0.7",
            "calmar_ratio": "0.6",
            "annualized_return_pct": "3",
            "max_drawdown_pct": "12",
        },
        benchmark={
            "sharpe_ratio": "0.9",
            "calmar_ratio": "0.8",
            "annualized_return_pct": "8",
            "max_drawdown_pct": "10",
        },
        cash_annualized_return_pct=Decimal("4"),
        provenance_ok=False,
    )

    assert gate["reasons"] == [
        "sharpe_below_floor",
        "sharpe_below_benchmark",
        "calmar_below_floor",
        "calmar_below_benchmark",
        "return_below_cash",
        "drawdown_above_benchmark",
        "provenance_incomplete",
        "calibration_required",
    ]
