from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

import open_trader.advice.premarket as premarket
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
    "currency",
    "last_price",
    "market_value_hkd",
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
            last_price=row.last_price,
            price_currency=row.price_currency,
            portfolio_weight_hkd=row.portfolio_weight_hkd,
            market_value_hkd=row.market_value_hkd,
            risk_flag=row.risk_flag,
            source="fake",
            advice_action="reduce" if row.symbol == "VIXY" else "hold",
            advice_summary=f"{row.symbol} summary",
            raw_decision="{}",
            status="ok",
            error="",
        )


class ReturningErrorAdviceRunner(FakeAdviceRunner):
    def __init__(self, error_symbols: set[str]) -> None:
        super().__init__()
        self.error_symbols = error_symbols

    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        self.calls.append((row.symbol, run_date))
        if row.symbol in self.error_symbols:
            return TradingAdvice(
                run_date=run_date,
                symbol=row.symbol,
                market=row.market,
                asset_class=row.asset_class,
                last_price=row.last_price,
                price_currency=row.price_currency,
                portfolio_weight_hkd=row.portfolio_weight_hkd,
                market_value_hkd=row.market_value_hkd,
                risk_flag=row.risk_flag,
                source="tradingagents",
                advice_action="",
                advice_summary="",
                raw_decision="",
                status="error",
                error=f"{row.symbol} subprocess timed out",
                source_status="error",
            )
        return super().analyze(row, run_date)


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


class BlockingAdviceRunner(FakeAdviceRunner):
    def __init__(self) -> None:
        super().__init__()
        self.qqq_started = threading.Event()
        self.vixy_waited_for_qqq = False

    def analyze(self, row: PortfolioInputRow, run_date: str) -> TradingAdvice:
        if row.symbol == "VIXY":
            self.calls.append((row.symbol, run_date))
            self.vixy_waited_for_qqq = self.qqq_started.wait(timeout=1)
            return TradingAdvice(
                run_date=run_date,
                symbol=row.symbol,
                market=row.market,
                asset_class=row.asset_class,
                last_price=row.last_price,
                price_currency=row.price_currency,
                portfolio_weight_hkd=row.portfolio_weight_hkd,
                market_value_hkd=row.market_value_hkd,
                risk_flag=row.risk_flag,
                source="fake",
                advice_action="reduce",
                advice_summary="VIXY summary",
                raw_decision="{}",
                status="ok",
                error="",
            )

        if row.symbol == "QQQ":
            self.calls.append((row.symbol, run_date))
            self.qqq_started.set()
            return TradingAdvice(
                run_date=run_date,
                symbol=row.symbol,
                market=row.market,
                asset_class=row.asset_class,
                last_price=row.last_price,
                price_currency=row.price_currency,
                portfolio_weight_hkd=row.portfolio_weight_hkd,
                market_value_hkd=row.market_value_hkd,
                risk_flag=row.risk_flag,
                source="fake",
                advice_action="hold",
                advice_summary="QQQ summary",
                raw_decision="{}",
                status="ok",
                error="",
            )

        return super().analyze(row, run_date)


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
                    "currency": "USD",
                    "last_price": "21.82",
                    "market_value_hkd": "38015.98",
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
                    "currency": "USD",
                    "last_price": "448.10",
                    "market_value_hkd": "17387.20",
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
                    "currency": "HKD",
                    "last_price": "23.10",
                    "market_value_hkd": "189400.00",
                    "portfolio_weight_hkd": "15.20%",
                    "ai_eligible": "false",
                    "analysis_symbol": "",
                    "risk_flag": "overweight",
                },
            ]
        )


def write_blacklisted_portfolio(path: Path) -> None:
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
                    "asset_class": "etf",
                    "symbol": "AGRZ",
                    "name": "Ignored ETF",
                    "portfolio_weight_hkd": "0.15%",
                    "ai_eligible": "true",
                    "analysis_symbol": "AGRZ",
                    "risk_flag": "normal",
                },
                {
                    "market": "US",
                    "asset_class": "etf",
                    "symbol": "ARGG",
                    "name": "Ignored typo ETF",
                    "portfolio_weight_hkd": "0.10%",
                    "ai_eligible": "true",
                    "analysis_symbol": "ARGG",
                    "risk_flag": "normal",
                },
            ]
        )


