# LLM Research Chat Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local dashboard workflow that shows TradingAgents' original conclusion, lets the user discuss that context with an LLM, and writes a validated user/LLM final conclusion back to the dashboard.

**Architecture:** Keep the dashboard as the existing Python stdlib HTTP server plus static HTML/CSS/JS. Add one focused Python module, `open_trader.research_chat`, for research bundle discovery, session persistence, LLM calls, finalization validation, and dashboard-view writes. Extend `load_dashboard_state()` to attach `research_view` to each holding, extend `dashboard_web.py` with local JSON APIs, and render the approved two-card conclusion section plus modal chat in the current symbol detail page.

**Tech Stack:** Python 3.12, stdlib `http.server`, stdlib JSON/file persistence, OpenAI-compatible DeepSeek chat completions, pytest, static JavaScript/CSS, Node `vm` checks, Playwright with the local Chrome executable for final browser verification.

---

## File Structure

- Create `src/open_trader/research_chat.py`
  - Owns all research-bundle and chat-session behavior.
  - Exposes `ResearchChatService`, `ResearchChatError`, `ResearchChatLLM`, `DeepSeekResearchChatClient`, and helper functions used by dashboard loading.
  - Reads `data/research_data/<market>/<symbol>/<date>/dashboard_view.json`.
  - Falls back to `data/research_data/<symbol>/<date>/dashboard_view.json` for bundles produced before market-scoped export exists.
  - Writes sessions under `data/research_chat/sessions/`.
- Modify `src/open_trader/dashboard.py`
  - Calls `load_research_view_for_holding()` while merging each holding.
  - Keeps missing or invalid research data local to `holding["research_view"]`; `/api/dashboard` must remain usable.
- Modify `src/open_trader/dashboard_web.py`
  - Adds POST routes for chat session creation, chat messages, and finalization.
  - Adds GET route for reading a session.
  - Keeps existing `GET /`, `/api/dashboard`, and `/api/quotes` behavior unchanged.
- Modify `src/open_trader/dashboard_static/index.html`
  - Adds one chat modal shell near the end of `<body>`.
- Modify `src/open_trader/dashboard_static/dashboard.js`
  - Renders two conclusion cards from `holding.research_view`.
  - Opens/resumes chat from the second card.
  - Sends messages and finalization requests to the new local APIs.
  - Refreshes `/api/dashboard` after successful finalization.
- Modify `src/open_trader/dashboard_static/dashboard.css`
  - Adds the two-card conclusion grid and compact chat modal styling based on the approved mock.
- Modify `tests/test_research_chat.py`
  - Covers bundle discovery, missing bundle shape, session creation, message persistence, valid finalization, invalid finalization, and dashboard-view updates.
- Modify `tests/test_dashboard.py`
  - Covers `research_view` attached to holdings and dashboard resilience when the research bundle is absent or invalid.
- Modify `tests/test_dashboard_web.py`
  - Covers new HTTP routes, static shell contents, and JavaScript helper behavior.
- Modify `README.md` and `README.zh-CN.md`
  - Documents local research data layout, chat/finalize behavior, and the fact that the dashboard remains read-only for orders.

---

### Task 1: Research Bundle Discovery And Dashboard Payload

**Files:**
- Create: `src/open_trader/research_chat.py`
- Modify: `src/open_trader/dashboard.py`
- Test: `tests/test_research_chat.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing tests for research bundle loading**

Create `tests/test_research_chat.py` with:

```python
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
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_chat.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.research_chat'`.

- [ ] **Step 3: Implement research bundle loader**

Create `src/open_trader/research_chat.py` with this initial content:

```python
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
```

