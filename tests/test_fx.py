from decimal import Decimal

import pytest

from open_trader.fx import StaticMonthEndFxProvider, convert_to_hkd, month_end_date


def test_month_end_date_handles_leap_year_february():
    assert month_end_date("2024-02") == "2024-02-29"


def test_static_fx_provider_returns_hkd_for_hkd():
    provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.84")})

    quote = provider.get_rate_to_hkd("HKD")

    assert quote.fx_date == "2026-05-31"
    assert quote.rate == Decimal("1")
    assert quote.source == "external_month_end_static"


def test_static_fx_provider_returns_configured_rate():
    provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.84")})

    quote = provider.get_rate_to_hkd("usd")

    assert quote.currency == "USD"
    assert quote.rate == Decimal("7.84")


def test_static_fx_provider_rejects_missing_currency():
    provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.84")})

    with pytest.raises(KeyError, match="Missing HKD FX rate for EUR"):
        provider.get_rate_to_hkd("EUR")


def test_convert_to_hkd_multiplies_amount_by_fx_rate():
    provider = StaticMonthEndFxProvider("2026-05", {"USD": Decimal("7.84")})

    assert convert_to_hkd(Decimal("10"), "usd", provider) == Decimal("78.40")
