from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_trader.llm_providers as llm_providers
from open_trader.llm_providers import (
    CODEX_DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEEPSEEK_DEFAULT_MODEL,
    PROVIDER_IDS,
    USAGE_FIELDS,
    ZHIPU_DEFAULT_MODEL,
    LlmCompletion,
    codex_completion,
    deepseek_completion,
    infer_provider_from_model,
    provider_credentials_configured,
    provider_model,
    provider_models,
    reason_summary,
    resolve_provider,
    title_completers,
    validation_completers,
    zhipu_completion,
    zhipu_validation_timeout,
)


SCHEMA = (
    Path(__file__).parents[1]
    / "src/open_trader/schemas/polymarket_threshold_relation.json"
)


def test_resolve_provider_accepts_known_ids_case_insensitively() -> None:
    assert PROVIDER_IDS == ("codex", "deepseek", "zhipu")
    assert DEFAULT_PROVIDER == "zhipu"
    assert resolve_provider("codex") == "codex"
    assert resolve_provider(" DeepSeek ") == "deepseek"
    assert resolve_provider("ZHIPU") == "zhipu"


@pytest.mark.parametrize("value", ["", None, "unknown", "gpt", "codex2", 0])
def test_resolve_provider_falls_back_to_default(value: object) -> None:
    assert resolve_provider(value) == DEFAULT_PROVIDER


def test_resolve_provider_honors_custom_default() -> None:
    assert resolve_provider(None, default="codex") == "codex"
    assert resolve_provider("bogus", default="deepseek") == "deepseek"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("deepseek-v4-flash", "deepseek"),
        ("DeepSeek-Reasoner", "deepseek"),
        ("glm-5", "zhipu"),
        ("GLM-4.7", "zhipu"),
        ("zhipu-lite", "zhipu"),
        ("gpt-5.6-sol", "codex"),
        ("", "codex"),
        (None, "codex"),
    ],
)
def test_infer_provider_from_model(model: object, expected: str) -> None:
    assert infer_provider_from_model(model) == expected


def test_provider_credentials_configured_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)

    assert provider_credentials_configured() == {
        "codex": True,
        "deepseek": False,
        "zhipu": False,
    }

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")

    assert provider_credentials_configured() == {
        "codex": True,
        "deepseek": True,
        "zhipu": True,
    }


def test_provider_model_reads_env_and_keeps_shipped_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_name in (
        "OPEN_TRADER_CODEX_MODEL",
        "OPEN_TRADER_DEEPSEEK_MODEL",
        "OPEN_TRADER_ZHIPU_MODEL",
    ):
        monkeypatch.delenv(env_name, raising=False)

    assert provider_models() == {
        "codex": CODEX_DEFAULT_MODEL,
        "deepseek": DEEPSEEK_DEFAULT_MODEL,
        "zhipu": ZHIPU_DEFAULT_MODEL,
    }

    monkeypatch.setenv("OPEN_TRADER_CODEX_MODEL", "gpt-custom")
    monkeypatch.setenv("OPEN_TRADER_DEEPSEEK_MODEL", "  deepseek-custom  ")
    monkeypatch.setenv("OPEN_TRADER_ZHIPU_MODEL", "")

    assert provider_model("codex") == "gpt-custom"
    assert provider_model("deepseek") == "deepseek-custom"
    assert provider_model("zhipu") == ZHIPU_DEFAULT_MODEL

    with pytest.raises(ValueError, match="unknown llm provider"):
        provider_model("unknown")


@pytest.mark.parametrize(
    ("code", "expected_fragment"),
    [
        ("CODEX_TIMEOUT", "Codex 语义校验超时"),
        ("CODEX_FAILED", "Codex 语义校验不可用"),
        ("CODEX_KEY_MISSING", "Codex API Key 未配置"),
        ("CODEX_HTTP_ERROR", "Codex API 请求失败"),
        ("DEEPSEEK_AUTH_FAILED", "DeepSeek 认证失败"),
        ("DEEPSEEK_EMPTY_CONTENT", "DeepSeek 返回空内容"),
        ("DEEPSEEK_CIRCUIT_OPEN", "DeepSeek 连续失败已临时熔断"),
        ("ZHIPU_OUTPUT_INVALID", "智谱 GLM 返回的结构化结果无效"),
        ("ZHIPU_BUDGET_EXHAUSTED", "智谱 GLM 校验额度已耗尽"),
        ("ZHIPU_CONNECTION_FAILED", "智谱 GLM 网络连接失败"),
        ("ZHIPU_RATE_LIMITED", "智谱 GLM 限流"),
    ],
)
def test_reason_summary_renders_provider_prefixed_codes(
    code: str, expected_fragment: str
) -> None:
    summary = reason_summary(code)

    assert summary is not None
    assert summary.startswith(expected_fragment)
    assert summary.endswith("。")


