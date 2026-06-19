from __future__ import annotations

import json
from pathlib import Path

from open_trader.research_chat import (
    missing_research_view,
    load_research_view_for_holding,
)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_research_view_prefers_latest_market_scoped_bundle(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    old_bundle = data_dir / "research_data" / "US" / "VIXY" / "2026-06-18"
    latest_bundle = data_dir / "research_data" / "US" / "VIXY" / "2026-06-19"
    write_json(
        old_bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "2026-06-18",
            "tradingagents_conclusion": {"status": "present", "content": "old"},
            "user_llm_conclusion": {"status": "missing", "content": ""},
        },
    )
    write_json(
        latest_bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "2026-06-19",
            "tradingagents_conclusion": {
                "status": "present",
                "content": "低配，当前动作为减仓。",
                "reason": "达到第一目标价。",
                "condition": "财报后复评。",
            },
            "user_llm_conclusion": {"status": "missing", "content": ""},
        },
    )

    view = load_research_view_for_holding(
        data_dir=data_dir,
        market="US",
        symbol="VIXY",
    )

    assert view["available"] is True
    assert view["bundle_dir"].endswith("data/research_data/US/VIXY/2026-06-19")
    assert view["research_date"] == "2026-06-19"
    assert view["tradingagents_conclusion"]["content"] == "低配，当前动作为减仓。"
    assert view["user_llm_conclusion"] == {"status": "missing", "content": ""}


def test_load_research_view_supports_symbol_scoped_legacy_bundle(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bundle = data_dir / "research_data" / "VIXY" / "2026-06-19"
    write_json(
        bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "2026-06-19",
            "tradingagents_conclusion": {"status": "present", "content": "legacy"},
            "user_llm_conclusion": {"status": "missing", "content": ""},
        },
    )

    view = load_research_view_for_holding(
        data_dir=data_dir,
        market="US",
        symbol="VIXY",
    )

    assert view["available"] is True
    assert view["bundle_dir"].endswith("data/research_data/VIXY/2026-06-19")
    assert view["tradingagents_conclusion"]["content"] == "legacy"


def test_missing_research_view_is_explicit() -> None:
    assert missing_research_view("US", "VIXY") == {
        "schema_version": "dashboard.research_view.v1",
        "available": False,
        "market": "US",
        "symbol": "VIXY",
        "research_date": "",
        "bundle_dir": "",
        "error": "",
        "tradingagents_conclusion": {"status": "missing", "content": ""},
        "user_llm_conclusion": {"status": "missing", "content": ""},
    }


def test_invalid_research_view_does_not_raise(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "research_data" / "US" / "VIXY" / "2026-06-19" / "dashboard_view.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    view = load_research_view_for_holding(
        data_dir=data_dir,
        market="US",
        symbol="VIXY",
    )

    assert view["available"] is False
    assert view["error"].startswith("invalid research view:")
    assert view["tradingagents_conclusion"] == {"status": "missing", "content": ""}
