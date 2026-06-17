from __future__ import annotations

import csv
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Mapping

from .models import PREMARKET_ACTION_FIELDNAMES, PremarketAction, TradingAdvice


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def write_premarket_outputs(
    *,
    run_date: str,
    actions: Iterable[PremarketAction],
    data_dir: Path,
    reports_dir: Path,
    advice_records: Iterable[TradingAdvice] = (),
    update_latest: bool = True,
    no_eligible: bool = False,
) -> tuple[Path, Path, Path]:
    sorted_actions = sorted(
        actions,
        key=lambda action: (
            SEVERITY_ORDER[action.severity],
            _negative_weight(action.portfolio_weight_hkd),
            action.symbol,
        ),
    )
    rows = [action.to_row() for action in sorted_actions]
    sorted_advice_records = list(advice_records)

    run_actions_path = data_dir / "runs" / run_date / "premarket_actions.csv"
    latest_actions_path = data_dir / "latest" / "premarket_actions.csv"
    report_path = reports_dir / "premarket" / f"{run_date}.md"

    _atomic_write_csv(run_actions_path, PREMARKET_ACTION_FIELDNAMES, rows)
    if update_latest:
        _atomic_write_csv(latest_actions_path, PREMARKET_ACTION_FIELDNAMES, rows)
    _atomic_write_text(
        report_path,
        _render_markdown(
            run_date,
            sorted_actions,
            advice_records=sorted_advice_records,
            no_eligible=no_eligible,
        ),
    )

    return run_actions_path, latest_actions_path, report_path


