from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .models import (
    ChangeClassification,
    PortfolioInputRow,
    PremarketAction,
    TradingAdvice,
)
from .portfolio_loader import load_eligible_portfolio_rows
from .report import write_premarket_outputs
from .store import (
    load_latest_advice_by_symbol,
    write_change_classifications,
    write_trading_advice,
)


class AdviceRunner(Protocol):
    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        pass


class Classifier(Protocol):
    def classify(
        self,
        *,
        run_date: str,
        portfolio_row: PortfolioInputRow,
        previous_advice: dict[str, str] | None,
        latest_advice: TradingAdvice,
    ) -> ChangeClassification:
        pass


@dataclass(frozen=True)
class PremarketResult:
    eligible_count: int
    advice_count: int
    action_count: int
    advice_path: Path
    classifications_path: Path
    actions_path: Path
    report_path: Path


def run_premarket(
    *,
    run_date: str,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    advice_runner: AdviceRunner,
    classifier: Classifier,
    symbols: set[str] | None,
    update_latest: bool,
) -> PremarketResult:
    rows = load_eligible_portfolio_rows(portfolio_path)
    if symbols is not None:
        normalized_symbols = {symbol.casefold() for symbol in symbols}
        rows = [
            row
            for row in rows
            if row.symbol.casefold() in normalized_symbols
            or row.analysis_symbol.casefold() in normalized_symbols
        ]

    previous_by_symbol = load_latest_advice_by_symbol(data_dir)
    advice_records: list[TradingAdvice] = []
    classifications: list[ChangeClassification] = []
    actions: list[PremarketAction] = []

    for row in rows:
        advice = _analyze_symbol(
            advice_runner=advice_runner,
            row=row,
            run_date=run_date,
        )
        advice_records.append(advice)

        classification = _classify_symbol(
            classifier=classifier,
            run_date=run_date,
            row=row,
            previous_advice=previous_by_symbol.get(row.symbol),
            latest_advice=advice,
        )
        classifications.append(classification)
        if classification.status == "ok" and classification.include_in_report:
            actions.append(PremarketAction.from_classification(row, classification))

    advice_path, _ = write_trading_advice(
        run_date=run_date,
        records=advice_records,
        data_dir=data_dir,
        update_latest=False,
    )
    classifications_path = write_change_classifications(
        run_date=run_date,
        records=classifications,
        data_dir=data_dir,
    )
    actions_path, _, report_path = write_premarket_outputs(
        run_date=run_date,
        actions=actions,
        data_dir=data_dir,
        reports_dir=reports_dir,
    )
    if update_latest:
        _promote_latest_advice(advice_path=advice_path, data_dir=data_dir)

    return PremarketResult(
        eligible_count=len(rows),
        advice_count=len(advice_records),
        action_count=len(actions),
        advice_path=advice_path,
        classifications_path=classifications_path,
        actions_path=actions_path,
        report_path=report_path,
    )


def _analyze_symbol(
    *,
    advice_runner: AdviceRunner,
    row: PortfolioInputRow,
    run_date: str,
) -> TradingAdvice:
    try:
        return advice_runner.analyze(row, run_date)
    except Exception as exc:
        return TradingAdvice(
            run_date=run_date,
            symbol=row.symbol,
            market=row.market,
            asset_class=row.asset_class,
            portfolio_weight_hkd=row.portfolio_weight_hkd,
            risk_flag=row.risk_flag,
            source="",
            advice_action="",
            advice_summary="",
            raw_decision="",
            status="error",
            error=str(exc),
        )


def _promote_latest_advice(*, advice_path: Path, data_dir: Path) -> None:
    latest_path = data_dir / "latest" / "trading_advice.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            dir=latest_path.parent,
            prefix=f".{latest_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            with advice_path.open("rb") as source:
                shutil.copyfileobj(source, handle)
        temp_path.replace(latest_path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            _best_effort_unlink(temp_path)
        raise


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


def _classify_symbol(
    *,
    classifier: Classifier,
    run_date: str,
    row: PortfolioInputRow,
    previous_advice: dict[str, str] | None,
    latest_advice: TradingAdvice,
) -> ChangeClassification:
    try:
        return classifier.classify(
            run_date=run_date,
            portfolio_row=row,
            previous_advice=previous_advice,
            latest_advice=latest_advice,
        )
    except Exception as exc:
        return ChangeClassification(
            run_date=run_date,
            symbol=row.symbol,
            include_in_report=False,
            change_type="no_material_change",
            severity="low",
            suggested_action="",
            summary="",
            rationale="",
            watch_trigger="",
            status="error",
            error=str(exc),
        )