@pytest.mark.parametrize(
    "code", ["CODEX_UNKNOWN_CODE", "ZHIPU", "TIMEOUT", "", "other"]
)
def test_reason_summary_returns_none_for_unknown_codes(code: str) -> None:
    assert reason_summary(code) is None


def _codex_jsonl(
    message: str,
    usage: dict[str, int] | None = None,
) -> str:
    return "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": message},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": usage or {}}),
        )
    )


def test_codex_completion_success_normalizes_prompt_and_usage() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_codex_jsonl(
                '{"decision": "APPROVE"}',
                usage={
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            ),
            stderr="",
        )

    completion = codex_completion(
        "SYSTEM PROMPT",
        "USER PAYLOAD",
        model="gpt-test",
        output_schema=SCHEMA,
        runner=runner,
    )

    assert isinstance(completion, LlmCompletion)
    assert completion.content == '{"decision": "APPROVE"}'
    assert completion.reason is None
    assert completion.usage == {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:4] == ["codex", "exec", "--model", "gpt-test"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--output-schema") + 1] == str(SCHEMA)
    assert str(kwargs["input"]) == "SYSTEM PROMPT\nINPUT JSON\nUSER PAYLOAD\n"
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 45.0
    assert kwargs["check"] is False
    assert Path(str(kwargs["cwd"])) != Path.cwd()


def test_codex_completion_respects_custom_label_and_timeout() -> None:
    calls: list[dict[str, object]] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(dict(kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout=_codex_jsonl("ok"), stderr=""
        )

    completion = codex_completion(
        "sys",
        "user",
        model="gpt-test",
        output_schema=SCHEMA,
        timeout_seconds=12.5,
        user_label="UNTRUSTED TITLE",
        runner=runner,
    )

    assert completion.content == "ok"
    assert calls[0]["timeout"] == 12.5
    assert str(calls[0]["input"]) == "sys\nUNTRUSTED TITLE\nuser\n"


def test_codex_completion_non_int_usage_values_are_zeroed() -> None:
    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_codex_jsonl(
                "ok",
                usage={
                    "input_tokens": -3,
                    "cached_input_tokens": True,
                    "output_tokens": "7",
                },
            ),
            stderr="",
        )

    completion = codex_completion(
        "sys", "user", model="gpt-test", output_schema=SCHEMA, runner=runner
    )

    assert completion.content == "ok"
    assert completion.usage == {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (subprocess.TimeoutExpired(["codex"], 45), "CODEX_TIMEOUT"),
        (
            subprocess.CompletedProcess(["codex"], 1, stdout="", stderr="401"),
            "CODEX_FAILED",
        ),
        (RuntimeError("runner exploded"), "CODEX_FAILED"),
        (
            subprocess.CompletedProcess(
                ["codex"], 0, stdout=json.dumps({"type": "turn.completed"}), stderr=""
            ),
            "CODEX_OUTPUT_INVALID",
        ),
        (
            subprocess.CompletedProcess(["codex"], 0, stdout="not json\n", stderr=""),
            "CODEX_OUTPUT_INVALID",
        ),
        (
            _codex_jsonl("ok").replace("agent_message", "other_item"),
            "CODEX_OUTPUT_INVALID",
        ),
    ],
)
def test_codex_completion_failures_never_raise(
    response: object, expected_reason: str
) -> None:
    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return subprocess.CompletedProcess(
                command, 0, stdout=response, stderr=""
            )
        assert not isinstance(response, BaseException)
        return response  # type: ignore[return-value]

    completion = codex_completion(
        "sys", "user", model="gpt-test", output_schema=SCHEMA, runner=runner
    )

    assert completion.content is None
    assert completion.reason == expected_reason
    assert all(value == 0 for value in completion.usage.values())
    assert set(completion.usage) <= set(USAGE_FIELDS)


class _FakeApiError(Exception):
    def __init__(self, status_code: int | None = None) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _FakeConnectionError(Exception):
    pass