def _render_markdown(
    run_date: str,
    actions: list[PremarketAction],
    *,
    advice_records: list[TradingAdvice],
    no_eligible: bool = False,
) -> str:
    lines = [f"# 开盘前交易简报 - {run_date}", ""]
    if no_eligible:
        lines.extend(["没有找到符合条件的美股或 ETF 标的。", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "## 持仓全景",
            "",
            f"本次分析标的：{len(advice_records)} 个｜今日重点：{len(actions)} 个",
            f"已分析持仓合计仓位：{_total_weight_text(advice_records)}",
            "",
            "| 标的 | 最新价 | 港元市值 | 当前仓位 | 风险标记 | 当前观点 | 状态 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if advice_records:
        for advice in advice_records:
            lines.append(
                "| "
                f"{_escape_table_cell(advice.symbol)} | "
                f"{_last_price_text(advice.last_price, advice.price_currency)} | "
                f"{_market_value_hkd_text(advice.market_value_hkd)} | "
                f"{_escape_table_cell(advice.portfolio_weight_hkd)} | "
                f"{_risk_flag_text(advice.risk_flag)} | "
                f"{_advice_action_text(advice.advice_action)} | "
                f"{_advice_status_text(advice.status)} |"
            )
        lines.append(
            "| "
            "合计 | "
            "- | "
            f"{_total_market_value_hkd_text(advice_records)} | "
            f"{_total_weight_text(advice_records)} | "
            "- | - | - |"
        )
    else:
        lines.append("| 无 | - | - | - | - | - | - |")

    lines.extend(["", "## 今日重点策略", ""])
    if not actions:
        lines.extend(["今日没有需要特别关注的交易建议变化。", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "| 标的 | 重要性 | 当前仓位 | 建议动作 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for action in actions:
        lines.append(
            "| "
            f"{_escape_table_cell(action.symbol)} | "
            f"{_severity_text(action.severity)} | "
            f"{_escape_table_cell(action.portfolio_weight_hkd)} | "
            f"{_suggested_action_text(action.suggested_action)} |"
        )
    lines.extend(["", "## 详细说明", ""])
    for index, action in enumerate(actions, start=1):
        lines.extend(
            [
                f"### {index}. {action.symbol}",
                "",
                "| 项目 | 内容 |",
                "| --- | --- |",
                f"| 重要性 | {_severity_text(action.severity)} |",
                f"| 当前仓位 | {_escape_table_cell(action.portfolio_weight_hkd)} |",
                f"| 变化类型 | {_change_type_text(action.change_type)} |",
                f"| 建议动作 | {_suggested_action_text(action.suggested_action)} |",
                "",
                f"**为什么重要：** {_action_rationale_text(action)}",
                "",
                f"**摘要：** {_action_summary_text(action)}",
            ]
        )
        if action.watch_trigger:
            lines.extend(["", f"**观察条件：** {_watch_trigger_text(action.watch_trigger)}"])
        lines.append("")

    return "\n".join(lines)


def _severity_text(value: str) -> str:
    return {
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(value.strip().lower(), value.strip())


def _change_type_text(value: str) -> str:
    return {
        "new_signal": "新信号",
        "action_changed": "建议动作变化",
        "risk_changed": "风险变化",
        "trigger_changed": "触发条件变化",
        "no_material_change": "无实质变化",
    }.get(value.strip().lower(), value.strip())


def _suggested_action_text(value: str) -> str:
    normalized = value.strip().lower()
    if "initiate" in normalized or "starter position" in normalized:
        return "建仓"
    if "scale" in normalized and "buy" in normalized:
        return "分批买入"
    if "reduce" in normalized or "trim" in normalized:
        return "减仓"
    if "exit" in normalized or "sell all" in normalized:
        return "清仓"
    return {
        "hold": "持有",
        "watch": "观察",
        "reduce": "减仓",
        "add": "加仓",
        "exit": "清仓",
        "trim": "减仓",
        "buy": "买入",
        "sell": "卖出",
    }.get(normalized, "人工复核" if _looks_like_english_prose(value) else value.strip())


def _advice_action_text(value: str) -> str:
    return {
        "overweight": "高配",
        "neutral": "中性",
        "underweight": "低配",
        "hold": "持有",
        "buy": "买入",
        "sell": "卖出",
        "reduce": "减仓",
        "trim": "减仓",
        "add": "加仓",
        "exit": "清仓",
    }.get(value.strip().lower(), value.strip())


def _risk_flag_text(value: str) -> str:
    return {
        "normal": "正常",
        "data_check": "数据需复核",
        "overweight": "仓位偏高",
        "underweight": "仓位偏低",
    }.get(value.strip().lower(), value.strip())


def _advice_status_text(value: str) -> str:
    return {
        "ok": "正常",
        "fallback": "沿用旧建议",
        "error": "分析失败",
    }.get(value.strip().lower(), value.strip())


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _total_weight_text(advice_records: list[TradingAdvice]) -> str:
    total = 0.0
    for advice in advice_records:
        try:
            total += float(advice.portfolio_weight_hkd.strip().rstrip("%").replace(",", ""))
        except ValueError:
            continue
    return f"{total:.2f}%"


def _total_market_value_hkd_text(advice_records: list[TradingAdvice]) -> str:
    total = 0.0
    found_value = False
    for advice in advice_records:
        try:
            total += float(advice.market_value_hkd.strip().replace(",", ""))
            found_value = True
        except ValueError:
            continue
    if not found_value:
        return "-"
    return f"HKD {total:,.2f}"


def _market_value_hkd_text(value: str) -> str:
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return "-"
    try:
        return f"HKD {float(cleaned):,.2f}"
    except ValueError:
        return _escape_table_cell(value)


def _last_price_text(value: str, currency: str) -> str:
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return "-"
    prefix = currency.strip().upper()
    try:
        price = f"{float(cleaned):,.2f}"
    except ValueError:
        price = _escape_table_cell(value)
    if prefix:
        return f"{prefix} {price}"
    return price


def _action_rationale_text(action: PremarketAction) -> str:
    fallback = (
        f"{action.symbol} 的建议被标记为{_severity_text(action.severity)}重要性，"
        f"变化类型为{_change_type_text(action.change_type)}，"
        f"建议动作是{_suggested_action_text(action.suggested_action)}。"
        "开盘前需要人工确认是否执行。"
    )
    return _chinese_text_or_fallback(action.rationale, fallback)


def _action_summary_text(action: PremarketAction) -> str:
    fallback = (
        f"建议开盘前重点复核 {action.symbol} 的仓位、价格条件和下单风险。"
    )
    return _chinese_text_or_fallback(action.summary, fallback)


def _watch_trigger_text(value: str) -> str:
    return _chinese_text_or_fallback(value, "请以交易计划中的价格触发条件为准。")


def _chinese_text_or_fallback(value: str, fallback: str) -> str:
    cleaned = value.strip()
    if not cleaned or _looks_like_english_prose(cleaned):
        return fallback
    return _escape_table_cell(cleaned)


def _looks_like_english_prose(value: str) -> bool:
    letters = sum(1 for char in value if char.isascii() and char.isalpha())
    cjk = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    return letters >= 6 and letters > cjk


def _negative_weight(value: str) -> float:
    try:
        return -float(value.strip().rstrip("%").replace(",", ""))
    except ValueError:
        return 0.0


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
                        fieldname: (
                            "" if row.get(fieldname) is None else row.get(fieldname)
                        )
                        for fieldname in fieldnames
                    }
                )
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            _best_effort_unlink(temp_path)
        raise


def _atomic_write_text(path: Path, content: str) -> None:
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
            handle.write(content)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            _best_effort_unlink(temp_path)
        raise


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass
