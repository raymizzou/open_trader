from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RESEARCH_VIEW_SCHEMA = "dashboard.research_view.v1"
SESSION_SCHEMA = "open_trader.research_chat_session.v1"
FINAL_CONCLUSION_SCHEMA = "user.llm_conclusion.v1"


class ResearchChatError(RuntimeError):
    pass


def missing_research_view(market: str, symbol: str, *, error: str = "") -> dict[str, Any]:
    return {
        "schema_version": RESEARCH_VIEW_SCHEMA,
        "available": False,
        "market": market.strip().upper(),
        "symbol": symbol.strip().upper(),
        "research_date": "",
        "bundle_dir": "",
        "error": error,
        "tradingagents_conclusion": {"status": "missing", "content": ""},
        "user_llm_conclusion": {"status": "missing", "content": ""},
    }


def load_research_view_for_holding(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
) -> dict[str, Any]:
    market_key = market.strip().upper()
    symbol_key = symbol.strip().upper()
    bundle_dir = latest_research_bundle_dir(
        data_dir=data_dir,
        market=market_key,
        symbol=symbol_key,
    )
    if bundle_dir is None:
        return missing_research_view(market_key, symbol_key)

    path = bundle_dir / "dashboard_view.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return missing_research_view(
            market_key,
            symbol_key,
            error=f"invalid research view: {exc}",
        )
    if not isinstance(payload, dict):
        return missing_research_view(
            market_key,
            symbol_key,
            error="invalid research view: root is not object",
        )
    return normalize_research_view(
        payload,
        market=market_key,
        symbol=symbol_key,
        bundle_dir=bundle_dir,
    )


def latest_research_bundle_dir(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
) -> Path | None:
    research_root = data_dir / "research_data"
    candidates = [
        research_root / market.strip().upper() / symbol.strip().upper(),
        research_root / symbol.strip().upper(),
    ]
    dated_dirs: list[Path] = []
    for parent in candidates:
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and (child / "dashboard_view.json").is_file():
                dated_dirs.append(child)
    return max(dated_dirs, key=lambda path: path.name) if dated_dirs else None


def normalize_research_view(
    payload: dict[str, Any],
    *,
    market: str,
    symbol: str,
    bundle_dir: Path,
) -> dict[str, Any]:
    tradingagents = _normalized_conclusion(payload.get("tradingagents_conclusion"))
    user_llm = _normalized_conclusion(payload.get("user_llm_conclusion"))
    return {
        "schema_version": RESEARCH_VIEW_SCHEMA,
        "available": True,
        "market": str(payload.get("market") or market).strip().upper(),
        "symbol": str(payload.get("symbol") or symbol).strip().upper(),
        "research_date": str(payload.get("research_date") or bundle_dir.name),
        "bundle_dir": str(bundle_dir),
        "error": "",
        "tradingagents_conclusion": tradingagents,
        "user_llm_conclusion": user_llm,
    }


def _normalized_conclusion(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing", "content": ""}
    status = str(value.get("status") or "missing").strip() or "missing"
    content = str(value.get("content") or "").strip()
    normalized = {str(key): item for key, item in value.items() if isinstance(key, str)}
    normalized["status"] = status
    normalized["content"] = content
    if status != "present" or not content:
        normalized["status"] = "missing"
        normalized["content"] = ""
    return normalized
