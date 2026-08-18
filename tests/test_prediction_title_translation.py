from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from open_trader.llm_providers import PROVIDER_IDS, LlmCompletion
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_title_translation import (
    TITLE_TRANSLATION_PROMPT_VERSION,
    LlmTitleTranslator,
    cached_prediction_title_zh,
    legacy_prediction_title_cache_keys,
    prediction_title_cache_key,
)


TITLE_USAGE: dict[str, int] = {
    "input_tokens": 8,
    "cached_input_tokens": 0,
    "output_tokens": 2,
    "reasoning_output_tokens": 0,
}


@pytest.fixture(autouse=True)
def _isolated_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPEN_TRADER_PREDICTION_LLM_PROVIDER",
        "OPEN_TRADER_CODEX_MODEL",
        "OPEN_TRADER_LLM_FALLBACK_MODEL",
        "OPEN_TRADER_DEEPSEEK_MODEL",
        "OPEN_TRADER_ZHIPU_MODEL",
        "DEEPSEEK_API_KEY",
        "ZHIPU_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def title_completer(
    title_zh: str | None = None,
    *,
    content: str | None = None,
    reason: str | None = None,
):
    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> LlmCompletion:
        calls.append((system, user))
        if reason is not None:
            return LlmCompletion(None, reason, dict(TITLE_USAGE))
        payload = (
            content
            if content is not None
            else json.dumps({"title_zh": title_zh or "比特币高于 9 万美元吗？"})
        )
        return LlmCompletion(payload, None, dict(TITLE_USAGE))

    return complete, calls


def completers_for(complete) -> dict[str, object]:
    return {provider: complete for provider in PROVIDER_IDS}


def store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def test_cache_key_normalizes_title_and_namespaces_prompt_version() -> None:
    assert prediction_title_cache_key("  Bitcoin   above $90? ") == (
        prediction_title_cache_key("Bitcoin above $90?")
    )
    assert prediction_title_cache_key("Bitcoin above $90?").startswith(
        "prediction-title-zh:"
    )
    assert prediction_title_cache_key("Bitcoin above $90?") != (
        prediction_title_cache_key("Bitcoin above $91?")
    )
    assert prediction_title_cache_key(
        "Bitcoin above $90?", prompt_version="v2"
    ) != prediction_title_cache_key("Bitcoin above $90?")


