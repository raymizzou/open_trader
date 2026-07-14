from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import urlopen


CENT = Decimal("0.01")


class TigerUsFeeModel:
    """Tiger's published US-stock fees as captured on 2026-07-14."""

    commission_per_share = Decimal("0.0049")
    commission_minimum = Decimal("0.99")
    commission_notional_cap = Decimal("0.005")
    platform_per_share = Decimal("0.005")
    platform_minimum = Decimal("1")
    platform_notional_cap = Decimal("0.005")
    settlement_per_share = Decimal("0.003")
    settlement_notional_cap = Decimal("0.07")
    sec_sell_rate = Decimal("0.0000206")
    finra_sell_per_share = Decimal("0.000195")
    finra_minimum = Decimal("0.01")
    finra_maximum = Decimal("9.79")

    def fee(self, side: str, quantity: Decimal, price: Decimal) -> Decimal:
        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("Tiger fee side must be BUY or SELL")
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            raise ValueError("Tiger fee quantity and price must be positive and finite")
        trade_value = quantity * price
        if quantity < 1:
            return min(trade_value * Decimal("0.01"), Decimal("1")).quantize(
                CENT,
                rounding=ROUND_HALF_UP,
            )

        commission = max(
            min(quantity * self.commission_per_share, trade_value * self.commission_notional_cap),
            self.commission_minimum,
        )
        platform = max(
            min(quantity * self.platform_per_share, trade_value * self.platform_notional_cap),
            self.platform_minimum,
        )
        settlement = min(
            quantity * self.settlement_per_share,
            trade_value * self.settlement_notional_cap,
        )
        total = sum(
            (self._cent(commission), self._cent(platform), self._cent(settlement)),
            Decimal("0"),
        )
        if normalized_side == "SELL":
            sec = max(trade_value * self.sec_sell_rate, CENT)
            finra = min(
                max(quantity * self.finra_sell_per_share, self.finra_minimum),
                self.finra_maximum,
            )
            total += self._cent(sec) + self._cent(finra)
        return total

    @staticmethod
    def _cent(value: Decimal) -> Decimal:
        return value.quantize(CENT, rounding=ROUND_HALF_UP)


def load_dgs3mo_csv(path: Path) -> dict[date, Decimal]:
    rates: dict[date, Decimal] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or not {"DATE", "DGS3MO"}.issubset(reader.fieldnames):
                raise ValueError("DGS3MO CSV must contain DATE and DGS3MO")
            for row in reader:
                raw_date = str(row.get("DATE") or "").strip()
                raw_rate = str(row.get("DGS3MO") or "").strip()
                if raw_rate == ".":
                    continue
                try:
                    observation_date = date.fromisoformat(raw_date)
                    rate = Decimal(raw_rate)
                except (ValueError, InvalidOperation) as exc:
                    raise ValueError("DGS3MO CSV contains an invalid observation") from exc
                if observation_date in rates:
                    raise ValueError("DGS3MO CSV contains a duplicate date")
                if not rate.is_finite() or rate < 0:
                    raise ValueError("DGS3MO rate must be finite and non-negative")
                rates[observation_date] = rate
    except OSError as exc:
        raise ValueError(f"cannot read DGS3MO CSV: {path}") from exc
    if not rates:
        raise ValueError("DGS3MO series has no valid observations")
    return dict(sorted(rates.items()))


def cash_growth(rate: Decimal, calendar_days: int) -> Decimal:
    if not rate.is_finite() or rate < 0 or calendar_days < 0:
        raise ValueError("cash rate and calendar days must be non-negative")
    return (Decimal("1") + rate / Decimal("100")) ** (
        Decimal(calendar_days) / Decimal("365")
    ) - Decimal("1")


def ensure_dgs3mo_rates(
    data_dir: Path,
    end_date: date,
    *,
    opener: Callable[[str], Any] = urlopen,
) -> tuple[dict[date, Decimal], str]:
    path = data_dir / "rates" / "DGS3MO.csv"
    if path.exists() and _last_csv_date(path) >= end_date:
        return load_dgs3mo_csv(path), hashlib.sha256(path.read_bytes()).hexdigest()

    query = urlencode({
        "id": "DGS3MO",
        "cosd": "1962-01-02",
        "coed": end_date.isoformat(),
    })
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?{query}"
    try:
        with opener(url) as response:
            body = response.read()
    except OSError as exc:
        raise ValueError("DGS3MO download failed") from exc
    if not body:
        raise ValueError("DGS3MO download was empty")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_bytes(body)
        rates = load_dgs3mo_csv(temp_path)
        if _last_csv_date(temp_path) < end_date:
            raise ValueError("DGS3MO download does not reach the requested end date")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return rates, hashlib.sha256(body).hexdigest()


def _last_csv_date(path: Path) -> date:
    latest: date | None = None
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    observation_date = date.fromisoformat(str(row.get("DATE") or "").strip())
                except ValueError:
                    continue
                latest = observation_date if latest is None else max(latest, observation_date)
    except OSError as exc:
        raise ValueError(f"cannot read DGS3MO CSV: {path}") from exc
    return latest or date.min
