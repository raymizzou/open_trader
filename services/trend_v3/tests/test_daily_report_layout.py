"""Regression checks for the approved V1 report and its offline render command."""

import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / ".agents/skills/trend-v3-daily-report"
TEMPLATE = SKILL / "assets/report.html"
RENDERER = SKILL / "render_report.py"

# Capture the approved reference independently of the shipping template.
APPROVED_CSS = "a81b6168b5ec0ca83b50fa74337cbccdff1d9b3343f961b5ccf41de8c30a9695"
APPROVED_SHELL = "6c198190abeee41c63c316cc2b2e69146beadb6411f65df09ba5e119d0184b20"
APPROVED_CLASS_ROW = "6bb3340318353800b31d64d6d275cb84f506efd37de07e817179b76a5deae354"
FORMAL_RECORDS = {
    "live": (
        '<h2 id="live-title" class="section-title">当前实盘持仓</h2>',
        '<div class="reserved"><p class="positions-note">未接入</p></div>',
    ),
    "simulation": (
        '<h2 id="simulation-title" class="section-title">模拟盘持仓情况</h2>',
        '<p class="positions-note">未接入</p>',
    ),
    "kelly": (
        '<h2 id="kelly-title" class="section-title">当前凯利胜率</h2>',
        '<dl class="kelly-values">',
        "<div><dt>实盘</dt><dd>未接入</dd></div>",
        "<div><dt>模拟盘</dt><dd>未接入</dd></div>",
        "</dl>",
    ),
}


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.events = []
        self.feed(html)
        self.close()

    def handle_decl(self, declaration):
        self.events.append(("decl", declaration))

    def handle_starttag(self, tag, attrs):
        self.events.append(("start", tag, sorted(attrs)))

    def handle_endtag(self, tag):
        self.events.append(("end", tag))

    def handle_data(self, data):
        if text := " ".join(data.split()):
            self.events.append(("text", text))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def stylesheet(html):
    return re.search(r"<style>(.*?)</style>", html, re.S).group(1)


def shell_digest(html):
    # Mask only repeatable or report-day content. The report shell and its labels
    # keep their order, semantic elements, and attributes.
    html = re.sub(r"<(style|template|details)\b.*?</\1>", "", html, flags=re.S)
    html = html.replace("｜界面样稿", "")
    html = re.sub(r"(<div class=\"edition\">).*?(</div>)", r"\1\2", html, flags=re.S)
    html = re.sub(r"(<h1>.*?</h1>\s*)<p>.*?</p>", r"\1", html, flags=re.S)
    html = re.sub(r"(<div class=\"classes\">).*?(</div>)", r"\1\2", html, flags=re.S)
    return digest(json.dumps(Markup(html).events, ensure_ascii=False))


def assert_formal_records(html):
    for section, expected in FORMAL_RECORDS.items():
        body = re.search(rf'<section id="{section}"[^>]*>(.*?)</section>', html, re.S)
        assert body is not None, section
        assert tuple(line.strip() for line in body[1].strip().splitlines()) == expected
    assert html.count("未接入") == 4


def class_row_digest(row):
    row = row.replace('class="class-row" open', 'class="class-row"')
    row = re.sub(
        r'(<span class="class-name">).*?(?=</span>\s*<span class="stat">)',
        r"\1",
        row,
        flags=re.S,
    )
    row = re.sub(r'(<span class="number">).*?(</span>)', r"\1\2", row)
    row = re.sub(
        r'(<ul class="asset-list" role="list">).*?(</ul>)', r"\1\2", row, flags=re.S
    )
    return digest(json.dumps(Markup(row).events, ensure_ascii=False))


