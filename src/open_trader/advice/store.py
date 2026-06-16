from __future__ import annotations

import csv
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Mapping

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

    _atomic_write_csv(run_path, TRADING_ADVICE_FIELDNAMES, rows)
    if update_latest:
        _atomic_write_csv(latest_path, TRADING_ADVICE_FIELDNAMES, rows)

    return run_path, latest_path


def write_change_classifications(
    *,
    run_date: str,
    records: Iterable[ChangeClassification],
    data_dir: Path,
) -> Path:
    run_path = data_dir / "runs" / run_date / "change_classifications.csv"
    _atomic_write_csv(
        run_path,
        CHANGE_CLASSIFICATION_FIELDNAMES,
        (record.to_row() for record in records),
    )
    return run_path


def load_latest_advice_by_symbol(data_dir: Path) -> dict[str, dict[str, str]]:
    latest_path = data_dir / "latest" / "trading_advice.csv"
    if not latest_path.exists():
        return {}

    csv.field_size_limit(sys.maxsize)
    with latest_path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            normalized["symbol"]: normalized
            for row in csv.DictReader(handle)
            if row.get("symbol")
            for normalized in [_normalize_advice_row(row)]
        }


def _normalize_advice_row(row: dict[str, str]) -> dict[str, str]:
    normalized = {field: row.get(field, "") for field in TRADING_ADVICE_FIELDNAMES}
    if not normalized["source_status"]:
        normalized["source_status"] = normalized["status"] or "ok"
    return normalized


def _atomic_write_csv(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[Mapping[str, object]],
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
            for row in rows:
                writer.writerow(
                    {
                        key: "" if row.get(key) is None else row.get(key)
                        for key in fieldnames
                    }
                )
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            _best_effort_unlink(temp_path)
        raise


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass
