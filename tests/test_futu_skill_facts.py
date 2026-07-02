from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from open_trader.futu_skill_facts import (
    CAPITAL_ANOMALY_CATEGORY_LABELS,
    DERIVATIVES_ANOMALY_CATEGORY_LABELS_HK,
    DERIVATIVES_ANOMALY_CATEGORY_LABELS_US,
    FUTU_SKILL_FACTS_SCHEMA_VERSION,
    FutuAnomalyScriptClient,
    FutuNewsSentimentExtractor,
    FutuSkillFactsExtractor,
    FutuSkillNewsSentimentExtractor,
    LLMFutuDomesticDiscussionSummarizer,
    TECHNICAL_ANOMALY_CATEGORY_LABELS,
    futu_skill_facts_latest_path,
    futu_skill_facts_run_path,
    generate_futu_skill_facts,
    index_futu_skill_facts_by_market_symbol,
    load_futu_skill_facts_cache,
    validate_futu_skill_fact_record,
)


PORTFOLIO_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "quantity",
    "cost_price",
    "last_price",
    "market_value",
    "cost_value",
    "unrealized_pnl",
    "market_value_hkd",
    "portfolio_weight_hkd",
    "portfolio_value_incomplete",
    "risk_flag",
    "confidence",
    "notes",
]


class FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "market": market,
                "symbol": symbol,
                "name": name,
                "run_date": run_date,
            }
        )
        return {
            "status": "ok",
            "signal": "supportive",
            "confidence": "medium",
            "freshness": {
                "generated_at": "2026-07-01T09:10:00+08:00",
                "source_window": "latest",
            },
            "evidence": [
                {
                    "title": "NVIDIA news digest",
                    "summary": "AI 需求继续支持市场关注。",
                    "url": "https://example.com/nvda",
                }
            ],
            "blocking_reason": "",
            "suggested_constraint": "",
        }

    def extract_technical_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        return self._missing_anomaly(window_days)

    def extract_capital_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        return self._missing_anomaly(window_days)

    def extract_derivatives_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        return self._missing_anomaly(window_days)

    def _missing_anomaly(self, window_days: int) -> dict[str, object]:
        return {
            "status": "missing",
            "signal": "neutral",
            "confidence": "low",
            "suggested_constraint": "review",
            "window_days": window_days,
            "summary": "测试未接入异动信号。",
            "categories": [],
        }


class FakeFullFutuSkillExtractor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def extract_news_sentiment(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "signal": "supportive",
            "confidence": "medium",
            "freshness": {
                "generated_at": "2026-07-02T09:10:00+08:00",
                "source_window": "latest",
            },
            "evidence": [
                {
                    "title": f"{symbol} news",
                    "summary": "AI 需求继续支持市场关注。",
                    "url": f"https://example.com/{symbol.lower()}",
                }
            ],
            "blocking_reason": "",
            "suggested_constraint": "",
        }

    def extract_technical_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "module": "technical",
                "market": market,
                "symbol": symbol,
                "window_days": window_days,
            }
        )
        return {
            "status": "ok",
            "signal": "supportive",
            "confidence": "medium",
            "suggested_constraint": "",
            "window_days": window_days,
            "summary": "技术信号支持趋势，但不构成单独买入理由。",
            "categories": [
                {
                    "name": "MACD",
                    "state": "anomaly",
                    "direction": "bullish",
                    "detail": "金叉后继续放大，支持短线趋势延续。",
                    "evidence_date": "2026-07-01",
                },
                {
                    "name": "RSI",
                    "state": "anomaly",
                    "direction": "risk_up",
                    "detail": "接近超买区，追高风险上升。",
                    "evidence_date": "2026-07-02",
                },
                {
                    "name": "K线形态",
                    "state": "none",
                    "direction": "",
                    "detail": "窗口内无异常。",
                    "evidence_date": "",
                },
            ],
        }

    def extract_capital_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "module": "capital",
                "market": market,
                "symbol": symbol,
                "window_days": window_days,
            }
        )
        return {
            "status": "ok",
            "signal": "mixed",
            "confidence": "medium",
            "suggested_constraint": "no_add",
            "window_days": window_days,
            "summary": "资金流向与加仓动作存在分歧。",
            "categories": [
                {
                    "name": "资金流向",
                    "state": "anomaly",
                    "direction": "bearish",
                    "detail": "主力资金连续净流出，和加仓动作冲突。",
                    "evidence_date": "2026-07-02",
                },
                {
                    "name": "卖空情况",
                    "state": "none",
                    "direction": "",
                    "detail": "窗口内无异常。",
                    "evidence_date": "",
                },
            ],
        }

    def extract_derivatives_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "module": "derivatives",
                "market": market,
                "symbol": symbol,
                "window_days": window_days,
            }
        )
        return {
            "status": "partial",
            "signal": "risk_up",
            "confidence": "low",
            "suggested_constraint": "no_add",
            "window_days": window_days,
            "summary": "期权波动率偏高，不宜追高。",
            "categories": [
                {
                    "name": "期权波动率",
                    "state": "anomaly",
                    "direction": "risk_up",
                    "detail": "IV 位于高位，短线波动定价偏贵。",
                    "evidence_date": "2026-07-02",
                },
                {
                    "name": "期权大单",
                    "state": "anomaly",
                    "direction": "bullish",
                    "detail": "出现看涨大单，但不能单独覆盖资金分歧。",
                    "evidence_date": "2026-07-01",
                },
            ],
        }


