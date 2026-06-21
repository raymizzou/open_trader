from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from open_trader.market_scope import MarketScope
from open_trader.technical_facts import (
    LLMTechnicalFactsExtractor,
    OpenAITextClient,
    TechnicalFactsExtractor,
    build_freshness,
    extract_market_report,
    generate_technical_facts,
    load_advice_sources,
    load_technical_facts_cache,
    source_hash,
    technical_facts_has_missing_timeframe,
    technical_facts_latest_path,
    technical_facts_run_path,
)


class FakeExtractor(TechnicalFactsExtractor):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract(
        self, *, market: str, symbol: str, run_date: str, market_report: str
    ) -> dict[str, object]:
        self.calls.append(market_report)
        assert "FINAL TRANSACTION PROPOSAL" not in market_report
        return {
            "schema_version": "open_trader.technical_facts.v1",
            "status": "present",
            "source_date": run_date,
            "market_data_as_of": "2026-06-18",
            "symbol": f"{market}.{symbol}",
            "timeframes": [
                {
                    "timeframe": "daily",
                    "timeframe_label": "日线",
                    "current_price": "411.60",
                    "trend_summary": "价格高于主要均线。",
                    "moving_averages": {"ema_10": "398.15", "sma_50": "368.24"},
                    "macd": {"crossover": "6月17日金叉"},
                    "rsi": {"value": "56.88"},
                    "bollinger": {},
                    "atr": {"value": "33.17", "percent_of_price": "8.1%"},
                    "volume": {},
                    "support_resistance": {"support_levels": [], "resistance_levels": []},
                    "price_action": {"timeline": []},
                    "risks": [],
                    "evidence_quotes": ["MACD line crossed above Signal line on June 17."],
                }
            ],
        }


class FailingExtractor(TechnicalFactsExtractor):
    def extract(
        self, *, market: str, symbol: str, run_date: str, market_report: str
    ) -> dict[str, object]:
        raise RuntimeError("llm unavailable")


class MissingTimeframesExtractor(TechnicalFactsExtractor):
    def extract(
        self, *, market: str, symbol: str, run_date: str, market_report: str
    ) -> dict[str, object]:
        return {
            "schema_version": "open_trader.technical_facts.v1",
            "status": "present",
            "source_date": run_date,
            "market_data_as_of": "2026-06-18",
            "symbol": f"{market}.{symbol}",
        }


class UnknownTimeframeExtractor(TechnicalFactsExtractor):
    def extract(
        self, *, market: str, symbol: str, run_date: str, market_report: str
    ) -> dict[str, object]:
        return {
            "schema_version": "open_trader.technical_facts.v1",
            "status": "present",
            "source_date": run_date,
            "market_data_as_of": "2026-06-18",
            "symbol": f"{market}.{symbol}",
            "timeframes": [
                {
                    "timeframe": "unknown",
                    "timeframe_label": "周期缺失",
                    "current_price": "411.60",
                }
            ],
        }


