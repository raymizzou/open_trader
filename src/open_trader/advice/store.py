from __future__ import annotations

import csv
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from open_trader.csv_io import write_rows

from .models import (
    CHANGE_CLASSIFICATION_FIELDNAMES,
    TRADING_ADVICE_FIELDNAMES,
    ChangeClassification,
    TradingAdvice,
)


def write_trading_advice(
    *,
    run_date: str,
    records: Iterable[TradingAdvice],
    data_dir: Path,
    update_latest: bool,
) -> tuple[Path, Path]:
    rows = [record.to_row() for record in records]
    run_path = data_dir / "runs" / run_date / "trading_advice.csv"
    latest_path = data_dir / "latest" / "trading_advice.csv"

    write_rows(run_path, TRADING_ADVICE_FIELDNAMES, rows)
    if update_latest:
        _atomic_write_latest(latest_path, TRADING_ADVICE_FIELDNAMES, rows)

    return run_path, latest_path


def write_change_classifications(
    *,
    run_date: str,
    records: Iterable[ChangeClassification],
    data_dir: Path,
) -> Path:
    run_path = data_dir / "runs" / run_date / "change_classifications.csv"
    write_rows(
        run_path,
        CHANGE_CLASSIFICATION_FIELDNAMES,
        (record.to_row() for record in records),
    )
    return run_path


def load_latest_advice_by_symbol(data_dir: Path) -> dict[str, dict[str, str]]:
    latest_path = data_dir / "latest" / "trading_advice.csv"
    if not latest_path.exists():
        return {}

    with latest_path.open(encoding="utf-8", newline="") as handle:
        return {
            row["symbol"]: row
            for row in csv.DictReader(handle)
            if row.get("symbol")
        }


def _atomic_write_latest(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
