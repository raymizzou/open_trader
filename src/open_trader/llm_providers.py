"""Selectable LLM providers for prediction-market semantic validation.

Three interchangeable transports share one completion contract so operators
can switch the active provider at runtime:

- codex: local Codex CLI subprocess (login-based auth, no API key)
- deepseek: DeepSeek OpenAI-compatible HTTP API (DEEPSEEK_API_KEY)
- zhipu: Zhipu GLM OpenAI-compatible HTTP API (ZHIPU_API_KEY)

Every adapter returns an :class:`LlmCompletion` with either content or a
provider-prefixed failure reason (CODEX_*, DEEPSEEK_*, ZHIPU_*), never both
and never an exception.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PROVIDER_IDS = ("codex", "deepseek", "zhipu")
DEFAULT_PROVIDER = "deepseek"
PROVIDER_LABELS = {
    "codex": "Codex",
    "deepseek": "DeepSeek",
    "zhipu": "智谱 GLM",
}

CODEX_DEFAULT_MODEL = "gpt-5.6-sol"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
ZHIPU_DEFAULT_MODEL = "glm-5"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

_PROVIDER_MODEL_ENV = {
    "codex": "OPEN_TRADER_CODEX_MODEL",
    "deepseek": "OPEN_TRADER_DEEPSEEK_MODEL",
    "zhipu": "OPEN_TRADER_ZHIPU_MODEL",
}
_PROVIDER_DEFAULT_MODEL = {
    "codex": CODEX_DEFAULT_MODEL,
    "deepseek": DEEPSEEK_DEFAULT_MODEL,
    "zhipu": ZHIPU_DEFAULT_MODEL,
}

USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

Completion = Callable[[str, str], "LlmCompletion"]


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    """One provider attempt: exactly one of content/reason is set."""

    content: str | None
    reason: str | None
    usage: dict[str, int]


def resolve_provider(value: object, *, default: str = DEFAULT_PROVIDER) -> str:
    """Return a validated provider id, falling back to the default."""

    candidate = str(value or "").strip().lower()
    return candidate if candidate in PROVIDER_IDS else default


def infer_provider_from_model(model: object) -> str:
    """Best-effort provider attribution for cache rows written pre-provider."""

    lowered = str(model or "").lower()
    if "deepseek" in lowered:
        return "deepseek"
    if "glm" in lowered or "zhipu" in lowered:
        return "zhipu"
    return "codex"


def provider_credentials_configured() -> dict[str, bool]:
    """Which providers currently have usable credentials in this process."""

    return {
        "codex": True,
        "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "zhipu": bool(os.environ.get("ZHIPU_API_KEY")),
    }


def provider_model(provider: str) -> str:
    """Resolve one provider's model from env with its shipped default."""

    if provider not in PROVIDER_IDS:
        raise ValueError(f"unknown llm provider: {provider}")
    default = _PROVIDER_DEFAULT_MODEL[provider]
    resolved = (os.environ.get(_PROVIDER_MODEL_ENV[provider], default) or default).strip()
    return resolved or default


def provider_models() -> dict[str, str]:
    return {provider: provider_model(provider) for provider in PROVIDER_IDS}


def deepseek_reasoning_effort() -> str | None:
    return os.environ.get("OPEN_TRADER_DEEPSEEK_REASONING_EFFORT", "max") or None


def zhipu_validation_timeout() -> float:
    """Validation timeout for glm-5 thinking audits (P95≈93s measured)."""

    try:
        return float(
            os.environ.get("OPEN_TRADER_ZHIPU_TIMEOUT_SECONDS", "120") or 120
        )
    except ValueError:
        return 120.0


