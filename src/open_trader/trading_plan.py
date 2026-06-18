from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Mapping


TRADING_PLAN_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "source_status",
    "fallback_reason",
    "fallback_from_date",
    "rating",
    "entry_zone_low",
    "entry_zone_high",
    "add_price",
    "stop_loss",
    "target_1",
    "target_2",
    "max_weight",
    "catalyst",
    "time_horizon",
    "plan_text",
    "status",
    "error",
]

ADVICE_REQUIRED_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "advice_action",
    "advice_summary",
    "status",
    "error",
]


@dataclass(frozen=True)
class TradingPlanBuildResult:
    run_date: str
    plan_count: int
    plan_path: Path
    latest_path: Path


@dataclass(frozen=True)
class TradingPlanRow:
    run_date: str
    symbol: str
    market: str
    source_status: str
    fallback_reason: str
    fallback_from_date: str
    rating: str
    entry_zone_low: Decimal | None
    entry_zone_high: Decimal | None
    add_price: Decimal | None
    stop_loss: Decimal | None
    target_1: Decimal | None
    target_2: Decimal | None
    max_weight: str
    catalyst: str
    time_horizon: str
    plan_text: str
    status: str
    error: str

    @property
    def futu_symbol(self) -> str:
        market = self.market.upper()
        symbol = self.symbol.upper()
        if market == "HK" and symbol.isdigit():
            return f"HK.{symbol.zfill(5)}"
        return f"{market}.{symbol}"


@dataclass(frozen=True)
class PlanQuoteStatus:
    symbol: str
    futu_symbol: str
    last_price: Decimal
    status: str
    message: str


def build_trading_plan(
    advice_path: Path,
    data_dir: Path,
    run_date: str | None = None,
    update_latest: bool = True,
    market: str | None = None,
) -> TradingPlanBuildResult:
    advice_rows = _read_advice_rows(advice_path)
    effective_run_date = run_date or _latest_run_date(advice_rows)
    filtered_rows = [
        row
        for row in advice_rows
        if not row.get("run_date", "").strip()
        or row.get("run_date", "").strip() == effective_run_date
    ]
    market_filter = market.strip().upper() if market else None
    if market_filter is not None:
        filtered_rows = [
            row
            for row in filtered_rows
            if row.get("market", "").strip().upper() == market_filter
        ]
    if not filtered_rows and run_date is not None:
        raise ValueError(f"no advice rows match run_date {effective_run_date}")

    plan_rows = [_plan_row_from_advice(row, effective_run_date) for row in filtered_rows]
    if market_filter:
        plan_path = (
            data_dir / "runs" / effective_run_date / market_filter / "trading_plan.csv"
        )
        latest_path = data_dir / "latest" / market_filter / "trading_plan.csv"
    else:
        plan_path = data_dir / "runs" / effective_run_date / "trading_plan.csv"
        latest_path = data_dir / "latest" / "trading_plan.csv"
    _atomic_write_csv(plan_path, TRADING_PLAN_FIELDNAMES, plan_rows)
    if update_latest:
        _atomic_write_csv(latest_path, TRADING_PLAN_FIELDNAMES, plan_rows)
    return TradingPlanBuildResult(
        run_date=effective_run_date,
        plan_count=len(plan_rows),
        plan_path=plan_path,
        latest_path=latest_path,
    )


def load_trading_plan_rows(plan_path: Path) -> list[TradingPlanRow]:
    with plan_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        optional = {"source_status", "fallback_reason", "fallback_from_date"}
        missing = sorted(set(TRADING_PLAN_FIELDNAMES) - optional - set(fieldnames))
        if missing:
            raise ValueError(f"missing trading plan column(s): {', '.join(missing)}")
        return [_trading_plan_from_row(row) for row in reader]