class FakeInvalidAnomalyWindowExtractor(FakeFullFutuSkillExtractor):
    def extract_technical_anomaly(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        run_date: str,
        window_days: int,
    ) -> dict[str, object]:
        module = super().extract_technical_anomaly(
            market=market,
            symbol=symbol,
            name=name,
            run_date=run_date,
            window_days=window_days,
        )
        module["window_days"] = 0
        return module


class FakeDomesticSummarizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def summarize(
        self,
        *,
        market: str,
        symbol: str,
        name: str,
        news_items: list[dict[str, str]],
        community_items: list[dict[str, str]],
        post_count: int,
        relevant_post_count: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "market": market,
                "symbol": symbol,
                "name": name,
                "news_items": news_items,
                "community_items": community_items,
                "post_count": post_count,
                "relevant_post_count": relevant_post_count,
            }
        )
        return {
            "status": "ok",
            "keyword_counts": [
                {"keyword": "AI需求", "count": 1},
                {"keyword": "看多", "count": 1},
            ],
            "summary": "国内讨论认为 AI 需求仍强，但样本有限。",
            "focus": "关注 NVIDIA 与 AI 服务器需求。",
            "divergence_risk": "讨论样本偏少，不能代表稳定共识。",
            "credibility": "低",
            "trading_constraint": "仅作为国内讨论温度参考，不作为单独交易依据。",
            "post_count": post_count,
            "relevant_post_count": relevant_post_count,
        }


def write_portfolio(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_rows = []
    for row in rows:
        normalized = {field: "" for field in PORTFOLIO_FIELDNAMES}
        normalized.update(row)
        normalized_rows.append(normalized)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_FIELDNAMES)
        writer.writeheader()
        writer.writerows(normalized_rows)


def test_futu_skill_news_sentiment_extractor_protocol_accepts_fake() -> None:
    extractor: FutuSkillNewsSentimentExtractor = FakeExtractor()

    result = extractor.extract_news_sentiment(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-01",
    )

    assert result["signal"] == "supportive"


def test_futu_news_sentiment_extractor_builds_evidence_from_futu_apis() -> None:
    calls: list[dict[str, object]] = []
    summarizer = FakeDomesticSummarizer()

    def fake_get_json(url: str, params: dict[str, object]) -> dict[str, object]:
        calls.append({"url": url, "params": params})
        if url.endswith("/news_search"):
            return {
                "code": 0,
                "data": [
                    {
                        "title": "NVIDIA AI demand boosts chip outlook",
                        "url": "https://news.example/nvda-ai",
                    }
                ],
            }
        if url.endswith("/stock_feed"):
            return {
                "code": 0,
                "data": [
                    {
                        "title": "继续看好 NVIDIA",
                        "desc": "<p>AI 需求仍强。</p>",
                        "url": "https://feed.example/nvda",
                    }
                ],
            }
        raise AssertionError(f"unexpected URL {url}")

    extractor = FutuNewsSentimentExtractor(
        http_get_json=fake_get_json,
        domestic_summarizer=summarizer,
    )

    result = extractor.extract_news_sentiment(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-01",
    )

    assert result["status"] == "ok"
    assert result["signal"] == "supportive"
    assert result["confidence"] == "medium"
    assert result["suggested_constraint"] == ""
    assert result["freshness"]["source_window"] == "latest"
    assert result["domestic_discussion"] == {
        "status": "ok",
        "keyword_counts": [
            {"keyword": "AI需求", "count": 1},
            {"keyword": "看多", "count": 1},
        ],
        "summary": "国内讨论认为 AI 需求仍强，但样本有限。",
        "focus": "关注 NVIDIA 与 AI 服务器需求。",
        "divergence_risk": "讨论样本偏少，不能代表稳定共识。",
        "credibility": "低",
        "trading_constraint": "仅作为国内讨论温度参考，不作为单独交易依据。",
        "post_count": 1,
        "relevant_post_count": 1,
    }
    assert summarizer.calls == [
        {
            "market": "US",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "news_items": [
                {
                    "title": "NVIDIA AI demand boosts chip outlook",
                    "summary": "NVIDIA AI demand boosts chip outlook",
                    "url": "https://news.example/nvda-ai",
                    "source": "news",
                }
            ],
            "community_items": [
                {
                    "title": "继续看好 NVIDIA",
                    "summary": "继续看好 NVIDIA AI 需求仍强。",
                    "url": "https://feed.example/nvda",
                    "source": "community",
                }
            ],
            "post_count": 1,
            "relevant_post_count": 1,
        }
    ]
    assert result["evidence"] == [
        {
            "title": "NVIDIA AI demand boosts chip outlook",
            "summary": "NVIDIA AI demand boosts chip outlook",
            "url": "https://news.example/nvda-ai",
            "source": "news",
        },
        {
            "title": "继续看好 NVIDIA",
            "summary": "继续看好 NVIDIA AI 需求仍强。",
            "url": "https://feed.example/nvda",
            "source": "community",
        },
    ]
    assert calls == [
        {
            "url": "https://ai-news-search.futunn.com/news_search",
            "params": {
                "keyword": "NVIDIA",
                "size": 10,
                "news_type": 1,
                "lang": "zh-CN",
                "sort_type": 2,
            },
        },
            {
                "url": "https://ai-news-search.futunn.com/stock_feed",
                "params": {
                    "keyword": "NVDA",
                    "size": 30,
                },
            },
    ]


def test_futu_skill_facts_paths_are_market_scoped(tmp_path: Path) -> None:
    assert futu_skill_facts_run_path(
        tmp_path / "data",
        "2026-07-01",
        "US",
    ) == tmp_path / "data" / "runs" / "2026-07-01" / "US" / "futu_skill_facts.json"
    assert futu_skill_facts_latest_path(
        tmp_path / "data",
        "US",
    ) == tmp_path / "data" / "latest" / "US" / "futu_skill_facts.json"


def test_generate_futu_skill_facts_writes_news_sentiment_artifact(tmp_path: Path) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [
            {
                "market": "US",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "asset_class": "stock",
            }
        ],
    )
    extractor = FakeExtractor()

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-01",
        market="US",
        extractor=extractor,
        update_latest=True,
    )

    payload = json.loads(result.run_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == FUTU_SKILL_FACTS_SCHEMA_VERSION
    assert payload["run_date"] == "2026-07-01"
    assert payload["market"] == "US"
    assert payload["records"][0]["schema_version"] == FUTU_SKILL_FACTS_SCHEMA_VERSION
    assert payload["records"][0]["market"] == "US"
    assert payload["records"][0]["symbol"] == "NVDA"
    assert payload["records"][0]["news_sentiment"]["signal"] == "supportive"
    assert payload["records"][0]["news_sentiment"]["evidence"][0]["url"] == "https://example.com/nvda"
    assert result.records == 1
    assert result.generated == 1
    assert result.failed == 0
    assert result.latest_path.read_text(encoding="utf-8") == result.run_path.read_text(encoding="utf-8")
    assert extractor.calls == [
        {
            "market": "US",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "run_date": "2026-07-01",
        }
    ]


def test_generate_futu_skill_facts_writes_anomaly_modules(tmp_path: Path) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [
            {"market": "US", "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock"},
            {"market": "HK", "symbol": "00700", "name": "腾讯控股", "asset_class": "stock"},
        ],
    )
    extractor = FakeFullFutuSkillExtractor()

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market=None,
        extractor=extractor,
        update_latest=True,
        window_days=7,
    )

    payload = load_futu_skill_facts_cache(result.run_path)
    assert result.records == 2
    assert result.generated == 2
    assert result.failed == 0
    assert payload["schema_version"] == FUTU_SKILL_FACTS_SCHEMA_VERSION
    assert payload["window_days"] == 7
    nvda = next(record for record in payload["records"] if record["symbol"] == "NVDA")
    assert nvda["technical_anomaly"]["signal"] == "supportive"
    assert nvda["capital_anomaly"]["suggested_constraint"] == "no_add"
    assert nvda["derivatives_anomaly"]["categories"][0]["name"] == "期权波动率"
    assert [call["module"] for call in extractor.calls[:3]] == [
        "technical",
        "capital",
        "derivatives",
    ]
    assert all(call["window_days"] == 7 for call in extractor.calls)
    assert result.latest_path.read_text(encoding="utf-8") == result.run_path.read_text(encoding="utf-8")