def _api_usage(
    *,
    prompt_tokens: int = 11,
    cached_tokens: int = 4,
    completion_tokens: int = 7,
    reasoning_tokens: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        completion_tokens=completion_tokens,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


def _api_response(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=_api_usage(),
    )


class FakeOpenAI:
    """Class-level stand-in for openai.OpenAI (the adapter builds its own)."""

    responses: list[object] = []
    error: BaseException | None = None
    init_kwargs: list[dict[str, object]] = []
    create_kwargs: list[dict[str, object]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.init_kwargs = dict(kwargs)
        FakeOpenAI.init_kwargs.append(dict(kwargs))

    @property
    def chat(self) -> "FakeOpenAI":
        return self

    @property
    def completions(self) -> "FakeOpenAI":
        return self

    def create(self, **kwargs: object) -> object:
        FakeOpenAI.create_kwargs.append(dict(kwargs))
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        if FakeOpenAI.responses:
            return FakeOpenAI.responses.pop(0)
        return _api_response('{"ok": true}')


@pytest.fixture()
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> type[FakeOpenAI]:
    # The adapters do a lazy `from openai import OpenAI` inside the function,
    # so the fake must be visible on the real openai module at call time.
    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    FakeOpenAI.responses = []
    FakeOpenAI.error = None
    FakeOpenAI.init_kwargs = []
    FakeOpenAI.create_kwargs = []
    return FakeOpenAI


def test_deepseek_completion_missing_key_reports_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    completion = deepseek_completion("sys", "user", model="deepseek-v4-flash")

    assert completion.content is None
    assert completion.reason == "DEEPSEEK_KEY_MISSING"
    assert all(value == 0 for value in completion.usage.values())


def test_deepseek_completion_success_maps_usage_and_kwargs(
    fake_openai: type[FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    fake_openai.responses.append(_api_response('{"ok": true}'))

    completion = deepseek_completion(
        "system prompt",
        "user payload",
        model="deepseek-v4-flash",
        timeout_seconds=30.0,
        reasoning_effort="max",
    )

    assert completion.content == '{"ok": true}'
    assert completion.reason is None
    assert completion.usage == {
        "input_tokens": 11,
        "cached_input_tokens": 4,
        "output_tokens": 7,
        "reasoning_output_tokens": 3,
    }
    assert fake_openai.init_kwargs[0]["base_url"] == llm_providers.DEEPSEEK_BASE_URL
    assert fake_openai.init_kwargs[0]["api_key"] == "sk-test"
    assert fake_openai.init_kwargs[0]["timeout"] == 30.0
    kwargs = fake_openai.create_kwargs[0]
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user payload"},
    ]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["reasoning_effort"] == "max"


def test_deepseek_completion_retries_once_on_empty_content(
    fake_openai: type[FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    fake_openai.responses.append(_api_response(None))
    fake_openai.responses.append(_api_response('{"ok": true}'))

    completion = deepseek_completion("sys", "user", model="deepseek-v4-flash")

    assert completion.content == '{"ok": true}'
    assert completion.reason is None
    assert len(fake_openai.create_kwargs) == 2


def test_deepseek_completion_reports_empty_after_single_retry(
    fake_openai: type[FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    fake_openai.responses.append(_api_response(None))
    fake_openai.responses.append(_api_response(""))

    completion = deepseek_completion("sys", "user", model="deepseek-v4-flash")

    assert completion.content is None
    assert completion.reason == "DEEPSEEK_EMPTY_CONTENT"
    assert completion.usage == {
        "input_tokens": 11,
        "cached_input_tokens": 4,
        "output_tokens": 7,
        "reasoning_output_tokens": 3,
    }
    assert len(fake_openai.create_kwargs) == 2


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (_FakeApiError(401), "DEEPSEEK_AUTH_FAILED"),
        (_FakeApiError(403), "DEEPSEEK_AUTH_FAILED"),
        (_FakeApiError(429), "DEEPSEEK_RATE_LIMITED"),
        (_FakeApiError(500), "DEEPSEEK_HTTP_ERROR"),
        (TimeoutError("timed out"), "DEEPSEEK_TIMEOUT"),
        (_FakeConnectionError("no route"), "DEEPSEEK_CONNECTION_FAILED"),
        (RuntimeError("surprise"), "DEEPSEEK_FAILED"),
    ],
)
def test_deepseek_completion_maps_http_failures(
    fake_openai: type[FakeOpenAI],
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_reason: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    fake_openai.error = error

    completion = deepseek_completion("sys", "user", model="deepseek-v4-flash")

    assert completion.content is None
    assert completion.reason == expected_reason
    assert all(value == 0 for value in completion.usage.values())


def test_zhipu_completion_missing_key_reports_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)

    completion = zhipu_completion("sys", "user", model="glm-5")

    assert completion.content is None
    assert completion.reason == "ZHIPU_KEY_MISSING"
    assert all(value == 0 for value in completion.usage.values())


def test_zhipu_completion_success_passes_thinking_body(
    fake_openai: type[FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
    fake_openai.responses.append(_api_response('{"ok": true}'))

    completion = zhipu_completion(
        "system prompt",
        "user payload",
        model="glm-5",
        timeout_seconds=30.0,
        thinking=False,
        max_tokens=1024,
    )

    assert completion.content == '{"ok": true}'
    assert completion.reason is None
    assert completion.usage == {
        "input_tokens": 11,
        "cached_input_tokens": 4,
        "output_tokens": 7,
        "reasoning_output_tokens": 3,
    }
    assert fake_openai.init_kwargs[0]["base_url"] == llm_providers.ZHIPU_BASE_URL
    assert fake_openai.init_kwargs[0]["api_key"] == "zhipu-test"
    assert fake_openai.init_kwargs[0]["timeout"] == 30.0
    kwargs = fake_openai.create_kwargs[0]
    assert kwargs["model"] == "glm-5"
    assert kwargs["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user payload"},
    ]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["extra_body"] == {
        "thinking": {"type": "disabled"},
        "max_tokens": 1024,
    }


def test_zhipu_completion_default_body_enables_thinking(
    fake_openai: type[FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
    fake_openai.responses.append(_api_response('{"ok": true}'))

    completion = zhipu_completion("sys", "user", model="glm-5")

    assert completion.content == '{"ok": true}'
    assert fake_openai.create_kwargs[0]["extra_body"] == {
        "thinking": {"type": "enabled"},
        "max_tokens": 16384,
    }


def test_zhipu_completion_retries_empty_and_maps_failures(
    fake_openai: type[FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
    fake_openai.responses.append(_api_response(None))
    fake_openai.responses.append(_api_response(None))

    empty = zhipu_completion("sys", "user", model="glm-5")
    assert empty.content is None
    assert empty.reason == "ZHIPU_EMPTY_CONTENT"
    assert len(fake_openai.create_kwargs) == 2

    fake_openai.error = _FakeApiError(429)
    limited = zhipu_completion("sys", "user", model="glm-5")
    assert limited.reason == "ZHIPU_RATE_LIMITED"

    fake_openai.error = TimeoutError("timed out")
    timed_out = zhipu_completion("sys", "user", model="glm-5")
    assert timed_out.reason == "ZHIPU_TIMEOUT"


def test_validation_completers_cover_all_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)

    completers = validation_completers(SCHEMA)

    assert set(completers) == set(PROVIDER_IDS)
    assert completers["deepseek"]("sys", "user").reason == "DEEPSEEK_KEY_MISSING"
    assert completers["zhipu"]("sys", "user").reason == "ZHIPU_KEY_MISSING"


def test_zhipu_validation_timeout_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPEN_TRADER_ZHIPU_TIMEOUT_SECONDS", raising=False)
    assert zhipu_validation_timeout() == 120.0

    monkeypatch.setenv("OPEN_TRADER_ZHIPU_TIMEOUT_SECONDS", "77.5")
    assert zhipu_validation_timeout() == 77.5

    monkeypatch.setenv("OPEN_TRADER_ZHIPU_TIMEOUT_SECONDS", "not-a-number")
    assert zhipu_validation_timeout() == 120.0


def test_validation_completers_pass_zhipu_timeout_to_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPEN_TRADER_ZHIPU_TIMEOUT_SECONDS", "77")
    calls: list[dict[str, object]] = []

    def fake_completion(system: str, user: str, **kwargs: object) -> LlmCompletion:
        calls.append({"system": system, "user": user, **kwargs})
        return LlmCompletion(None, "ZHIPU_KEY_MISSING", {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0})

    monkeypatch.setattr(llm_providers, "zhipu_completion", fake_completion)

    completers = validation_completers(SCHEMA)
    result = completers["zhipu"]("sys", "user")

    assert result.reason == "ZHIPU_KEY_MISSING"
    assert len(calls) == 1
    assert calls[0]["system"] == "sys"
    assert calls[0]["user"] == "user"
    assert calls[0]["timeout_seconds"] == 77.0


def test_title_completers_cover_all_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)

    completers = title_completers(SCHEMA)

    assert set(completers) == set(PROVIDER_IDS)
    assert completers["deepseek"]("sys", "user").reason == "DEEPSEEK_KEY_MISSING"
    assert completers["zhipu"]("sys", "user").reason == "ZHIPU_KEY_MISSING"