def write_advice(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
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
        "source_status",
        "fallback_reason",
        "fallback_from_date",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def raw_decision_with_market_report(report: str) -> str:
    return json.dumps({"state": {"market_report": report}}, ensure_ascii=False)


def test_extract_market_report_reads_raw_decision_state() -> None:
    raw = raw_decision_with_market_report("Technical report text")

    assert extract_market_report(raw) == "Technical report text"


def test_extract_market_report_returns_empty_for_invalid_json() -> None:
    assert extract_market_report("{not-json") == ""


def test_source_hash_is_stable_and_prefixed() -> None:
    first = source_hash("Technical report text")
    second = source_hash("Technical report text")

    assert first == second
    assert first.startswith("sha256:")
    assert source_hash("Other report text") != first


def test_load_advice_sources_reads_rows_with_market_report(tmp_path: Path) -> None:
    advice_path = tmp_path / "trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    sources = load_advice_sources(advice_path)

    assert len(sources) == 1
    assert sources[0].market == "HK"
    assert sources[0].symbol == "02476"
    assert sources[0].run_date == "2026-06-19"
    assert sources[0].market_report == "Daily RSI 56.88"
    assert sources[0].source_advice_hash == source_hash("Daily RSI 56.88")


def test_load_advice_sources_rejects_missing_path(tmp_path: Path) -> None:
    advice_path = tmp_path / "missing_trading_advice.csv"

    with pytest.raises(FileNotFoundError) as exc_info:
        load_advice_sources(advice_path)

    assert str(advice_path) in str(exc_info.value)


def test_build_freshness_prefers_timeframe_and_market_data_date() -> None:
    freshness = build_freshness(
        market_data_as_of="2026-06-18",
        run_date="2026-06-19",
        has_unknown_timeframe=False,
    )

    assert freshness == {
        "status": "fresh",
        "message": "日线数据截至 2026-06-18",
    }


def test_build_freshness_marks_missing_date() -> None:
    freshness = build_freshness(
        market_data_as_of="",
        run_date="2026-06-19",
        has_unknown_timeframe=False,
    )

    assert freshness["status"] == "missing_date"
    assert freshness["message"] == "行情日期缺失，报告生成于 2026-06-19"


def test_build_freshness_marks_missing_timeframe_with_exact_review_message() -> None:
    freshness = build_freshness(
        market_data_as_of="2026-06-18",
        run_date="2026-06-19",
        has_unknown_timeframe=True,
    )

    assert freshness["status"] == "missing_timeframe"
    assert freshness["message"] == "指标周期缺失，需复核"


@pytest.mark.parametrize(
    ("timeframe", "timeframe_label"),
    [
        ("unknown", "周期缺失"),
        ("UNKNOWN", ""),
        ("unknown timeframe", ""),
        ("周期缺失", ""),
        ("", ""),
    ],
)
def test_technical_facts_has_missing_timeframe_rejects_placeholder_values(
    timeframe: str,
    timeframe_label: str,
) -> None:
    facts: dict[str, object] = {
        "timeframes": [
            {
                "timeframe": timeframe,
                "timeframe_label": timeframe_label,
            }
        ]
    }

    assert technical_facts_has_missing_timeframe(facts) is True


def test_load_technical_facts_cache_returns_empty_for_invalid_json(tmp_path: Path) -> None:
    cache_path = tmp_path / "technical_facts.json"
    cache_path.write_text("{not-json", encoding="utf-8")

    assert load_technical_facts_cache(cache_path) == {}


def test_generate_technical_facts_writes_run_and_latest_cache(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Buy",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(
                    "Daily MACD crossed. FINAL TRANSACTION PROPOSAL: BUY"
                ),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    extractor = FakeExtractor()

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=extractor,
        update_latest=True,
        market=None,
    )

    assert result.records == 1
    assert result.extracted == 1
    assert result.failed == 0
    assert result.reused == 0
    assert result.run_path == tmp_path / "data/runs/2026-06-19/technical_facts.json"
    assert result.latest_path == tmp_path / "data/latest/technical_facts.json"
    cache = load_technical_facts_cache(result.latest_path)
    row = cache["records"][0]
    assert row["market"] == "HK"
    assert row["symbol"] == "02476"
    assert row["extraction_status"] == "ok"
    assert row["freshness"]["message"] == "日线数据截至 2026-06-18"
    assert "BUY" not in json.dumps(row["facts"], ensure_ascii=False)


def test_generate_technical_facts_marks_unknown_timeframe_as_missing_timeframe(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Buy",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(
                    "RSI report without explicit period"
                ),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=UnknownTimeframeExtractor(),
        update_latest=True,
        market=None,
    )

    cache = load_technical_facts_cache(result.latest_path)
    row = cache["records"][0]
    assert result.extracted == 1
    assert row["freshness"]["status"] == "missing_timeframe"


def test_generate_technical_facts_rejects_malformed_explicit_run_date(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    with pytest.raises(ValueError, match="run_date must be YYYY-MM-DD"):
        generate_technical_facts(
            advice_path=advice_path,
            data_dir=tmp_path / "data",
            run_date="../latest",
            extractor=FakeExtractor(),
            update_latest=False,
            market=None,
        )

    assert not (tmp_path / "data/latest/technical_facts.json").exists()


def test_generate_technical_facts_rejects_malformed_csv_run_date(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/input/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "../latest",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    with pytest.raises(ValueError, match="run_date must be YYYY-MM-DD"):
        generate_technical_facts(
            advice_path=advice_path,
            data_dir=tmp_path / "data",
            run_date="",
            extractor=FakeExtractor(),
            update_latest=False,
            market=None,
        )

    assert not (tmp_path / "data/latest/technical_facts.json").exists()
    assert not (tmp_path / "data/technical_facts.json").exists()


def test_generate_technical_facts_rejects_mixed_malformed_and_valid_csv_run_dates(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/input/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "../latest",
                "symbol": "09988",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "5.00%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Weekly trend"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            },
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            },
        ],
    )

    with pytest.raises(ValueError, match="run_date must be YYYY-MM-DD"):
        generate_technical_facts(
            advice_path=advice_path,
            data_dir=tmp_path / "data",
            run_date="",
            extractor=FakeExtractor(),
            update_latest=False,
            market=None,
        )

    assert not (tmp_path / "data/runs/2026-06-19/technical_facts.json").exists()
    assert not (tmp_path / "data/latest/technical_facts.json").exists()


