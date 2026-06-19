from __future__ import annotations

import csv
from pathlib import Path

from open_trader.report_translation import translate_agent_report_files


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        return f"译文：{text}"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_translate_agent_report_files_adds_chinese_fields_without_replacing_source(
    tmp_path: Path,
) -> None:
    advice_path = tmp_path / "trading_advice.csv"
    plan_path = tmp_path / "trading_plan.csv"
    write_csv(
        advice_path,
        ["run_date", "symbol", "market", "advice_summary"],
        [
            {
                "run_date": "2026-06-19",
                "symbol": "DRAM",
                "market": "US",
                "advice_summary": "Reduce existing DRAM exposure by 50%.",
            }
        ],
    )
    write_csv(
        plan_path,
        ["run_date", "symbol", "market", "plan_text", "agent_excerpt"],
        [
            {
                "run_date": "2026-06-19",
                "symbol": "DRAM",
                "market": "US",
                "plan_text": "Place a hard stop at $60.",
                "agent_excerpt": "The decision is Underweight.",
            }
        ],
    )
    translator = FakeTranslator()

    result = translate_agent_report_files(
        advice_path=advice_path,
        plan_path=plan_path,
        translator=translator,
    )

    advice = read_rows(advice_path)[0]
    plan = read_rows(plan_path)[0]
    assert result.translated_fields == 3
    assert translator.calls == [
        "Reduce existing DRAM exposure by 50%.",
        "Place a hard stop at $60.",
        "The decision is Underweight.",
    ]
    assert advice["advice_summary"] == "Reduce existing DRAM exposure by 50%."
    assert advice["advice_summary_zh"] == "译文：Reduce existing DRAM exposure by 50%."
    assert plan["plan_text"] == "Place a hard stop at $60."
    assert plan["plan_text_zh"] == "译文：Place a hard stop at $60."
    assert plan["agent_excerpt_zh"] == "译文：The decision is Underweight."
