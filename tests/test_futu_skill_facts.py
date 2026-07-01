from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from open_trader.futu_skill_facts import (
    FUTU_SKILL_FACTS_SCHEMA_VERSION,
    FutuNewsSentimentExtractor,
    FutuSkillNewsSentimentExtractor,
    LLMFutuDomesticDiscussionSummarizer,
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


def test_index_futu_skill_facts_by_market_symbol() -> None:
    record = valid_record()

    indexed = index_futu_skill_facts_by_market_symbol({"records": [record]})

    assert indexed == {("US", "NVDA"): record}


def test_validate_futu_skill_fact_record_rejects_invalid_module_status() -> None:
    record = valid_record()
    record["news_sentiment"]["status"] = "unknown"

    with pytest.raises(ValueError, match="news_sentiment status is invalid"):
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
        "error": "",
    }
