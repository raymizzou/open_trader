from __future__ import annotations

import csv
from pathlib import Path

from open_trader.advice.models import PremarketAction, TradingAdvice
from open_trader.advice.report import write_premarket_outputs


def action(
    symbol: str,
    severity: str = "medium",
    weight: str = "3.05%",
) -> PremarketAction:
    return PremarketAction(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        portfolio_weight_hkd=weight,
        severity=severity,  # type: ignore[arg-type]
        change_type="action_changed",
        suggested_action="减仓",
        summary=f"建议开盘前重点复核 {symbol} 的仓位和风险。",
        rationale=f"{symbol} 今日建议相对上次发生变化，需要优先确认。",
        watch_trigger="若开盘后触发计划价位，应优先处理。",
    )


def advice(
    symbol: str,
    action_text: str,
    weight: str = "3.05%",
    risk_flag: str = "normal",
    status: str = "ok",
) -> TradingAdvice:
    return TradingAdvice(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        asset_class="stock",
        portfolio_weight_hkd=weight,
        risk_flag=risk_flag,
        source="tradingagents",
        advice_action=action_text,
        advice_summary="",
        raw_decision="",
        status=status,  # type: ignore[arg-type]
        error="",
    )


def test_write_premarket_outputs_writes_actions_csv_and_markdown(
    tmp_path: Path,
) -> None:
    actions_csv, latest_csv, report_path = write_premarket_outputs(
        run_date="2026-06-16",
        actions=[
            action("QQQ", "low", "1.40%"),
            action("VIXY", "high", "3.05%"),
            action("SPY", "high", "5.10%"),
            action("AAPL", "high", "5.10%"),
            action("MSFT", "medium", "7.00%"),
        ],
        advice_records=[
            advice("AAPL", "Hold", "5.10%"),
            advice("MSFT", "Underweight", "7.00%", "data_check", "fallback"),
        ],
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )

    assert actions_csv == tmp_path / "data/runs/2026-06-16/premarket_actions.csv"
    assert latest_csv == tmp_path / "data/latest/premarket_actions.csv"
    assert report_path == tmp_path / "reports/premarket/2026-06-16.md"

    rows = list(csv.DictReader(actions_csv.open(encoding="utf-8")))
    assert [row["symbol"] for row in rows] == ["AAPL", "SPY", "VIXY", "MSFT", "QQQ"]
    assert latest_csv.read_text(encoding="utf-8") == actions_csv.read_text(
        encoding="utf-8"
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "# 开盘前交易简报 - 2026-06-16" in markdown
    assert "## 持仓全景" in markdown
    assert "本次分析标的：2 个｜今日重点：5 个" in markdown
    assert "| 标的 | 当前仓位 | 风险标记 | 当前观点 | 状态 |" in markdown
    assert "| AAPL | 5.10% | 正常 | 持有 | 正常 |" in markdown
    assert "| MSFT | 7.00% | 数据需复核 | 低配 | 沿用旧建议 |" in markdown
    assert "## 今日重点策略" in markdown
    assert "| 标的 | 重要性 | 当前仓位 | 建议动作 |" in markdown
    assert "| AAPL | 高 | 5.10% | 减仓 |" in markdown
    assert "| MSFT | 中 | 7.00% | 减仓 |" in markdown
    assert "## 详细说明" in markdown
    assert "| 变化类型 | 建议动作变化 |" in markdown
    assert "**为什么重要：** AAPL 今日建议相对上次发生变化，需要优先确认。" in markdown
    assert "**摘要：** 建议开盘前重点复核 AAPL 的仓位和风险。" in markdown
    assert "**观察条件：** 若开盘后触发计划价位，应优先处理。" in markdown
    assert "VIXY" in markdown
    assert "QQQ" in markdown
    assert "Premarket Trading Brief" not in markdown
    assert "Action Items" not in markdown
    assert "Severity:" not in markdown
    assert "action_changed" not in markdown


def test_write_premarket_outputs_handles_no_actions(tmp_path: Path) -> None:
    _, _, report_path = write_premarket_outputs(
        run_date="2026-06-16",
        actions=[],
        advice_records=[
            advice("AAPL", "Hold", "5.10%"),
            advice("MSFT", "Underweight", "7.00%"),
        ],
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "# 开盘前交易简报 - 2026-06-16" in markdown
    assert "## 持仓全景" in markdown
    assert "本次分析标的：2 个｜今日重点：0 个" in markdown
    assert "| AAPL | 5.10% | 正常 | 持有 | 正常 |" in markdown
    assert "| MSFT | 7.00% | 正常 | 低配 | 正常 |" in markdown
    assert "## 今日重点策略" in markdown
    assert "今日没有需要特别关注的交易建议变化。" in markdown
    assert "No material trading advice changes" not in markdown


def test_write_premarket_outputs_handles_no_eligible_symbols(tmp_path: Path) -> None:
    _, _, report_path = write_premarket_outputs(
        run_date="2026-06-16",
        actions=[],
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        no_eligible=True,
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "# 开盘前交易简报 - 2026-06-16" in markdown
    assert "没有找到符合条件的美股或 ETF 标的。" in markdown
    assert "No eligible US stocks or ETFs were found" not in markdown


def test_change_classifier_prompt_requires_chinese_output() -> None:
    prompt = (
        Path(__file__).resolve().parents[1]
        / "src/open_trader/advice/prompts/change_classifier.md"
    ).read_text(encoding="utf-8")

    assert "suggested_action、summary、rationale、watch_trigger 必须使用中文" in prompt
    assert "不要在报告字段中混用英文枚举值" in prompt
