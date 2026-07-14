from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from open_trader.tiger_long_term_backtest import (
    TigerUsFeeModel,
    cash_growth,
    ensure_dgs3mo_rates,
    load_dgs3mo_csv,
)


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
