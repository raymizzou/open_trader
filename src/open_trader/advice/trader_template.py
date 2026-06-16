from __future__ import annotations

import re


SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:\*\*)?(Rating|Executive Summary|Investment Thesis|Price Target|Time Horizon)(?:\*\*)?\s*:\s*",
    re.IGNORECASE,
)


def format_trader_template(final_trade_decision: object, action: str) -> str:
    text = str(final_trade_decision).strip()
    if not text:
        return ""

    sections = _parse_sections(text)
    if not sections:
        return text

    summary = sections.get("executive summary", "")
    thesis = sections.get("investment thesis", "")
    rating = sections.get("rating") or action
    return "\n".join(
        [
            f"评级：{rating}",
            f"操作计划：{summary}",
            f"风控：{_extract_sentence(summary, ('止损', '停损', '风控', '风险'))}",
            f"仓位：{_extract_sentence(summary, ('仓位', '目标仓位', '满仓'))}",
            f"催化剂：{_extract_sentence(summary, ('催化', '财报'))}",
            f"目标价：{sections.get('price target', '')}",
            f"时间窗口：{sections.get('time horizon', '')}",
            f"理由：{thesis}",
        ]
    )


def _parse_sections(text: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        key = match.group(1).lower()
        sections[key] = _clean_value(text[start:end])
    return sections


def _clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _extract_sentence(text: str, keywords: tuple[str, ...]) -> str:
    for sentence in _sentences(text):
        if any(keyword in sentence for keyword in keywords):
            return sentence
    return ""


def _sentences(text: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(r"(?<=[。.!?])\s*", text)
        if segment.strip()
    ]
