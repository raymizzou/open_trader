from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Protocol

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


AdviceRunnerFactory = Callable[[], AdviceRunner]
DEFAULT_EXCLUDED_SYMBOLS = {"AGRZ", "ARGG"}


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


@dataclass(frozen=True)
class _SymbolResult:
    index: int
    row: PortfolioInputRow
    advice: TradingAdvice
    classification: ChangeClassification


@dataclass
class _LatestPromotion:
    source_path: Path
    latest_path: Path
    temp_path: Path | None = None
    backup_path: Path | None = None
    latest_replaced: bool = False


def run_premarket(
    *,
    run_date: str,
    portfolio_path: Path,
    data_dir: Path,
    reports_dir: Path,
    advice_runner: AdviceRunner | None,
    classifier: Classifier,
    symbols: set[str] | None,
    update_latest: bool,
    max_workers: int = 1,
    advice_runner_factory: AdviceRunnerFactory | None = None,
    excluded_symbols: set[str] | None = None,
) -> PremarketResult:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    rows = load_eligible_portfolio_rows(portfolio_path)
    normalized_excluded_symbols = {
        symbol.casefold()
        for symbol in (
            DEFAULT_EXCLUDED_SYMBOLS
            if excluded_symbols is None
            else DEFAULT_EXCLUDED_SYMBOLS | excluded_symbols
        )
    }
    if normalized_excluded_symbols:
        rows = [
            row
            for row in rows
            if row.symbol.casefold() not in normalized_excluded_symbols
            and row.analysis_symbol.casefold() not in normalized_excluded_symbols
        ]
    if symbols is not None:
        normalized_symbols = {symbol.casefold() for symbol in symbols}
        rows = [
            row
            for row in rows
            if row.symbol.casefold() in normalized_symbols
            or row.analysis_symbol.casefold() in normalized_symbols
        ]

    if not rows:
        advice_path, _ = write_trading_advice(
            run_date=run_date,
            records=[],
            data_dir=data_dir,
            update_latest=False,
        )
        classifications_path = write_change_classifications(
            run_date=run_date,
            records=[],
            data_dir=data_dir,
        )
        actions_path, _, report_path = write_premarket_outputs(
            run_date=run_date,
            actions=[],
            data_dir=data_dir,
            reports_dir=reports_dir,
            update_latest=False,
            no_eligible=True,
        )
        return PremarketResult(
            eligible_count=0,
            advice_count=0,
            action_count=0,
            advice_path=advice_path,
            classifications_path=classifications_path,
            actions_path=actions_path,
            report_path=report_path,
        )

    previous_by_symbol = load_latest_advice_by_symbol(data_dir)
    symbol_results = _run_symbols(
        rows=rows,
        run_date=run_date,
        advice_runner=advice_runner,
        advice_runner_factory=advice_runner_factory,
        classifier=classifier,
        previous_by_symbol=previous_by_symbol,
        max_workers=max_workers,
    )
    advice_records = [result.advice for result in symbol_results]
    classifications = [result.classification for result in symbol_results]
    actions: list[PremarketAction] = []

    for result in symbol_results:
        classification = result.classification
        if classification.status == "ok" and classification.include_in_report:
            actions.append(PremarketAction.from_classification(result.row, classification))

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
        update_latest=False,
    )
    if update_latest:
        _promote_latest_outputs(
            advice_path=advice_path,
            actions_path=actions_path,
            data_dir=data_dir,
        )

    return PremarketResult(
        eligible_count=len(rows),
        advice_count=len(advice_records),
        action_count=len(actions),
        advice_path=advice_path,
        classifications_path=classifications_path,
        actions_path=actions_path,
        report_path=report_path,
    )