def test_anomaly_category_templates_are_fixed() -> None:
    assert TECHNICAL_ANOMALY_CATEGORY_LABELS[:3] == ("K线形态", "MACD", "RSI")
    assert CAPITAL_ANOMALY_CATEGORY_LABELS == ("资金分布与买卖经纪商", "资金流向", "卖空情况")
    assert DERIVATIVES_ANOMALY_CATEGORY_LABELS_US == (
        "期权大单",
        "期权波动率",
        "期权量价",
        "期权情绪",
        "期权综合信号",
    )
    assert DERIVATIVES_ANOMALY_CATEGORY_LABELS_HK[:2] == (
        "牛熊证街货比例",
        "牛熊证街货价格区间",
    )


def test_futu_anomaly_script_client_invokes_expected_scripts(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str]) -> object:
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "method": "get_technical_unusual",
                    "stock_symbol": "US.NVDA",
                    "time_range": 7,
                    "data": [
                        {
                            "name": "MACD",
                            "direction": "bullish",
                            "date": "2026-07-01",
                            "description": "MACD 金叉",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    client = FutuAnomalyScriptClient(
        skill_root=tmp_path / "skills",
        runner=fake_runner,
    )

    payload = client.run("technical", market="US", symbol="NVDA", window_days=7)
    client.run("capital", market="HK", symbol="00700", window_days=14)
    client.run("derivatives", market="US", symbol="AAPL", window_days=3)

    assert payload["stock_symbol"] == "US.NVDA"
    assert calls[0][2:] == ["US.NVDA", "--time-range", "7", "--json"]
    assert "handle_technical_anomaly.py" in calls[0][1]
    assert calls[1][2:] == ["HK.00700", "--time-range", "14", "--json"]
    assert "handle_capital_anomaly.py" in calls[1][1]
    assert calls[2][2:] == ["US.AAPL", "--time-range", "3", "--json"]
    assert "handle_derivatives_anomaly.py" in calls[2][1]


def test_futu_anomaly_script_client_extracts_json_from_sdk_logs(
    tmp_path: Path,
) -> None:
    def fake_runner(command: list[str]) -> object:
        del command
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "2026-07-02 10:33:56 | [open_context_base.py:411] "
                "_init_connect_sync: New connect ready\n"
                '{\n  "method": "get_technical_unusual",\n'
                '  "stock_symbol": "US.DRAM",\n'
                '  "data": {"content": "MACD 金叉，包含 {括号} 文本"}\n'
                "}\n"
                "2026-07-02 10:33:58 | [open_context_base.py:521] "
                "on_disconnect: Disconnected\n"
            ),
            stderr="",
        )

    client = FutuAnomalyScriptClient(
        skill_root=tmp_path / "skills",
        runner=fake_runner,
    )

    payload = client.run("technical", market="US", symbol="DRAM", window_days=7)

    assert payload["stock_symbol"] == "US.DRAM"
    assert payload["data"]["content"] == "MACD 金叉，包含 {括号} 文本"


def test_futu_anomaly_script_client_reports_script_failure(tmp_path: Path) -> None:
    def fake_runner(command: list[str]) -> object:
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="get_technical_unusual error: no permission",
        )

    client = FutuAnomalyScriptClient(
        skill_root=tmp_path / "skills",
        runner=fake_runner,
    )

    with pytest.raises(RuntimeError, match="no permission"):
        client.run("technical", market="US", symbol="NVDA", window_days=7)


def test_futu_skill_facts_extractor_normalizes_fake_anomaly_payloads() -> None:
    class FakeAnomalyClient:
        def run(
            self,
            module: str,
            *,
            market: str,
            symbol: str,
            window_days: int,
        ) -> dict[str, object]:
            del market, symbol, window_days
            if module == "technical":
                return {
                    "data": [
                        {
                            "name": "MACD",
                            "direction": "bullish",
                            "date": "2026-07-01",
                            "description": "MACD 金叉",
                        },
                        {
                            "name": "RSI",
                            "direction": "risk_up",
                            "date": "2026-07-02",
                            "description": "RSI 接近超买",
                        },
                    ]
                }
            if module == "capital":
                return {
                    "data": [
                        {
                            "name": "资金流向",
                            "direction": "bearish",
                            "date": "2026-07-02",
                            "description": "主力资金连续净流出",
                        }
                    ]
                }
            return {
                "data": [
                    {
                        "name": "期权波动率",
                        "direction": "risk_up",
                        "date": "2026-07-02",
                        "description": "IV 位于高位",
                    }
                ]
            }

    extractor = FutuSkillFactsExtractor(
        news_extractor=FakeExtractor(),
        anomaly_client=FakeAnomalyClient(),
    )

    technical = extractor.extract_technical_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    capital = extractor.extract_capital_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    derivatives = extractor.extract_derivatives_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )

    assert technical["categories"][1]["name"] == "MACD"
    assert technical["categories"][1]["direction"] == "bullish"
    assert technical["categories"][2]["name"] == "RSI"
    assert technical["categories"][2]["direction"] == "risk_up"
    assert technical["signal"] == "mixed"
    assert technical["suggested_constraint"] == "no_add"
    assert capital["suggested_constraint"] == "no_add"
    assert derivatives["signal"] == "risk_up"