def write_all_ineligible_portfolio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            [
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
                {
                    "market": "US",
                    "asset_class": "stock",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "portfolio_weight_hkd": "8.00%",
                    "ai_eligible": "false",
                    "analysis_symbol": "AAPL",
                    "risk_flag": "normal",
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
    report = result.report_path.read_text(encoding="utf-8")
    assert "## 持仓全景" in report
    assert "| VIXY | USD 21.82 | HKD 38,015.98 | 3.05% | 正常 | 减仓 | 正常 |" in report
    assert "| QQQ | USD 448.10 | HKD 17,387.20 | 1.40% | 正常 | 持有 | 正常 |" in report
    assert "## 今日重点策略" in report
    assert "overweight" not in report

    actions = list(csv.DictReader(result.actions_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in actions] == ["VIXY"]

    advice_rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    assert [row["symbol"] for row in advice_rows] == ["VIXY", "QQQ"]


def test_run_premarket_excludes_default_blacklisted_symbols(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_blacklisted_portfolio(portfolio_path)
    advice_runner = FakeAdviceRunner()

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=advice_runner,
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
    )

    assert result.eligible_count == 1
    assert result.advice_count == 1
    assert advice_runner.calls == [("VIXY", "2026-06-16")]


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


def test_run_premarket_parallelizes_symbols_but_preserves_output_order(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    advice_runner = BlockingAdviceRunner()

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=advice_runner,
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
        max_workers=2,
    )

    assert advice_runner.vixy_waited_for_qqq is True
    advice_rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    classification_rows = list(
        csv.DictReader(result.classifications_path.open(encoding="utf-8"))
    )
    assert [row["symbol"] for row in advice_rows] == ["VIXY", "QQQ"]
    assert [row["symbol"] for row in classification_rows] == ["VIXY", "QQQ"]


def test_run_premarket_uses_advice_runner_factory_per_symbol(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    created_runners: list[FakeAdviceRunner] = []

    def advice_runner_factory() -> FakeAdviceRunner:
        runner = FakeAdviceRunner()
        created_runners.append(runner)
        return runner

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=None,
        advice_runner_factory=advice_runner_factory,
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
        max_workers=2,
    )

    assert result.advice_count == 2
    assert len(created_runners) == 2
    assert sorted(runner.calls[0][0] for runner in created_runners) == ["QQQ", "VIXY"]


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
    assert not (data_dir / "latest" / "premarket_actions.csv").exists()


def test_run_premarket_all_ineligible_writes_empty_run_outputs_and_preserves_latest(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_all_ineligible_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    write_previous_latest_advice(data_dir)
    write_previous_latest_actions(data_dir)
    original_advice = (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    )
    original_actions = (data_dir / "latest" / "premarket_actions.csv").read_text(
        encoding="utf-8"
    )
    advice_runner = FakeAdviceRunner()
    classifier = FakeClassifier()

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

    assert result.eligible_count == 0
    assert result.advice_count == 0
    assert result.action_count == 0
    assert advice_runner.calls == []
    assert classifier.previous_by_symbol == {}
    assert result.advice_path.exists()
    assert result.classifications_path.exists()
    assert result.actions_path.exists()
    assert "没有找到符合条件的美股或 ETF 标的。" in result.report_path.read_text(
        encoding="utf-8"
    )
    assert (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    ) == original_advice
    assert (data_dir / "latest" / "premarket_actions.csv").read_text(
        encoding="utf-8"
    ) == original_actions


def test_run_premarket_no_matching_symbols_writes_empty_run_outputs_and_preserves_latest(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    write_previous_latest_advice(data_dir)
    write_previous_latest_actions(data_dir)
    original_advice = (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    )
    original_actions = (data_dir / "latest" / "premarket_actions.csv").read_text(
        encoding="utf-8"
    )
    advice_runner = FakeAdviceRunner()
    classifier = FakeClassifier()

    result = run_premarket(
        run_date="2026-06-16",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=advice_runner,
        classifier=classifier,
        symbols={"MSFT"},
        update_latest=True,
    )

    assert result.eligible_count == 0
    assert result.advice_count == 0
    assert result.action_count == 0
    assert advice_runner.calls == []
    assert classifier.previous_by_symbol == {}
    assert result.advice_path.exists()
    assert result.classifications_path.exists()
    assert result.actions_path.exists()
    assert "没有找到符合条件的美股或 ETF 标的。" in result.report_path.read_text(
        encoding="utf-8"
    )
    assert (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    ) == original_advice
    assert (data_dir / "latest" / "premarket_actions.csv").read_text(
        encoding="utf-8"
    ) == original_actions


def test_run_premarket_keeps_existing_latest_advice_when_later_output_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    write_previous_latest_advice(data_dir)
    original_latest = (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    )

    def fail_write_premarket_outputs(**_: object) -> tuple[Path, Path, Path]:
        raise OSError("simulated report failure")

    monkeypatch.setattr(
        premarket,
        "write_premarket_outputs",
        fail_write_premarket_outputs,
    )

    with pytest.raises(OSError, match="simulated report failure"):
        run_premarket(
            run_date="2026-06-16",
            portfolio_path=portfolio_path,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            advice_runner=FakeAdviceRunner(),
            classifier=FakeClassifier(),
            symbols=None,
            update_latest=True,
        )

    assert (
        data_dir / "runs" / "2026-06-16" / "trading_advice.csv"
    ).exists()
    assert (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    ) == original_latest


def test_run_premarket_latest_promotion_failure_restores_previous_latest_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    write_previous_latest_advice(data_dir)
    write_previous_latest_actions(data_dir)
    original_advice = (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    )
    original_actions = (data_dir / "latest" / "premarket_actions.csv").read_text(
        encoding="utf-8"
    )
    original_replace = Path.replace

    def fail_action_latest_replace(self: Path, target: Path) -> Path:
        if target == data_dir / "latest" / "premarket_actions.csv":
            assert (data_dir / "latest" / "trading_advice.csv").read_text(
                encoding="utf-8"
            ) != original_advice
            raise OSError("simulated action latest promotion failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_action_latest_replace)

    with pytest.raises(OSError, match="simulated action latest promotion failure"):
        run_premarket(
            run_date="2026-06-16",
            portfolio_path=portfolio_path,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            advice_runner=FakeAdviceRunner(),
            classifier=FakeClassifier(),
            symbols=None,
            update_latest=True,
        )

    assert (data_dir / "latest" / "trading_advice.csv").read_text(
        encoding="utf-8"
    ) == original_advice
    assert (data_dir / "latest" / "premarket_actions.csv").read_text(
        encoding="utf-8"
    ) == original_actions
    assert list((data_dir / "latest").glob("*.backup")) == []
    assert list((data_dir / "latest").glob(".*.tmp")) == []


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


def test_run_premarket_falls_back_to_latest_ok_advice_on_symbol_failure(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    latest = data_dir / "latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=premarket.TRADING_ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "QQQ",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.40%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "QQQ prior summary",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        )

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=reports_dir,
        advice_runner=FakeAdviceRunner(fail_symbols={"QQQ"}),
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
        use_fallback=True,
    )

    rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    qqq = next(row for row in rows if row["symbol"] == "QQQ")
    assert qqq["run_date"] == "2026-06-17"
    assert qqq["status"] == "fallback"
    assert qqq["source_status"] == "fallback"
    assert qqq["fallback_reason"] == "QQQ analysis failed"
    assert qqq["fallback_from_date"] == "2026-06-16"
    assert qqq["advice_summary"] == "QQQ prior summary"
    assert result.advice_count == 2


def test_run_premarket_falls_back_when_runner_returns_error_advice(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    latest = data_dir / "latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=premarket.TRADING_ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "QQQ",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.40%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "QQQ prior summary",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        )

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=ReturningErrorAdviceRunner(error_symbols={"QQQ"}),
        classifier=FakeClassifier(),
        symbols={"QQQ"},
        update_latest=True,
        use_fallback=True,
    )

    rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "QQQ"
    assert rows[0]["status"] == "fallback"
    assert rows[0]["fallback_from_date"] == "2026-06-16"
    assert rows[0]["advice_action"] == "hold"
    action_rows = list(csv.DictReader(result.actions_path.open(encoding="utf-8")))
    assert action_rows == []


def test_run_premarket_falls_back_from_latest_fallback_advice(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    latest = data_dir / "latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=premarket.TRADING_ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "3.05%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "reduce",
                "advice_summary": "VIXY carried summary",
                "raw_decision": "{}",
                "status": "fallback",
                "error": "",
                "source_status": "fallback",
                "fallback_reason": "daily deadline exceeded",
                "fallback_from_date": "",
            }
        )
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "QQQ",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "1.40%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "QQQ carried summary",
                "raw_decision": "{}",
                "status": "fallback",
                "error": "",
                "source_status": "fallback",
                "fallback_reason": "daily deadline exceeded",
                "fallback_from_date": "2026-06-15",
            }
        )

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=ReturningErrorAdviceRunner(error_symbols={"VIXY", "QQQ"}),
        classifier=FakeClassifier(),
        symbols={"VIXY", "QQQ"},
        update_latest=True,
        use_fallback=True,
    )

    rows = {
        row["symbol"]: row
        for row in csv.DictReader(result.advice_path.open(encoding="utf-8"))
    }
    assert rows["QQQ"]["status"] == "fallback"
    assert rows["QQQ"]["fallback_reason"] == "QQQ subprocess timed out"
    assert rows["QQQ"]["fallback_from_date"] == "2026-06-15"
    assert rows["QQQ"]["advice_action"] == "hold"
    assert rows["QQQ"]["advice_summary"] == "QQQ carried summary"
    assert rows["VIXY"]["status"] == "fallback"
    assert rows["VIXY"]["fallback_from_date"] == "2026-06-16"
    assert rows["VIXY"]["advice_action"] == "reduce"
    assert rows["VIXY"]["advice_summary"] == "VIXY carried summary"


def test_run_premarket_records_error_when_failure_has_no_fallback(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(fail_symbols={"QQQ"}),
        classifier=FakeClassifier(),
        symbols=None,
        update_latest=True,
        use_fallback=True,
    )

    rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    qqq = next(row for row in rows if row["symbol"] == "QQQ")
    assert qqq["status"] == "error"
    assert qqq["error"] == "QQQ analysis failed"
    assert qqq["source_status"] == "error"
    assert qqq["fallback_from_date"] == ""


def test_run_premarket_uses_fallback_when_deadline_has_passed_before_symbol(
    tmp_path: Path,
) -> None:
    portfolio_path = tmp_path / "portfolio.csv"
    write_portfolio(portfolio_path)
    data_dir = tmp_path / "data"
    latest = data_dir / "latest/trading_advice.csv"
    latest.parent.mkdir(parents=True)
    with latest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=premarket.TRADING_ADVICE_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "3.05%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "reduce",
                "advice_summary": "VIXY prior summary",
                "raw_decision": "{}",
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        )

    result = run_premarket(
        run_date="2026-06-17",
        portfolio_path=portfolio_path,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        advice_runner=FakeAdviceRunner(),
        classifier=FakeClassifier(),
        symbols={"VIXY"},
        update_latest=True,
        use_fallback=True,
        deadline_reached=lambda: True,
    )

    rows = list(csv.DictReader(result.advice_path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["status"] == "fallback"
    assert rows[0]["fallback_reason"] == "daily deadline exceeded"
    assert rows[0]["fallback_from_date"] == "2026-06-16"


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


def write_previous_latest_actions(data_dir: Path) -> None:
    latest_path = data_dir / "latest" / "premarket_actions.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with latest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "run_date",
                "symbol",
                "market",
                "portfolio_weight_hkd",
                "severity",
                "change_type",
                "suggested_action",
                "summary",
                "rationale",
                "watch_trigger",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "run_date": "2026-06-15",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "hold",
                "summary": "Old VIXY action",
                "rationale": "Old rationale.",
                "watch_trigger": "",
            }
        )