- [ ] **Step 4: Verify research loader tests pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_chat.py -v
```

Expected: PASS for all tests in `tests/test_research_chat.py`.

- [ ] **Step 5: Write failing dashboard payload test**

In `tests/test_dashboard.py`, append:

```python
def test_load_dashboard_state_attaches_research_view(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    bundle = config.data_dir / "research_data" / "US" / "VIXY" / "2026-06-19"
    bundle.mkdir(parents=True)
    (bundle / "dashboard_view.json").write_text(
        json.dumps(
            {
                "schema_version": "dashboard.research_view.v1",
                "market": "US",
                "symbol": "VIXY",
                "research_date": "2026-06-19",
                "tradingagents_conclusion": {
                    "status": "present",
                    "content": "低配，当前动作为减仓。",
                },
                "user_llm_conclusion": {"status": "missing", "content": ""},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["research_view"]["available"] is True
    assert vixy["research_view"]["research_date"] == "2026-06-19"
    assert (
        vixy["research_view"]["tradingagents_conclusion"]["content"]
        == "低配，当前动作为减仓。"
    )
    assert vixy["research_view"]["user_llm_conclusion"] == {
        "status": "missing",
        "content": "",
    }


def test_load_dashboard_state_marks_missing_research_view(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["research_view"]["available"] is False
    assert vixy["research_view"]["tradingagents_conclusion"] == {
        "status": "missing",
        "content": "",
    }
```

Also add `import json` near the top of `tests/test_dashboard.py`.

- [ ] **Step 6: Run dashboard tests and verify new test fails**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py::test_load_dashboard_state_attaches_research_view tests/test_dashboard.py::test_load_dashboard_state_marks_missing_research_view -v
```

Expected: FAIL with `KeyError: 'research_view'`.

- [ ] **Step 7: Attach research view in dashboard loader**

In `src/open_trader/dashboard.py`, add this import:

```python
from .research_chat import load_research_view_for_holding
```

Update `_merge_holding()` signature:

```python
def _merge_holding(
    row: dict[str, str],
    data_dir: Path,
    positions_by_holding: dict[tuple[str, str], list[dict[str, str]]],
    agent_reports_by_holding: dict[tuple[str, str], dict[str, str]],
    strategies_by_holding: dict[tuple[str, str], dict[str, str]],
    premarket_actions_by_holding: dict[tuple[str, str], dict[str, str]],
    actions_by_holding: dict[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
```

Update the call in `load_dashboard_state()`:

```python
        _merge_holding(
            row,
            config.data_dir,
            positions_by_holding,
            agent_reports_by_holding,
            strategies_by_holding,
            premarket_actions_by_holding,
            actions_by_holding,
        )
```

Add this assignment before `return holding` in `_merge_holding()`:

```python
    holding["research_view"] = (
        load_research_view_for_holding(
            data_dir=data_dir,
            market=key[0],
            symbol=key[1],
        )
        if key is not None
        else load_research_view_for_holding(
            data_dir=data_dir,
            market=row.get("market", ""),
            symbol=row.get("symbol", ""),
        )
    )
```

- [ ] **Step 8: Run dashboard and research loader tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_chat.py tests/test_dashboard.py -v
```

Expected: PASS for both test files.

- [ ] **Step 9: Commit Task 1**

Run:

```bash
git add src/open_trader/research_chat.py src/open_trader/dashboard.py tests/test_research_chat.py tests/test_dashboard.py
git commit -m "feat: attach research views to dashboard holdings"
```

Expected: commit succeeds with only Task 1 files.

---

### Task 2: Chat Session Persistence And Finalization

**Files:**
- Modify: `src/open_trader/research_chat.py`
- Test: `tests/test_research_chat.py`

- [ ] **Step 1: Add failing chat-session tests**

Append to `tests/test_research_chat.py`:

```python
import pytest

from open_trader.research_chat import (
    ResearchChatError,
    ResearchChatService,
)


class FakeLLM:
    def __init__(self, *, reply: str = "assistant reply", final: str = "") -> None:
        self.reply = reply
        self.final = final or json.dumps(
            {
                "schema_version": "user.llm_conclusion.v1",
                "status": "present",
                "content": "确认减仓 100 股，但保留复评窗口。",
                "updated_at": "2026-06-20T10:40:00+08:00",
                "source": "downstream_llm_conversation",
            },
            ensure_ascii=False,
        )
        self.chat_calls: list[dict[str, object]] = []
        self.finalize_calls: list[dict[str, object]] = []

    def chat(self, *, system_prompt: str, combined_input: dict[str, object], messages: list[dict[str, str]]) -> str:
        self.chat_calls.append(
            {
                "system_prompt": system_prompt,
                "combined_input": combined_input,
                "messages": messages,
            }
        )
        return self.reply

    def finalize(self, *, system_prompt: str, combined_input: dict[str, object], messages: list[dict[str, str]]) -> str:
        self.finalize_calls.append(
            {
                "system_prompt": system_prompt,
                "combined_input": combined_input,
                "messages": messages,
            }
        )
        return self.final


def write_bundle(data_dir: Path, *, market: str = "US", symbol: str = "VIXY") -> Path:
    bundle = data_dir / "research_data" / market / symbol / "2026-06-19"
    write_json(
        bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": market,
            "symbol": symbol,
            "research_date": "2026-06-19",
            "tradingagents_conclusion": {
                "status": "present",
                "content": "低配，当前动作为减仓。",
            },
            "user_llm_conclusion": {"status": "missing", "content": ""},
        },
    )
    write_json(
        bundle / "combined_input.json",
        {
            "schema_version": "combined_input.v1",
            "holding": {"market": market, "symbol": symbol},
        },
    )
    (bundle / "llm_system_prompt.md").write_text(
        "你是投研讨论助手。",
        encoding="utf-8",
    )
    return bundle


def test_research_chat_service_creates_session_with_loaded_context(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bundle = write_bundle(data_dir)
    service = ResearchChatService(data_dir=data_dir, llm=FakeLLM())

    session = service.create_session(market="US", symbol="VIXY")

    assert session["schema_version"] == "open_trader.research_chat_session.v1"
    assert session["market"] == "US"
    assert session["symbol"] == "VIXY"
    assert session["research_bundle_dir"] == str(bundle)
    assert session["messages"] == []
    assert (data_dir / "research_chat" / "sessions" / f"{session['session_id']}.json").is_file()


def test_research_chat_service_appends_message_and_assistant_reply(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_bundle(data_dir)
    llm = FakeLLM(reply="可以先减仓并保留复评窗口。")
    service = ResearchChatService(data_dir=data_dir, llm=llm)
    session = service.create_session(market="US", symbol="VIXY")

    updated = service.append_message(
        session_id=session["session_id"],
        content="如果财报超预期怎么办？",
    )

    assert [message["role"] for message in updated["messages"]] == ["user", "assistant"]
    assert updated["messages"][0]["content"] == "如果财报超预期怎么办？"
    assert updated["messages"][1]["content"] == "可以先减仓并保留复评窗口。"
    assert llm.chat_calls[0]["system_prompt"] == "你是投研讨论助手。"


def test_research_chat_service_finalizes_and_updates_dashboard_view(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bundle = write_bundle(data_dir)
    service = ResearchChatService(data_dir=data_dir, llm=FakeLLM())
    session = service.create_session(market="US", symbol="VIXY")
    service.append_message(session_id=session["session_id"], content="请给最终结论。")

    payload = service.finalize_session(session_id=session["session_id"])

    assert payload["status"] == "ok"
    assert payload["conclusion"]["schema_version"] == "user.llm_conclusion.v1"
    conclusion_path = bundle / "user_llm_conclusion.json"
    assert conclusion_path.is_file()
    dashboard_view = json.loads((bundle / "dashboard_view.json").read_text(encoding="utf-8"))
    assert dashboard_view["user_llm_conclusion"]["content"] == "确认减仓 100 股，但保留复评窗口。"
    assert dashboard_view["user_llm_conclusion"]["conversation_reference"].endswith(
        f"{session['session_id']}.json"
    )


def test_research_chat_service_rejects_invalid_finalization_json(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bundle = write_bundle(data_dir)
    service = ResearchChatService(data_dir=data_dir, llm=FakeLLM(final="not json"))
    session = service.create_session(market="US", symbol="VIXY")
    service.append_message(session_id=session["session_id"], content="请给最终结论。")

    with pytest.raises(ResearchChatError, match="最终结论格式无效"):
        service.finalize_session(session_id=session["session_id"])

    dashboard_view = json.loads((bundle / "dashboard_view.json").read_text(encoding="utf-8"))
    assert dashboard_view["user_llm_conclusion"] == {"status": "missing", "content": ""}


def test_research_chat_service_requires_context_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bundle = data_dir / "research_data" / "US" / "VIXY" / "2026-06-19"
    write_json(
        bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "2026-06-19",
            "tradingagents_conclusion": {"status": "present", "content": "x"},
            "user_llm_conclusion": {"status": "missing", "content": ""},
        },
    )
    service = ResearchChatService(data_dir=data_dir, llm=FakeLLM())

    with pytest.raises(ResearchChatError, match="missing research context file"):
        service.create_session(market="US", symbol="VIXY")
```

- [ ] **Step 2: Run new tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_chat.py -v
```

Expected: FAIL because `ResearchChatService` is not implemented.

- [ ] **Step 3: Implement service interfaces**

In `src/open_trader/research_chat.py`, add imports:

```python
import os
from dataclasses import dataclass
from datetime import datetime
from tempfile import NamedTemporaryFile
from typing import Protocol

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL
```

Add these classes below `_normalized_conclusion()`:

```python
class ResearchChatLLM(Protocol):
    def chat(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
    ) -> str:
        pass

    def finalize(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
    ) -> str:
        pass


class DeepSeekResearchChatClient:
    def __init__(
        self,
        *,
        model: str = DEFAULT_CLASSIFIER_MODEL,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY"),
            base_url=base_url,
        )
        self._model = model

    def chat(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
    ) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"context": combined_input, "messages": messages},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise ResearchChatError("LLM 返回了空内容")
        return content.strip()

    def finalize(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
    ) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "context": combined_input,
                            "messages": messages,
                            "output_schema": FINAL_CONCLUSION_SCHEMA,
                            "instruction": (
                                "基于上下文和对话，输出一个 JSON object。"
                                "schema_version 必须是 user.llm_conclusion.v1，"
                                "status 必须是 present，content 必须是中文最终结论。"
                            ),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ResearchChatError("LLM 返回了空最终结论")
        return content.strip()
```

Add the service:

```python
@dataclass
class ResearchChatService:
    data_dir: Path
    llm: ResearchChatLLM | None = None

    def __post_init__(self) -> None:
        if self.llm is None:
            self.llm = DeepSeekResearchChatClient()

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "research_chat" / "sessions"

    def create_session(self, *, market: str, symbol: str) -> dict[str, Any]:
        market_key = market.strip().upper()
        symbol_key = symbol.strip().upper()
        bundle_dir = latest_research_bundle_dir(
            data_dir=self.data_dir,
            market=market_key,
            symbol=symbol_key,
        )
        if bundle_dir is None:
            raise ResearchChatError("暂无投研上下文，无法开始讨论")
        self._load_context(bundle_dir)
        now = datetime.now().astimezone().replace(microsecond=0)
        session_id = f"{now.strftime('%Y%m%dT%H%M%S')}-{market_key}-{symbol_key}"
        session = {
            "schema_version": SESSION_SCHEMA,
            "session_id": session_id,
            "market": market_key,
            "symbol": symbol_key,
            "research_bundle_dir": str(bundle_dir),
            "status": "active",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "messages": [],
        }
        self._write_session(session)
        return session

    def get_session(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        if not path.is_file():
            raise ResearchChatError("chat session not found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ResearchChatError("chat session is invalid")
        return payload

    def append_message(self, *, session_id: str, content: str) -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise ResearchChatError("message content is empty")
        session = self.get_session(session_id)
        bundle_dir = Path(str(session["research_bundle_dir"]))
        system_prompt, combined_input = self._load_context(bundle_dir)
        messages = _session_messages(session)
        messages.append({"role": "user", "content": text})
        assert self.llm is not None
        reply = self.llm.chat(
            system_prompt=system_prompt,
            combined_input=combined_input,
            messages=messages,
        ).strip()
        if not reply:
            raise ResearchChatError("LLM 返回了空内容")
        messages.append({"role": "assistant", "content": reply})
        session["messages"] = messages
        session["updated_at"] = datetime.now().astimezone().replace(microsecond=0).isoformat()
        self._write_session(session)
        return session

    def finalize_session(self, *, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        messages = _session_messages(session)
        if len(messages) < 2:
            raise ResearchChatError("没有足够的对话内容，无法生成最终结论")
        bundle_dir = Path(str(session["research_bundle_dir"]))
        system_prompt, combined_input = self._load_context(bundle_dir)
        assert self.llm is not None
        raw = self.llm.finalize(
            system_prompt=system_prompt,
            combined_input=combined_input,
            messages=messages,
        )
        conclusion = _parse_final_conclusion(raw)
        conclusion["conversation_reference"] = str(self._session_path(session_id))
        _write_json_atomic(bundle_dir / "user_llm_conclusion.json", conclusion)
        dashboard_view = json.loads((bundle_dir / "dashboard_view.json").read_text(encoding="utf-8"))
        if not isinstance(dashboard_view, dict):
            raise ResearchChatError("dashboard_view.json is invalid")
        dashboard_view["user_llm_conclusion"] = conclusion
        _write_json_atomic(bundle_dir / "dashboard_view.json", dashboard_view)
        session["status"] = "finalized"
        session["updated_at"] = datetime.now().astimezone().replace(microsecond=0).isoformat()
        self._write_session(session)
        return {
            "status": "ok",
            "conclusion": conclusion,
            "dashboard_view": normalize_research_view(
                dashboard_view,
                market=str(session["market"]),
                symbol=str(session["symbol"]),
                bundle_dir=bundle_dir,
            ),
        }

    def _load_context(self, bundle_dir: Path) -> tuple[str, dict[str, object]]:
        prompt_path = bundle_dir / "llm_system_prompt.md"
        input_path = bundle_dir / "combined_input.json"
        for path in (prompt_path, input_path):
            if not path.is_file():
                raise ResearchChatError(f"missing research context file: {path.name}")
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        combined_input = json.loads(input_path.read_text(encoding="utf-8"))
        if not system_prompt:
            raise ResearchChatError("missing research context file: llm_system_prompt.md")
        if not isinstance(combined_input, dict):
            raise ResearchChatError("combined_input.json is invalid")
        return system_prompt, combined_input

    def _session_path(self, session_id: str) -> Path:
        safe_id = session_id.strip()
        if "/" in safe_id or "\\" in safe_id or not safe_id:
            raise ResearchChatError("chat session not found")
        return self.sessions_dir / f"{safe_id}.json"

    def _write_session(self, session: dict[str, Any]) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self._session_path(str(session["session_id"])), session)
```

Add helpers:

```python
def _session_messages(session: dict[str, Any]) -> list[dict[str, str]]:
    raw_messages = session.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ResearchChatError("chat session messages are invalid")
    messages: list[dict[str, str]] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise ResearchChatError("chat session messages are invalid")
        role = str(raw.get("role") or "").strip()
        content = str(raw.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            raise ResearchChatError("chat session messages are invalid")
        messages.append({"role": role, "content": content})
    return messages


def _parse_final_conclusion(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchChatError("最终结论格式无效，请重试") from exc
    if not isinstance(payload, dict):
        raise ResearchChatError("最终结论格式无效，请重试")
    if payload.get("schema_version") != FINAL_CONCLUSION_SCHEMA:
        raise ResearchChatError("最终结论格式无效，请重试")
    if payload.get("status") != "present":
        raise ResearchChatError("最终结论格式无效，请重试")
    content = str(payload.get("content") or "").strip()
    if not content:
        raise ResearchChatError("最终结论格式无效，请重试")
    normalized = {str(key): value for key, value in payload.items() if isinstance(key, str)}
    normalized["content"] = content
    return normalized


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)
```

- [ ] **Step 4: Run research chat tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_chat.py -v
```

Expected: PASS for all research chat tests.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add src/open_trader/research_chat.py tests/test_research_chat.py
git commit -m "feat: persist research chat sessions"
```

Expected: commit succeeds with only Task 2 files.

---

### Task 3: Local Dashboard Chat APIs

**Files:**
- Modify: `src/open_trader/dashboard_web.py`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add failing API tests**

In `tests/test_dashboard_web.py`, add imports:

```python
from http import HTTPStatus
```

Add helpers near `read_json()`:

```python
def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        return json.loads(response.read().decode("utf-8"))
```

Add fake service:

```python
class FakeResearchChatService:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.messages: list[dict[str, str]] = []
        self.finalized: list[str] = []

    def create_session(self, *, market: str, symbol: str) -> dict[str, Any]:
        self.created.append({"market": market, "symbol": symbol})
        return {
            "schema_version": "open_trader.research_chat_session.v1",
            "session_id": "20260620T103000-US-VIXY",
            "market": market,
            "symbol": symbol,
            "research_bundle_dir": "data/research_data/US/VIXY/2026-06-19",
            "status": "active",
            "created_at": "2026-06-20T10:30:00+08:00",
            "updated_at": "2026-06-20T10:30:00+08:00",
            "messages": [],
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        return {
            "schema_version": "open_trader.research_chat_session.v1",
            "session_id": session_id,
            "market": "US",
            "symbol": "VIXY",
            "research_bundle_dir": "data/research_data/US/VIXY/2026-06-19",
            "status": "active",
            "created_at": "2026-06-20T10:30:00+08:00",
            "updated_at": "2026-06-20T10:30:00+08:00",
            "messages": [],
        }

    def append_message(self, *, session_id: str, content: str) -> dict[str, Any]:
        self.messages.append({"session_id": session_id, "content": content})
        return {
            **self.get_session(session_id),
            "messages": [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "assistant reply"},
            ],
        }

    def finalize_session(self, *, session_id: str) -> dict[str, Any]:
        self.finalized.append(session_id)
        return {
            "status": "ok",
            "conclusion": {
                "schema_version": "user.llm_conclusion.v1",
                "status": "present",
                "content": "确认减仓 100 股。",
            },
            "dashboard_view": {
                "schema_version": "dashboard.research_view.v1",
                "available": True,
                "market": "US",
                "symbol": "VIXY",
            },
        }
```

Add test:

```python
def test_dashboard_server_serves_research_chat_apis(tmp_path) -> None:
    from open_trader.dashboard_web import create_dashboard_server

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])
    chat_service = FakeResearchChatService()
    server = create_dashboard_server(
        config=config,
        host="127.0.0.1",
        port=0,
        quote_service=FakeQuoteService(quote_result()),
        research_chat_service=chat_service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"
        session = post_json(
            f"{base}/api/research-chat/sessions",
            {"market": "US", "symbol": "VIXY"},
        )
        loaded = read_json(f"{base}/api/research-chat/sessions/{session['session_id']}")
        message_payload = post_json(
            f"{base}/api/research-chat/sessions/{session['session_id']}/messages",
            {"content": "请解释风险。"},
        )
        finalize_payload = post_json(
            f"{base}/api/research-chat/sessions/{session['session_id']}/finalize",
            {},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert session["session_id"] == "20260620T103000-US-VIXY"
    assert loaded["session_id"] == "20260620T103000-US-VIXY"
    assert message_payload["messages"][1]["content"] == "assistant reply"
    assert finalize_payload["conclusion"]["content"] == "确认减仓 100 股。"
    assert chat_service.created == [{"market": "US", "symbol": "VIXY"}]
    assert chat_service.messages == [
        {"session_id": "20260620T103000-US-VIXY", "content": "请解释风险。"}
    ]
    assert chat_service.finalized == ["20260620T103000-US-VIXY"]
```

- [ ] **Step 2: Run API test and verify it fails**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_server_serves_research_chat_apis -v
```

Expected: FAIL because `create_dashboard_server()` has no `research_chat_service` argument.

- [ ] **Step 3: Add API support**

In `src/open_trader/dashboard_web.py`, add:

```python
from .research_chat import ResearchChatError, ResearchChatService
```

Update `create_dashboard_server()` signature:

```python
def create_dashboard_server(
    config: DashboardConfig,
    host: str,
    port: int,
    quote_service: DashboardQuoteService | None = None,
    research_chat_service: ResearchChatService | None = None,
) -> ThreadingHTTPServer:
```

After `service = ...`, add:

```python
    chat_service = research_chat_service or ResearchChatService(data_dir=config.data_dir)
```

Inside `DashboardRequestHandler`, add `do_POST()`:

```python
        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                if path == "/api/research-chat/sessions":
                    payload = self._read_json_body()
                    self._send_json(
                        chat_service.create_session(
                            market=str(payload.get("market") or ""),
                            symbol=str(payload.get("symbol") or ""),
                        )
                    )
                    return
                if path.startswith("/api/research-chat/sessions/"):
                    parts = path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] == "messages":
                        payload = self._read_json_body()
                        self._send_json(
                            chat_service.append_message(
                                session_id=parts[2],
                                content=str(payload.get("content") or ""),
                            )
                        )
                        return
                    if len(parts) == 4 and parts[3] == "finalize":
                        self._read_json_body()
                        self._send_json(chat_service.finalize_session(session_id=parts[2]))
                        return
            except Exception as exc:
                self._send_error_json(exc)
                return
            self._send_not_found()
```

Inside `do_GET()`, before `_send_not_found()`:

```python
            if path.startswith("/api/research-chat/sessions/"):
                session_id = path.rsplit("/", 1)[-1]
                try:
                    self._send_json(chat_service.get_session(session_id))
                except Exception as exc:
                    self._send_error_json(exc)
                return
```

Add `_read_json_body()`:

```python
        def _read_json_body(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(content_length) if content_length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ResearchChatError("request body must be a JSON object")
            return payload
```

- [ ] **Step 4: Run dashboard web tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -v
```

Expected: PASS for all dashboard web tests.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add src/open_trader/dashboard_web.py tests/test_dashboard_web.py
git commit -m "feat: add research chat dashboard APIs"
```

Expected: commit succeeds with only Task 3 files.

---

### Task 4: Frontend Two-Card Conclusion And Chat Modal

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add failing static shell assertions**

In `tests/test_dashboard_web.py`, inside `test_dashboard_static_assets_include_local_shell()`, add:

```python
    assert "research-chat-modal" in html
    assert "research-chat-messages" in html
    assert "research-chat-input" in html
    assert "生成最终结论" in html
    assert "renderResearchConclusions" in js
    assert "openResearchChat" in js
    assert "sendResearchChatMessage" in js
    assert "finalizeResearchChat" in js
    assert "投研给出的结论" in js
    assert "我和 LLM 探讨后的结论" in js
    assert ".research-conclusion-grid" in css
    assert ".research-chat-layer" in css
```

- [ ] **Step 2: Run static shell test and verify it fails**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: FAIL because the modal and research helpers are absent.

- [ ] **Step 3: Add chat modal HTML**

In `src/open_trader/dashboard_static/index.html`, add this before `</body>`:

```html
    <div id="research-chat-layer" class="research-chat-layer hidden" hidden role="dialog" aria-modal="true" aria-label="LLM 深度讨论">
      <section class="research-chat-modal">
        <header class="research-chat-header">
          <div>
            <h2 id="research-chat-title">LLM 深度讨论</h2>
            <p id="research-chat-context-note">上下文已自动加载。</p>
          </div>
          <button id="research-chat-close" class="raw-toggle" type="button">关闭</button>
        </header>
        <div class="research-chat-body">
          <aside class="research-chat-context">
            <span class="status-pill status-ok">上下文已加载</span>
            <dl id="research-chat-context-list"></dl>
          </aside>
          <div id="research-chat-messages" class="research-chat-messages"></div>
        </div>
        <footer class="research-chat-footer">
          <div class="research-chat-input-row">
            <input id="research-chat-input" type="text" autocomplete="off" aria-label="输入讨论消息">
            <button id="research-chat-send" class="primary-button" type="button">发送</button>
          </div>
          <div class="research-chat-action-row">
            <span id="research-chat-status">多轮对话不会自动写入看板。</span>
            <button id="research-chat-finalize" class="raw-toggle" type="button" disabled>生成最终结论</button>
          </div>
        </footer>
      </section>
    </div>
```

- [ ] **Step 4: Add frontend state and event bindings**

In `src/open_trader/dashboard_static/dashboard.js`, extend `state`:

```javascript
  researchChat: {
    holdingKey: "",
    sessionId: "",
    busy: false,
  },
```

In `bindElements()`, add these ids to the list:

```javascript
    "research-chat-layer",
    "research-chat-title",
    "research-chat-context-note",
    "research-chat-context-list",
    "research-chat-messages",
    "research-chat-input",
    "research-chat-send",
    "research-chat-close",
    "research-chat-finalize",
    "research-chat-status",
```

In `bindEvents()`, add:

```javascript
  elements["research-chat-close"].addEventListener("click", closeResearchChat);
  elements["research-chat-send"].addEventListener("click", sendResearchChatMessage);
  elements["research-chat-finalize"].addEventListener("click", finalizeResearchChat);
  elements["research-chat-input"].addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendResearchChatMessage();
    }
  });
```

In the `symbol-detail-panel` click handler, before raw-report handling, add:

```javascript
    const chatButton = event.target.closest("[data-research-chat]");
    if (chatButton) {
      openResearchChat(chatButton.dataset.researchChat || "");
      return;
    }
```

- [ ] **Step 5: Replace final conclusion renderer**

Replace `renderFinalConclusion(holding)` with:

```javascript
function renderFinalConclusion(holding) {
  return renderResearchConclusions(holding);
}

function renderResearchConclusions(holding) {
  const researchView = holding.research_view || {};
  const original = researchConclusion(researchView.tradingagents_conclusion);
  const userConclusion = researchConclusion(researchView.user_llm_conclusion);
  const detailKey = holdingKey(holding);
  return `
    <section class="final-conclusion research-conclusion-section">
      <div class="research-conclusion-header">
        <h4>最终结论</h4>
        <span>展示两个来源：投研原始结论，以及你和 LLM 讨论后的最终结论。</span>
      </div>
      <div class="research-conclusion-grid">
        ${renderResearchConclusionCard({
          title: "投研给出的结论",
          conclusion: original,
          actionHtml: renderSourceReviewButton(holding),
          missingText: "缺失",
        })}
        ${renderResearchConclusionCard({
          title: "我和 LLM 探讨后的结论",
          conclusion: userConclusion,
          actionHtml: `<button class="raw-toggle" type="button" data-research-chat="${escapeHtml(detailKey)}">${userConclusion.present ? "继续讨论" : "开始讨论"}</button>`,
          missingText: "缺失",
        })}
      </div>
    </section>
  `;
}

function researchConclusion(value) {
  const conclusion = value && typeof value === "object" ? value : {};
  const content = formatPlain(conclusion.content || "");
  return {
    present: conclusion.status === "present" && hasValue(content),
    content,
    reason: formatPlain(conclusion.reason || ""),
    condition: formatPlain(conclusion.condition || conclusion.conditions || ""),
    failure: formatPlain(conclusion.failure_condition || conclusion.failure || ""),
  };
}

function renderResearchConclusionCard({ title, conclusion, actionHtml, missingText }) {
  const statusText = conclusion.present ? "已生成" : "缺失";
  const body = conclusion.present
    ? `
      <div class="research-conclusion-body">
        <strong>${escapeHtml(conclusion.content)}</strong>
        ${renderResearchConclusionField("理由", conclusion.reason)}
        ${renderResearchConclusionField("条件", conclusion.condition)}
        ${renderResearchConclusionField("失败条件", conclusion.failure)}
      </div>
    `
    : `
      <div class="research-conclusion-body missing">
        <strong>${escapeHtml(missingText)}</strong>
        <p>打开聊天窗口后，系统会自动加载投研结论、原始资料、你的仓位与关注点。只有点击“生成最终结论”后才写入这里。</p>
      </div>
    `;
  return `
    <article class="research-conclusion-card">
      <div class="research-conclusion-card-header">
        <h5>${escapeHtml(title)}</h5>
        <span class="status-pill ${conclusion.present ? "status-ok" : "status-muted"}">${escapeHtml(statusText)}</span>
      </div>
      ${body}
      <div class="research-conclusion-actions">${actionHtml}</div>
    </article>
  `;
}

function renderResearchConclusionField(label, value) {
  if (!hasValue(value) || value === "-") {
    return "";
  }
  return `
    <div class="research-conclusion-field">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function renderSourceReviewButton(holding) {
  return hasValue(sourceReviewText(holding))
    ? `<button class="raw-toggle english-source-toggle" type="button" data-toggle-raw-report>查看英文原文</button>`
    : "";
}
```

Update `renderSourceReview(holding)` so it does not duplicate the English button when the final conclusion card already shows it:

```javascript
function renderSourceReview(holding) {
  const sourceText = sourceReviewText(holding);
  if (!hasValue(sourceText)) {
    return "";
  }
  return `
    <section class="source-review">
      ${renderSplitSourceRows(sourceText)}
    </section>
  `;
}
```

- [ ] **Step 6: Add chat API functions**

Add below the research conclusion helpers:

```javascript
async function openResearchChat(detailKey) {
  const holding = holdingByKey(detailKey);
  if (!holding) {
    return;
  }
  const researchView = holding.research_view || {};
  if (!researchView.available) {
    setResearchChatStatus("暂无投研上下文，无法开始讨论");
    return;
  }
  state.researchChat.holdingKey = detailKey;
  elements["research-chat-title"].textContent = `LLM 深度讨论 · ${holding.market}.${holding.symbol}`;
  elements["research-chat-context-note"].textContent = `上下文已自动加载 · ${researchView.research_date || "-"}`;
  renderResearchChatContext(holding);
  openResearchChatLayer();
  if (!state.researchChat.sessionId) {
    await createResearchChatSession(holding);
  }
}

function openResearchChatLayer() {
  elements["research-chat-layer"].hidden = false;
  elements["research-chat-layer"].classList.remove("hidden");
  elements["research-chat-input"].focus();
}

function closeResearchChat() {
  elements["research-chat-layer"].hidden = true;
  elements["research-chat-layer"].classList.add("hidden");
}

function renderResearchChatContext(holding) {
  const researchView = holding.research_view || {};
  const original = researchConclusion(researchView.tradingagents_conclusion);
  elements["research-chat-context-list"].innerHTML = `
    <div><dt>投研结论</dt><dd>${escapeHtml(original.content || "缺失")}</dd></div>
    <div><dt>用户上下文</dt><dd>组合权重 ${escapeHtml(formatPlain(holding.portfolio_weight_hkd || "-"))}；风险标记 ${escapeHtml(formatPlain(holding.risk_flag || "-"))}</dd></div>
    <div><dt>输出目标</dt><dd>生成 user_llm_conclusion.json 后刷新看板。</dd></div>
  `;
}

async function createResearchChatSession(holding) {
  setResearchChatBusy(true, "正在加载上下文...");
  try {
    const session = await postDashboardJson("/api/research-chat/sessions", {
      market: holding.market,
      symbol: holding.symbol,
    });
    state.researchChat.sessionId = session.session_id || "";
    renderResearchChatMessages(session.messages || []);
    setResearchChatStatus("上下文已自动加载。");
  } catch (error) {
    setResearchChatStatus(error.message || String(error));
  } finally {
    setResearchChatBusy(false);
  }
}

async function sendResearchChatMessage() {
  const content = elements["research-chat-input"].value.trim();
  if (!content || !state.researchChat.sessionId || state.researchChat.busy) {
    return;
  }
  setResearchChatBusy(true, "正在发送...");
  try {
    const session = await postDashboardJson(
      `/api/research-chat/sessions/${encodeURIComponent(state.researchChat.sessionId)}/messages`,
      { content },
    );
    elements["research-chat-input"].value = "";
    renderResearchChatMessages(session.messages || []);
    setResearchChatStatus("对话已保存。");
  } catch (error) {
    setResearchChatStatus(error.message || String(error));
  } finally {
    setResearchChatBusy(false);
  }
}

async function finalizeResearchChat() {
  if (!state.researchChat.sessionId || state.researchChat.busy) {
    return;
  }
  setResearchChatBusy(true, "正在生成最终结论...");
  try {
    await postDashboardJson(
      `/api/research-chat/sessions/${encodeURIComponent(state.researchChat.sessionId)}/finalize`,
      {},
    );
    setResearchChatStatus("最终结论已生成。");
    closeResearchChat();
    await loadDashboard();
  } catch (error) {
    setResearchChatStatus(error.message || String(error));
  } finally {
    setResearchChatBusy(false);
  }
}

function renderResearchChatMessages(messages) {
  const rows = Array.isArray(messages) ? messages : [];
  elements["research-chat-messages"].innerHTML = rows.length
    ? rows.map((message) => `
      <div class="research-chat-message ${message.role === "user" ? "user" : "assistant"}">
        <strong>${message.role === "user" ? "你" : "LLM"}</strong>
        <span>${escapeHtml(message.content || "")}</span>
      </div>
    `).join("")
    : `<p class="compact-empty">上下文已加载，可以开始讨论。</p>`;
  elements["research-chat-finalize"].disabled = rows.length < 2;
}

function setResearchChatBusy(busy, statusText) {
  state.researchChat.busy = busy;
  elements["research-chat-send"].disabled = busy;
  elements["research-chat-finalize"].disabled = busy || !state.researchChat.sessionId;
  if (statusText) {
    setResearchChatStatus(statusText);
  }
}

function setResearchChatStatus(text) {
  elements["research-chat-status"].textContent = text;
}

async function postDashboardJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json; charset=utf-8"},
    body: JSON.stringify(payload || {}),
  });
  const data = await response.json();
  if (!response.ok || data.status === "error") {
    throw new Error(data.message || `request ${response.status}`);
  }
  return data;
}

function holdingByKey(detailKey) {
  return filteredHoldings().find((holding) => holdingKey(holding) === detailKey)
    || (state.dashboard && Array.isArray(state.dashboard.holdings)
      ? state.dashboard.holdings.find((holding) => holdingKey(holding) === detailKey)
      : null);
}
```

- [ ] **Step 7: Add CSS**

Append to `src/open_trader/dashboard_static/dashboard.css` before media queries:

```css
.research-conclusion-header,
.research-conclusion-card-header,
.research-conclusion-actions,
.research-chat-action-row {
  align-items: center;
  display: flex;
  gap: 10px;
  justify-content: space-between;
}

.research-conclusion-header span {
  color: var(--muted);
  font-weight: 700;
}

.research-conclusion-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.research-conclusion-card {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 12px;
  min-width: 0;
  padding: 12px;
}

.research-conclusion-card h5 {
  font-size: 15px;
  margin: 0;
}

.research-conclusion-body {
  display: grid;
  gap: 8px;
}

.research-conclusion-body strong,
.research-conclusion-field strong {
  overflow-wrap: anywhere;
}

.research-conclusion-body p {
  color: var(--muted);
  line-height: 1.55;
  margin: 0;
}

.research-conclusion-field {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 7px;
  display: grid;
  gap: 4px;
  padding: 8px;
}

.research-chat-layer {
  align-items: center;
  background: rgba(32, 33, 36, 0.28);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: 18px;
  position: fixed;
  z-index: 20;
}

.research-chat-modal {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 24px 70px rgba(35, 42, 32, 0.22);
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  max-height: min(760px, calc(100vh - 36px));
  max-width: 980px;
  min-height: 620px;
  overflow: hidden;
  width: min(980px, 100%);
}

.research-chat-header,
.research-chat-footer {
  background: var(--surface-soft);
  padding: 14px;
}

.research-chat-header {
  align-items: start;
  border-bottom: 1px solid var(--line);
  display: flex;
  gap: 12px;
  justify-content: space-between;
}

.research-chat-header h2 {
  font-size: 18px;
  margin: 0 0 5px;
}

.research-chat-header p,
.research-chat-status {
  color: var(--muted);
  margin: 0;
}

.research-chat-body {
  display: grid;
  gap: 12px;
  grid-template-columns: minmax(220px, 0.34fr) minmax(0, 1fr);
  min-height: 0;
  padding: 14px;
}

.research-chat-context {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 10px;
  padding: 12px;
}

.research-chat-context .status-pill {
  align-self: start;
  justify-self: start;
}

.research-chat-context dl {
  display: grid;
  gap: 9px;
  margin: 0;
}

.research-chat-context div {
  display: grid;
  gap: 3px;
}

.research-chat-context dt {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}

.research-chat-context dd {
  line-height: 1.45;
  margin: 0;
}

.research-chat-messages {
  display: grid;
  gap: 10px;
  min-height: 0;
  overflow-y: auto;
}

.research-chat-message {
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 5px;
  line-height: 1.55;
  padding: 10px 12px;
}

.research-chat-message.user {
  background: #fffaf1;
  border-color: #efc47e;
}

.research-chat-message.assistant {
  background: var(--surface-soft);
}

.research-chat-footer {
  border-top: 1px solid var(--line);
  display: grid;
  gap: 10px;
}

.research-chat-input-row {
  display: grid;
  gap: 8px;
  grid-template-columns: minmax(0, 1fr) auto;
}

.research-chat-input-row input {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 7px;
  min-height: 38px;
  min-width: 0;
  padding: 8px 10px;
}
```

In the existing `@media (max-width: 1180px)` block, add `.research-conclusion-grid` to the two-column group if needed. In the existing `@media (max-width: 760px)` block, add:

```css
  .research-conclusion-grid,
  .research-chat-body,
  .research-chat-input-row {
    grid-template-columns: 1fr;
  }

  .research-chat-modal {
    min-height: 0;
  }

  .research-chat-header,
  .research-chat-action-row,
  .research-conclusion-header {
    align-items: stretch;
    display: grid;
    grid-template-columns: 1fr;
  }
```

- [ ] **Step 8: Run static shell test**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: PASS.

- [ ] **Step 9: Add JavaScript behavior test**

In `tests/test_dashboard_web.py`, extend the existing Node `vm` test block or add a new Node check that sets:

```javascript
state.dashboard = {
  holdings: [{
    market: "US",
    symbol: "VIXY",
    portfolio_weight_hkd: "7.11%",
    risk_flag: "normal",
    broker_details: [],
    agent_report: {available: false},
    strategy: {available: false},
    premarket_action: {available: false},
    trade_action: {available: false},
    research_view: {
      available: true,
      research_date: "2026-06-19",
      tradingagents_conclusion: {
        status: "present",
        content: "低配，当前动作为减仓。",
        reason: "达到第一目标价。",
        condition: "财报后复评。"
      },
      user_llm_conclusion: {status: "missing", content: ""}
    }
  }]
};
const html = renderResearchConclusions(state.dashboard.holdings[0]);
if (!html.includes("投研给出的结论") || !html.includes("我和 LLM 探讨后的结论")) {
  throw new Error("research conclusion labels missing: " + html);
}
if (!html.includes("低配，当前动作为减仓。") || !html.includes("缺失")) {
  throw new Error("research conclusion content missing: " + html);
}
state.dashboard.holdings[0].research_view.user_llm_conclusion = {
  status: "present",
  content: "确认减仓 100 股。",
};
const finalizedHtml = renderResearchConclusions(state.dashboard.holdings[0]);
if (!finalizedHtml.includes("确认减仓 100 股。") || finalizedHtml.includes("<strong>缺失</strong>")) {
  throw new Error("finalized user conclusion did not render: " + finalizedHtml);
}
```

- [ ] **Step 10: Run dashboard web tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -v
```

Expected: PASS.

- [ ] **Step 11: Commit Task 4**

Run:

```bash
git add src/open_trader/dashboard_static/index.html src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render research chat workflow"
```

Expected: commit succeeds with only Task 4 files.

---

### Task 5: Documentation And End-To-End Verification

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Verify: dashboard tests and browser UI

- [ ] **Step 1: Update README docs**

In both README files, add a dashboard subsection after the existing local dashboard deployment section.

English text for `README.md`:

```markdown
### Research Chat Workflow

The dashboard can display a TradingAgents research bundle for each holding when
the bundle exists under `data/research_data/<market>/<symbol>/<date>/`.

Required bundle files:

- `dashboard_view.json`: dashboard-facing conclusions.
- `combined_input.json`: raw TradingAgents output plus local user context.
- `llm_system_prompt.md`: the system prompt loaded automatically when chat starts.

The symbol detail page shows two conclusion cards:

- `投研给出的结论`: the original TradingAgents conclusion.
- `我和 LLM 探讨后的结论`: missing until the user clicks `生成最终结论` in chat.

Chat transcripts are stored under `data/research_chat/sessions/`. Finalization
writes `user_llm_conclusion.json` into the research bundle and updates that
bundle's `dashboard_view.json`. This workflow is read-only for trading: it does
not place orders and does not modify trade action files.
```

Chinese text for `README.zh-CN.md`:

```markdown
### 投研结论与 LLM 深度讨论

当前端标的存在 `data/research_data/<market>/<symbol>/<date>/` 投研包时，
仪表盘会在标的详情页展示投研结论。

投研包需要包含：

- `dashboard_view.json`：给前端渲染的结论视图。
- `combined_input.json`：TradingAgents 原始输出和本地用户上下文。
- `llm_system_prompt.md`：打开聊天窗口时自动加载的系统提示词。

标的详情页会展示两张结论卡：

- `投研给出的结论`：TradingAgents 原始结论。
- `我和 LLM 探讨后的结论`：点击聊天窗口里的 `生成最终结论` 前显示 `缺失`。

聊天记录保存在 `data/research_chat/sessions/`。生成最终结论后，系统会把
`user_llm_conclusion.json` 写回投研包，并更新该投研包的 `dashboard_view.json`。
这个流程只服务于人工复核，不会下单，也不会修改交易动作文件。
```

- [ ] **Step 2: Run focused automated tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_chat.py tests/test_dashboard.py tests/test_dashboard_web.py -v
```

Expected: PASS for all focused tests.

- [ ] **Step 3: Run broader dashboard test set**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_quotes.py tests/test_dashboard_cli.py -v
```

Expected: PASS for all listed tests.

- [ ] **Step 4: Create a local sample research bundle for browser verification**

Run:

```bash
mkdir -p data/research_data/US/VIXY/2026-06-19
cat > data/research_data/US/VIXY/2026-06-19/dashboard_view.json <<'JSON'
{
  "schema_version": "dashboard.research_view.v1",
  "market": "US",
  "symbol": "VIXY",
  "research_date": "2026-06-19",
  "tradingagents_conclusion": {
    "status": "present",
    "content": "低配，当前动作为减仓。",
    "reason": "达到第一目标价。",
    "condition": "财报后复评。"
  },
  "user_llm_conclusion": {
    "status": "missing",
    "content": ""
  }
}
JSON
cat > data/research_data/US/VIXY/2026-06-19/combined_input.json <<'JSON'
{
  "schema_version": "combined_input.v1",
  "holding": {"market": "US", "symbol": "VIXY"},
  "user_context": {"portfolio_weight_hkd": "7.11%", "concern": "财报后是否还有上行空间"}
}
JSON
cat > data/research_data/US/VIXY/2026-06-19/llm_system_prompt.md <<'EOF'
你是投研讨论助手。基于投研结论和用户上下文，用中文和用户讨论。
EOF
```

Expected: the three files exist under `data/research_data/US/VIXY/2026-06-19/`.

- [ ] **Step 5: Start local dashboard**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766
```

Expected: server prints `dashboard_url: http://127.0.0.1:8766` and keeps running.

- [ ] **Step 6: Verify browser UI with local Chrome**

Use Playwright with the local Chrome executable:

```javascript
const { chromium } = require("playwright");
const browser = await chromium.launch({
  headless: true,
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
await page.goto("http://127.0.0.1:8766");
await page.getByText("VIXY").first().click();
await page.waitForSelector(".research-conclusion-grid");
const labels = await page.locator(".research-conclusion-grid").innerText();
if (!labels.includes("投研给出的结论") || !labels.includes("我和 LLM 探讨后的结论")) {
  throw new Error(labels);
}
if (!labels.includes("低配，当前动作为减仓。") || !labels.includes("缺失")) {
  throw new Error(labels);
}
await page.getByText("开始讨论").click();
await page.waitForSelector(".research-chat-modal");
const modal = await page.locator(".research-chat-modal").innerText();
if (!modal.includes("上下文已加载")) {
  throw new Error(modal);
}
await page.screenshot({ path: "reports/research-chat-desktop.png", fullPage: true });
await page.setViewportSize({ width: 390, height: 1000 });
await page.screenshot({ path: "reports/research-chat-mobile.png", fullPage: true });
const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
if (overflow) {
  throw new Error("horizontal overflow detected");
}
await browser.close();
```

Expected:

- `.research-conclusion-grid` is visible.
- Both conclusion labels are visible.
- TradingAgents conclusion is visible immediately.
- User/LLM conclusion displays `缺失`.
- Chat modal opens and shows `上下文已加载`.
- No horizontal overflow on mobile.

- [ ] **Step 7: Stop local dashboard**

Stop the dashboard server with `Ctrl-C`.

Expected: no `open_trader dashboard` process remains running.

- [ ] **Step 8: Run final focused tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_chat.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_quotes.py tests/test_dashboard_cli.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit Task 5**

Run:

```bash
git add README.md README.zh-CN.md
git commit -m "docs: document research chat workflow"
```

Expected: commit succeeds with README changes only.

---

## Self-Review

- Spec coverage:
  - TradingAgents original conclusion shown immediately: Task 1 attaches `research_view`; Task 4 renders the first card.
  - User/LLM conclusion missing until finalization: Task 1 normalizes missing status; Task 4 renders `缺失`; Task 2 writes only during finalization.
  - Automatic context loading: Task 2 requires `combined_input.json` and `llm_system_prompt.md`; Task 4 opens chat without copy/paste.
  - Multi-turn chat: Task 2 appends transcript messages; Task 3 exposes message API; Task 4 sends messages.
  - `生成最终结论`: Task 2 validates and writes `user_llm_conclusion.json`; Task 3 exposes finalize API; Task 4 wires the button.
  - Current main UI: Task 4 modifies the existing symbol detail panel and modal shell, not a new workspace.
  - Error handling: Task 1 isolates invalid bundles; Task 2 raises explicit `ResearchChatError`; Task 3 returns JSON errors; Task 4 shows status text.
  - Read-only trading boundary: Task 2 writes only chat/session/research artifacts; Task 5 documents no order placement.
- Placeholder scan:
  - No unresolved marker words or unspecified implementation steps are present.
  - Each task lists exact files, exact commands, and expected outcomes.
- Type consistency:
  - `research_view`, `tradingagents_conclusion`, `user_llm_conclusion`, `session_id`, and API route names are consistent across backend, tests, and frontend tasks.
