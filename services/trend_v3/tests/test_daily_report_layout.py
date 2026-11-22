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
    assert re.findall(r'<dd class="reader-event">(.*?)</dd>', rows[0]) == [
        "凉转寒",
        "开香槟",
    ]
    assert '<dd class="reader-event">无</dd>' in partial
    assert "合成的同向观察。" in rows[0]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({}, "无", id="absent"),
        pytest.param({"signal_events": []}, "无", id="empty"),
        pytest.param({"signal_events": ["寒转凉"]}, "寒转凉", id="one"),
        pytest.param(
            {"signal_events": ["温转热", "开香槟", "温转热", "上天台", "反抽警报"]},
            "温转热，开香槟，上天台，反抽警报",
            id="first-seen",
        ),
        pytest.param(
            {"signal_events": ["UNKNOWN", "未触发", "未出现温转热", "开香槟", "待核"]},
            "开香槟",
            id="partial-confirmation",
        ),
        pytest.param(
            {
                "signal_events": [
                    "  热转沸  ",
                    "寒转冻",
                    "合成回稳 A&B",
                    "热转沸",
                    "合成回稳 A&B ",
                    " unknown ",
                    "无事件",
                    "未确认合成信号",
                    "可能触发",
                    "合成信号（其余待核）",
                    "未触发：开香槟、上天台",
                ],
            },
            "热转沸，寒转冻，合成回稳 A&amp;B",
            id="additional-verified-names",
        ),
        pytest.param(
            {"signal_events": [], "signal": "开香槟"}, "无", id="withdrawn-legacy"
        ),
        pytest.param(
            {"signal_events": ["无量转强", "无开香槟", "无特殊信号", "未见反抽警报"]},
            "无量转强",
            id="structured-negation",
        ),
        pytest.param(
            {"signal": "温转热、未触发反抽警报，开香槟（其余待核）, 温转热"},
            "温转热",
            id="legacy-mixed",
        ),
        pytest.param(
            {"signal": "温转热、未触发开香槟，上天台（其余待核）"},
            "温转热",
            id="legacy-coverage-after-inline-negation",
        ),
        pytest.param(
            {"signal": "已确认：温转热、上天台（其余待核）,热转沸。寒转冻\n反抽警报"},
            "温转热，上天台，热转沸，寒转冻，反抽警报",
            id="legacy-affirmative-coverage",
        ),
        pytest.param(
            {"signal": "未触发：温转热、反抽警报；寒转凉、上天台 (其余待核)"},
            "寒转凉，上天台",
            id="legacy-negated-list",
        ),
        pytest.param(
            {"signal": "未触发开香槟、上天台"},
            "无",
            id="legacy-unpunctuated-negation",
        ),
        pytest.param(
            {"signal": "疑似开香槟，上天台；已确认：反抽警报"},
            "反抽警报",
            id="legacy-unpunctuated-tentative",
        ),
        pytest.param(
            {"signal": "温转热、未触发开香槟，上天台；寒转冻、热转沸"},
            "温转热，寒转冻，热转沸",
            id="legacy-ambiguous-tail",
        ),
        pytest.param(
            {"signal": "未触发开香槟、上天台（其余待核）"},
            "无",
            id="legacy-coverage-inside-negation",
        ),
        pytest.param(
            {"signal": "温转热，未触发开香槟，上天台（其余待核）"},
            "温转热",
            id="legacy-coverage-after-negated-group",
        ),
        pytest.param(
            {
                "signal": "已确认：热转沸、沸中创历史新高；"
                "未触发：开香槟、反抽警报；寒转冻"
            },
            "热转沸，沸中创历史新高，寒转冻",
            id="legacy-discipline-names",
        ),
        pytest.param(
            {"signal": "未触发、查不到、UNKNOWN、待核、无信号、普通标签"},
            "无",
            id="legacy-no-confirmed-event",
        ),
        pytest.param(
            {"lookup": "miss", "signal": "开香槟", "reason": "完整查询无精确匹配。"},
            "无",
            id="lookup-miss",
        ),
        pytest.param(
            {
                "lookup": "pending",
                "signal_events": ["上天台"],
                "reason": "当前查询只收到第一页。",
            },
            "无",
            id="lookup-pending",
        ),
    ],
)
def test_special_signal_display(tmp_path, fields, expected):
    asset = {"code": "TEST-A", "name": "合成标的", **fields}
    html = render(
        tmp_path,
        {
            "date": "2040-02-29",
            "long": [{"seat": 1, "name": "合成类", "direct": [asset]}],
        },
        "special-signal",
    )
    assert re.findall(r'<dd class="reader-event">(.*?)</dd>', html) == [expected]
    if "reason" in fields:
        assert f'<p class="reader-reason">{fields["reason"]}</p>' in html


