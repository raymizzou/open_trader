from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol


SCHEMA_VERSION = "open_trader.t_signal.v1"
SESSION_PHASES = {"pre_market", "regular", "post_market", "closed", "unknown"}
ACTIONS = {"BUY_T", "SELL_T", "HOLD", "REVIEW"}
SUGGESTED_RATIOS = {"", "6", "10", "15", "20"}
DEPTH_STATUSES = {"pass", "thin", "wide_spread", "missing"}
PRICE_POSITIONS = {
    "near_support",
    "near_resistance",
    "below_vwap_reclaim",
    "above_vwap_reject",
    "middle_range",
    "breakout",
    "breakdown",
    "unknown",
}
TREND_STATES = {
    "range_rebound",
    "range_fade",
    "uptrend",
    "downtrend",
    "choppy",
    "unknown",
}
GATE_STATUSES = {"pass", "block", "warn", "missing"}
EVIDENCE_DIRECTIONS = {"buy", "sell", "neutral", "risk"}
EVIDENCE_STRENGTHS = {"low", "medium", "high"}
TIMELINE_EVENT_TYPES = {
    "signal_created",
    "signal_changed",
    "notification_sent",
    "notification_suppressed",
    "signal_expired",
    "review_required",
}
STATUSES = {"ok", "review", "blocked", "error", "stale"}
AI_INTERPRETATION_FIELDS = {
    "action",
    "suggested_ratio",
    "signal_summary_zh",
    "ratio_rationale_zh",
    "evidence_refs",
}
RAW_ENGLISH_PROSE_PATTERN = re.compile(r"\b[A-Za-z]+(?:\s+[A-Za-z]+){2,}\b")
NUMERIC_LITERAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9])"
)
T_SIGNAL_INTERPRETER_PROMPT = """你是做T信号解释器。
只能解释系统给出的结构化信号，不得改写 action，不得改写 suggested_ratio。
只能引用 payload.evidence 中已经存在的 name，不得编造价格、指标、盘口或成交量。
用户可见字段必须使用中文，不要输出英文交易建议。
返回 JSON object，字段固定为：
action, suggested_ratio, signal_summary_zh, ratio_rationale_zh, evidence_refs。
"""


@dataclass(frozen=True)
class TSignalPrice:
    last_price: str
    day_change_pct: str
    vwap: str
    ma_1m: str
    ma_5m: str
    day_low: str
    day_high: str


@dataclass(frozen=True)
class TSignalLiquidity:
    bid: str
    ask: str
    spread_pct: str
    bid_depth: str
    ask_depth: str
    depth_status: str


@dataclass(frozen=True)
class TSignalTechnical:
    rsi_5m: str
    volume_ratio_5m: str
    price_position: str
    trend_state: str


@dataclass(frozen=True)
class TSignalHardGate:
    name: str
    status: str
    message_zh: str


@dataclass(frozen=True)
class TSignalEvidence:
    name: str
    direction: str
    strength: str
    message_zh: str


@dataclass(frozen=True)
class TSignalTimelineEvent:
    event_at: str
    event_type: str
    action: str
    suggested_ratio: str
    message_zh: str


@dataclass(frozen=True)
class TSignalNotification:
    should_notify: bool
    notified: bool
    dedupe_key: str
    last_notified_at: str


@dataclass(frozen=True)
class TSignal:
    schema_version: str
    run_date: str
    market: str
    symbol: str
    futu_symbol: str
    name: str
    session_phase: str
    updated_at: str
    action: str
    suggested_ratio: str
    current_status: str
    signal_summary_zh: str
    price: TSignalPrice
    liquidity: TSignalLiquidity
    technical: TSignalTechnical
    hard_gates: list[TSignalHardGate]
    evidence: list[TSignalEvidence]
    timeline: list[TSignalTimelineEvent]
    notification: TSignalNotification
    status: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        validate_t_signal(self)
        return asdict(self)

    def with_field(self, name: str, value: object) -> TSignal:
        return replace(self, **{name: value})


@dataclass(frozen=True)
class TSignalAIInterpretation:
    action: str
    suggested_ratio: str
    signal_summary_zh: str
    ratio_rationale_zh: str
    evidence_refs: list[str]