def _run_symbols(
    *,
    rows: list[PortfolioInputRow],
    run_date: str,
    advice_runner: AdviceRunner | None,
    advice_runner_factory: AdviceRunnerFactory | None,
    classifier: Classifier,
    previous_by_symbol: dict[str, dict[str, str]],
    max_workers: int,
) -> list[_SymbolResult]:
    if max_workers == 1:
        return [
            _run_symbol(
                index=index,
                row=row,
                run_date=run_date,
                advice_runner=advice_runner,
                advice_runner_factory=advice_runner_factory,
                classifier=classifier,
                previous_by_symbol=previous_by_symbol,
            )
            for index, row in enumerate(rows)
        ]

    results: list[_SymbolResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _run_symbol,
                index=index,
                row=row,
                run_date=run_date,
                advice_runner=advice_runner,
                advice_runner_factory=advice_runner_factory,
                classifier=classifier,
                previous_by_symbol=previous_by_symbol,
            )
            for index, row in enumerate(rows)
        ]
        for future in as_completed(futures):
            results.append(future.result())

    return sorted(results, key=lambda result: result.index)


def _run_symbol(
    *,
    index: int,
    row: PortfolioInputRow,
    run_date: str,
    advice_runner: AdviceRunner | None,
    advice_runner_factory: AdviceRunnerFactory | None,
    classifier: Classifier,
    previous_by_symbol: dict[str, dict[str, str]],
) -> _SymbolResult:
    runner = advice_runner_factory() if advice_runner_factory is not None else advice_runner
    if runner is None:
        raise ValueError("advice_runner or advice_runner_factory is required")

    advice = _analyze_symbol(
        advice_runner=runner,
        row=row,
        run_date=run_date,
    )
    classification = _classify_symbol(
        classifier=classifier,
        run_date=run_date,
        row=row,
        previous_advice=previous_by_symbol.get(row.symbol),
        latest_advice=advice,
    )
    return _SymbolResult(
        index=index,
        row=row,
        advice=advice,
        classification=classification,
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


def _promote_latest_outputs(
    *,
    advice_path: Path,
    actions_path: Path,
    data_dir: Path,
) -> None:
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    promotions = [
        _LatestPromotion(
            source_path=advice_path,
            latest_path=latest_dir / "trading_advice.csv",
        ),
        _LatestPromotion(
            source_path=actions_path,
            latest_path=latest_dir / "premarket_actions.csv",
        ),
    ]

    try:
        for promotion in promotions:
            promotion.temp_path = _copy_latest_temp(
                source_path=promotion.source_path,
                latest_path=promotion.latest_path,
            )

        for promotion in promotions:
            if promotion.latest_path.exists():
                promotion.backup_path = _make_backup_latest_path(
                    promotion.latest_path
                )
                promotion.latest_path.rename(promotion.backup_path)
            if promotion.temp_path is None:
                raise RuntimeError("latest promotion temp path was not staged")
            promotion.temp_path.replace(promotion.latest_path)
            promotion.latest_replaced = True
            promotion.temp_path = None
    except Exception:
        _restore_latest_promotions(promotions)
        raise
    else:
        for promotion in promotions:
            if promotion.backup_path is not None and promotion.backup_path.exists():
                _best_effort_unlink(promotion.backup_path)
    finally:
        for promotion in promotions:
            if promotion.temp_path is not None and promotion.temp_path.exists():
                _best_effort_unlink(promotion.temp_path)


def _copy_latest_temp(*, source_path: Path, latest_path: Path) -> Path:
    with NamedTemporaryFile(
        "wb",
        dir=latest_path.parent,
        prefix=f".{latest_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        with source_path.open("rb") as source:
            shutil.copyfileobj(source, handle)
    return temp_path


def _make_backup_latest_path(latest_path: Path) -> Path:
    with NamedTemporaryFile(
        "wb",
        dir=latest_path.parent,
        prefix=f".{latest_path.name}.",
        suffix=".backup",
        delete=False,
    ) as handle:
        backup_path = Path(handle.name)
    backup_path.unlink()
    return backup_path


def _restore_latest_promotions(promotions: list[_LatestPromotion]) -> None:
    for promotion in reversed(promotions):
        if promotion.backup_path is not None and promotion.backup_path.exists():
            if promotion.latest_path.exists():
                _best_effort_unlink(promotion.latest_path)
            try:
                promotion.backup_path.rename(promotion.latest_path)
            except Exception:
                pass
        elif promotion.latest_replaced and promotion.latest_path.exists():
            _best_effort_unlink(promotion.latest_path)


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