REASON_SUMMARIES = {
    "TIMEOUT": "{label} 语义校验超时，当前不可下单。",
    "FAILED": "{label} 语义校验不可用，当前不可下单。",
    "OUTPUT_INVALID": "{label} 返回的结构化结果无效，当前不可下单。",
    "BUDGET_EXHAUSTED": "{label} 校验额度已耗尽，当前不可下单。",
    "CIRCUIT_OPEN": "{label} 连续失败已临时熔断，暂停新校验，稍后自动恢复。",
    "KEY_MISSING": "{label} API Key 未配置。",
    "EMPTY_CONTENT": "{label} 返回空内容，当前不可下单。",
    "CONNECTION_FAILED": "{label} 网络连接失败，当前不可下单。",
    "AUTH_FAILED": "{label} 认证失败，当前不可下单。",
    "RATE_LIMITED": "{label} 限流，当前不可下单。",
    "HTTP_ERROR": "{label} API 请求失败，当前不可下单。",
}


def reason_summary(reason_code: str) -> str | None:
    """Render a provider-prefixed reason code as operator-facing copy."""

    for provider in PROVIDER_IDS:
        prefix = f"{provider.upper()}_"
        if reason_code.startswith(prefix):
            template = REASON_SUMMARIES.get(reason_code[len(prefix) :])
            if template is not None:
                return template.format(label=PROVIDER_LABELS[provider])
            return None
    return None


def _normalized_usage(usage: Mapping[str, object] | None) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for field in USAGE_FIELDS:
        value = (usage or {}).get(field, 0)
        normalized[field] = (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )
    return normalized


def _codex_events(stdout: str) -> tuple[str | None, dict[str, int]]:
    """Extract the final agent message and usage from codex exec JSONL."""

    final_message: str | None = None
    usage: Mapping[str, object] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None, {}
        if not isinstance(event, Mapping):
            return None, {}
        if event.get("type") == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, Mapping)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                final_message = str(item["text"])
        elif event.get("type") == "turn.completed":
            candidate = event.get("usage")
            if isinstance(candidate, Mapping):
                usage = candidate
    return final_message, _normalized_usage(usage)