def test_futu_skill_facts_extractor_normalizes_sdk_content_payload() -> None:
    technical_content = "MACD 金叉，RSI 接近超买，风险上升"
    capital_content = "资金流向显示主力资金连续净流出"
    derivatives_content = "期权波动率 IV 位于高位，风险上升"

    class FakeAnomalyClient:
        def run(
            self,
            module: str,
            *,
            market: str,
            symbol: str,
            window_days: int,
        ) -> dict[str, object]:
            del market, symbol, window_days
            content_by_module = {
                "technical": technical_content,
                "capital": capital_content,
                "derivatives": derivatives_content,
            }
            return {
                "data": {
                    "err_code": 0,
                    "time_range": 7,
                    "content": content_by_module[module],
                }
            }

    extractor = FutuSkillFactsExtractor(
        news_extractor=FakeExtractor(),
        anomaly_client=FakeAnomalyClient(),
    )

    technical = extractor.extract_technical_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    capital = extractor.extract_capital_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    derivatives = extractor.extract_derivatives_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    categories = {item["name"]: item for item in technical["categories"]}
    capital_categories = {item["name"]: item for item in capital["categories"]}
    derivatives_categories = {
        item["name"]: item for item in derivatives["categories"]
    }

    assert categories["MACD"]["state"] == "anomaly"
    assert categories["MACD"]["detail"] == technical_content
    assert categories["RSI"]["state"] == "anomaly"
    assert categories["RSI"]["detail"] == technical_content
    assert categories["MA"]["state"] == "none"
    assert technical["signal"] != "neutral"
    assert any(item["state"] == "anomaly" for item in technical["categories"])
    assert capital_categories["资金流向"]["state"] == "anomaly"
    assert capital_categories["资金流向"]["detail"] == capital_content
    assert capital["signal"] != "neutral"
    assert derivatives_categories["期权波动率"]["state"] == "anomaly"
    assert derivatives_categories["期权波动率"]["detail"] == derivatives_content
    assert derivatives["signal"] != "neutral"


def test_futu_skill_facts_extractor_preserves_structured_sdk_row_details() -> None:
    class FakeAnomalyClient:
        def run(
            self,
            module: str,
            *,
            market: str,
            symbol: str,
            window_days: int,
        ) -> dict[str, object]:
            del market, symbol, window_days
            if module == "capital":
                return {
                    "data": [
                        {
                            "name": "资金流向",
                            "direction": "bullish",
                            "broker": "富途证券",
                            "net_inflow": "1234000",
                            "note": "卖空比例下降，资金净流入",
                        }
                    ]
                }
            return {"data": []}

    extractor = FutuSkillFactsExtractor(
        news_extractor=FakeExtractor(),
        anomaly_client=FakeAnomalyClient(),
    )

    capital = extractor.extract_capital_anomaly(
        market="US",
        symbol="NVDA",
        name="NVIDIA",
        run_date="2026-07-02",
        window_days=7,
    )
    category = next(
        item for item in capital["categories"] if item["name"] == "资金流向"
    )

    assert category["direction"] == "bullish"
    assert "富途证券" in category["detail"]
    assert "1234000" in category["detail"]
    assert "卖空比例下降" in category["detail"]
    assert category["detail"] != "资金流向"


def test_generate_futu_skill_facts_records_error_for_zero_anomaly_window_days(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [{"market": "US", "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock"}],
    )

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-02",
        market="US",
        extractor=FakeInvalidAnomalyWindowExtractor(),
        update_latest=False,
        window_days=7,
    )

    payload = load_futu_skill_facts_cache(result.run_path)
    record = payload["records"][0]
    assert result.records == 1
    assert result.generated == 0
    assert result.failed == 1
    assert record["technical_anomaly"]["status"] == "error"
    assert record["technical_anomaly"]["window_days"] == 7
    assert record["technical_anomaly"]["categories"][0]["name"] == "技术异动"
    assert "technical_anomaly: window_days must be between 1 and 30" in record["error"]


