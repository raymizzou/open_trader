from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .advice.change_classifier import DEEPSEEK_BASE_URL, DEFAULT_CLASSIFIER_MODEL


ADVICE_TRANSLATION_FIELDS = {
    "advice_summary": "advice_summary_zh",
}

PLAN_TRANSLATION_FIELDS = {
    "plan_text": "plan_text_zh",
    "agent_excerpt": "agent_excerpt_zh",
}


class ReportTranslator(Protocol):
    def translate(self, text: str) -> str:
        pass


@dataclass(frozen=True)
class TranslationResult:
    advice_path: Path
    plan_path: Path
    translated_fields: int


class DeepSeekReportTranslator:
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

    def translate(self, text: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是金融投研报告翻译器。把用户提供的英文 TradingAgents "
                        "报告逐句翻译成简体中文。不要摘要、不要删减、不要改写结论；"
                        "保留所有数字、价格、百分比、日期、股票代码、评级和段落结构。"
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("translation model returned empty content")
        return content.strip()


def translate_agent_report_files(
    *,
    advice_path: Path,
    plan_path: Path,
    translator: ReportTranslator,
    force: bool = False,
) -> TranslationResult:
    advice_count = _translate_csv_file(
        advice_path,
        ADVICE_TRANSLATION_FIELDS,
        translator=translator,
        force=force,
    )
    plan_count = _translate_csv_file(
        plan_path,
        PLAN_TRANSLATION_FIELDS,
        translator=translator,
        force=force,
    )
    return TranslationResult(
        advice_path=advice_path,
        plan_path=plan_path,
        translated_fields=advice_count + plan_count,
    )


def _translate_csv_file(
    path: Path,
    field_map: dict[str, str],
    *,
    translator: ReportTranslator,
    force: bool,
) -> int:
    rows, fieldnames = _read_csv_rows(path)
    output_fields = [*fieldnames]
    for translated_field in field_map.values():
        if translated_field not in output_fields:
            output_fields.append(translated_field)

    translated_count = 0
    for row in rows:
        for source_field, translated_field in field_map.items():
            source_text = row.get(source_field, "").strip()
            if not source_text:
                row[translated_field] = row.get(translated_field, "")
                continue
            if not force and row.get(translated_field, "").strip():
                continue
            row[translated_field] = translator.translate(source_text)
            translated_count += 1

    _atomic_write_csv(path, output_fields, rows)
    return translated_count


def _read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [
            {
                str(key): "" if value is None else str(value)
                for key, value in row.items()
                if key is not None
            }
            for row in reader
        ]
    return rows, fieldnames


def _atomic_write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)
