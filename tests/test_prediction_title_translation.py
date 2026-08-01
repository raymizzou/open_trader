from __future__ import annotations

import subprocess
from pathlib import Path

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_title_translation import (
    CodexTitleTranslator,
    cached_prediction_title_zh,
    prediction_title_cache_key,
)


def codex_output(payload: object) -> str:
    import json

    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(payload, ensure_ascii=False),
            },
        },
        ensure_ascii=False,
    )


def store(tmp_path: Path) -> PredictionArbitrageStore:
    return PredictionArbitrageStore(tmp_path / "data")


def test_cache_key_normalizes_title_and_namespaces_model_prompt() -> None:
    assert prediction_title_cache_key("  Bitcoin   above $90? ") == prediction_title_cache_key(
        "Bitcoin above $90?"
    )
    assert prediction_title_cache_key("Bitcoin above $90?").startswith(
        "prediction-title-zh:"
    )
    assert prediction_title_cache_key("Bitcoin above $90?") != prediction_title_cache_key(
        "Bitcoin above $91?"
    )
    assert prediction_title_cache_key(
        "Bitcoin above $90?", model="other", prompt_version="v2"
    ) != prediction_title_cache_key("Bitcoin above $90?")


def test_cache_hit_does_not_invoke_subprocess(tmp_path: Path) -> None:
    target = store(tmp_path)
    target.save_llm_cache(
        prediction_title_cache_key("Bitcoin above $90?"),
        {"title_zh": "比特币高于 9 万美元吗？"},
    )
    translator = CodexTitleTranslator(
        target,
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runner called")
        ),
    )
    assert translator.translate("Bitcoin above $90?") == "比特币高于 9 万美元吗？"
    assert cached_prediction_title_zh(target, "Bitcoin above $90?") == "比特币高于 9 万美元吗？"


def test_miss_uses_luna_priority_command_and_caches_valid_translation(tmp_path: Path) -> None:
    target = store(tmp_path)
    calls: list[tuple[list[str], str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, str(kwargs["input"])))
        return subprocess.CompletedProcess(command, 0, codex_output({"title_zh": "比特币高于九万美元吗？"}), "")

    translator = CodexTitleTranslator(target, runner=runner)
    assert translator.translate("Bitcoin above $90?") == "比特币高于九万美元吗？"
    assert len(calls) == 1
    command, prompt = calls[0]
    assert command[:4] == ["codex", "exec", "--model", "gpt-5.6-luna"]
    assert 'model_reasoning_effort="high"' in command
    assert 'service_tier="priority"' in command
    assert "untrusted" in prompt.lower()
    assert cached_prediction_title_zh(target, "Bitcoin above $90?") == "比特币高于九万美元吗？"


def test_invalid_or_failed_output_does_not_poison_cache(tmp_path: Path) -> None:
    target = store(tmp_path)
    outputs = [
        subprocess.TimeoutExpired("codex", 1),
        subprocess.CompletedProcess(["codex"], 1, "", "failed"),
        subprocess.CompletedProcess(["codex"], 0, "not-json\n", ""),
        subprocess.CompletedProcess(["codex"], 0, codex_output({"title_zh": ""}), ""),
        subprocess.CompletedProcess(["codex"], 0, codex_output({"title_zh": "Bitcoin above $90?"}), ""),
    ]

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = outputs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    translator = CodexTitleTranslator(target, runner=runner)
    for _ in range(5):
        assert translator.translate("Bitcoin above $90?") is None
    assert cached_prediction_title_zh(target, "Bitcoin above $90?") is None
