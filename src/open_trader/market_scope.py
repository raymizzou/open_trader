from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class MarketScope(StrEnum):
    HK = "HK"
    US = "US"
    CN = "CN"


def parse_market_scope(value: str) -> MarketScope:
    normalized = value.strip().upper()
    try:
        return MarketScope(normalized)
    except ValueError as exc:
        raise ValueError("market must be one of: HK, US, CN") from exc


def market_run_dir(data_dir: Path, run_date: str, market: MarketScope) -> Path:
    return data_dir / "runs" / run_date / market.value


def market_scoped_latest_dir(data_dir: Path, market: MarketScope) -> Path:
    return data_dir / "latest" / market.value


def market_scoped_latest_path(data_dir: Path, market: MarketScope, name: str) -> Path:
    return market_scoped_latest_dir(data_dir, market) / name


def market_report_path(
    reports_dir: Path,
    section: str,
    run_date: str,
    market: MarketScope,
) -> Path:
    return reports_dir / section / f"{run_date}-{market.value}.md"
