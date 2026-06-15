from __future__ import annotations

import csv
from pathlib import Path

from open_trader.advice.models import (
    ChangeClassification,
    PortfolioInputRow,
    TradingAdvice,
)
from open_trader.advice.premarket import PremarketResult, run_premarket


PORTFOLIO_FIELDNAMES = [
    "market",
    "asset_class",
    "symbol",
    "name",
    "portfolio_weight_hkd",
    "ai_eligible",
    "analysis_symbol",
    "risk_flag",
]


class FakeAdviceRunner:
    def __init__(self, fail_symbols: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_symbols = fail_symbols or set()

    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        self.calls.append((row.symbol, run_date))
        if row.symbol in self.fail_symbols:
            raise RuntimeError(f"{row.symbol} analysis failed")
        return TradingAdvice(
            run_date=run_date,
            symbol=row.symbol,
            market=row.market,
            asset_class=row.asset_class,
            portfolio_weight_hkd=row.portfolio_weight_hkd,
            risk_flag=row.risk_flag,
            source="fake",
            advice_action="reduce" if row.symbol == "VIXY" else "hold",
            advice_summary=f"{row.symbol} summary",
            raw_decision="{}",
            status="ok",
            error="",
        )


class FakeClassifier:
    def __init__(self, fail_symbols: set[str] | None = None) -> None:
        self.fail_symbols = fail_symbols or set()
        self.previous_by_symbol: dict[str, dict[str, str] | None] = {}

    def classify(
        self,
        *,
        run_date: str,
        portfolio_row: PortfolioInputRow,
        previous_advice: dict[str, str] | None,
        latest_advice: TradingAdvice,
    ) -> ChangeClassification:
        self.previous_by_symbol[portfolio_row.symbol] = previous_advice
        if portfolio_row.symbol in self.fail_symbols:
            raise RuntimeError(f"{portfolio_row.symbol} classification failed")
        return ChangeClassification(
            run_date=run_date,
            symbol=portfolio_row.symbol,
            include_in_report=portfolio_row.symbol == "VIXY"
            and latest_advice.status == "ok",
            change_type=(
                "action_changed"
                if portfolio_row.symbol == "VIXY" and latest_advice.status == "ok"
                else "no_material_change"
            ),
            severity="high" if portfolio_row.symbol == "VIXY" else "low",
            suggested_action=latest_advice.advice_action,
            summary=f"{portfolio_row.symbol} changed",
            rationale="Fake classifier rationale.",
            watch_trigger="",
            status="ok",
            error="",
        )


def write_portfolio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            [
                {
                    "market": "US",
                    "asset_class": "etf",
                    "symbol": "VIXY",
                    "name": "Volatility ETF",
                    "portfolio_weight_hkd": "3.05%",
                    "ai_eligible": "true",
                    "analysis_symbol": "VIXY",
                    "risk_flag": "normal",
                },
                {
                    "market": "US",
                    "asset_class": "stock",
                    "symbol": "QQQ",
                    "name": "Nasdaq ETF",
                    "portfolio_weight_hkd": "1.40%",
                    "ai_eligible": "true",
                    "analysis_symbol": "TQQQ",
                    "risk_flag": "normal",
                },
                {
                    "market": "HK",
                    "asset_class": "stock",
                    "symbol": "02476",
                    "name": "VGT",
                    "portfolio_weight_hkd": "15.20%",
                    "ai_eligible": "false",
                    "analysis_symbol": "",
                    "risk_flag": "overweight",
                },
            ]
        )


def test_run_premarket_writes_full_advice_classifications_and_actions(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    advice_runner = FakeAdviceRunner()
    classifier = FakeClassifier()
    write_previous_latest_advice(data_dir)

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=advice_runner,
        classifier=classifier,
        symbols=None,
        update_latest=True,
    )

    assert isinstance(result, PremarketResult)
    assert result.eligible_count == 2
    assert result.advice_count == 2
    assert result.action_count == 1
    assert advice_runner.calls == [("VIXY", "2026-06-16"), ("QQQ", "2026-06-16")]
    assert classifier.previous_by_symbol["VIXY"]["advice_action"] == "hold"
    assert result.report_path.exists()

    actions = list(csv.DictReader(result.actions_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in actions] == ["VIXY"]

    advice_rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in advice_rows] == ["VIXY", "QQQ"]


def test_run_premarket_symbols_subset_limits_analysis_case_insensitively(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    advice_runner = FakeAdviceRunner()

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=advice_runner,
        classifier=FakeClassifier(),
        symbols={"tqqq"},
        update_latest=True,
    )

    assert result.eligible_count == 1
    assert advice_runner.calls == [("QQQ", "2026-06-16")]


def test_run_premarket_dry_run_does_not_update_latest_advice(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"

    run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=False,
    )

    assert not (data_dir / "latest" / "trading_advice.csv").exists()
    assert (data_dir / "latest" / "premarket_actions.csv").exists()


def test_run_premarket_converts_advice_runner_failure_and_continues(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(fail_symbols={"QQQ"}),
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
    )

    advice_rows = {
        row["symbol"]: row
        for row in csv.DictReader(result.advice_path.open(encoding="utf-8"))
    }
    assert advice_rows["VIXY"]["status"] == "ok"
    assert advice_rows["QQQ"]["status"] == "error"
    assert advice_rows["QQQ"]["error"] == "QQQ analysis failed"
    assert result.advice_count == 2


def test_run_premarket_converts_classifier_failure_and_continues(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(fail_symbols={"VIXY"}),
        symbols=None,
        update_latest=True,
    )

    classification_rows = {
        row["symbol"]: row
        for row in csv.DictReader(result.classifications_path.open(encoding="utf-8"))
    }
    assert classification_rows["VIXY"]["status"] == "error"
    assert classification_rows["VIXY"]["error"] == "VIXY classification failed"
    assert classification_rows["QQQ"]["status"] == "ok"
    assert result.action_count == 0


def write_previous_latest_advice(data_dir: Path) -> None:
    latest_path = data_dir / "latest" / "trading_advice.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with latest_path.open("w", encoding="utf-8", newline="") as handle:
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
                "run_date": "2026-06-15",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "3.05%",
                "risk_flag": "normal",
                "source": "fake",
                "advice_action": "hold",
                "advice_summary": "Old VIXY summary",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
            }
        )