def test_generate_technical_facts_rejects_malformed_csv_run_date_with_explicit_date(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/input/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "../latest",
                "symbol": "09988",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "5.00%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Weekly trend"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            },
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            },
        ],
    )

    with pytest.raises(ValueError, match="run_date must be YYYY-MM-DD"):
        generate_technical_facts(
            advice_path=advice_path,
            data_dir=tmp_path / "data",
            run_date="2026-06-19",
            extractor=FakeExtractor(),
            update_latest=False,
            market=None,
        )

    assert not (tmp_path / "data/runs/2026-06-19/technical_facts.json").exists()
    assert not (tmp_path / "data/latest/technical_facts.json").exists()


def test_generate_technical_facts_accepts_valid_run_date(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=FakeExtractor(),
        update_latest=False,
        market=None,
    )

    assert result.extracted == 1
    assert result.run_path == tmp_path / "data/runs/2026-06-19/technical_facts.json"
    assert result.run_path.exists()


def test_generate_technical_facts_defaults_to_latest_run_date_within_market(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/latest/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("HK Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            },
            {
                "run_date": "2026-06-19",
                "symbol": "AAPL",
                "market": "US",
                "asset_class": "stock",
                "portfolio_weight_hkd": "10.00%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Hold",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("US Daily RSI 60.00"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            },
        ],
    )

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date=None,
        extractor=FakeExtractor(),
        update_latest=True,
        market="HK",
    )

    assert result.run_date == "2026-06-18"
    assert result.records == 1
    assert result.run_path == tmp_path / "data/runs/2026-06-18/HK/technical_facts.json"
    assert result.latest_path == tmp_path / "data/latest/HK/technical_facts.json"
    assert not (tmp_path / "data/runs/2026-06-19/HK/technical_facts.json").exists()
    latest = load_technical_facts_cache(result.latest_path)
    assert latest["run_date"] == "2026-06-18"
    assert latest["records"][0]["market"] == "HK"


def test_generate_technical_facts_reuses_matching_latest_cache(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    report = "Daily RSI 56.88"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    extractor = FakeExtractor()
    first = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=extractor,
        update_latest=True,
        market=None,
    )

    second_extractor = FakeExtractor()
    second = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=second_extractor,
        update_latest=True,
        market=None,
    )

    assert first.extracted == 1
    assert second.extracted == 0
    assert second.failed == 0
    assert second.reused == 1
    assert second_extractor.calls == []


def test_generate_technical_facts_does_not_reuse_failed_latest_cache(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    report = "Daily RSI 56.88"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    first = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=FailingExtractor(),
        update_latest=True,
        market=None,
    )

    second_extractor = FakeExtractor()
    second = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=second_extractor,
        update_latest=True,
        market=None,
    )

    cache = load_technical_facts_cache(second.latest_path)
    row = cache["records"][0]
    assert first.failed == 1
    assert second.extracted == 1
    assert second.reused == 0
    assert second.failed == 0
    assert second_extractor.calls == [report]
    assert row["extraction_status"] == "ok"


def test_generate_technical_facts_normalizes_reused_record_to_current_run(
    tmp_path: Path,
) -> None:
    report = "Daily RSI 56.88"
    old_advice_path = tmp_path / "data/runs/2026-06-18/trading_advice.csv"
    write_advice(
        old_advice_path,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    generate_technical_facts(
        advice_path=old_advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-18",
        extractor=FakeExtractor(),
        update_latest=True,
        market=None,
    )
    new_advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        new_advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    second_extractor = FakeExtractor()
    second = generate_technical_facts(
        advice_path=new_advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=second_extractor,
        update_latest=True,
        market=None,
    )

    cache = load_technical_facts_cache(second.run_path)
    row = cache["records"][0]
    assert second.extracted == 0
    assert second.reused == 1
    assert second.failed == 0
    assert second_extractor.calls == []
    assert row["run_date"] == "2026-06-19"
    assert row["reused_from_cache"] is True


def test_generate_technical_facts_counts_missing_source_as_failed(tmp_path: Path) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report(""),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=FakeExtractor(),
        update_latest=True,
        market=None,
    )

    cache = load_technical_facts_cache(result.latest_path)
    row = cache["records"][0]
    assert result.extracted == 0
    assert result.failed == 1
    assert row["extraction_status"] == "missing_source"


