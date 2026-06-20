from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from open_trader.research_chat import (
    DeepSeekResearchChatClient,
    ResearchChatError,
    ResearchChatService,
    missing_research_view,
    load_research_view_for_holding,
)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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

    def chat(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
    ) -> str:
        self.chat_calls.append(
            {
                "system_prompt": system_prompt,
                "combined_input": combined_input,
                "messages": messages,
            }
        )
        return self.reply

    def finalize(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
    ) -> str:
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


def test_load_research_view_market_scoped_bundle_beats_newer_legacy_bundle(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    market_bundle = data_dir / "research_data" / "US" / "VIXY" / "2026-06-19"
    legacy_bundle = data_dir / "research_data" / "VIXY" / "2026-06-20"
    write_json(
        market_bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "2026-06-19",
            "tradingagents_conclusion": {"status": "present", "content": "market"},
            "user_llm_conclusion": {"status": "missing", "content": ""},
        },
    )
    write_json(
        legacy_bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "2026-06-20",
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
    assert view["bundle_dir"].endswith("data/research_data/US/VIXY/2026-06-19")
    assert view["tradingagents_conclusion"]["content"] == "market"


def test_load_research_view_ignores_non_date_directories(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    dated_bundle = data_dir / "research_data" / "US" / "VIXY" / "2026-06-19"
    latest_bundle = data_dir / "research_data" / "US" / "VIXY" / "latest"
    write_json(
        dated_bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "2026-06-19",
            "tradingagents_conclusion": {"status": "present", "content": "dated"},
            "user_llm_conclusion": {"status": "missing", "content": ""},
        },
    )
    write_json(
        latest_bundle / "dashboard_view.json",
        {
            "schema_version": "dashboard.research_view.v1",
            "market": "US",
            "symbol": "VIXY",
            "research_date": "latest",
            "tradingagents_conclusion": {"status": "present", "content": "latest"},
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
    assert view["tradingagents_conclusion"]["content"] == "dated"


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


def test_research_chat_service_rejects_empty_assistant_reply(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_bundle(data_dir)
    service = ResearchChatService(data_dir=data_dir, llm=FakeLLM(reply="  "))
    session = service.create_session(market="US", symbol="VIXY")

    with pytest.raises(ResearchChatError, match="assistant reply is empty"):
        service.append_message(
            session_id=session["session_id"],
            content="如果财报超预期怎么办？",
        )


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


def test_research_chat_service_does_not_partially_finalize_on_bad_dashboard(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    bundle = write_bundle(data_dir)
    service = ResearchChatService(data_dir=data_dir, llm=FakeLLM())
    session = service.create_session(market="US", symbol="VIXY")
    service.append_message(session_id=session["session_id"], content="请给最终结论。")
    session_before = service.get_session(session["session_id"])
    (bundle / "dashboard_view.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ResearchChatError, match="invalid research view"):
        service.finalize_session(session_id=session["session_id"])

    assert not (bundle / "user_llm_conclusion.json").exists()
    assert service.get_session(session["session_id"]) == session_before


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


def test_research_chat_service_rejects_refinalizing_session(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_bundle(data_dir)
    llm = FakeLLM()
    service = ResearchChatService(data_dir=data_dir, llm=llm)
    session = service.create_session(market="US", symbol="VIXY")
    service.append_message(session_id=session["session_id"], content="请给最终结论。")
    service.finalize_session(session_id=session["session_id"])

    with pytest.raises(ResearchChatError, match="chat session already finalized"):
        service.finalize_session(session_id=session["session_id"])

    assert len(llm.finalize_calls) == 1


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


def test_deepseek_research_chat_finalize_requests_json_schema_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeMessage:
        content = '{"schema_version":"user.llm_conclusion.v1","status":"present","content":"结论","updated_at":"2026-06-20T10:40:00+08:00","source":"downstream_llm_conversation"}'

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            calls.append(kwargs)
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = FakeChat()

    class FakeOpenAIModule:
        OpenAI = FakeOpenAI

    monkeypatch.setitem(sys.modules, "openai", FakeOpenAIModule())
    client = DeepSeekResearchChatClient(api_key="test-key")

    client.finalize(
        system_prompt="你是投研讨论助手。",
        combined_input={"holding": {"market": "US", "symbol": "VIXY"}},
        messages=[
            {"role": "user", "content": "请给最终结论。"},
            {"role": "assistant", "content": "可以减仓。"},
        ],
    )

    assert calls[0]["response_format"] == {"type": "json_object"}
    request_messages = calls[0]["messages"]
    assert isinstance(request_messages, list)
    serialized_messages = json.dumps(request_messages, ensure_ascii=False)
    assert "user.llm_conclusion.v1" in serialized_messages
    assert "schema_version" in serialized_messages
    assert "updated_at" in serialized_messages
    assert "source" in serialized_messages
    assert "最终结论" in serialized_messages