def test_generate_futu_skill_facts_skips_missing_symbols_and_cash(tmp_path: Path) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [
            {"market": "US", "symbol": "", "asset_class": "stock"},
            {"market": "US", "symbol": "USD", "asset_class": "cash"},
            {"market": "US", "symbol": "MSFT", "name": "Microsoft", "asset_class": "stock"},
        ],
    )
    extractor = FakeExtractor()

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-01",
        market="US",
        extractor=extractor,
        update_latest=False,
    )

    payload = load_futu_skill_facts_cache(result.run_path)
    assert [record["symbol"] for record in payload["records"]] == ["MSFT"]
    assert [call["symbol"] for call in extractor.calls] == ["MSFT"]


def test_generate_futu_skill_facts_assigns_shared_feed_posts_to_multiple_symbols(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [
            {"market": "US", "symbol": "MU", "name": "美光科技", "asset_class": "stock"},
            {"market": "US", "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock"},
            {"market": "US", "symbol": "DRAM", "name": "Roundhill Memory ETF", "asset_class": "stock"},
        ],
    )
    calls: list[dict[str, object]] = []
    publish_time = str(int(datetime(2026, 7, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()))

    def fake_get_json(url: str, params: dict[str, object]) -> dict[str, object]:
        calls.append({"url": url, "params": params})
        if url.endswith("/news_search"):
            return {"code": 0, "data": []}
        if url.endswith("/stock_feed"):
            return {
                "code": 0,
                "data": [
                    {
                        "id": "shared-memory-post",
                        "title": (
                            '<p><nnstock stocksymbol="MU.US" stockname="美光科技" '
                            'stockcode="MU">$美光科技 (MU.US)$</nnstock> '
                            '<nnstock stocksymbol="NVDA.US" stockname="英伟达" '
                            'stockcode="NVDA">$英伟达 (NVDA.US)$</nnstock> '
                            "AI 内存需求继续升温</p>"
                        ),
                        "desc": "美光和英伟达都受 AI 服务器需求影响，存储链震荡。",
                        "publish_time": publish_time,
                        "url": "https://feed.example/shared-memory-post",
                    }
                ],
            }
        raise AssertionError(f"unexpected URL {url}")

    summarizer = FakeDomesticSummarizer()
    extractor = FutuNewsSentimentExtractor(
        http_get_json=fake_get_json,
        domestic_summarizer=summarizer,
    )

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-01",
        market="US",
        extractor=extractor,
        update_latest=True,
    )

    assert result.failed == 0
    by_symbol = {call["symbol"]: call for call in summarizer.calls}
    assert sorted(by_symbol) == ["DRAM", "MU", "NVDA"]
    for symbol in ("MU", "NVDA", "DRAM"):
        assert by_symbol[symbol]["relevant_post_count"] == 1
        assert by_symbol[symbol]["community_items"][0]["url"] == "https://feed.example/shared-memory-post"
    stock_feed_calls = [call for call in calls if call["url"].endswith("/stock_feed")]
    assert [call["params"]["keyword"] for call in stock_feed_calls] == ["MU", "NVDA", "DRAM"]
    cache_path = tmp_path / "data/latest/US/futu_stock_feed_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(cache["items"]) == 1
    assert cache["items"][0]["id"] == "shared-memory-post"