def evaluate_plan_quote(plan: TradingPlanRow, last_price: Decimal) -> PlanQuoteStatus:
    if plan.stop_loss is not None and last_price <= plan.stop_loss:
        return _status(plan, last_price, "stop_loss_hit", "Current price is at or below the stop loss.")
    if plan.target_2 is not None and last_price >= plan.target_2:
        return _status(plan, last_price, "target_2_hit", "Current price is at or above target 2.")
    if plan.target_1 is not None and last_price >= plan.target_1:
        return _status(plan, last_price, "target_1_hit", "Current price is at or above target 1.")
    if (
        plan.entry_zone_low is not None
        and plan.entry_zone_high is not None
        and plan.entry_zone_low <= last_price <= plan.entry_zone_high
    ):
        return _status(plan, last_price, "entry_zone", "Current price is inside the planned entry zone.")
    if plan.add_price is not None:
        tolerance = plan.add_price * Decimal("0.01")
        if abs(last_price - plan.add_price) <= tolerance:
            return _status(plan, last_price, "add_zone", "Current price is near the planned add price.")
    return _status(plan, last_price, "watch", "No plan trigger is active.")


def _status(
    plan: TradingPlanRow,
    last_price: Decimal,
    status: str,
    message: str,
) -> PlanQuoteStatus:
    return PlanQuoteStatus(
        symbol=plan.symbol,
        futu_symbol=plan.futu_symbol,
        last_price=last_price,
        status=status,
        message=message,
    )


def _read_advice_rows(advice_path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with advice_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = sorted(set(ADVICE_REQUIRED_FIELDNAMES) - set(fieldnames))
        if missing:
            raise ValueError(f"missing advice column(s): {', '.join(missing)}")
        return [
            {
                column: "" if value is None else str(value)
                for column, value in row.items()
                if column
            }
            for row in reader
        ]


def _latest_run_date(rows: list[dict[str, str]]) -> str:
    dates = sorted({row.get("run_date", "").strip() for row in rows if row.get("run_date", "").strip()})
    if not dates:
        raise ValueError("--date is required when advice file has no run_date rows")
    return dates[-1]


def _plan_row_from_advice(row: dict[str, str], fallback_run_date: str) -> dict[str, str]:
    run_date = row.get("run_date", "").strip() or fallback_run_date
    symbol = row.get("symbol", "").strip().upper()
    market = row.get("market", "").strip().upper()
    advice_status = row.get("status", "").strip()
    if advice_status not in {"ok", "fallback"}:
        return _base_plan_row(
            run_date=run_date,
            symbol=symbol,
            market=market,
            source_status=advice_status or "error",
            fallback_reason=row.get("fallback_reason", "").strip(),
            fallback_from_date=row.get("fallback_from_date", "").strip(),
            status="error",
            error=row.get("error", "").strip(),
        )

    sections = _parse_template(row.get("advice_summary", ""))
    if not sections:
        return _base_plan_row(
            run_date=run_date,
            symbol=symbol,
            market=market,
            source_status=row.get("source_status", "").strip() or advice_status,
            fallback_reason=row.get("fallback_reason", "").strip(),
            fallback_from_date=row.get("fallback_from_date", "").strip(),
            rating=row.get("advice_action", "").strip(),
            plan_text=row.get("advice_summary", "").strip(),
            status="manual_review",
            error="unstructured advice_summary",
        )

    entry_low, entry_high = _extract_entry_zone(sections.get("操作计划", ""))
    target_1, target_2 = _extract_targets(sections.get("目标价", ""))
    row_values = _base_plan_row(
        run_date=run_date,
        symbol=symbol,
        market=market,
        source_status=row.get("source_status", "").strip() or advice_status,
        fallback_reason=row.get("fallback_reason", "").strip(),
        fallback_from_date=row.get("fallback_from_date", "").strip(),
        rating=sections.get("评级") or row.get("advice_action", "").strip(),
        entry_zone_low=_decimal_to_text(entry_low),
        entry_zone_high=_decimal_to_text(entry_high),
        add_price=_decimal_to_text(_extract_add_price(sections.get("操作计划", ""))),
        stop_loss=_decimal_to_text(_first_decimal(sections.get("风控", ""))),
        target_1=_decimal_to_text(target_1),
        target_2=_decimal_to_text(target_2),
        max_weight=_extract_max_weight(sections.get("仓位", "")),
        catalyst=sections.get("催化剂", ""),
        time_horizon=sections.get("时间窗口", ""),
        plan_text=row.get("advice_summary", "").strip(),
        status="active",
        error="",
    )
    return row_values


def _base_plan_row(
    *,
    run_date: str,
    symbol: str,
    market: str,
    source_status: str = "ok",
    fallback_reason: str = "",
    fallback_from_date: str = "",
    rating: str = "",
    entry_zone_low: str = "",
    entry_zone_high: str = "",
    add_price: str = "",
    stop_loss: str = "",
    target_1: str = "",
    target_2: str = "",
    max_weight: str = "",
    catalyst: str = "",
    time_horizon: str = "",
    plan_text: str = "",
    status: str,
    error: str,
) -> dict[str, str]:
    return {
        "run_date": run_date,
        "symbol": symbol,
        "market": market,
        "source_status": source_status,
        "fallback_reason": fallback_reason,
        "fallback_from_date": fallback_from_date,
        "rating": rating,
        "entry_zone_low": entry_zone_low,
        "entry_zone_high": entry_zone_high,
        "add_price": add_price,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "max_weight": max_weight,
        "catalyst": catalyst,
        "time_horizon": time_horizon,
        "plan_text": plan_text,
        "status": status,
        "error": error,
    }


def _parse_template(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for line in text.splitlines():
        if "：" not in line:
            continue
        key, value = line.split("：", 1)
        key = key.strip()
        if key in {"评级", "操作计划", "风控", "仓位", "催化剂", "目标价", "时间窗口", "理由"}:
            sections[key] = value.strip()
    return sections


def _extract_entry_zone(text: str) -> tuple[Decimal | None, Decimal | None]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|~|到|至)\s*(\d+(?:\.\d+)?)", text)
    if not match:
        return None, None
    return Decimal(match.group(1)), Decimal(match.group(2))


def _extract_add_price(text: str) -> Decimal | None:
    sentence = _first_sentence_containing(text, "加仓")
    if not sentence:
        return None
    keyword_index = sentence.index("加仓")
    matches = list(re.finditer(r"\d+(?:\.\d+)?", sentence))
    before_keyword = [match for match in matches if match.start() < keyword_index]
    if before_keyword:
        return Decimal(before_keyword[-1].group(0))
    return Decimal(matches[0].group(0)) if matches else None


def _extract_targets(text: str) -> tuple[Decimal | None, Decimal | None]:
    numbers = _decimals(text)
    target_1 = numbers[0] if numbers else None
    target_2 = numbers[1] if len(numbers) > 1 else None
    return target_1, target_2


def _extract_max_weight(text: str) -> str:
    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*%?\s*(?:-|~|到|至)\s*(\d+(?:\.\d+)?)\s*%",
        text,
    )
    if range_match:
        return f"{range_match.group(2)}%"
    percent_matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", text)
    if percent_matches:
        return f"{percent_matches[-1]}%"
    return ""


