from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL


RESEARCH_VIEW_SCHEMA = "dashboard.research_view.v1"
SESSION_SCHEMA = "open_trader.research_chat_session.v1"
FINAL_CONCLUSION_SCHEMA = "user.llm_conclusion.v1"
RESEARCH_BUNDLE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ResearchChatError(RuntimeError):
    pass


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
        content = self._complete(
            system_prompt=system_prompt,
            combined_input=combined_input,
            messages=messages,
        )
        return content.strip()

    def finalize(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
    ) -> str:
        return self._complete(
            system_prompt=system_prompt,
            combined_input=combined_input,
            messages=messages,
            response_format={"type": "json_object"},
        )

    def _complete(
        self,
        *,
        system_prompt: str,
        combined_input: dict[str, object],
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
    ) -> str:
        request_messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"combined_input": combined_input},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
            *messages,
        ]
        kwargs: dict[str, object] = {
            "model": self._model,
            "messages": request_messages,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        response = self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        if not content:
            raise ResearchChatError("model returned empty content")
        return content


@dataclass
class ResearchChatService:
    data_dir: Path
    llm: ResearchChatLLM | None = None

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
            raise ResearchChatError("research bundle not found")
        self._load_context(bundle_dir)

        now = _now_iso()
        session = {
            "schema_version": SESSION_SCHEMA,
            "session_id": uuid.uuid4().hex,
            "market": market_key,
            "symbol": symbol_key,
            "research_bundle_dir": str(bundle_dir),
            "messages": [],
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self._write_session(session)
        return session

    def get_session(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        if not path.is_file():
            raise ResearchChatError("chat session not found")
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchChatError(f"invalid chat session: {exc}") from exc
        if not isinstance(session, dict):
            raise ResearchChatError("invalid chat session: root is not object")
        if session.get("schema_version") != SESSION_SCHEMA:
            raise ResearchChatError("invalid chat session: schema mismatch")
        _session_messages(session)
        return session

    def append_message(self, *, session_id: str, content: str) -> dict[str, Any]:
        user_content = content.strip()
        if not user_content:
            raise ResearchChatError("message content is required")
        session = self.get_session(session_id)
        if session.get("status") == "finalized":
            raise ResearchChatError("chat session already finalized")
        context = self._load_context(Path(str(session.get("research_bundle_dir", ""))))

        messages = _session_messages(session)
        messages.append({"role": "user", "content": user_content})
        assistant_reply = self._require_llm().chat(
            system_prompt=context["system_prompt"],
            combined_input=context["combined_input"],
            messages=messages,
        ).strip()
        if not assistant_reply:
            raise ResearchChatError("assistant reply is empty")
        messages.append({"role": "assistant", "content": assistant_reply})
        session["messages"] = messages
        session["updated_at"] = _now_iso()
        self._write_session(session)
        return session

    def finalize_session(self, *, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        messages = _session_messages(session)
        if len(messages) < 2:
            raise ResearchChatError("chat session has insufficient messages")
        bundle_dir = Path(str(session.get("research_bundle_dir", "")))
        context = self._load_context(bundle_dir)

        conclusion = _parse_final_conclusion(
            self._require_llm().finalize(
                system_prompt=context["system_prompt"],
                combined_input=context["combined_input"],
                messages=messages,
            )
        )
        conversation_reference = str(self._session_path(session_id))
        conclusion["conversation_reference"] = conversation_reference
        _write_json_atomic(bundle_dir / "user_llm_conclusion.json", conclusion)

        dashboard_view = self._load_dashboard_view(bundle_dir)
        dashboard_view["user_llm_conclusion"] = conclusion
        _write_json_atomic(bundle_dir / "dashboard_view.json", dashboard_view)

        session["status"] = "finalized"
        session["finalized_at"] = _now_iso()
        session["updated_at"] = session["finalized_at"]
        session["final_conclusion_path"] = str(bundle_dir / "user_llm_conclusion.json")
        self._write_session(session)
        return {
            "status": "ok",
            "conclusion": conclusion,
            "dashboard_view": dashboard_view,
        }

    def _load_context(self, bundle_dir: Path) -> dict[str, Any]:
        prompt_path = bundle_dir / "llm_system_prompt.md"
        combined_path = bundle_dir / "combined_input.json"
        for path in (prompt_path, combined_path):
            if not path.is_file():
                raise ResearchChatError(f"missing research context file: {path}")
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        if not system_prompt:
            raise ResearchChatError(f"missing research context file: {prompt_path}")
        try:
            combined_input = json.loads(combined_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchChatError(f"invalid research context file: {exc}") from exc
        if not isinstance(combined_input, dict):
            raise ResearchChatError("invalid research context file: combined input is not object")
        return {
            "system_prompt": system_prompt,
            "combined_input": combined_input,
        }

    def _session_path(self, session_id: str) -> Path:
        clean_id = session_id.strip()
        if not clean_id or "/" in clean_id or "\\" in clean_id:
            raise ResearchChatError("invalid chat session id")
        return self.sessions_dir / f"{clean_id}.json"

    def _write_session(self, session: dict[str, Any]) -> None:
        session_id = str(session.get("session_id", ""))
        _write_json_atomic(self._session_path(session_id), session)

    def _load_dashboard_view(self, bundle_dir: Path) -> dict[str, Any]:
        path = bundle_dir / "dashboard_view.json"
        try:
            dashboard_view = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchChatError(f"invalid research view: {exc}") from exc
        if not isinstance(dashboard_view, dict):
            raise ResearchChatError("invalid research view: root is not object")
        return dashboard_view

    def _require_llm(self) -> ResearchChatLLM:
        if self.llm is None:
            self.llm = DeepSeekResearchChatClient()
        return self.llm


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
    candidate_parents = [
        research_root / market.strip().upper() / symbol.strip().upper(),
        research_root / symbol.strip().upper(),
    ]
    for parent in candidate_parents:
        latest_dir = _latest_dated_bundle_dir(parent)
        if latest_dir is not None:
            return latest_dir
    return None


def _latest_dated_bundle_dir(parent: Path) -> Path | None:
    if not parent.is_dir():
        return None
    dated_dirs: list[Path] = []
    for child in parent.iterdir():
        if (
            child.is_dir()
            and RESEARCH_BUNDLE_DATE_PATTERN.fullmatch(child.name)
            and (child / "dashboard_view.json").is_file()
        ):
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


def _session_messages(session: dict[str, Any]) -> list[dict[str, str]]:
    value = session.get("messages")
    if not isinstance(value, list):
        raise ResearchChatError("invalid chat session: messages must be list")
    messages: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ResearchChatError("invalid chat session: message must be object")
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"}:
            raise ResearchChatError("invalid chat session: invalid message role")
        if not isinstance(content, str) or not content.strip():
            raise ResearchChatError("invalid chat session: empty message content")
        messages.append({"role": str(role), "content": content.strip()})
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
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ResearchChatError("最终结论格式无效，请重试")
    normalized = {str(key): value for key, value in payload.items() if isinstance(key, str)}
    normalized["content"] = content.strip()
    return normalized


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
            temp_file.write("\n")
        os.replace(temp_name, path)
    finally:
        if temp_name:
            temp_path = Path(temp_name)
            if temp_path.exists():
                temp_path.unlink()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