def test_generate_futu_skill_facts_reuses_recent_feed_cache_when_api_snapshot_is_empty(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [{"market": "US", "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock"}],
    )
    cache_path = tmp_path / "data/latest/US/futu_stock_feed_cache.json"
    cache_path.parent.mkdir(parents=True)
    publish_time = str(int(datetime(2026, 6, 30, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()))
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": FUTU_SKILL_FACTS_SCHEMA_VERSION,
                "market": "US",
                "items": [
                    {
                        "id": "cached-nvda-post",
                        "title": "$NVIDIA (NVDA.US)$ AI 需求仍强",
                        "summary": "$NVIDIA (NVDA.US)$ AI 需求仍强",
                        "url": "https://feed.example/cached-nvda",
                        "source": "community",
                        "publish_time": publish_time,
                        "query_symbols": ["NVDA"],
                        "stock_terms": ["nvda", "nvidia"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_get_json(url: str, params: dict[str, object]) -> dict[str, object]:
        if url.endswith("/news_search"):
            return {"code": 0, "data": []}
        if url.endswith("/stock_feed"):
            return {"code": 0, "data": []}
        raise AssertionError(f"unexpected URL {url}")

    summarizer = FakeDomesticSummarizer()
    extractor = FutuNewsSentimentExtractor(
        http_get_json=fake_get_json,
        domestic_summarizer=summarizer,
    )

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-01",
        market="US",
        extractor=extractor,
        update_latest=False,
    )

    assert result.failed == 0
    assert summarizer.calls[0]["symbol"] == "NVDA"
    assert summarizer.calls[0]["post_count"] == 1
    assert summarizer.calls[0]["relevant_post_count"] == 1
    assert summarizer.calls[0]["community_items"][0]["url"] == "https://feed.example/cached-nvda"


def test_generate_futu_skill_facts_ignores_transient_stock_feed_prepare_failure(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(
        portfolio,
        [
            {"market": "US", "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock"},
            {"market": "US", "symbol": "MSFT", "name": "Microsoft", "asset_class": "stock"},
        ],
    )

    def fake_get_json(url: str, params: dict[str, object]) -> dict[str, object]:
        if url.endswith("/news_search"):
            return {
                "code": 0,
                "data": [{"title": f"{params['keyword']} news", "url": "https://news.example/item"}],
            }
        if url.endswith("/stock_feed") and params["keyword"] == "NVDA":
            raise OSError("temporary ssl failure")
        if url.endswith("/stock_feed"):
            return {"code": 0, "data": []}
        raise AssertionError(f"unexpected URL {url}")

    extractor = FutuNewsSentimentExtractor(
        http_get_json=fake_get_json,
        domestic_summarizer=FakeDomesticSummarizer(),
    )

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-01",
        market="US",
        extractor=extractor,
        update_latest=True,
    )

    payload = load_futu_skill_facts_cache(result.run_path)
    assert result.failed == 0
    assert [record["symbol"] for record in payload["records"]] == ["NVDA", "MSFT"]
    assert all(record["news_sentiment"]["status"] == "ok" for record in payload["records"])


def test_index_futu_skill_facts_by_market_symbol() -> None:
    record = valid_record()

    indexed = index_futu_skill_facts_by_market_symbol({"records": [record]})

    assert indexed == {("US", "NVDA"): record}


def test_validate_futu_skill_fact_record_rejects_invalid_module_status() -> None:
    record = valid_record()
    record["news_sentiment"]["status"] = "unknown"

    with pytest.raises(ValueError, match="news_sentiment status is invalid"):
        validate_futu_skill_fact_record(record)


def test_validate_futu_skill_fact_record_rejects_invalid_anomaly_category_state() -> None:
    record = valid_record()
    record["technical_anomaly"]["categories"][0]["state"] = "maybe"

    with pytest.raises(ValueError, match="technical_anomaly category state is invalid"):
        validate_futu_skill_fact_record(record)


def test_validate_futu_skill_fact_record_rejects_invalid_anomaly_window_days() -> None:
    record = valid_record()
    record["technical_anomaly"]["window_days"] = 999

    with pytest.raises(ValueError, match="window_days must be between 1 and 30"):
        validate_futu_skill_fact_record(record)


def test_futu_news_sentiment_extractor_marks_noisy_feed_as_unusable() -> None:
    def fake_get_json(url: str, params: dict[str, object]) -> dict[str, object]:
        if url.endswith("/news_search"):
            return {"code": 0, "data": []}
        if url.endswith("/stock_feed"):
            return {
                "code": 0,
                "data": [
                    {
                        "title": "$SPY.US$ unrelated market chatter",
                        "desc": "general comment",
                        "publish_time": "1782869556",
                    },
                    {
                        "title": "$DRAM.US$ 为什么 ETF 跌幅大于成分股？",
                        "desc": "",
                        "publish_time": "1782869555",
                    },
                    {
                        "title": "$SOXL.US$ 半导体 ETF 开始下跌趋势了吗？",
                        "desc": "",
                        "publish_time": "1782869554",
                    },
                ],
            }
        raise AssertionError(f"unexpected URL {url}")

    class BrokenSummarizer:
        def summarize(self, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("llm unavailable")

    extractor = FutuNewsSentimentExtractor(
        http_get_json=fake_get_json,
        domestic_summarizer=BrokenSummarizer(),
    )

    result = extractor.extract_news_sentiment(
        market="US",
        symbol="DRAM",
        name="Roundhill Memory ETF",
        run_date="2026-07-01",
    )

    assert result["domestic_discussion"] == {
        "status": "ok",
        "keyword_counts": [],
        "summary": "富途社区相关讨论较少，3 条 feed 中 1 条与 DRAM 明确相关。",
        "focus": "少量讨论关注 DRAM 的短线走势或 ETF 结构问题。",
        "divergence_risk": "社区样本少且噪声高，不能代表稳定共识。",
        "credibility": "噪声高",
        "trading_constraint": "仅作为国内讨论温度和 ETF 结构风险提示，不支持单独加仓或减仓。",
        "post_count": 3,
        "relevant_post_count": 1,
    }


def test_llm_domestic_discussion_summarizer_sends_fixed_schema_prompt() -> None:
    class FakeTextClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create(self, *, messages: list[dict[str, str]], temperature: float) -> str:
            self.calls.append({"messages": messages, "temperature": temperature})
            return json.dumps(
                {
                    "keyword_counts": [
                        {"keyword": "震荡", "count": 3},
                        {"keyword": "看空", "count": 2},
                        {"keyword": "损耗", "count": 1},
                    ],
                    "summary": "富途社区相关讨论较少，主要关注 ETF 与存储链成分股联动。",
                    "focus": "关注海力士、三星、美光对 DRAM ETF 的影响。",
                    "divergence_risk": "样本少且噪声高，不能代表稳定共识。",
                    "credibility": "低",
                    "trading_constraint": "仅作为国内讨论温度和 ETF 结构风险提示，不支持单独加仓或减仓。",
                },
                ensure_ascii=False,
            )

    client = FakeTextClient()
    summarizer = LLMFutuDomesticDiscussionSummarizer(client=client)

    result = summarizer.summarize(
        market="US",
        symbol="DRAM",
        name="Roundhill Memory ETF",
        news_items=[
            {
                "title": "DRAM ETF attracts AI memory flows",
                "summary": "DRAM ETF attracts AI memory flows",
                "url": "https://news.example/dram",
                "source": "news",
            }
        ],
        community_items=[
            {
                "title": "$DRAM.US$ 为什么比成分股跌得多？",
                "summary": "$DRAM.US$ 为什么比成分股跌得多？",
                "url": "",
                "source": "community",
            }
        ],
        post_count=30,
        relevant_post_count=1,
    )

    assert result == {
        "status": "ok",
        "keyword_counts": [
            {"keyword": "震荡", "count": 3},
            {"keyword": "看空", "count": 2},
            {"keyword": "损耗", "count": 1},
        ],
        "summary": "富途社区相关讨论较少，主要关注 ETF 与存储链成分股联动。",
        "focus": "关注海力士、三星、美光对 DRAM ETF 的影响。",
        "divergence_risk": "样本少且噪声高，不能代表稳定共识。",
        "credibility": "低",
        "trading_constraint": "仅作为国内讨论温度和 ETF 结构风险提示，不支持单独加仓或减仓。",
        "post_count": 30,
        "relevant_post_count": 1,
    }
    assert client.calls[0]["temperature"] == 0
    messages = client.calls[0]["messages"]
    assert "国内讨论结论" in messages[0]["content"]
    assert "keyword_counts" in messages[0]["content"]
    assert "每个关键词对应多少条相关社区帖子" in messages[0]["content"]
    assert "交易可读主题词" in messages[0]["content"]
    user_payload = json.loads(messages[1]["content"])
    assert user_payload["symbol"] == "DRAM"
    assert user_payload["community_items"][0]["summary"] == "$DRAM.US$ 为什么比成分股跌得多？"


def valid_record() -> dict[str, object]:
    return {
        "schema_version": FUTU_SKILL_FACTS_SCHEMA_VERSION,
        "run_date": "2026-07-01",
        "market": "US",
        "symbol": "NVDA",
        "name": "NVIDIA",
        "news_sentiment": {
            "status": "ok",
            "signal": "supportive",
            "confidence": "medium",
            "freshness": {
                "generated_at": "2026-07-01T09:10:00+08:00",
                "source_window": "latest",
            },
            "evidence": [
                {
                    "title": "NVIDIA news digest",
                    "summary": "AI 需求继续支持市场关注。",
                    "url": "https://example.com/nvda",
                }
            ],
            "blocking_reason": "",
            "suggested_constraint": "",
        },
        "technical_anomaly": {
            "status": "ok",
            "signal": "supportive",
            "confidence": "medium",
            "suggested_constraint": "",
            "window_days": 7,
            "summary": "技术信号支持趋势。",
            "categories": [
                {
                    "name": "MACD",
                    "state": "anomaly",
                    "direction": "bullish",
                    "detail": "MACD 金叉。",
                    "evidence_date": "2026-07-01",
                }
            ],
        },
        "capital_anomaly": {
            "status": "ok",
            "signal": "mixed",
            "confidence": "medium",
            "suggested_constraint": "no_add",
            "window_days": 7,
            "summary": "资金信号分歧。",
            "categories": [
                {
                    "name": "资金流向",
                    "state": "anomaly",
                    "direction": "bearish",
                    "detail": "主力资金净流出。",
                    "evidence_date": "2026-07-01",
                }
            ],
        },
        "derivatives_anomaly": {
            "status": "partial",
            "signal": "risk_up",
            "confidence": "low",
            "suggested_constraint": "review",
            "window_days": 7,
            "summary": "衍生品风险上升。",
            "categories": [
                {
                    "name": "期权波动率",
                    "state": "anomaly",
                    "direction": "risk_up",
                    "detail": "IV 位于高位。",
                    "evidence_date": "2026-07-01",
                }
            ],
        },
        "error": "",
    }