@pytest.mark.parametrize(
    ("local_strength", "expected"),
    [
        pytest.param(0, "同品种 0 / 100", id="zero"),
        pytest.param(6.25, "同品种 6.25 / 100", id="number"),
        pytest.param("6.250", "同品种 6.250 / 100", id="source-precision"),
        pytest.param(None, "待核", id="missing"),
    ],
)
def test_btc_observation_strength(tmp_path, local_strength, expected):
    html = render(
        tmp_path,
        {
            "date": "2040-02-29",
            "long": [
                {
                    "seat": 2,
                    "name": "比特币",
                    "board_strength": "93",
                    "direct": [
                        {
                            "code": "BTC",
                            "name": "比特币现货",
                            "trend": "热",
                            "signal_events": ["开香槟"],
                            "local_strength": local_strength,
                            "strength": "87 / 100",
                        }
                    ],
                }
            ],
        },
        "btc-observation",
    )
    assert '<span class="number">02</span>' in html
    assert '<span class="number">93</span>' in html
    assert re.findall(r"<dt>强度</dt><dd>(.*?)</dd>", html) == [expected]
    assert "87 / 100" not in html


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
    assert re.findall(metric_values, cards[0]) == ["热", "无", "待核"]
    lookups = [
        ("查不到", "已核对合成查询 &amp; 全部结果，无精确匹配。"),
        ("待核", "只收到合成查询第一页，后续页为 &quot;&lt;待核&gt;&quot;。"),
        ("查不到", "查不到"),
        ("待核", "待核"),
    ]
    for card, (status, reason) in zip(cards[1:], lookups, strict=True):
        assert re.findall(metric_values, card) == [status, "无", status]
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


@pytest.mark.parametrize("direction", ["long", "short"])
def test_direct_watch_entries(tmp_path, direction):
    # Hand-worked orders: all five qualify, including the equal-strength B/C.
    codes = (
        ["B", "C", "E", "A", "D"]
        if direction == "long"
        else ["D", "A", "E", "B", "C"]
    )
    strengths = {"A": 60, "B": 90, "C": 90, "D": 30, "E": 75}
    cards = [
        {
            "code": code,
            "name": f"合成标的 {code}",
            "trend": "热" if direction == "long" else "寒",
            "signal_events": ["温转热" if direction == "long" else "凉转寒"],
            "strength": f"{strengths[code]} / 100",
            "direct_verified": True,
            "watch_eligible": True,
        }
        for code in codes
    ]
    html = render(
        tmp_path,
        {
            "date": "2040-02-29",
            direction: [{"seat": 1, "name": "合成类", "direct": cards}],
        },
        f"direct-watch-{direction}",
    )
    rendered = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
    assert len(rendered) == 5
    assert re.findall(r'<h4 class="reader-name">(\w+) <span>', html) == codes
    for card in rendered:
        assert '<span class="reader-marker">直属</span>' in card
        assert '<span class="reader-marker">应建观察仓</span>' in card
    assert_formal_records(html)


def test_direct_watch_markers_require_evidence(tmp_path):
    assets = [
        {"code": "OLD", "name": "旧投影", "signal": "温转热"},
        {
            "code": "Z", "name": "仅警报",
            "signal_events": ["反抽警报"], "direct_verified": True,
        },
        {
            "code": "Y", "name": "日期待核",
            "direct_verified": True, "watch_eligible": False,
        },
        {
            "code": "STR", "name": "文本不是核实值",
            "direct_verified": "true", "watch_eligible": "true",
        },
        {
            "code": "MISS", "name": "完整无匹配",
            "lookup": "miss", "direct_verified": True,
        },
        {
            "code": "PENDING", "name": "分页待核",
            "lookup": "pending", "direct_verified": True,
        },
        {
            "code": "F", "name": "仅缺强度", "trend": "热",
            "signal_events": ["温转热"], "direct_verified": True,
            "watch_eligible": True,
        },
    ]
    html = render(
        tmp_path,
        {
            "date": "2040-02-29",
            "long": [{
                "seat": 1, "name": "合成类", "direct": assets,
                "related": [{
                    "code": "X", "name": "关联 ETF",
                    "signal_events": ["温转热"], "strength": "99 / 100",
                }],
            }],
        },
        "evidence-markers",
    )
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
    assert len(cards) == 8
    assert 'reader-marker' not in cards[0]
    assert 'reader-marker' not in cards[3]
    for index in (1, 2, 4, 5):
        assert '<span class="reader-marker">直属</span>' in cards[index]
        assert '应建观察仓' not in cards[index]
    assert 'reader-marker' not in cards[7]
    assert '<span class="reader-marker">直属</span>' in cards[6]
    assert '<span class="reader-marker">应建观察仓</span>' in cards[6]
    assert '<dt>强度</dt><dd>待核</dd>' in cards[6]
    assert '<dt>当前趋势</dt><dd>查不到</dd>' in cards[4]
    assert '<dt>当前趋势</dt><dd>待核</dd>' in cards[5]