def _first_sentence_containing(text: str, keyword: str) -> str:
    for sentence in re.split(r"(?<=[。.!?])\s*", text):
        if keyword in sentence:
            return sentence
    return ""


def _first_decimal(text: str) -> Decimal | None:
    numbers = _decimals(text)
    return numbers[0] if numbers else None


def _decimals(text: str) -> list[Decimal]:
    return [Decimal(match) for match in re.findall(r"\d+(?:\.\d+)?", text)]


def _decimal_to_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _trading_plan_from_row(row: Mapping[str, str]) -> TradingPlanRow:
    return TradingPlanRow(
        run_date=row.get("run_date", "").strip(),
        symbol=row.get("symbol", "").strip().upper(),
        market=row.get("market", "").strip().upper(),
        source_status=row.get("source_status", "").strip() or "ok",
        fallback_reason=row.get("fallback_reason", "").strip(),
        fallback_from_date=row.get("fallback_from_date", "").strip(),
        rating=row.get("rating", "").strip(),
        entry_zone_low=_optional_decimal(row.get("entry_zone_low", "")),
        entry_zone_high=_optional_decimal(row.get("entry_zone_high", "")),
        add_price=_optional_decimal(row.get("add_price", "")),
        stop_loss=_optional_decimal(row.get("stop_loss", "")),
        target_1=_optional_decimal(row.get("target_1", "")),
        target_2=_optional_decimal(row.get("target_2", "")),
        max_weight=row.get("max_weight", "").strip(),
        catalyst=row.get("catalyst", "").strip(),
        time_horizon=row.get("time_horizon", "").strip(),
        plan_text=row.get("plan_text", "").strip(),
        status=row.get("status", "").strip(),
        error=row.get("error", "").strip(),
    )


def _optional_decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _atomic_write_csv(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: "" if row.get(key) is None else row.get(key)
                        for key in fieldnames
                    }
                )
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
