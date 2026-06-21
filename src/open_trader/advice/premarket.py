from __future__ import annotations

import csv
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterable, Mapping, Protocol

from open_trader.market_scope import (
    MarketScope,
    market_run_dir,
    market_scoped_latest_dir,
    market_scoped_latest_path,
    parse_market_scope,
)
from open_trader.technical_facts import (
    LLMTechnicalFactsExtractor,
    TechnicalFactsResult,
    generate_technical_facts,
)

from .models import (
    CHANGE_CLASSIFICATION_FIELDNAMES,
    ChangeClassification,
    PortfolioInputRow,
    PremarketAction,
    TRADING_ADVICE_FIELDNAMES,
    TradingAdvice,
)
from .portfolio_loader import load_eligible_portfolio_rows
from .report import write_premarket_outputs
from .store import load_latest_advice_by_symbol


class AdviceRunner(Protocol):
    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        pass


AdviceRunnerFactory = Callable[[], AdviceRunner]
DeadlineReached = Callable[[], bool]
TechnicalFactsGenerator = Callable[..., TechnicalFactsResult]
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
    advice_runner: AdviceRunner | None = None,
    advice_runner_factory: AdviceRunnerFactory | None = None,
    classifier: Classifier | None = None,
    symbols: set[str] | None = None,
    excluded_symbols: set[str] | None = None,
    update_latest: bool = True,
    max_workers: int = 1,
    use_fallback: bool = False,
    deadline_reached: DeadlineReached | None = None,
    market: str | None = None,
    technical_facts_generator: TechnicalFactsGenerator | None = None,
) -> PremarketResult:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    market_scope = parse_market_scope(market) if market is not None else None
    rows = load_eligible_portfolio_rows(
        portfolio_path,
        market=market_scope.value if market_scope is not None else None,
    )
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
        advice_path = _write_trading_advice_run(
            run_date=run_date,
            records=[],
            data_dir=data_dir,
            market=market_scope,
        )
        classifications_path = _write_change_classifications_run(
            run_date=run_date,
            records=[],
            data_dir=data_dir,
            market=market_scope,
        )
        actions_path, _, report_path = write_premarket_outputs(
            run_date=run_date,
            actions=[],
            data_dir=data_dir,
            reports_dir=reports_dir,
            update_latest=False,
            no_eligible=True,
            market=market_scope,
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

    if classifier is None:
        raise ValueError("classifier is required")

    previous_by_symbol = _load_latest_advice_by_symbol(data_dir, market=market_scope)
    symbol_results = _run_symbols(
        rows=rows,
        run_date=run_date,
        advice_runner=advice_runner,
        advice_runner_factory=advice_runner_factory,
        classifier=classifier,
        previous_by_symbol=previous_by_symbol,
        max_workers=max_workers,
        use_fallback=use_fallback,
        deadline_reached=deadline_reached,
    )
    advice_records = [result.advice for result in symbol_results]
    classifications = [result.classification for result in symbol_results]
    actions: list[PremarketAction] = []

    for result in symbol_results:
        classification = result.classification
        if classification.status == "ok" and classification.include_in_report:
            actions.append(PremarketAction.from_classification(result.row, classification))

    advice_path = _write_trading_advice_run(
        run_date=run_date,
        records=advice_records,
        data_dir=data_dir,
        market=market_scope,
    )
    classifications_path = _write_change_classifications_run(
        run_date=run_date,
        records=classifications,
        data_dir=data_dir,
        market=market_scope,
    )
    actions_path, _, report_path = write_premarket_outputs(
        run_date=run_date,
        actions=actions,
        data_dir=data_dir,
        reports_dir=reports_dir,
        update_latest=False,
        market=market_scope,
    )
    _generate_technical_facts_after_advice(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=update_latest,
        market=market_scope,
        technical_facts_generator=technical_facts_generator,
    )
    if update_latest:
        _promote_latest_outputs(
            advice_path=advice_path,
            actions_path=actions_path,
            data_dir=data_dir,
            market=market_scope,
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


def _generate_technical_facts_after_advice(
    *,
    advice_path: Path,
    data_dir: Path,
    run_date: str,
    update_latest: bool,
    market: MarketScope | None,
    technical_facts_generator: TechnicalFactsGenerator | None,
) -> TechnicalFactsResult:
    generator = technical_facts_generator
    if generator is None:
        extractor = LLMTechnicalFactsExtractor()

        def generator(**kwargs: object) -> TechnicalFactsResult:
            return generate_technical_facts(extractor=extractor, **kwargs)  # type: ignore[arg-type]

    return generator(
        advice_path=advice_path,
        data_dir=data_dir,
        run_date=run_date,
        update_latest=update_latest,
        market=market,
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
    use_fallback: bool,
    deadline_reached: DeadlineReached | None,
) -> list[_SymbolResult]:
    if max_workers == 1:
        results: list[_SymbolResult] = []
        for index, row in enumerate(rows):
            results.append(
                _run_symbol(
                    index=index,
                    row=row,
                    run_date=run_date,
                    advice_runner=advice_runner,
                    advice_runner_factory=advice_runner_factory,
                    classifier=classifier,
                    previous_by_symbol=previous_by_symbol,
                    use_fallback=use_fallback,
                    deadline_reached=deadline_reached,
                )
            )
        return results

    results = []
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
                use_fallback=use_fallback,
                deadline_reached=deadline_reached,
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
    use_fallback: bool,
    deadline_reached: DeadlineReached | None,
) -> _SymbolResult:
    if deadline_reached is not None and deadline_reached():
        advice = _fallback_or_error_advice(
            row=row,
            run_date=run_date,
            previous_by_symbol=previous_by_symbol,
            reason="daily deadline exceeded",
        )
        classification = _classification_for_non_ok(
            row=row,
            advice=advice,
            run_date=run_date,
        )
        return _SymbolResult(
            index=index,
            row=row,
            advice=advice,
            classification=classification,
        )

    runner = advice_runner_factory() if advice_runner_factory is not None else advice_runner
    if runner is None:
        raise ValueError("advice_runner or advice_runner_factory is required")

    advice = _analyze_symbol(
        advice_runner=runner,
        row=row,
        run_date=run_date,
        previous_by_symbol=previous_by_symbol,
        use_fallback=use_fallback,
    )
    if advice.status != "ok":
        classification = _classification_for_non_ok(
            row=row,
            advice=advice,
            run_date=run_date,
        )
        return _SymbolResult(
            index=index,
            row=row,
            advice=advice,
            classification=classification,
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


def _fallback_or_error_advice(
    *,
    row: PortfolioInputRow,
    run_date: str,
    previous_by_symbol: dict[str, dict[str, str]],
    reason: str,
) -> TradingAdvice:
    previous = previous_by_symbol.get(row.symbol)
    if previous and previous.get("status") in {"ok", "fallback"}:
        fallback_from_date = previous.get("fallback_from_date", "") or previous.get(
            "run_date", ""
        )
        return TradingAdvice(
            run_date=run_date,
            symbol=row.symbol,
            market=row.market,
            asset_class=row.asset_class,
            portfolio_weight_hkd=row.portfolio_weight_hkd,
            risk_flag=row.risk_flag,
            source=previous.get("source", "tradingagents"),
            advice_action=previous.get("advice_action", ""),
            advice_summary=previous.get("advice_summary", ""),
            raw_decision=previous.get("raw_decision", ""),
            status="fallback",
            error="",
            source_status="fallback",
            fallback_reason=reason,
            fallback_from_date=fallback_from_date,
        )
    return TradingAdvice(
        run_date=run_date,
        symbol=row.symbol,
        market=row.market,
        asset_class=row.asset_class,
        portfolio_weight_hkd=row.portfolio_weight_hkd,
        risk_flag=row.risk_flag,
        source="tradingagents",
        advice_action="",
        advice_summary="",
        raw_decision="",
        status="error",
        error=reason,
        source_status="error",
        fallback_reason="",
        fallback_from_date="",
    )


def _classification_for_non_ok(
    *,
    row: PortfolioInputRow,
    advice: TradingAdvice,
    run_date: str,
) -> ChangeClassification:
    return ChangeClassification(
        run_date=run_date,
        symbol=row.symbol,
        include_in_report=False,
        change_type="no_material_change",
        severity="low",
        suggested_action=advice.advice_action,
        summary="",
        rationale="",
        watch_trigger="",
        status="ok" if advice.status == "fallback" else "error",
        error=advice.error,
    )


def _analyze_symbol(
    *,
    advice_runner: AdviceRunner,
    row: PortfolioInputRow,
    run_date: str,
    previous_by_symbol: dict[str, dict[str, str]],
    use_fallback: bool,
) -> TradingAdvice:
    try:
        advice = advice_runner.analyze(row, run_date)
    except Exception as exc:
        if use_fallback:
            return _fallback_or_error_advice(
                row=row,
                run_date=run_date,
                previous_by_symbol=previous_by_symbol,
                reason=str(exc),
            )
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
            source_status="error",
            fallback_reason="",
            fallback_from_date="",
        )
    if use_fallback and advice.status != "ok":
        return _fallback_or_error_advice(
            row=row,
            run_date=run_date,
            previous_by_symbol=previous_by_symbol,
            reason=advice.error or f"{row.symbol} analysis returned {advice.status}",
        )
    return advice


def _promote_latest_outputs(
    *,
    advice_path: Path,
    actions_path: Path,
    data_dir: Path,
    market: MarketScope | None,
) -> None:
    latest_dir = _latest_dir(data_dir, market)
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


def _write_trading_advice_run(
    *,
    run_date: str,
    records: Iterable[TradingAdvice],
    data_dir: Path,
    market: MarketScope | None,
) -> Path:
    rows = [record.to_row() for record in records]
    run_path = _run_path(data_dir, run_date, market, "trading_advice.csv")
    _atomic_write_csv(run_path, TRADING_ADVICE_FIELDNAMES, rows)
    return run_path


def _write_change_classifications_run(
    *,
    run_date: str,
    records: Iterable[ChangeClassification],
    data_dir: Path,
    market: MarketScope | None,
) -> Path:
    run_path = _run_path(data_dir, run_date, market, "change_classifications.csv")
    _atomic_write_csv(
        run_path,
        CHANGE_CLASSIFICATION_FIELDNAMES,
        (record.to_row() for record in records),
    )
    return run_path


def _load_latest_advice_by_symbol(
    data_dir: Path,
    *,
    market: MarketScope | None,
) -> dict[str, dict[str, str]]:
    if market is None:
        return load_latest_advice_by_symbol(data_dir)

    latest_path = market_scoped_latest_path(data_dir, market, "trading_advice.csv")
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


def _run_path(
    data_dir: Path,
    run_date: str,
    market: MarketScope | None,
    name: str,
) -> Path:
    if market is not None:
        return market_run_dir(data_dir, run_date, market) / name
    return data_dir / "runs" / run_date / name


def _latest_dir(data_dir: Path, market: MarketScope | None) -> Path:
    if market is not None:
        return market_scoped_latest_dir(data_dir, market)
    return data_dir / "latest"


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