def render(tmp_path, report, name):
    input_file = tmp_path / f"{name}.json"
    output_file = tmp_path / f"{name}.html"
    input_file.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(RENDERER), str(input_file), str(output_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return output_file.read_text(encoding="utf-8")


def test_renders_approved_shell_across_dates(tmp_path):
    # Pin the shell separately so a replacement design fails even if there is
    # no executable renderer in that revision.
    template = TEMPLATE.read_text(encoding="utf-8")
    assert digest(stylesheet(template)) == APPROVED_CSS
    assert shell_digest(template) == APPROVED_SHELL

    direct = {
        "code": "TEST-A",
        "name": "合成标的甲",
        "trend": "寒",
        "signal": "凉转寒",
        "strength": "12 / 100",
        "reason": "合成的开仓判断。",
    }
    related = {
        "code": "TEST-B",
        "name": "合成标的乙",
        "trend": "热",
        "signal": "开香槟",
        "strength": "同品种 43 / 100",
        "reason": "合成的同向观察。",
    }
    full = render(
        tmp_path,
        {
            "date": "2040-02-29",
            "board_date": "2040-02-28",
            "short": [
                {
                    "seat": 1,
                    "name": "合成甲类",
                    "code": "CAT-A",
                    "board_strength": "3",
                    "direct": [direct],
                    "related": [related],
                },
                {
                    "seat": 2,
                    "name": "合成乙类",
                    "board_strength": "7",
                    "related": [related],
                },
            ],
            "long": [
                {
                    "seat": 1,
                    "name": "合成丙类",
                    "board_strength": "92",
                    "direct": [related],
                },
            ],
        },
        "full",
    )
    partial = render(
        tmp_path,
        {
            "date": "2041-05-03",
            "short_note": "本方向榜图席位待核。",
            "long": [
                {
                    "seat": 2,
                    "name": "合成丁类",
                    "direct_note": "直属成分证据待核。",
                    "related": [{"code": "TEST-C", "name": "合成标的丙"}],
                },
            ],
            "long_note": "第一席待核。",
        },
        "partial",
    )
    for rendered in (full, partial):
        assert digest(stylesheet(rendered)) == APPROVED_CSS
        assert shell_digest(rendered) == APPROVED_SHELL
        assert_formal_records(rendered)
        assert re.findall(r"<nav.*?</nav>", rendered, re.S)
        assert re.findall(r'<a href="#([^"]+)"', rendered) == [
            "short",
            "long",
            "live",
            "simulation",
            "kelly",
        ]
        assert not re.search(r"<template\b|\$\{[a-z_]+}", rendered)

    assert '<time datetime="2040-02-29">' in full
    assert '<span class="number">29</span> 日' in full
    assert "2040-02-28" in full
    assert '<time datetime="2041-05-03">' in partial
    assert (
        '<span class="number">5</span> 月 <span class="number">3</span> 日' in partial
    )
    assert "2040-02-29" not in partial
    assert "官方榜图日期待核" in partial
    assert "本方向榜图席位待核。" in partial
    assert "第一席待核。" in partial
    assert "直属成分证据待核。" in partial

    rows = re.findall(r"<details\b.*?</details>", full, re.S)
    assert len(rows) == 3
    assert class_row_digest(rows[0]) == APPROVED_CLASS_ROW
    assert ['class="class-row" open>' in row for row in rows] == [True, False, True]
    assert '<span class="number">01</span>' in rows[0]
    assert '<span class="number">3</span>' in rows[0]
    assert '<span class="number">02</span>' in rows[1]
    assert '<span class="number">02</span>' in partial
    assert '<span class="number">UNKNOWN</span>' in partial
    assert full.count('<li class="asset reader-card">') == 4
    assert partial.count('<li class="asset reader-card">') == 1
    assert (
        re.findall(r"<dt>(.*?)</dt>", rows[0]) == ["当前趋势", "特殊信号", "强度"] * 2
    )
    assert re.search(r"<dt>强度</dt><dd>12 / 100</dd>", rows[0])
    assert re.search(r"<dt>强度</dt><dd>同品种 43 / 100</dd>", rows[0])
    assert "合成的同向观察。" in rows[0]


def test_excludes_example_and_private_data(tmp_path):
    zero = render(tmp_path, {"date": "2042-06-07"}, "zero")
    assert shell_digest(zero) == APPROVED_SHELL
    assert_formal_records(zero)
    assert '<time datetime="2042-06-07">' in zero
    assert zero.count("暂无已核实的大类席位。") == 2
    assert not re.search(
        r"<details\b|<li\b|<article\b|<script\b|界面样稿|示例数据", zero
    )
    assert not re.search(r"<template\b|\$\{[a-z_]+}", zero)

    raw_script = "<script>alert(1)</script>"
    private = "TEST-ONLY-PRIVATE-PAYLOAD"
    result = render(
        tmp_path,
        {
            "date": "2042-06-07",
            "board_date": "2042-06-05",
            "board_version": '<img src="https://example.invalid/">',
            "live": private,
            "simulation": private,
            "kelly": private,
            "account": private,
            "billing": private,
            "appendix": private,
            "long": [
                {
                    "seat": 1,
                    "name": "甲 & 乙",
                    "code": '<"group">',
                    "direct": [
                        {
                            "code": "TEST-A",
                            "name": raw_script,
                            "trend": "热",
                            "signal": "UNKNOWN",
                            "strength": "",
                            "reason": '保留 "理由" & 文本。',
                            "evidence": private,
                        }
                    ],
                    "related": [
                        {
                            "code": "TEST-B",
                            "name": "完整查询",
                            "lookup": "miss",
                            "trend": "不适用行情",
                            "strength": "不适用强度",
                            "reason": "已核对合成查询 & 全部结果，无精确匹配。",
                        },
                        {
                            "code": "TEST-C",
                            "name": "查询未完成",
                            "lookup": "pending",
                            "signal": "不适用信号",
                            "reason": '只收到合成查询第一页，后续页为 "<待核>"。',
                        },
                        {"code": "TEST-D", "name": "合成无匹配", "lookup": "miss"},
                        {"code": "TEST-E", "name": "合成查询失败", "lookup": "pending"},
                    ],
                }
            ],
        },
        "private",
    )
    assert not re.search(
        r"<script\b|<img\b|<article\b|position-head\"|<template\b", result
    )
    assert private not in result
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result
    assert "甲 &amp; 乙" in result
    assert "&lt;&quot;group&quot;&gt;" in result
    assert "保留 &quot;理由&quot; &amp; 文本。" in result
    assert "&lt;img src=&quot;https://example.invalid/&quot;&gt;" in result
    assert_formal_records(result)
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', result, re.S)
    assert len(cards) == 5
    metric_values = r"<dd(?: class=\"reader-event\")?>(.*?)</dd>"
    assert re.findall(metric_values, cards[0]) == ["热", "待核", "待核"]
    lookups = [
        ("查不到", "已核对合成查询 &amp; 全部结果，无精确匹配。"),
        ("待核", "只收到合成查询第一页，后续页为 &quot;&lt;待核&gt;&quot;。"),
        ("查不到", "查不到"),
        ("待核", "待核"),
    ]
    for card, (status, reason) in zip(cards[1:], lookups, strict=True):
        assert re.findall(metric_values, card) == [status] * 3
        assert f'<p class="reader-reason">{reason}</p>' in card
        assert "不适用" not in card
    assert all("查不到" not in card for card in (cards[2], cards[4]))

    output = tmp_path / "zero.html"
    command = [
        sys.executable,
        str(RENDERER),
        str(tmp_path / "private.json"),
        str(output),
    ]
    duplicate = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert duplicate.returncode != 0
    assert output.read_text(encoding="utf-8") == zero
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("direction", "seats"),
    [
        pytest.param("short", [17], id="global-rank-17"),
        pytest.param("short", [18], id="global-rank-18"),
        pytest.param("long", [1, 1], id="duplicate-seat"),
        pytest.param("short", [True], id="non-integer-seat"),
    ],
)
def test_rejects_invalid_direction_seats(tmp_path, direction, seats):
    other_direction = "long" if direction == "short" else "short"
    report = {
        "date": "2040-02-29",
        direction: [
            {"name": f"合成分类 {index}", "seat": seat}
            for index, seat in enumerate(seats)
        ],
        other_direction: [{"name": "合成分类", "seat": 1}],
    }
    input_file = tmp_path / "invalid-seats.json"
    output_file = tmp_path / "candidate.html"
    input_file.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(RENDERER), str(input_file), str(output_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "seat" in result.stderr
    assert not output_file.exists()