def codex_completion(
    system: str,
    user: str,
    *,
    model: str,
    output_schema: Path,
    timeout_seconds: float = 45.0,
    extra_args: Sequence[str] = (),
    user_label: str = "INPUT JSON",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> LlmCompletion:
    """Run one isolated, read-only codex exec with structured output."""

    command = [
        "codex",
        "exec",
        "--model",
        model,
        *extra_args,
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--disable",
        "hooks",
        "--output-schema",
        str(output_schema),
        "--json",
        "-",
    ]
    prompt = f"{system}\n{user_label}\n{user}\n"
    try:
        with tempfile.TemporaryDirectory(prefix="open-trader-codex-") as working_dir:
            completed = runner(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=working_dir,
                timeout=timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return LlmCompletion(None, "CODEX_TIMEOUT", _normalized_usage(None))
    except Exception:
        return LlmCompletion(None, "CODEX_FAILED", _normalized_usage(None))
    if completed.returncode != 0:
        return LlmCompletion(None, "CODEX_FAILED", _normalized_usage(None))
    content, usage = _codex_events(completed.stdout or "")
    if content is None:
        return LlmCompletion(None, "CODEX_OUTPUT_INVALID", usage)
    return LlmCompletion(content, None, usage)


def _api_usage(response: object) -> dict[str, int]:
    usage = getattr(response, "usage", None)

    def _num(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return _normalized_usage(
        {
            "input_tokens": _num("prompt_tokens"),
            "cached_input_tokens": getattr(details, "cached_tokens", 0)
            if details is not None
            else 0,
            "output_tokens": _num("completion_tokens"),
            "reasoning_output_tokens": getattr(
                completion_details, "reasoning_tokens", 0
            )
            if completion_details is not None
            else 0,
        }
    )


def _http_failure_reason(prefix: str, exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return f"{prefix}_AUTH_FAILED"
    if status == 429:
        return f"{prefix}_RATE_LIMITED"
    if status is not None:
        return f"{prefix}_HTTP_ERROR"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return f"{prefix}_TIMEOUT"
    if "connection" in type(exc).__name__.lower():
        return f"{prefix}_CONNECTION_FAILED"
    return f"{prefix}_FAILED"


def deepseek_completion(
    system: str,
    user: str,
    *,
    model: str,
    timeout_seconds: float = 60.0,
    reasoning_effort: str | None = None,
) -> LlmCompletion:
    """Call the DeepSeek OpenAI-compatible chat API once (one empty retry)."""

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return LlmCompletion(None, "DEEPSEEK_KEY_MISSING", _normalized_usage(None))
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=timeout_seconds,
        )

        def create() -> object:
            kwargs: dict[str, object] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "timeout": timeout_seconds,
            }
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            return client.chat.completions.create(**kwargs)

        response = create()
        content = response.choices[0].message.content
        if not content:
            # One immediate retry: transient empty responses are common.
            response = create()
            content = response.choices[0].message.content
        usage = _api_usage(response)
        if not content:
            return LlmCompletion(None, "DEEPSEEK_EMPTY_CONTENT", usage)
        return LlmCompletion(content, None, usage)
    except Exception as exc:
        return LlmCompletion(
            None, _http_failure_reason("DEEPSEEK", exc), _normalized_usage(None)
        )


def zhipu_completion(
    system: str,
    user: str,
    *,
    model: str,
    timeout_seconds: float = 60.0,
    thinking: bool = True,
    max_tokens: int = 16384,
) -> LlmCompletion:
    """Call the Zhipu GLM OpenAI-compatible chat API once (one empty retry)."""

    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        return LlmCompletion(None, "ZHIPU_KEY_MISSING", _normalized_usage(None))
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=ZHIPU_BASE_URL,
            timeout=timeout_seconds,
        )

        def create() -> object:
            return client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                extra_body={
                    "thinking": {"type": "enabled" if thinking else "disabled"},
                    "max_tokens": max_tokens,
                },
                timeout=timeout_seconds,
            )

        response = create()
        content = response.choices[0].message.content
        if not content:
            # One immediate retry: transient empty responses are common.
            response = create()
            content = response.choices[0].message.content
        usage = _api_usage(response)
        if not content:
            return LlmCompletion(None, "ZHIPU_EMPTY_CONTENT", usage)
        return LlmCompletion(content, None, usage)
    except Exception as exc:
        return LlmCompletion(
            None, _http_failure_reason("ZHIPU", exc), _normalized_usage(None)
        )


def validation_completers(
    output_schema: Path, *, timeout_seconds: float = 45.0
) -> dict[str, Completion]:
    """Default per-provider completers for semantic validation prompts."""

    return {
        "codex": lambda system, user: codex_completion(
            system,
            user,
            model=provider_model("codex"),
            output_schema=output_schema,
            timeout_seconds=timeout_seconds,
        ),
        "deepseek": lambda system, user: deepseek_completion(
            system,
            user,
            model=provider_model("deepseek"),
            reasoning_effort=deepseek_reasoning_effort(),
        ),
        "zhipu": lambda system, user: zhipu_completion(
            system,
            user,
            model=provider_model("zhipu"),
            timeout_seconds=zhipu_validation_timeout(),
        ),
    }


def title_completers(output_schema: Path) -> dict[str, Completion]:
    """Default per-provider completers for title translation prompts."""

    return {
        "codex": lambda system, user: codex_completion(
            system,
            user,
            model=provider_model("codex"),
            output_schema=output_schema,
            timeout_seconds=45.0,
            extra_args=(
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                'service_tier="priority"',
            ),
            user_label="UNTRUSTED TITLE",
        ),
        "deepseek": lambda system, user: deepseek_completion(
            system,
            user,
            model=provider_model("deepseek"),
            timeout_seconds=30.0,
            reasoning_effort=None,
        ),
        "zhipu": lambda system, user: zhipu_completion(
            system,
            user,
            model=provider_model("zhipu"),
            timeout_seconds=30.0,
            thinking=False,
            max_tokens=1024,
        ),
    }