def test_generate_technical_facts_counts_extraction_failure_as_failed(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=FailingExtractor(),
        update_latest=True,
        market=None,
    )

    cache = load_technical_facts_cache(result.latest_path)
    row = cache["records"][0]
    assert result.extracted == 0
    assert result.failed == 1
    assert row["extraction_status"] == "extraction_failed"


class FakeLLMClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[list[dict[str, str]]] = []

    def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
        self.messages.append(messages)
        return self.content


def test_llm_extractor_parses_strict_json() -> None:
    client = FakeLLMClient(
        json.dumps(
            {
                "schema_version": "open_trader.technical_facts.v1",
                "status": "present",
                "source_date": "2026-06-19",
                "market_data_as_of": "2026-06-18",
                "symbol": "HK.02476",
                "timeframes": [
                    {
                        "timeframe": "daily",
                        "timeframe_label": "日线",
                        "current_price": "411.60",
                    }
                ],
            }
        )
    )
    extractor = LLMTechnicalFactsExtractor(client=client)

    facts = extractor.extract(
        market="HK",
        symbol="02476",
        run_date="2026-06-19",
        market_report="Daily technical report. FINAL TRANSACTION PROPOSAL: BUY",
    )

    assert facts["schema_version"] == "open_trader.technical_facts.v1"
    assert facts["market_data_as_of"] == "2026-06-18"
    prompt_text = json.dumps(client.messages, ensure_ascii=False)
    assert "只抽取客观技术面事实" in prompt_text
    assert "忽略 FINAL TRANSACTION PROPOSAL" in prompt_text


def test_llm_extractor_rejects_non_json_response() -> None:
    extractor = LLMTechnicalFactsExtractor(client=FakeLLMClient("not json"))

    try:
        extractor.extract(
            market="HK",
            symbol="02476",
            run_date="2026-06-19",
            market_report="Daily technical report",
        )
    except ValueError as exc:
        assert "valid JSON" in str(exc)
    else:
        raise AssertionError("expected ValueError")


class FakeOpenAICompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        message = type("Message", (), {"content": "{}"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeOpenAIChat:
    def __init__(self) -> None:
        self.completions = FakeOpenAICompletions()


class FakeOpenAI:
    def __init__(self) -> None:
        self.chat = FakeOpenAIChat()


def test_openai_text_client_requests_json_response_format() -> None:
    fake_openai = FakeOpenAI()
    client = object.__new__(OpenAITextClient)
    client.model = "test"
    client.client = fake_openai

    client.create(messages=[{"role": "user", "content": "report"}], temperature=0)

    assert fake_openai.chat.completions.kwargs["response_format"] == {
        "type": "json_object"
    }


def test_generate_technical_facts_rejects_missing_timeframes_as_extraction_failed(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "data/runs/2026-06-19/trading_advice.csv"
    write_advice(
        advice_path,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "02476",
                "market": "HK",
                "asset_class": "stock",
                "portfolio_weight_hkd": "8.97%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "",
                "raw_decision": raw_decision_with_market_report("Daily RSI 56.88"),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    result = generate_technical_facts(
        advice_path=advice_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-19",
        extractor=MissingTimeframesExtractor(),
        update_latest=True,
        market=None,
    )

    cache = load_technical_facts_cache(result.latest_path)
    row = cache["records"][0]
    assert result.extracted == 0
    assert result.failed == 1
    assert row["extraction_status"] == "extraction_failed"
    assert row["error"] == "technical facts timeframes must be a list"


def test_technical_facts_paths_support_market_scope(tmp_path: Path) -> None:
    assert technical_facts_run_path(
        tmp_path / "data", "2026-06-19", MarketScope.HK
    ) == tmp_path / "data/runs/2026-06-19/HK/technical_facts.json"
    assert technical_facts_latest_path(
        tmp_path / "data", MarketScope.HK
    ) == tmp_path / "data/latest/HK/technical_facts.json"