class TSignalInterpreterClient(Protocol):
    def interpret(self, prompt: str, payload: dict[str, object]) -> str:
        ...


class OpenAITSignalInterpreterClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        from openai import OpenAI

        from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for t signal interpretation")

        self._client = OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=timeout_seconds,
        )
        self._model = model or DEFAULT_CLASSIFIER_MODEL
        self._timeout_seconds = timeout_seconds

    def interpret(self, prompt: str, payload: dict[str, object]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            response_format={"type": "json_object"},
            timeout=self._timeout_seconds,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("model returned empty content")
        return content


class TSignalInterpreter:
    def __init__(
        self,
        *,
        client: TSignalInterpreterClient | None = None,
        prompt: str = T_SIGNAL_INTERPRETER_PROMPT,
    ) -> None:
        self._client = client
        self._prompt = prompt

    def interpret(self, signal: TSignal) -> TSignal:
        try:
            client = self._client or OpenAITSignalInterpreterClient()
            raw = client.interpret(
                self._prompt,
                build_ai_interpretation_payload(signal),
            )
        except Exception as exc:
            return _degrade_signal_for_ai_error(signal, str(exc))
        return apply_ai_interpretation(
            signal,
            raw,
        )


def validate_t_signal(signal: TSignal) -> None:
    _require_member("schema_version", signal.schema_version, {SCHEMA_VERSION})
    _require_member("session_phase", signal.session_phase, SESSION_PHASES)
    _require_member("action", signal.action, ACTIONS)
    _require_member("suggested_ratio", signal.suggested_ratio, SUGGESTED_RATIOS)
    _require_member("status", signal.status, STATUSES)

    _require_member("depth_status", signal.liquidity.depth_status, DEPTH_STATUSES)
    _require_member("price_position", signal.technical.price_position, PRICE_POSITIONS)
    _require_member("trend_state", signal.technical.trend_state, TREND_STATES)

    for gate in signal.hard_gates:
        _require_member("hard_gates.status", gate.status, GATE_STATUSES)

    for item in signal.evidence:
        _require_member("evidence.direction", item.direction, EVIDENCE_DIRECTIONS)
        _require_member("evidence.strength", item.strength, EVIDENCE_STRENGTHS)

    for event in signal.timeline:
        _require_member("timeline.event_type", event.event_type, TIMELINE_EVENT_TYPES)
        _require_member("timeline.action", event.action, ACTIONS)
        _require_member("timeline.suggested_ratio", event.suggested_ratio, SUGGESTED_RATIOS)
        _validate_ratio_invariant(event.action, event.suggested_ratio)

    _validate_ratio_invariant(signal.action, signal.suggested_ratio)


def _validate_ratio_invariant(action: str, suggested_ratio: str) -> None:
    if action in {"BUY_T", "SELL_T"} and not suggested_ratio:
        raise ValueError(f"{action} requires suggested_ratio")
    if action in {"HOLD", "REVIEW"} and suggested_ratio:
        raise ValueError(f"{action} requires empty suggested_ratio")


def _require_member(field_name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {field_name}: {value}")


@dataclass(frozen=True)
class TPortfolioBaseline:
    total_quantity: Decimal


@dataclass(frozen=True)
class TMarketFacts:
    run_date: str
    market: str
    symbol: str
    futu_symbol: str
    name: str
    session_phase: str
    updated_at: str
    last_price: Decimal | None
    day_change_pct: Decimal | None
    vwap: Decimal | None
    ma_1m: Decimal | None
    ma_5m: Decimal | None
    day_low: Decimal | None
    day_high: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    bid_depth: Decimal | None
    ask_depth: Decimal | None
    rsi_5m: Decimal | None
    volume_ratio_5m: Decimal | None

    def with_field(self, name: str, value: object) -> TMarketFacts:
        return replace(self, **{name: value})


def to_futu_symbol(market: str, symbol: str) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if normalized_market not in {"HK", "US"}:
        raise ValueError(f"unsupported market for t signal: {market}")
    if "." in normalized_symbol:
        prefix, normalized_symbol = normalized_symbol.split(".", 1)
        if prefix == normalized_market:
            pass
        elif normalized_market == "US" and prefix not in {"HK", "US", "CN"}:
            normalized_symbol = f"{prefix}.{normalized_symbol}"
        else:
            raise ValueError(
                f"symbol prefix {prefix} does not match market {normalized_market}"
            )
    if not normalized_symbol:
        raise ValueError(f"empty symbol for market {normalized_market}")
    if normalized_market == "HK" and normalized_symbol.isdigit():
        if len(normalized_symbol) > 5:
            raise ValueError(f"invalid HK symbol length: {symbol}")
        return f"HK.{normalized_symbol.zfill(5)}"
    if normalized_market == "US":
        return f"US.{normalized_symbol}"
    raise ValueError(f"invalid symbol for market {normalized_market}: {symbol}")


def ratio_from_score(score: int) -> str:
    if score <= 0:
        return ""
    if score == 1:
        return "6"
    if score == 2:
        return "10"
    if score == 3:
        return "15"
    return "20"


def build_t_signal_from_facts(
    *,
    facts: TMarketFacts,
    baseline: TPortfolioBaseline,
    previous: TSignal | None,
    ai_summary_zh: str,
) -> TSignal:
    # Cycle state and duplicate suppression are handled by the watcher layer.
    del previous

    session_phase = _normalize_session_phase(facts.session_phase)
    futu_symbol, symbol_error = _canonicalize_fact_symbols(facts)
    liquidity = _build_liquidity(facts)
    technical = _build_technical(facts)
    hard_gates = _build_hard_gates(
        facts,
        baseline,
        liquidity,
        symbol_error,
        session_phase,
    )
    evidence, buy_score, sell_score = _build_evidence(facts, technical)
    has_blocker = any(gate.status == "block" for gate in hard_gates)

    if has_blocker:
        action = "REVIEW"
        suggested_ratio = ""
        status = "review"
        current_status = "硬性条件未通过，需要人工复核。"
        event_type = "review_required"
    elif technical.price_position == "below_vwap_reclaim" and buy_score > 0:
        action = "BUY_T"
        suggested_ratio = ratio_from_score(buy_score)
        status = "ok"
        current_status = "BUY_T 条件满足，等待执行确认。"
        event_type = "signal_created"
    elif technical.price_position == "above_vwap_reject" and sell_score > 0:
        action = "SELL_T"
        suggested_ratio = ratio_from_score(sell_score)
        status = "ok"
        current_status = "SELL_T 条件满足，等待执行确认。"
        event_type = "signal_created"
    else:
        action = "HOLD"
        suggested_ratio = ""
        status = "ok"
        current_status = "暂无明确做T信号，继续观察。"
        event_type = "signal_created"

    signal = TSignal(
        schema_version=SCHEMA_VERSION,
        run_date=facts.run_date,
        market=facts.market.strip().upper(),
        symbol=_normalize_display_symbol(facts.market, facts.symbol),
        futu_symbol=futu_symbol,
        name=facts.name,
        session_phase=session_phase,
        updated_at=facts.updated_at,
        action=action,
        suggested_ratio=suggested_ratio,
        current_status=current_status,
        signal_summary_zh=_build_summary(action, suggested_ratio, ai_summary_zh, has_blocker),
        price=TSignalPrice(
            last_price=_decimal_text(facts.last_price),
            day_change_pct=_decimal_text(facts.day_change_pct),
            vwap=_decimal_text(facts.vwap),
            ma_1m=_decimal_text(facts.ma_1m),
            ma_5m=_decimal_text(facts.ma_5m),
            day_low=_decimal_text(facts.day_low),
            day_high=_decimal_text(facts.day_high),
        ),
        liquidity=liquidity,
        technical=technical,
        hard_gates=hard_gates,
        evidence=evidence,
        timeline=[
            TSignalTimelineEvent(
                event_at=facts.updated_at,
                event_type=event_type,
                action=action,
                suggested_ratio=suggested_ratio,
                message_zh=_timeline_message(action, suggested_ratio),
            )
        ],
        notification=TSignalNotification(
            should_notify=action in {"BUY_T", "SELL_T"},
            notified=False,
            dedupe_key=f"{facts.run_date}|{futu_symbol}|{action}|{suggested_ratio}",
            last_notified_at="",
        ),
        status=status,
        error="",
    )
    validate_t_signal(signal)
    return signal


def build_ai_interpretation_payload(signal: TSignal) -> dict[str, object]:
    validate_t_signal(signal)
    return {
        "schema_version": signal.schema_version,
        "run_date": signal.run_date,
        "market": signal.market,
        "symbol": signal.symbol,
        "futu_symbol": signal.futu_symbol,
        "name": signal.name,
        "session_phase": signal.session_phase,
        "updated_at": signal.updated_at,
        "action": signal.action,
        "suggested_ratio": signal.suggested_ratio,
        "current_status": signal.current_status,
        "price": asdict(signal.price),
        "liquidity": asdict(signal.liquidity),
        "technical": asdict(signal.technical),
        "hard_gates": [asdict(gate) for gate in signal.hard_gates],
        "evidence": [asdict(item) for item in signal.evidence],
    }


def validate_ai_interpretation_output(
    raw: str,
    signal: TSignal,
) -> TSignalAIInterpretation:
    if not isinstance(raw, str):
        raise ValueError("AI interpretation response must be string")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI interpretation invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("AI interpretation must be an object")

    fields = set(data)
    missing = sorted(AI_INTERPRETATION_FIELDS - fields)
    if missing:
        raise ValueError(f"AI interpretation missing field(s): {', '.join(missing)}")
    extra = sorted(fields - AI_INTERPRETATION_FIELDS)
    if extra:
        raise ValueError(f"AI interpretation unexpected field(s): {', '.join(extra)}")

    action = data["action"]
    suggested_ratio = data["suggested_ratio"]
    if not isinstance(action, str):
        raise ValueError("AI interpretation action must be string")
    if not isinstance(suggested_ratio, str):
        raise ValueError("AI interpretation suggested_ratio must be string")
    if action not in ACTIONS:
        raise ValueError(f"AI interpretation invalid action: {action}")
    if suggested_ratio not in SUGGESTED_RATIOS:
        raise ValueError(f"AI interpretation invalid suggested_ratio: {suggested_ratio}")
    if action != signal.action:
        raise ValueError("AI interpretation action does not match rule action")
    if suggested_ratio != signal.suggested_ratio:
        raise ValueError("AI interpretation ratio does not match rule ratio")

    summary = _require_chinese_text(data["signal_summary_zh"], "signal_summary_zh")
    rationale = _require_chinese_text(data["ratio_rationale_zh"], "ratio_rationale_zh")
    _validate_visible_numeric_facts(summary, signal, "signal_summary_zh")
    _validate_visible_numeric_facts(rationale, signal, "ratio_rationale_zh")
    evidence_refs = _validate_evidence_refs(data["evidence_refs"], signal)

    return TSignalAIInterpretation(
        action=action,
        suggested_ratio=suggested_ratio,
        signal_summary_zh=summary,
        ratio_rationale_zh=rationale,
        evidence_refs=evidence_refs,
    )


def apply_ai_interpretation(signal: TSignal, raw: str) -> TSignal:
    try:
        interpretation = validate_ai_interpretation_output(raw, signal)
    except ValueError as exc:
        return _degrade_signal_for_ai_error(signal, str(exc))

    interpreted = replace(
        signal,
        signal_summary_zh=(
            f"{interpretation.signal_summary_zh}"
            f" 比例依据：{interpretation.ratio_rationale_zh}"
        ),
        error="",
    )
    validate_t_signal(interpreted)
    return interpreted


def _degrade_signal_for_ai_error(signal: TSignal, error: str) -> TSignal:
    degraded = replace(
        signal,
        action="REVIEW",
        suggested_ratio="",
        current_status="AI 解读未通过验证，需要人工复核。",
        signal_summary_zh=(
            "AI 解读未通过验证，转入人工复核。"
            f"规则信号：{signal.action}，建议比例 {signal.suggested_ratio or '无'}。"
        ),
        timeline=[
            *signal.timeline,
            TSignalTimelineEvent(
                event_at=signal.updated_at,
                event_type="review_required",
                action="REVIEW",
                suggested_ratio="",
                message_zh="AI 解读未通过验证，转入人工复核。",
            ),
        ],
        notification=replace(signal.notification, should_notify=False),
        status="review",
        error=f"AI interpretation invalid: {error}",
    )
    validate_t_signal(degraded)
    return degraded


def _require_chinese_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"AI interpretation {field_name} must be non-empty string")
    text = value.strip()
    if not any("\u3400" <= char <= "\u9fff" for char in text):
        raise ValueError(f"AI interpretation {field_name} must be Chinese text")
    _reject_disallowed_english_trading_phrase(text, field_name)
    return text


def _reject_disallowed_english_trading_phrase(text: str, field_name: str) -> None:
    lowered = text.lower()
    blocked_phrases = (
        "buy now",
        "sell now",
        "stop loss",
        "take profit",
        "position sizing",
        "place order",
        "submit order",
    )
    for phrase in blocked_phrases:
        if phrase in lowered:
            raise ValueError(
                f"AI interpretation {field_name} contains disallowed English phrase"
            )
    if RAW_ENGLISH_PROSE_PATTERN.search(text):
        raise ValueError(
            f"AI interpretation {field_name} contains raw English prose"
        )


def _validate_visible_numeric_facts(text: str, signal: TSignal, field_name: str) -> None:
    allowed_ratio = _canonical_decimal_text(signal.suggested_ratio)
    for match in NUMERIC_LITERAL_PATTERN.finditer(text):
        literal = match.group(0)
        if _is_timeframe_label(text, match.end()):
            continue
        if (
            allowed_ratio
            and _canonical_decimal_text(literal) == allowed_ratio
            and _is_percent_literal(text, match.end())
        ):
            continue
        raise ValueError(
            f"AI interpretation {field_name} invented numeric fact: {literal}"
        )


def _is_timeframe_label(text: str, match_end: int) -> bool:
    return text[match_end : match_end + 2] == "分钟"


def _is_percent_literal(text: str, match_end: int) -> bool:
    index = match_end
    while index < len(text) and text[index].isspace():
        index += 1
    return index < len(text) and text[index] == "%"


def _canonical_decimal_text(value: str) -> str:
    text = str(value or "").strip().rstrip("%")
    if not text:
        return ""
    try:
        decimal = Decimal(text)
    except InvalidOperation:
        return ""
    if not decimal.is_finite():
        return ""
    return format(decimal.normalize(), "f")


def _validate_evidence_refs(value: object, signal: TSignal) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("AI interpretation evidence_refs must be a non-empty list")
    evidence_names = {item.name for item in signal.evidence}
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("AI interpretation evidence_refs must contain strings")
        ref = item.strip()
        if ref not in evidence_names:
            raise ValueError(f"AI interpretation unknown evidence ref: {ref}")
        refs.append(ref)
    return refs


def _build_liquidity(facts: TMarketFacts) -> TSignalLiquidity:
    spread_pct = _spread_pct(facts.bid, facts.ask)
    depth_status = _depth_status(facts, spread_pct)
    return TSignalLiquidity(
        bid=_decimal_text(facts.bid),
        ask=_decimal_text(facts.ask),
        spread_pct=_decimal_text(spread_pct),
        bid_depth=_decimal_text(facts.bid_depth),
        ask_depth=_decimal_text(facts.ask_depth),
        depth_status=depth_status,
    )


def _normalize_session_phase(session_phase: str) -> str:
    if session_phase in SESSION_PHASES:
        return session_phase
    return "unknown"


def _canonicalize_fact_symbols(facts: TMarketFacts) -> tuple[str, str]:
    try:
        symbol_futu = to_futu_symbol(facts.market, facts.symbol)
        if not facts.futu_symbol:
            return symbol_futu, ""
        explicit_futu = to_futu_symbol(facts.market, facts.futu_symbol)
    except ValueError as exc:
        return "", str(exc)
    if explicit_futu != symbol_futu:
        return (
            symbol_futu,
            f"symbol {symbol_futu} does not match futu_symbol {explicit_futu}",
        )
    return explicit_futu, ""


def _normalize_display_symbol(market: str, symbol: str) -> str:
    normalized_market = market.strip().upper()
    normalized_symbol = symbol.strip().upper()
    if normalized_symbol.startswith(f"{normalized_market}."):
        normalized_symbol = normalized_symbol.split(".", 1)[1]
    if normalized_market == "HK" and normalized_symbol.isdigit():
        return normalized_symbol.zfill(5)
    return normalized_symbol


def _spread_pct(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if not _is_positive_finite_decimal(bid) or not _is_positive_finite_decimal(ask):
        return None
    if ask <= bid:
        return None
    midpoint = (bid + ask) / Decimal("2")
    if midpoint <= 0:
        return None
    return ((ask - bid) / midpoint * Decimal("100")).quantize(Decimal("0.001"))


def _depth_status(facts: TMarketFacts, spread_pct: Decimal | None) -> str:
    if (
        facts.bid is None
        or facts.ask is None
        or facts.bid_depth is None
        or facts.ask_depth is None
        or not _is_finite_decimal(facts.bid)
        or not _is_finite_decimal(facts.ask)
        or not _is_finite_decimal(facts.bid_depth)
        or not _is_finite_decimal(facts.ask_depth)
        or spread_pct is None
    ):
        return "missing"
    if spread_pct > Decimal("0.30"):
        return "wide_spread"
    if facts.bid_depth <= 0 or facts.ask_depth <= 0:
        return "thin"
    return "pass"


def _build_technical(facts: TMarketFacts) -> TSignalTechnical:
    price_position = _price_position(facts)
    trend_state = _trend_state(price_position)
    return TSignalTechnical(
        rsi_5m=_decimal_text(facts.rsi_5m),
        volume_ratio_5m=_decimal_text(facts.volume_ratio_5m),
        price_position=price_position,
        trend_state=trend_state,
    )


def _price_position(facts: TMarketFacts) -> str:
    if not _is_finite_decimal(facts.last_price) or not _is_finite_decimal(facts.vwap):
        return "unknown"
    if facts.last_price < facts.vwap:
        return "below_vwap_reclaim"
    if facts.last_price > facts.vwap:
        return "above_vwap_reject"
    return "middle_range"


def _trend_state(price_position: str) -> str:
    if price_position == "below_vwap_reclaim":
        return "range_rebound"
    if price_position == "above_vwap_reject":
        return "range_fade"
    if price_position == "middle_range":
        return "choppy"
    return "unknown"


def _build_hard_gates(
    facts: TMarketFacts,
    baseline: TPortfolioBaseline,
    liquidity: TSignalLiquidity,
    symbol_error: str,
    session_phase: str,
) -> list[TSignalHardGate]:
    session_status = "pass" if session_phase == "regular" else "block"
    baseline_status = (
        "pass" if _is_positive_finite_decimal(baseline.total_quantity) else "block"
    )
    technical_status = "pass" if _has_required_technical_facts(facts) else "block"
    liquidity_status = "pass" if liquidity.depth_status == "pass" else "block"
    symbol_status = "pass" if not symbol_error else "block"
    return [
        TSignalHardGate(
            name="session_phase",
            status=session_status,
            message_zh=(
                "当前处于盘中交易时段。"
                if session_status == "pass"
                else "非盘中交易时段，只允许观察。"
            ),
        ),
        TSignalHardGate(
            name="baseline",
            status=baseline_status,
            message_zh=(
                "底仓数量满足做T前提。"
                if baseline_status == "pass"
                else "底仓数量为空或无效，不能生成买卖动作。"
            ),
        ),
        TSignalHardGate(
            name="technical",
            status=technical_status,
            message_zh=(
                "盘中技术指标完整。"
                if technical_status == "pass"
                else "盘中技术指标缺失或异常，需要人工复核。"
            ),
        ),
        TSignalHardGate(
            name="liquidity",
            status=liquidity_status,
            message_zh=(
                "买卖盘和价差满足流动性要求。"
                if liquidity_status == "pass"
                else "买卖盘缺失、过薄或价差过大，需要人工复核。"
            ),
        ),
        TSignalHardGate(
            name="symbol",
            status=symbol_status,
            message_zh=(
                "富途代码与市场匹配。"
                if symbol_status == "pass"
                else f"富途代码无法规范化：{symbol_error}"
            ),
        ),
    ]


def _build_evidence(
    facts: TMarketFacts,
    technical: TSignalTechnical,
) -> tuple[list[TSignalEvidence], int, int]:
    evidence: list[TSignalEvidence] = []
    buy_score = 0
    sell_score = 0
    if facts.session_phase not in SESSION_PHASES:
        evidence.append(
            TSignalEvidence(
                name="unsupported_session_phase",
                direction="risk",
                strength="medium",
                message_zh="交易时段无法识别，已转入人工复核。",
            )
        )
    if technical.price_position == "below_vwap_reclaim":
        evidence.append(
            TSignalEvidence(
                name="vwap_reclaim",
                direction="buy",
                strength="medium",
                message_zh="价格低于 VWAP 后回收，出现低吸做T信号。",
            )
        )
        buy_score += 1
    if technical.price_position == "above_vwap_reject":
        evidence.append(
            TSignalEvidence(
                name="vwap_reject",
                direction="sell",
                strength="medium",
                message_zh="价格高于 VWAP 后受压，出现高抛做T信号。",
            )
        )
        sell_score += 1
    if _is_finite_decimal(facts.rsi_5m) and facts.rsi_5m <= Decimal("40"):
        evidence.append(
            TSignalEvidence(
                name="rsi_rebound_zone",
                direction="buy",
                strength="medium",
                message_zh="5分钟 RSI 处于偏低区间，反弹信号更明确。",
            )
        )
        buy_score += 1
    if _is_finite_decimal(facts.rsi_5m) and facts.rsi_5m >= Decimal("60"):
        evidence.append(
            TSignalEvidence(
                name="rsi_reject_zone",
                direction="sell",
                strength="medium",
                message_zh="5分钟 RSI 处于偏高区间，回落信号更明确。",
            )
        )
        sell_score += 1
    if (
        _is_finite_decimal(facts.volume_ratio_5m)
        and facts.volume_ratio_5m >= Decimal("1.20")
    ):
        if technical.price_position == "above_vwap_reject":
            direction = "sell"
            message_zh = "5分钟量比放大，价格受压具备成交配合。"
        elif technical.price_position == "below_vwap_reclaim":
            direction = "buy"
            message_zh = "5分钟量比放大，价格回收具备成交配合。"
        else:
            direction = "neutral"
            message_zh = "5分钟量比放大，但价格位置尚未形成明确方向。"
        evidence.append(
            TSignalEvidence(
                name="volume_confirm",
                direction=direction,
                strength="low",
                message_zh=message_zh,
            )
        )
        if direction == "sell":
            sell_score += 1
        elif direction == "buy":
            buy_score += 1
    if not evidence:
        evidence.append(
            TSignalEvidence(
                name="no_clear_edge",
                direction="neutral",
                strength="low",
                message_zh="当前技术条件没有形成明确做T优势。",
            )
        )
    return evidence, buy_score, sell_score


def _build_summary(
    action: str,
    suggested_ratio: str,
    ai_summary_zh: str,
    has_blocker: bool,
) -> str:
    if has_blocker:
        return f"硬性条件未通过，转入人工复核。{ai_summary_zh}"
    if action == "BUY_T":
        return f"触发 BUY_T，建议比例 {suggested_ratio}%。{ai_summary_zh}"
    if action == "SELL_T":
        return f"触发 SELL_T，建议比例 {suggested_ratio}%。{ai_summary_zh}"
    return f"暂不操作，继续观察。{ai_summary_zh}"


def _timeline_message(action: str, suggested_ratio: str) -> str:
    if action in {"BUY_T", "SELL_T"}:
        return f"生成 {action} 信号，建议比例 {suggested_ratio}%。"
    if action == "REVIEW":
        return "硬性条件阻断，转入人工复核。"
    return "未形成交易动作，继续观察。"


def _decimal_text(value: Decimal | None) -> str:
    if not _is_finite_decimal(value):
        return ""
    return format(value, "f")


def _has_required_technical_facts(facts: TMarketFacts) -> bool:
    return (
        all(
            _is_positive_finite_decimal(value)
            for value in (
                facts.last_price,
                facts.vwap,
                facts.ma_1m,
                facts.ma_5m,
                facts.day_low,
                facts.day_high,
                facts.volume_ratio_5m,
            )
        )
        and _is_finite_decimal(facts.day_change_pct)
        and _is_finite_decimal(facts.rsi_5m)
    )


def _is_positive_finite_decimal(value: Decimal | None) -> bool:
    return _is_finite_decimal(value) and value > 0


def _is_finite_decimal(value: Decimal | None) -> bool:
    return value is not None and value.is_finite()