def test_legacy_cache_keys_use_the_old_luna_model_namespace() -> None:
    normalized = "Bitcoin above $90?"
    digest = hashlib.sha256(
        json.dumps(
            {
                "title": normalized,
                "model": "gpt-5.6-luna",
                "prompt_version": TITLE_TRANSLATION_PROMPT_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert legacy_prediction_title_cache_keys("Bitcoin above $90?") == [
        f"prediction-title-zh:{digest}"
    ]
    assert legacy_prediction_title_cache_keys("Bitcoin above $90?")[0] != (
        prediction_title_cache_key("Bitcoin above $90?")
    )


def test_cache_hit_does_not_invoke_completer(tmp_path: Path) -> None:
    target = store(tmp_path)
    target.save_llm_cache(
        prediction_title_cache_key("Bitcoin above $90?"),
        {"title_zh": "比特币高于 9 万美元吗？"},
    )

    def must_not_complete(*_args: object) -> LlmCompletion:
        raise AssertionError("completer called")

    translator = LlmTitleTranslator(
        target, completers=completers_for(must_not_complete)
    )

    assert translator.translate("Bitcoin above $90?") == "比特币高于 9 万美元吗？"
    assert cached_prediction_title_zh(target, "Bitcoin above $90?") == (
        "比特币高于 9 万美元吗？"
    )


def test_miss_uses_selected_engine_and_caches_valid_translation(
    tmp_path: Path,
) -> None:
    target = store(tmp_path)
    complete, calls = title_completer("比特币高于九万美元吗？")

    translator = LlmTitleTranslator(
        target, default_provider="zhipu", completers=completers_for(complete)
    )

    assert translator.translate("Bitcoin above $90?") == "比特币高于九万美元吗？"
    assert len(calls) == 1
    system, user = calls[0]
    assert "untrusted" in system.lower()
    assert user == "Bitcoin above $90?"
    assert target.llm_usage_24h_by_provider()["zhipu"] == {
        "calls": 1,
        "successes": 1,
        "failures": 0,
        "cache_hits": 0,
        "input_tokens": 8,
        "cached_input_tokens": 0,
        "output_tokens": 2,
        "reasoning_output_tokens": 0,
    }
    assert cached_prediction_title_zh(target, "Bitcoin above $90?") == (
        "比特币高于九万美元吗？"
    )


def test_failed_engine_does_not_fallback_and_switch_restores_translation(
    tmp_path: Path,
) -> None:
    target = store(tmp_path)
    zhipu, zhipu_calls = title_completer(reason="ZHIPU_TIMEOUT")
    codex, codex_calls = title_completer("比特币高于九万美元吗？")
    translator = LlmTitleTranslator(
        target,
        completers={"codex": codex, "deepseek": codex, "zhipu": zhipu},
    )

    assert translator.current_provider() == "zhipu"
    assert translator.translate("Bitcoin above $90?") is None
    assert len(zhipu_calls) == 1
    assert codex_calls == []
    assert target.llm_usage_24h_by_provider()["zhipu"]["failures"] == 1
    assert cached_prediction_title_zh(target, "Bitcoin above $90?") is None

    target.set_llm_provider("codex")

    assert translator.translate("Bitcoin above $90?") == "比特币高于九万美元吗？"
    assert len(codex_calls) == 1
    assert target.llm_usage_24h_by_provider()["codex"]["successes"] == 1


def test_legacy_luna_cache_row_migrates_to_shared_key(tmp_path: Path) -> None:
    target = store(tmp_path)
    legacy_key = legacy_prediction_title_cache_keys("Bitcoin above $90?")[0]
    target.save_llm_cache(
        legacy_key,
        {"model": "gpt-5.6-luna", "title_zh": "比特币高于 9 万美元吗？"},
    )

    def must_not_complete(*_args: object) -> LlmCompletion:
        raise AssertionError("legacy cache row must be reused")

    translator = LlmTitleTranslator(
        target, completers=completers_for(must_not_complete)
    )

    assert translator.translate("Bitcoin above $90?") == "比特币高于 9 万美元吗？"
    migrated = target.load_llm_cache(
        prediction_title_cache_key("Bitcoin above $90?")
    )
    assert migrated is not None
    assert migrated["title_zh"] == "比特币高于 9 万美元吗？"
    assert migrated["provider"] == "codex"  # inferred from gpt-5.6-luna
    assert target.llm_usage_24h()["calls"] == 0


def test_invalid_or_failed_output_does_not_poison_cache(tmp_path: Path) -> None:
    target = store(tmp_path)
    attempts: list[LlmCompletion] = [
        LlmCompletion(None, "ZHIPU_TIMEOUT", dict(TITLE_USAGE)),
        LlmCompletion("not-json\n", None, dict(TITLE_USAGE)),
        LlmCompletion(json.dumps({"title_zh": ""}), None, dict(TITLE_USAGE)),
        LlmCompletion(
            json.dumps({"title_zh": "Bitcoin above $90?"}), None, dict(TITLE_USAGE)
        ),
        LlmCompletion(
            json.dumps({"title_zh": "extra", "other": 1}), None, dict(TITLE_USAGE)
        ),
    ]
    calls: list[int] = []

    def complete(system: str, user: str) -> LlmCompletion:
        calls.append(1)
        return attempts.pop(0)

    translator = LlmTitleTranslator(
        target, default_provider="zhipu", completers=completers_for(complete)
    )

    for _ in range(5):
        assert translator.translate("Bitcoin above $90?") is None

    assert len(calls) == 5
    assert cached_prediction_title_zh(target, "Bitcoin above $90?") is None
    assert target.llm_usage_24h_by_provider()["zhipu"]["failures"] == 5
