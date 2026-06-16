from __future__ import annotations

import csv
from pathlib import Path

import pytest

from open_trader.advice.models import ChangeClassification, TradingAdvice
from open_trader.advice.store import (
    load_latest_advice_by_symbol,
    write_change_classifications,
    write_trading_advice,
)


def advice(symbol: str, action: str = "hold") -> TradingAdvice:
    return TradingAdvice(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        asset_class="etf",
        portfolio_weight_hkd="3.05%",
        risk_flag="normal",
        source="tradingagents",
        advice_action=action,
        advice_summary=f"{symbol} {action}",
        raw_decision='{"action":"hold"}',
        status="ok",
        error="",
    )


def classification(symbol: str) -> ChangeClassification:
    return ChangeClassification(
        run_date="2026-06-16",
        symbol=symbol,
        include_in_report=True,
        change_type="new_signal",
        severity="medium",
        suggested_action="watch",
        summary=f"{symbol} watch",
        rationale="New symbol in advice store.",
        watch_trigger="",
        status="ok",
        error="",
    )


def test_write_trading_advice_writes_run_and_latest_files(tmp_path: Path) -> None:
    run_path, latest_path = write_trading_advice(
        run_date="2026-06-16",
        records=[advice("VIXY"), advice("QQQ")],
        data_dir=tmp_path / "data",
        update_latest=True,
    )

    assert run_path == tmp_path / "data" / "runs" / "2026-06-16" / "trading_advice.csv"
    assert latest_path == tmp_path / "data" / "latest" / "trading_advice.csv"
    assert run_path.read_text(encoding="utf-8") == latest_path.read_text(encoding="utf-8")

    rows = list(csv.DictReader(run_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in rows] == ["VIXY", "QQQ"]


def test_write_trading_advice_dry_run_does_not_update_latest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_trading_advice(
        run_date="2026-06-15",
        records=[advice("OLD")],
        data_dir=data_dir,
        update_latest=True,
    )
    original_latest = (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    )

    write_trading_advice(
        run_date="2026-06-16",
        records=[advice("NEW")],
        data_dir=data_dir,
        update_latest=False,
    )

    assert (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    ) == original_latest


def test_load_latest_advice_by_symbol_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load_latest_advice_by_symbol(tmp_path / "data") == {}


def test_load_latest_advice_by_symbol_indexes_existing_latest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_trading_advice(
        run_date="2026-06-16",
        records=[advice("VIXY", "reduce")],
        data_dir=data_dir,
        update_latest=True,
    )

    latest = load_latest_advice_by_symbol(data_dir)

    assert latest["VIXY"]["advice_action"] == "reduce"


def test_load_latest_advice_accepts_legacy_rows_without_fallback_columns(
    tmp_path: Path,
) -> None:
    latest = tmp_path / "data/latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "run_date",
                "symbol",
                "market",
                "asset_class",
                "portfolio_weight_hkd",
                "risk_flag",
                "source",
                "advice_action",
                "advice_summary",
                "raw_decision",
                "status",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "MSFT",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.13%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": "评级：Overweight",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            }
        )

    rows = load_latest_advice_by_symbol(tmp_path / "data")

    assert rows["MSFT"]["source_status"] == "ok"
    assert rows["MSFT"]["fallback_reason"] == ""
    assert rows["MSFT"]["fallback_from_date"] == ""


def test_write_trading_advice_writes_fallback_columns(tmp_path: Path) -> None:
    run_path, _ = write_trading_advice(
        run_date="2026-06-17",
        data_dir=tmp_path / "data",
        update_latest=False,
        records=[
            TradingAdvice(
                run_date="2026-06-17",
                symbol="MSFT",
                market="US",
                asset_class="stock",
                portfolio_weight_hkd="1.13%",
                risk_flag="normal",
                source="tradingagents",
                advice_action="Overweight",
                advice_summary="评级：Overweight",
                raw_decision="{}",
                status="fallback",
                error="",
                source_status="fallback",
                fallback_reason="daily deadline exceeded",
                fallback_from_date="2026-06-16",
            )
        ],
    )

    rows = list(csv.DictReader(run_path.open(encoding="utf-8")))

    assert rows[0]["source_status"] == "fallback"
    assert rows[0]["fallback_reason"] == "daily deadline exceeded"
    assert rows[0]["fallback_from_date"] == "2026-06-16"


def test_write_change_classifications_writes_run_file(tmp_path: Path) -> None:
    path = write_change_classifications(
        run_date="2026-06-16",
        records=[classification("VIXY")],
        data_dir=tmp_path / "data",
    )

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["include_in_report"] == "true"


def test_rerun_trading_advice_write_failure_preserves_previous_run_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    run_path, _ = write_trading_advice(
        run_date="2026-06-16",
        records=[advice("OLD")],
        data_dir=data_dir,
        update_latest=False,
    )
    original_run = run_path.read_text(encoding="utf-8")
    fail_csv_row_for_symbol(monkeypatch, "VIXY")

    with pytest.raises(OSError, match="simulated csv write failure"):
        write_trading_advice(
            run_date="2026-06-16",
            records=[advice("VIXY")],
            data_dir=data_dir,
            update_latest=False,
        )

    assert run_path.read_text(encoding="utf-8") == original_run
    assert list(run_path.parent.glob(".trading_advice.csv.*.tmp")) == []


def test_change_classification_write_failure_preserves_previous_run_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    path = write_change_classifications(
        run_date="2026-06-16",
        records=[classification("OLD")],
        data_dir=data_dir,
    )
    original_run = path.read_text(encoding="utf-8")
    fail_csv_row_for_symbol(monkeypatch, "VIXY")

    with pytest.raises(OSError, match="simulated csv write failure"):
        write_change_classifications(
            run_date="2026-06-16",
            records=[classification("VIXY")],
            data_dir=data_dir,
        )

    assert path.read_text(encoding="utf-8") == original_run
    assert list(path.parent.glob(".change_classifications.csv.*.tmp")) == []


def test_atomic_latest_write_cleans_temp_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    fail_csv_row_for_symbol(monkeypatch, "VIXY")

    with pytest.raises(OSError, match="simulated csv write failure"):
        write_trading_advice(
            run_date="2026-06-16",
            records=[advice("VIXY")],
            data_dir=data_dir,
            update_latest=True,
        )

    latest_dir = data_dir / "latest"
    assert not (latest_dir / "trading_advice.csv").exists()
    assert list(latest_dir.glob(".trading_advice.csv.*.tmp")) == []


def fail_csv_row_for_symbol(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
) -> None:
    original_writerow = csv.DictWriter.writerow

    def fail_after_header(self: csv.DictWriter, rowdict: dict[str, str]) -> object:
        if rowdict.get("symbol") == symbol:
            raise OSError("simulated csv write failure")
        return original_writerow(self, rowdict)

    monkeypatch.setattr(csv.DictWriter, "writerow", fail_after_header)
