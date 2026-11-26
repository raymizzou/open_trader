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
APPROVED_CSS = "fbeb823b2adba84a553833218129e5725935a2d91de6c62f291735d4da211f2f"
APPROVED_SHELL = "6387569e7dff66bef16ce2aed1b459e9f0433a59c0b87c4a3dd3d2bab87081ab"
APPROVED_CLASS_ROW = "b215fe8afb44d74c144d7d11fc04a4993b04567c786a98ed622d724ffa4af523"
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
    html = re.sub(r'<time\b.*?</time>', '', html, flags=re.S)
    html = re.sub(r'(<strong>).*?(</strong>)', r"\1\2", html, flags=re.S)
    html = re.sub(r'(<div class="collection-status"[^>]*>)(.*?)(</div>)',
                  lambda m: (m[1]
                             + re.sub(r'(<p>).*?(</p>)', r"\1\2", m[2], flags=re.S)
                             + m[3]),
                  html, flags=re.S)
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
    row = re.sub(r'(<p class="data-date">).*?(</p>)', r"\1\2", row)
    return digest(json.dumps(Markup(row).events, ensure_ascii=False))


def render(tmp_path, report, name, *, report_revision=None):
    input_file = tmp_path / f"{name}.json"
    output_file = tmp_path / f"{name}.html"
    input_file.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    command = [sys.executable, str(RENDERER), str(input_file), str(output_file)]
    if report_revision is not None:
        command += ["--report-revision", report_revision]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return output_file.read_text(encoding="utf-8")



def render_verified_watch(tmp_path, report, name):
    # Synthetic membership consumers supply current per-field observations under
    # the explicit collection contract; missing strengths remain missing.
    day = {
        "2040-02-29": "2040-02-28",
        "2040-01-01": "2039-12-31",
        "2041-01-02": "2041-01-01",
    }[report["date"]]
    collection = {"targets": {}, "evidence": {}}
    report["collection"] = collection
    for direction in ("short", "long"):
        for gi, group in enumerate(report.get(direction, [])):
            for section in ("direct", "searchable", "related"):
                for ai, asset in enumerate(group.get(section, [])):
                    asset.setdefault("instrument_id", "fixture-" + asset["code"])
                    asset.setdefault("market", "US")
                    asset.setdefault("instrument_type", "stock")
                    asset["data_date"] = day
                    collection["targets"][asset["market"]] = day
                    if asset.get("watch_eligible") is True:
                        asset.setdefault("trend", "热")
                        asset.setdefault("signal_events", [])
                    fields = (
                        ("lookup",)
                        if asset.get("lookup") == "miss"
                        else ()
                        if asset.get("lookup")
                        else ("trend", "signal_events", "strength", "local_strength")
                    )
                    for field in fields:
                        if field not in asset:
                            continue
                        path = f"/{direction}/{gi}/{section}/{ai}/{field}"
                        collection["evidence"][path] = {
                            "state": "verified",
                            "data_date": day,
                            "market": asset["market"],
                            "instrument_type": asset["instrument_type"],
                            "source": "fixture-current-observation",
                            "version": "fixture-v1",
                            "identity": asset["instrument_id"],
                            "value": asset[field],
                            **({"full_lookup": True} if field == "lookup" else {}),
                        }
    return render(tmp_path, report, name)


def test_renders_approved_shell_across_dates(tmp_path):
    # Pin the shell separately so a replacement design fails even if there is
    # no executable renderer in that revision.
    template = TEMPLATE.read_text(encoding="utf-8")
    assert digest(stylesheet(template)) == APPROVED_CSS
    unchanged_css = stylesheet(template).split("/* Approved reader cards:")[0] + "\n  "
    assert digest(unchanged_css) == (
        "ed5b22e5391d937e99574300ff08c2c939f04489540cb2cda1956419add69d52"
    )
    assert shell_digest(template) == APPROVED_SHELL

    direct = {
        "code": "TEST-A",
        "name": "合成标的甲",
        "direct_verified": True,
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
    assert '2040 年 2 月 29 日' in full
    assert "行情截至 待核" in full
    assert '<time datetime="2041-05-03">' in partial
    assert (
        '2041 年 5 月 3 日' in partial
    )
    assert "2040-02-29" not in partial
    assert "大类数据日期 待核" in partial
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
        re.findall(r"<dt>(.*?)</dt>", rows[0])
        == ["当前趋势", "特殊信号", "全局强度", "同品种强度"] * 2
    )
    assert re.search(r"<dt>全局强度</dt><dd>12 / 100</dd>", rows[0])
    assert re.search(r"<dt>同品种强度</dt><dd>43 / 100</dd>", rows[0])
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
        pytest.param(0, "0 / 100", id="zero"),
        pytest.param(6.25, "6.25 / 100", id="number"),
        pytest.param("6.250", "6.250 / 100", id="source-precision"),
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
    assert re.findall(r"<dt>同品种强度</dt><dd>(.*?)</dd>", html) == [expected]
    assert re.findall(r"<dt>全局强度</dt><dd>(.*?)</dd>", html) == ["87 / 100"]


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
        r"<script\b|<img\b|position-head\"|<template\b", result
    )
    assert private not in result
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result
    assert "甲 &amp; 乙" in result
    assert "&lt;&quot;group&quot;&gt;" in result
    assert "保留 &quot;理由&quot; &amp; 文本。" in result
    assert "https://example.invalid/" not in result
    assert_formal_records(result)
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', result, re.S)
    assert len(cards) == 5
    metric_values = r"<dd(?: class=\"reader-event\")?>(.*?)</dd>"
    assert re.findall(metric_values, cards[0]) == ["热", "无", "待核", "待核"]
    lookups = [
        ("查不到", "已核对合成查询 &amp; 全部结果，无精确匹配。"),
        ("待核", "只收到合成查询第一页，后续页为 &quot;&lt;待核&gt;&quot;。"),
        ("查不到", "查不到"),
        ("待核", "待核"),
    ]
    for card, (status, reason) in zip(cards[1:], lookups, strict=True):
        assert re.findall(metric_values, card) == [status, "无", status, status]
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
    html = render_verified_watch(
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
    html = render_verified_watch(
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
    cards_by_code = {re.search(r'<h4 class="reader-name">(\w+) <span>', card)[1]: card
                     for card in cards}
    assert 'reader-marker' not in cards_by_code["OLD"]
    assert 'reader-marker' not in cards_by_code["STR"]
    for code in ("Z", "Y", "MISS", "PENDING"):
        assert '<span class="reader-marker">直属</span>' in cards_by_code[code]
        assert '应建观察仓' not in cards_by_code[code]
    assert 'reader-marker' not in cards_by_code["X"]
    assert '<span class="reader-marker">直属</span>' in cards_by_code["F"]
    assert '<span class="reader-marker">应建观察仓</span>' in cards_by_code["F"]
    assert '<dt>全局强度</dt><dd>待核</dd>' in cards_by_code["F"]
    assert '<dt>当前趋势</dt><dd>查不到</dd>' in cards_by_code["MISS"]
    assert '<dt>当前趋势</dt><dd>待核</dd>' in cards_by_code["PENDING"]


def test_direct_partial_coverage_with_verified_cards(tmp_path):
    direct = {
        "code": "A",
        "name": "已核实标的",
        "direct_verified": True,
        "watch_eligible": True,
        "trend": "热",
        "signal_events": ["温转热"],
    }
    related = {"code": "X", "name": "关联标的"}
    public_note = "部分直属成员已核实，后续页 <待核> & 尚未确认。"
    escaped_note = "部分直属成员已核实，后续页 &lt;待核&gt; &amp; 尚未确认。"
    group = {
        "seat": 1,
        "name": "部分覆盖类",
        "direct": [direct],
        "direct_partial_note": public_note,
        "direct_note": "LEGACY-DIRECT-EMPTY-ONLY",
        "related": [related],
        "related_note": "LEGACY-RELATED-EMPTY-ONLY",
        "coverage_evidence": "PRIVATE-COVERAGE-DETAILS",
    }
    html = render_verified_watch(
        tmp_path,
        {
            "date": "2040-02-29",
            "long": [group, {"seat": 2, "name": "空列表类"}],
        },
        "partial-direct",
    )
    rows = re.findall(r"<details\b.*?</details>", html, re.S)
    assert len(rows) == 2
    direct_body, related_body = rows[0].split("<h3>相关的标的</h3>")
    assert '<h4 class="reader-name">A <span>' in direct_body
    assert '<span class="reader-marker">直属</span>' in direct_body
    assert '<span class="reader-marker">应建观察仓</span>' in direct_body
    note_html = f'<p class="empty-direct">{escaped_note}</p>'
    assert note_html in related_body
    assert related_body.index("</ul>") < related_body.index(note_html)
    assert escaped_note not in direct_body
    assert "LEGACY-" not in html
    assert "PRIVATE-COVERAGE-DETAILS" not in html
    assert "<待核>" not in html
    assert "已核实无合格直属标的" not in rows[0]
    assert "暂无已核实的直属成员。" not in rows[0]
    assert "暂无其他有据关联的标的。" not in rows[0]
    assert "暂无已核实的直属成员。" in rows[1]
    assert "暂无其他有据关联的标的。" in rows[1]
    assert rows[0].index("部分覆盖类") < rows[0].index("reader-card")
    assert "空列表类" in rows[1]
    assert digest(stylesheet(html)) == APPROVED_CSS
    assert shell_digest(html) == APPROVED_SHELL
    assert_formal_records(html)

    # Existing empty-only fields do not become new public partial-coverage text.
    legacy = {
        key: value for key, value in group.items() if key != "direct_partial_note"
    }
    for label, fields in (
        ("legacy", legacy),
        ("blank", {**group, "direct_partial_note": ""}),
    ):
        unchanged = render_verified_watch(
            tmp_path, {"date": "2040-02-29", "long": [fields]}, label
        )
        assert "LEGACY-" not in unchanged
        row = re.search(r"<details\b.*?</details>", unchanged, re.S)[0]
        # Empty platform block plus local coverage.
        assert row.count('class="empty-direct"') == 2
        assert "成员范围待查。" in row
        assert "PRIVATE-COVERAGE-DETAILS" not in unchanged
        assert unchanged.count('<li class="asset reader-card">') == 2

    empty = render_verified_watch(
        tmp_path,
        {
            "date": "2040-02-29",
            "long": [{
                "seat": 1,
                "name": "已知空列表",
                "direct_note": "直属范围待核 & 未完成。",
                "related_note": "关联范围待核。",
            }],
        },
        "empty-custom-notes",
    )
    assert "直属范围待核 &amp; 未完成。" in empty
    assert "关联范围待核。" in empty
    assert '<li class="asset reader-card">' not in empty


def test_three_instrument_sections(tmp_path):
    # Literal independent expectations: membership wins over exact lookup;
    # a same-code instrument in another market remains a separate card.
    def asset(code, **fields):
        return {"code": code, "name": f"合成 {code}", "market": "US",
                "instrument_type": "stock", **fields}

    a = asset("A", direct_verified=True, platform_match_verified=True,
              strength="60 / 100", signal_events=["开香槟"])
    b = asset("B", platform_match_verified=True, strength="90 / 100")
    c = asset("C", platform_match_verified=True, watch_eligible=True,
              local_strength=99,
              signal_events=["温转热"])
    group = {
        "seat": 1, "name": "三栏类", "direct": [a, dict(a)],
        "searchable": [b, c, dict(a, direct_verified=False, watch_eligible=True)],
        "related": [dict(a, direct_verified=False, watch_eligible=True), b,
                    asset("A", market="HK", lookup="pending"),
                    asset("A", instrument_type="future", lookup="pending"),
                    asset("A", instrument_id="PRIVATE-ID-1", lookup="pending"),
                    asset("A", instrument_id="PRIVATE-ID-2", lookup="pending"),
                    asset("D", lookup="miss"), asset("E", lookup="pending"),
                    asset("F", platform_match_verified=True, strength="0 / 100"),
                    asset("G", platform_match_verified=True, strength="90 / 100"),
                    asset("OLD", direct_verified="true",
                          platform_match_verified="true")],
        "direct_partial_note": "范围 <待核> & 未完成。",
        "private_identity": "PRIVATE-IDENTITY",
    }
    for direction, expected in (
        ("long", ["B", "G", "F", "C"]),
        ("short", ["F", "B", "G", "C"]),
    ):
        html = render_verified_watch(tmp_path, {"date": "2040-02-29", direction: [group,
            {"seat": 2, "name": "跨类归属", "related": [a]}]}, direction)
        rows = re.findall(r"<details\b.*?</details>", html, re.S)
        first, second, third = re.split(
            r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", rows[0]
        )[1:]
        def codes(body):
            return re.findall(r'<h4 class="reader-name">(\w+) <span>', body)
        assert codes(first) == ["A"]
        assert codes(second) == expected
        assert codes(third) == ["A", "A", "A", "A", "D", "E", "OLD"]
        assert "PRIVATE-ID" not in html
        assert "直属</span>" in first and "应建观察仓" not in first
        assert "趋势动物可查</span>" in first
        assert second.count("趋势动物可查</span>") == 4
        assert second.count("应建观察仓</span>") == 1
        assert "直属</span>" not in second + third
        assert "趋势动物可查</span>" not in third
        assert "查不到" in third and "待核" in third
        assert "PRIVATE-IDENTITY" not in html
        assert '"market"' not in html and 'instrument_type' not in html
        assert "范围 &lt;待核&gt; &amp; 未完成。" in third
        assert third.rindex("</ul>") < third.index("范围 &lt;待核&gt;")
        assert codes(rows[1]) == ["A"]  # independent class membership survives
        assert_formal_records(html)

    empty = render_verified_watch(tmp_path, {"date": "2041-01-02", "long": [
        {"seat": 1, "name": "空类"}]}, "three-empty")
    assert re.findall(r"<h3>(.*?)</h3>", empty) == [
        "大类直属", "趋势动物里面能够查到", "相关的标的"]
    empty_row = re.search(r"<details\b.*?</details>", empty, re.S)[0]
    assert empty_row.count('class="empty-direct"') == 4
    assert "成员范围待查。" in empty_row

    legacy = render_verified_watch(tmp_path, {"date": "2041-01-02", "long": [{
        "seat": 1, "name": "旧输入", "direct": [
            {"code": "OLD", "name": "未核实直属位置"}],
        "related": [dict(b)], "direct_note": "PRIVATE-OLD-EMPTY-NOTE",
        "related_note": "PRIVATE-OLD-RELATED-NOTE"}]}, "legacy-three")
    assert "PRIVATE-OLD" not in legacy
    legacy_row = re.search(r"<details\b.*?</details>", legacy, re.S)[0]
    first, second, third = re.split(
        r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", legacy_row
    )[1:]
    assert 'reader-card' not in first
    assert codes(second) == ["B"] and codes(third) == ["OLD"]
    assert 'reader-marker' not in third


def test_direct_once_with_mixed_identity_evidence(tmp_path):
    def asset(code="A", **fields):
        return {"code": code, "name": "同一标的", "market": "US",
                "instrument_type": "ETF", **fields}

    for label, identifier in (("text", "1"), ("integer", 1), ("zero", 0)):
        direct = asset(instrument_id=identifier, direct_verified=True)
        searchable = asset(platform_match_verified=True, watch_eligible=True)
        html = render(tmp_path, {"date": "2040-01-01", "long": [{
            "seat": 1, "name": "混合身份证据", "direct": [direct],
            "searchable": [searchable],
            "related": [asset(instrument_id=str(identifier)),
                        asset(instrument_id=identifier)]}]}, label)
        row = re.search(r"<details\b.*?</details>", html, re.S)[0]
        first, second, third = re.split(
            r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", row
        )[1:]
        assert row.count('class="asset reader-card"') == 1
        assert 'class="asset reader-card"' in first
        assert 'class="asset reader-card"' not in second + third
        assert '应建观察仓' not in first  # never copied from a lower alias
        assert 'instrument_id' not in html

    # Contradictory IDs make the code-only row ambiguous. No guesses based on
    # a shared name/symbol; differences of market, product and currency survive.
    html = render(tmp_path, {"date": "2040-01-01", "long": [{
        "seat": 1, "name": "身份边界", "direct": [
            asset(instrument_id="1", direct_verified=True)],
        "searchable": [asset(platform_match_verified=True)],
        "related": [asset(instrument_id="2"), asset(market="HK"),
                    asset(instrument_type="future"), asset(currency="EUR"),
                    asset(currency="USD"), {"code": "A", "name": "同一标的"}]
    }]}, "ambiguous-identities")
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
    assert len(cards) == 8
    assert html.count('class="reader-marker">直属</span>') == 1
    assert html.count('class="reader-marker">趋势动物可查</span>') == 1
    assert 'instrument_id' not in html and 'currency' not in html


def test_group_coverage_after_three_without_direct(tmp_path):
    public_note = "直属成员第二页 <待核> & 范围未完成。"
    escaped = "直属成员第二页 &lt;待核&gt; &amp; 范围未完成。"
    for label, cards in (("searchable", [{"code": "A", "name": "可查标的",
                                          "platform_match_verified": True}]),
                         ("all-empty", [])):
        html = render(tmp_path, {"date": "2040-01-01", "long": [{
            "seat": 1, "name": "未取得直属成员", "searchable": cards,
            "coverage_note": public_note,
            "direct_partial_note": "LEGACY-NOTE-NOT-AN-ADDITIONAL-NOTE",
            "coverage_evidence": "PRIVATE-COVERAGE-ID"}]}, label)
        row = re.search(r"<details\b.*?</details>", html, re.S)[0]
        assert re.findall(r"<h3>(.*?)</h3>", row) == [
            "大类直属", "趋势动物里面能够查到", "相关的标的"]
        assert row.count(escaped) == 1
        assert row.rindex('class="asset-group"') < row.index(escaped)
        if cards:
            assert row.index('</ul>') < row.index(escaped)
        assert 'LEGACY-NOTE' not in row and 'PRIVATE-COVERAGE' not in html
        assert '<待核>' not in html
        assert "暂无已核实的直属成员。" in row
        assert_formal_records(html)


@pytest.mark.parametrize("case", [
    "matched-alias", "pending-preferred", "pending-and-hit-on-one-row",
    "miss-versus-hit", "conflicting-flags", "different-date", "different-version",
    "scope-missing", "different-identity",
])
def test_direct_card_preserves_verified_lookup_fact(tmp_path, case):
    base = {"code": "A", "name": "合成同一标的", "market": "US",
            "instrument_type": "ETF", "instrument_id": "1",
            "lookup_scope": "2040-01-01/v1"}
    direct = {**base, "direct_verified": True}
    alias = {**base, "platform_match_verified": True,
             "trend": "LOSING-QUOTE", "strength": "99 / 100",
             "signal_events": ["开香槟"], "watch_eligible": True}
    aliases = [alias]
    if case in ("different-date", "different-version", "scope-missing"):
        # Scope conflicts require actual identity-query outcomes on both rows;
        # ordinary price/event records do not dispute a verified lookup.
        direct["lookup"] = "pending"
    if case in ("pending-preferred", "pending-and-hit-on-one-row"):
        direct.update(lookup="pending", trend="MASKED-QUOTE",
                      strength="88 / 100", local_strength=77,
                      signal_events=["反抽警报"], watch_eligible=True,
                      reason="OLD-PENDING-LOOKUP")
    if case == "pending-and-hit-on-one-row":
        direct["platform_match_verified"] = True
        aliases = []
    elif case == "miss-versus-hit":
        direct.update(lookup="miss", trend="MASKED-QUOTE", strength="88 / 100",
                      signal_events=["反抽警报"], watch_eligible=True)
    elif case == "conflicting-flags":
        direct.update(lookup="miss", platform_match_verified=True)
        aliases = []
    elif case == "different-date":
        alias["lookup_scope"] = "2039-12-31/v1"
    elif case == "different-version":
        alias["lookup_scope"] = "2040-01-01/v2"
    elif case == "scope-missing":
        del alias["lookup_scope"]
    elif case == "different-identity":
        alias["instrument_id"] = "2"
    html = render(tmp_path, {"date": "2040-01-01", "long": [{
        "seat": 1, "name": "核实归并", "direct": [direct],
        "searchable": aliases}]}, case)
    row = re.search(r"<details\b.*?</details>", html, re.S)[0]
    first, second, third = re.split(
        r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", row
    )[1:]
    assert first.count('class="asset reader-card"') == 1
    assert 'class="asset reader-card"' not in third
    assert 'class="reader-marker">直属</span>' in first
    compatible = case in ("matched-alias", "pending-preferred",
                          "pending-and-hit-on-one-row")
    assert ('class="reader-marker">趋势动物可查</span>' in first) is compatible
    assert row.count('class="asset reader-card"') == (
        2 if case == "different-identity" else 1)
    if case != "different-identity":
        assert 'class="asset reader-card"' not in second
    assert '应建观察仓' not in first
    assert 'LOSING-QUOTE' not in first and 'MASKED-QUOTE' not in first
    assert '99 / 100' not in first and '88 / 100' not in first
    assert '77 / 100' not in first
    assert '<dd class="reader-event">无</dd>' in first
    assert '<dt>当前趋势</dt><dd>待核</dd>' in first
    assert '<dt>全局强度</dt><dd>待核</dd>' in first
    assert '<dt>同品种强度</dt><dd>待核</dd>' in first
    assert 'OLD-PENDING-LOOKUP' not in first
    assert '查不到' not in first  # conflict is uncertain; positive is known found
    assert 'lookup_scope' not in html and '2040-01-01/v1' not in html


def test_non_direct_lookup_conflict_stays_related(tmp_path):
    base = {"code": "Q", "name": "合成关联", "market": "US",
            "instrument_type": "ETF", "instrument_id": "12",
            "lookup_scope": "2040-01-01/v1"}
    html = render(tmp_path, {"date": "2040-01-01", "long": [{
        "seat": 1, "name": "匹配冲突", "searchable": [{
            **base, "platform_match_verified": True,
            "trend": "MASKED-QUOTE", "strength": "99 / 100",
            "signal_events": ["开香槟"], "watch_eligible": True}],
        "related": [{**base, "lookup": "miss"}]}]}, "conflict-placement")
    row = re.search(r"<details\b.*?</details>", html, re.S)[0]
    first, second, third = re.split(
        r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", row
    )[1:]
    assert row.count('class="asset reader-card"') == 1
    assert 'class="asset reader-card"' not in first + second
    assert 'class="asset reader-card"' in third
    assert '<dt>当前趋势</dt><dd>待核</dd>' in third
    assert '<dt>全局强度</dt><dd>待核</dd>' in third
    assert '<dt>同品种强度</dt><dd>待核</dd>' in third
    assert '<dd class="reader-event">无</dd>' in third
    assert '趋势动物可查</span>' not in row and '应建观察仓' not in row
    assert '直属</span>' not in row and 'MASKED-QUOTE' not in row
    assert '99 / 100' not in row and '查不到' not in row
    assert 'lookup_scope' not in html


def test_same_scope_negative_lookup_survives_dedup(tmp_path):
    base = {"code": "MISS", "name": "合成阴性", "market": "US",
            "instrument_type": "ETF", "instrument_id": "negative-id",
            "lookup_scope": "2040-01-01/v1"}
    for placement in ("direct", "related"):
        for reverse in (False, True):
            pending = {**base, "lookup": "pending", "trend": "STALE-QUOTE",
                       "strength": 88, "local_strength": 77,
                       "signal_events": ["开香槟"], "watch_eligible": True,
                       "reason": "STALE-PENDING-REASON"}
            negative = {**base, "lookup": "miss", "reason": "LOSING-REASON"}
            group = {"seat": 1, "name": "阴性查询"}
            if placement == "direct":
                pending["direct_verified"] = True
                group["direct" if not reverse else "related"] = [pending]
                group["related" if not reverse else "direct"] = [negative]
            else:
                group["related"] = ([negative, pending] if reverse
                                    else [pending, negative])
            html = render(tmp_path, {"date": "2040-01-01", "long": [group]},
                          f"negative-{placement}-{reverse}")
            row = re.search(r"<details\b.*?</details>", html, re.S)[0]
            sections = re.split(
                r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", row
            )[1:]
            assert row.count('class="asset reader-card"') == 1
            assert 'class="asset reader-card"' in sections[
                0 if placement == "direct" else 2]
            for label in ("当前趋势", "全局强度", "同品种强度"):
                assert f"<dt>{label}</dt><dd>查不到</dd>" in row
            assert '<dd class="reader-event">无</dd>' in row
            for hidden in ("STALE-QUOTE", "88 / 100", "77 / 100", "开香槟",
                           "应建观察仓", "趋势动物可查</span>",
                           "STALE-PENDING-REASON", "lookup_scope"):
                assert hidden not in row
            if placement == "direct" or not reverse:
                assert "LOSING-REASON" not in row

    # Absent/partial scopes cannot promote another row's negative. An ordinary
    # quote/watch winner is not a pending query and keeps its independent facts.
    for case in ("no-scopes", "partial-scope", "quote-winner"):
        winner = {**base, "direct_verified": True, "lookup": "pending"}
        negative = {**base, "lookup": "miss"}
        if case == "no-scopes":
            winner.pop("lookup_scope")
            negative.pop("lookup_scope")
        elif case == "partial-scope":
            negative.pop("lookup_scope")
        else:
            winner.pop("lookup")
            winner.update(trend="热", strength=65, local_strength=45,
                          signal_events=["温转热"], watch_eligible=True)
        html = render_verified_watch(tmp_path, {
            "date": "2040-01-01", "long": [{
                "seat": 1, "name": "查询边界", "direct": [winner],
                "related": [negative]}]}, "negative-" + case)
        assert html.count('class="asset reader-card"') == 1
        assert "查不到" not in html
        if case == "quote-winner":
            assert '<dt>当前趋势</dt><dd>热</dd>' in html
            assert '<dt>全局强度</dt><dd>65 / 100</dd>' in html
            assert '<dt>同品种强度</dt><dd>45 / 100</dd>' in html
            assert '<dd class="reader-event">温转热</dd>' in html
            assert '应建观察仓</span>' in html
        else:
            for label in ("当前趋势", "全局强度", "同品种强度"):
                assert f"<dt>{label}</dt><dd>待核</dd>" in html
            assert '应建观察仓</span>' not in html


def test_negative_lookup_with_changed_scope_is_pending(tmp_path):
    base = {"code": "A", "name": "合成直属", "market": "US",
            "instrument_type": "ETF", "instrument_id": "1"}
    for label, alias_scope, expected in (
        ("changed", "2039-12-31/fixture-v2", "待核"),
        ("same", "2039-12-31/fixture-v1", "查不到"),
    ):
        html = render_verified_watch(tmp_path, {"date": "2040-01-01", "long": [{
            "seat": 1, "name": "查询版本核对", "direct": [{
                **base, "direct_verified": True, "lookup": "miss",
                "lookup_scope": "2039-12-31/fixture-v1", "trend": "MASKED-QUOTE",
                "strength": "88 / 100", "signal_events": ["开香槟"],
                "watch_eligible": True}], "related": [{
                    **base, "lookup": "pending", "lookup_scope": alias_scope,
                    "platform_match_verified": False}]}]}, label)
        row = re.search(r"<details\b.*?</details>", html, re.S)[0]
        first, second, third = re.split(
            r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", row
        )[1:]
        assert row.count('class="asset reader-card"') == 1
        assert 'class="asset reader-card"' in first
        assert 'class="asset reader-card"' not in second + third
        assert f'<dt>当前趋势</dt><dd>{expected}</dd>' in first
        assert f'<dt>全局强度</dt><dd>{expected}</dd>' in first
        assert f'<dt>同品种强度</dt><dd>{expected}</dd>' in first
        assert '<dd class="reader-event">待查</dd>' in first
        assert '直属</span>' in first and '趋势动物可查</span>' not in row
        assert '应建观察仓' not in row and 'MASKED-QUOTE' not in row
        assert '88 / 100' not in row and 'lookup_scope' not in html
        if label == "changed":
            assert '查不到' not in first

    # A price/event row is not an identity-query outcome. Its scope does not
    # dispute a separate verified match or mask the preferred current facts.
    for label, quote_scope in (("quote-other-scope", "2040-01-01/v2"),
                               ("quote-no-scope", None)):
        direct = {**base, "direct_verified": True, "trend": "热",
                  "strength": "65 / 100", "signal_events": ["温转热"],
                  "watch_eligible": True}
        if quote_scope is not None:
            direct["lookup_scope"] = quote_scope
        html = render_verified_watch(tmp_path, {"date": "2040-01-01", "long": [{
            "seat": 1, "name": "行情独立", "direct": [direct],
            "related": [{**base, "platform_match_verified": True,
                         "lookup_scope": "2040-01-01/v1"}]}]}, label)
        assert html.count('class="asset reader-card"') == 1
        assert '<dt>当前趋势</dt><dd>热</dd>' in html
        assert '<dt>全局强度</dt><dd>65 / 100</dd>' in html
        assert '<dd class="reader-event">温转热</dd>' in html
        assert '应建观察仓</span>' in html and '趋势动物可查</span>' in html


def test_dual_strength_values(tmp_path):
    # Literal source values intentionally differ; a peer value cannot fill G.
    cases = [
        ({"strength": "20.500 / 100", "local_strength": "90.250"},
         "20.500 / 100", "90.250 / 100", []),
        ({"strength": 0, "local_strength": 0}, "0 / 100", "0 / 100", []),
        ({"strength": "同品种 43.00 / 100"}, "待核", "43.00 / 100",
         ["全局强度字段缺失或无效。"]),
        ({"strength": "80 / 100", "local_strength": None,
          "local_strength_reason": "同品种字段本版未提供。"},
         "80 / 100", "待核", ["同品种字段本版未提供。"]),
        ({"local_strength": "10.0", "strength_reason": "全局字段尚未获准补取。"},
         "待核", "10.0 / 100", ["全局字段尚未获准补取。"]),
        ({}, "待核", "待核",
         ["全局强度字段缺失或无效。", "同品种强度字段缺失或无效。"]),
        ({"strength": "25 / 100"}, "25 / 100", "待核",
         ["同品种强度字段缺失或无效。"]),
    ]
    for index, (fields, global_value, local_value, reasons) in enumerate(cases):
        html = render(tmp_path, {"date": "2040-01-01", "long": [{
            "seat": 1, "name": "固定合成类", "searchable": [{
                "code": "FIXTURE", "name": "固定合成标的",
                "platform_match_verified": True, **fields,
            }],
        }]}, f"dual-values-{index}")
        assert re.findall(r"<dt>全局强度</dt><dd>(.*?)</dd>", html) == [global_value]
        assert re.findall(r"<dt>同品种强度</dt><dd>(.*?)</dd>", html) == [local_value]
        for reason in reasons:
            assert reason in html
        assert "同品种 43.00" not in html


def test_dual_strength_order(tmp_path):
    # Hand-worked G order: 0 < 20 = 20 < 80; missing follows every number.
    for direction, expected in (
        ("long", ["B", "A", "D", "Z", "C"]),
        ("short", ["Z", "A", "D", "B", "C"]),
    ):
        for swapped in (False, True):
            groups = []
            assets = []
            for section, flags in (
                ("DIRECT", {"direct_verified": True}),
                ("MATCH", {"platform_match_verified": True}),
                ("RELATED", {}),
            ):
                for code, g, local in (
                    ("A", 20, 90), ("B", 80, 10), ("C", None, 100),
                    ("D", 20, 5), ("Z", 0, 99),
                ):
                    assets.append({"code": f"{section}-{code}", "name": "合成",
                                   "strength": g,
                                   "local_strength": 100-local if swapped else local,
                                   **flags})
            groups.append({"seat": 1, "name": "合成排序类", "related": assets})
            html = render(tmp_path, {"date": "2040-01-01", direction: groups},
                          f"order-{direction}-{swapped}")
            assert re.findall(r'<h4 class="reader-name">(\S+)', html) == [
                f"{section}-{code}" for section in ("DIRECT", "MATCH", "RELATED")
                for code in expected
            ]


def test_lookup_cannot_expose_dual_strength(tmp_path):
    base = {"code": "FIXTURE", "name": "合成身份", "instrument_id": "id-1",
            "market": "US", "instrument_type": "ETF"}
    stale = {"trend": "STALE-TREND", "strength": "88.123 / 100",
             "local_strength": "77.456", "signal_events": ["开香槟"],
             "watch_eligible": True, "strength_reason": "STALE-G-REASON",
             "local_strength_reason": "STALE-L-REASON"}
    for case in ("miss", "pending", "conflict", "version", "resolved-identity"):
        direct = {**base, **stale, "direct_verified": True,
                  "lookup": "pending" if case == "resolved-identity" else "miss",
                  "lookup_scope": "2040-01-01/v1"}
        aliases = []
        if case == "pending":
            direct["lookup"] = "pending"
        if case in ("conflict", "version", "resolved-identity"):
            aliases = [{**base, **stale, "platform_match_verified": True,
                        "lookup_scope": "2040-01-01/v2" if case == "version"
                        else "2040-01-01/v1"}]
        html = render(tmp_path, {"date": "2040-01-01", "long": [{
            "seat": 1, "name": "合成类", "direct": [direct], "searchable": aliases,
        }]}, f"masked-{case}")
        cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
        assert len(cards) == 1
        row = cards[0]
        status = "查不到" if case == "miss" else "待核"
        assert f"<dt>全局强度</dt><dd>{status}</dd>" in row
        assert f"<dt>同品种强度</dt><dd>{status}</dd>" in row
        assert '<dd class="reader-event">无</dd>' in row
        for hidden in ("STALE", "88.123", "77.456", "开香槟", "应建观察仓"):
            assert hidden not in row
        assert "直属" in row
        assert ("趋势动物可查" in row) == (case == "resolved-identity")


def test_verified_dual_strength_survives_dedup(tmp_path):
    # Split facts enter the real CLI, not a manually consolidated test card.
    scope = {"data_date": "2040-01-01", "version": "v1"}
    proof = {**scope, "state": "verified"}
    base = {"code": "A", "name": "合成直属", "market": "US",
            "instrument_type": "ETF", "instrument_id": "id-A"}
    cases = [
        ("split", {}, {}, "80 / 100", "20 / 100"),
        ("no-opt-in", {"strength_scope": None}, {}, "待核", "待核"),
        ("unverified", {}, {"strength_evidence": {}}, "待核", "待核"),
        ("no-match-proof", {}, {"platform_match_verified": False}, "待核", "待核"),
        ("idless-alias", {}, {"instrument_id": None}, "待核", "待核"),
        ("unresolved-field", {}, {"strength_evidence": {
            "strength": {**proof, "state": "pending"},
            "local_strength": proof}}, "待核", "20 / 100"),
        ("alias-disagreement", {}, {}, "待核", "20 / 100"),
        ("foreign-market", {}, {"market": "HK"}, "待核", "待核"),
        ("foreign-id", {}, {"instrument_id": "id-B"}, "待核", "待核"),
        ("foreign-type", {}, {"instrument_type": "stock"}, "待核", "待核"),
        ("foreign-date", {}, {"strength_evidence": {
            "strength": {**proof, "data_date": "2039-12-31"},
            "local_strength": proof}}, "待核", "20 / 100"),
        ("foreign-version", {}, {"strength_evidence": {
            "strength": {**proof, "version": "v2"},
            "local_strength": proof}}, "待核", "20 / 100"),
        ("withdrawn", {"strength": None}, {}, "待核", "20 / 100"),
        ("pending-preferred", {"lookup": "pending"}, {}, "待核", "待核"),
        ("pending-alias", {}, {"lookup": "pending"}, "待核", "待核"),
        ("miss", {"lookup": "miss"}, {}, "待核", "待核"),
        ("conflict", {"strength": "70 / 100"}, {}, "待核", "20 / 100"),
        ("preferred-kept", {"strength": "70 / 100"},
         {"strength": None, "strength_evidence": {"local_strength": proof}},
         "70 / 100", "20 / 100"),
        ("withdrawal-proof", {"strength": "80 / 100"},
         {"strength_evidence": {"strength": {**proof, "state": "withdrawn"},
                                "local_strength": proof}}, "待核", "20 / 100"),
        ("stale-preferred", {"strength": "80 / 100", "strength_evidence": {
            "strength": {**proof, "data_date": "2039-12-31"}}},
         {}, "待核", "20 / 100"),
    ]
    for case, preferred_fields, alias_fields, global_value, local_value in cases:
        direct = {**base, "direct_verified": True, "strength_scope": scope,
                  **preferred_fields}
        quote = {**base, "platform_match_verified": True, "strength": "80 / 100",
                 "local_strength": "20", "strength_evidence": {
                     "strength": proof, "local_strength": proof},
                 "trend": "ALIEN-TREND", "signal_events": ["开香槟"],
                 "watch_eligible": True, "reason": "ALIEN-REASON", **alias_fields}
        # Reversing duplicate sources must not choose an arbitrary value.
        aliases = [quote]
        if case == "split":
            quote = {**quote, "strength_evidence": {"strength": proof}}
            aliases = [quote, {**quote, "strength_evidence": {
                "local_strength": proof}}]
        if case == "alias-disagreement":
            aliases = [quote, {**quote, "strength": "81 / 100"}]
        for reverse in (False, True):
            html = render(tmp_path, {"date": "2040-01-02", "long": [{
                "seat": 1, "name": "合成验证组", "direct": [direct, {
                    "code": "B", "name": "固定对照", "direct_verified": True,
                    "strength": "60 / 100"}],
                "searchable": list(reversed(aliases)) if reverse else aliases,
            }]}, f"dedup-{case}-{reverse}")
            first = re.split(r"<h3>大类直属</h3>", html)[1].split(
                "<h3>趋势动物里面能够查到</h3>")[0]
            cards = re.findall(r'<li class="asset reader-card">.*?</li>', first, re.S)
            by_code = {re.search(r'<h4 class="reader-name">(\S+)', c)[1]: c
                       for c in cards}
            assert len(cards) == 2
            a = by_code["A"]
            assert f'<dt>全局强度</dt><dd>{global_value}</dd>' in a, case
            assert f'<dt>同品种强度</dt><dd>{local_value}</dd>' in a, case
            assert '<dt>当前趋势</dt><dd>待核</dd>' in a
            assert '<dd class="reader-event">无</dd>' in a
            assert all(text not in a for text in (
                "ALIEN", "开香槟", "应建观察仓", "data_date", "strength_evidence"))
            assert re.findall(r'<h4 class="reader-name">(\S+)', first) == (
                ["B", "A"] if global_value == "待核" else ["A", "B"])


def test_legacy_peer_text_allows_verified_global_strength(tmp_path):
    scope = {"data_date": "2040-01-01", "version": "v1"}
    proof = {**scope, "state": "verified"}
    withdrawal = {**scope, "state": "withdrawn"}
    base = {"code": "A", "name": "合成兼容标的", "market": "US",
            "instrument_type": "ETF", "instrument_id": "id-A"}
    for case, direct_fields, donor_fields, global_value, local_value in [
        ("legacy-preferred", {"strength": "同品种 20 / 100"}, {},
         "80 / 100", "20 / 100"),
        ("explicit-global-null", {"strength": None, "local_strength": 20}, {},
         "待核", "20 / 100"),
        ("explicit-global-invalid", {"strength": "INVALID", "local_strength": 20},
         {}, "待核", "20 / 100"),
        ("global-withdrawal", {"strength": "同品种 20 / 100",
                               "strength_evidence": {"strength": withdrawal}},
         {}, "待核", "20 / 100"),
        ("local-null", {"strength": "同品种 20 / 100", "local_strength": None},
         {}, "80 / 100", "待核"),
        ("legacy-donor", {}, {"strength": "同品种 20 / 100",
                              "strength_evidence": {
                                  "strength": proof, "local_strength": proof}},
         "80 / 100", "20 / 100"),
        ("legacy-only-donor", {}, {"strength": "同品种 20 / 100",
                                   "strength_evidence": {
                                       "strength": proof, "local_strength": proof}},
         "待核", "20 / 100"),
        ("donor-withdrawal", {"strength": "80 / 100", "local_strength": 20},
         {"strength": "同品种 20 / 100", "strength_evidence": {
             "strength": withdrawal, "local_strength": proof}},
         "待核", "20 / 100"),
        ("unsupported-legacy-donor", {"strength": "80 / 100",
                                      "local_strength": 20},
         {"strength": "同品种 90 / 100", "strength_evidence": {"strength": proof}},
         "80 / 100", "20 / 100"),
    ]:
        direct = {**base, "direct_verified": True, "strength_scope": scope,
                  **direct_fields}
        global_source = {**base, "platform_match_verified": True,
                         "strength": "80 / 100", "strength_evidence": {
                             "strength": proof}}
        donor = {**base, "platform_match_verified": True, **donor_fields}
        for reverse in (False, True):
            aliases = [donor] if case == "legacy-only-donor" else [global_source, donor]
            html = render(tmp_path, {"date": "2040-01-02", "long": [{
                "seat": 1, "name": "合成兼容组", "direct": [direct, {
                    "code": "B", "name": "对照", "direct_verified": True,
                    "strength": "60 / 100"}],
                "searchable": aliases[::-1] if reverse else aliases,
            }]}, f"legacy-proof-{case}-{reverse}")
            cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
            assert len(cards) == 2
            a = next(card for card in cards if 'reader-name">A ' in card)
            assert f'<dt>全局强度</dt><dd>{global_value}</dd>' in a, case
            assert f'<dt>同品种强度</dt><dd>{local_value}</dd>' in a, case
            assert re.findall(r'<h4 class="reader-name">(\S+)', html) == (
                ["B", "A"] if global_value == "待核" else ["A", "B"])
            assert '<dt>当前趋势</dt><dd>待核</dd>' in a
            assert '<dd class="reader-event">无</dd>' in a
            assert '应建观察仓' not in a
            assert '同品种 20 / 100' not in a


def mixed_report():
    return {
        "date": "2026-11-23",
        "board_date": "2026-11-22",
        "board_version": "2026-11-23/v1",
        "long": [
            {
                "seat": 1,
                "name": "合成加密",
                "board_strength": "80",
                "data_date": "2026-11-22",
                "searchable": [
                    {
                        "code": "BTC",
                        "name": "合成比特币",
                        "market": "CRYPTO",
                        "instrument_type": "spot",
                        "instrument_id": "btc",
                        "platform_match_verified": True,
                        "data_date": "2026-11-21",
                        "trend": "热",
                        "strength": "80",
                        "local_strength": "90",
                        "signal_events": [],
                        "watch_eligible": True,
                    }
                ],
            },
            {
                "seat": 2,
                "name": "合成股票",
                "board_strength": "70",
                "data_date": "2026-11-22",
                "searchable": [
                    {
                        "code": "ETF",
                        "name": "合成基金",
                        "market": "US",
                        "instrument_type": "ETF",
                        "instrument_id": "etf",
                        "platform_match_verified": True,
                        "data_date": "2026-11-20",
                        "trend": "热",
                        "strength": "60",
                        "local_strength": "50",
                        "signal_events": [],
                    }
                ],
            },
        ],
        "collection": {
            "targets": {
                "BOARD": "2026-11-22",
                "CRYPTO": "2026-11-22",
                "US": "2026-11-20",
            },
            "next_check": "2026-11-23T10:30:00+08:00",
            "worklist": [],
            "evidence": {},
        },
    }


def test_monday_mixed_data_dates_pending(tmp_path):
    html = render(tmp_path, mixed_report(), "monday")
    assert "数据收集中" in html and "数据已完备" not in html
    from test_hourly_collection import control
    _, pending = control().dated_projection(mixed_report())
    assert "BTC：目标 2026-11-22，实际 2026-11-21" in pending
    assert "2026-11-23T10:30:00+08:00" in html
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
    btc = next(c for c in cards if 'reader-name">BTC ' in c)
    etf = next(c for c in cards if 'reader-name">ETF ' in c)
    assert "行情截至 2026-11-21" in btc and "历史数据" in btc
    assert "行情截至 2026-11-20" in etf
    assert "应建观察仓" not in btc
    assert "2026-11-23/v1" not in html


def test_release_without_matching_instrument_stays_pending(tmp_path):
    report = mixed_report()
    asset = report["long"][0]["searchable"][0]
    asset["data_date"] = "2026-11-22"
    report["collection"]["releases"] = {"CRYPTO": "2026-11-22"}
    report["collection"]["evidence"] = {
        "/long/0/searchable/0/strength": {
            "state": "verified",
            "data_date": "2026-11-21",
            "version": "v1",
            "source": "fixture-old-response",
            "value": "80",
            "identity": "btc",
        }
    }
    html = render(tmp_path, report, "release-only")
    assert "数据收集中" in html and "数据已完备" not in html
    from test_hourly_collection import control
    assert (
        "BTC strength：目标 2026-11-22，实际 2026-11-21"
        in control().assess(report)["pending"]
    )
    assert "全局强度日期" not in html
    assert "应建观察仓" not in html


    for label, day in (("future-card", "2026-11-24"), ("invalid-card", "bad-date")):
        invalid = mixed_report()
        card = invalid["long"][0]["searchable"][0]
        card.update(data_date=day, trend="INVALID-DATE-QUOTE", strength="88.888",
                    local_strength="77.777", signal_events=["开香槟"])
        html = render(tmp_path, invalid, label)
        btc = next(c for c in re.findall(
            r'<li class="asset reader-card">.*?</li>', html, re.S)
                   if 'reader-name">BTC ' in c)
        assert all(hidden not in btc for hidden in (
            "INVALID-DATE-QUOTE", "88.888", "77.777", "开香槟", "应建观察仓"))


def complete_report():
    report = mixed_report()
    report["long"][0]["searchable"][0]["data_date"] = "2026-11-22"
    report["short"] = [
        {
            "seat": 1,
            "name": "合成弱类甲",
            "board_strength": "10",
            "data_date": "2026-11-22",
        },
        {
            "seat": 2,
            "name": "合成弱类乙",
            "board_strength": "20",
            "data_date": "2026-11-22",
        },
    ]
    collection = report["collection"]
    collection["verified_at"] = "2026-11-23T10:12:00+08:00"
    facts = [
        ("/board_date", "榜图", "BOARD", "board", report["board_date"]),
        ("/board_version", "榜图版次", "BOARD", "board", report["board_version"]),
    ]
    for direction in ("short", "long"):
        for i, group in enumerate(report[direction]):
            prefix = f"/{direction}/{i}"
            members = [a["instrument_id"] for a in group.get("searchable", [])]
            group["coverage"] = {"state": "complete", "instrument_ids": members}
            for field in ("board_strength", "coverage"):
                facts.append(
                    (
                        prefix + "/" + field,
                        group["name"] + " " + field,
                        "BOARD",
                        group["name"],
                        group[field],
                    )
                )
            for j, asset in enumerate(group.get("searchable", [])):
                for field in ("trend", "strength", "local_strength", "signal_events"):
                    facts.append(
                        (
                            prefix + f"/searchable/{j}/" + field,
                            asset["code"] + " " + field,
                            asset["market"],
                            asset["instrument_id"],
                            asset[field],
                        )
                    )
    for path, label, market, identity, value in facts:
        day = collection["targets"][market]
        collection["worklist"].append(
            {"path": path, "label": label, "market": market, "target_date": day}
        )
        collection["evidence"][path] = {
            "state": "verified",
            "data_date": day,
            "version": "fixture-v1",
            "market": market,
            "instrument_type": ("spot" if market == "CRYPTO" else "ETF"),
            "source": "fixture-response-" + path,
            "identity": identity,
            "value": value,
        }
    return report


def test_complete_report_shows_confirmation_time(tmp_path):
    report = complete_report()
    html = render(tmp_path, report, "complete")
    assert "数据已完备" in html and "数据收集中" not in html
    assert "2026-11-23T10:12:00+08:00" in html
    assert "行情截至 2026-11-20" in html and "行情截至 2026-11-22" in html
    assert "应建观察仓" in html
    # A proof is bound to the field, identity, date and source, independently.
    path = "/long/0/searchable/0/strength"
    for label, changes in (
        ("mixed", {"data_date": "2026-11-21"}),
        ("identity", {"identity": "other"}),
        ("value", {"value": "99"}),
        ("source", {"source": ""}),
        ("withdrawn", {"state": "withdrawn"}),
    ):
        revised = complete_report()
        revised["collection"]["evidence"][path].update(changes)
        html = render(tmp_path, revised, label)
        assert "数据收集中" in html and "数据已完备" not in html
        assert "应建观察仓" not in html


    for label in ("card-date", "foreign-market", "invalid-strength"):
        revised = complete_report()
        asset = revised["long"][0]["searchable"][0]
        if label == "card-date":
            asset["data_date"] = "2026-11-21"
        elif label == "foreign-market":
            revised["collection"]["evidence"][path]["market"] = "OTHER"
        else:
            asset["strength"] = "INVALID"
            revised["collection"]["evidence"][path]["value"] = "INVALID"
        html = render(tmp_path, revised, label)
        assert "数据收集中" in html and "数据已完备" not in html


    revised = complete_report()
    asset = revised["long"][0]["searchable"][0]
    asset.update(lookup="miss", platform_match_verified=False)
    manifest = revised["collection"]
    prefix = "/long/0/searchable/0/"
    manifest["worklist"] = [item for item in manifest["worklist"]
                            if not item["path"].startswith(prefix)]
    manifest["worklist"].append({"path": prefix + "lookup", "label": "BTC lookup",
                                 "market": "CRYPTO", "target_date": "2026-11-22"})
    manifest["evidence"][prefix + "lookup"] = {
        "state": "verified", "data_date": "2026-11-22", "version": "fixture-v1",
        "source": "fixture-full-result", "identity": "btc", "market": "CRYPTO",
        "instrument_type": "spot", "value": "miss", "full_lookup": True}
    html = render(tmp_path, revised, "verified-miss")
    assert "数据已完备" in html and "查不到" in html
    assert "应建观察仓" not in html
    manifest["evidence"][prefix + "lookup"]["full_lookup"] = False
    html = render(tmp_path, revised, "unverified-miss")
    assert "数据收集中" in html and "数据已完备" not in html
    assert "查不到" not in html and "待核" in html


def test_missing_manifest_cannot_finish(tmp_path):
    for name, manifest in (
        ("absent", None),
        ("empty", {}),
        ("bare", {"complete": True, "missing": []}),
    ):
        report = {"date": "2026-11-23"}
        if manifest is not None:
            report["collection"] = manifest
        html = render(tmp_path, report, name)
        assert "数据收集中" in html and "数据已完备" not in html
    for name in ("unresolved-extra", "duplicate", "invalid-target", "missing-item"):
        report = complete_report()
        manifest = report["collection"]
        if name == "unresolved-extra":
            manifest["worklist"].append(
                {
                    "path": "/unknown",
                    "label": "尚未核完的目录",
                    "market": "US",
                    "target_date": "2026-11-20",
                }
            )
        elif name == "duplicate":
            manifest["worklist"].append(manifest["worklist"][0])
        elif name == "invalid-target":
            manifest["worklist"][0]["target_date"] = "unknown"
        else:
            manifest["worklist"].pop()
        html = render(tmp_path, report, name)
        assert "数据收集中" in html and "数据已完备" not in html, name


@pytest.mark.parametrize(
    "cited_day", ["2026-11-19", "2026-11-24", None],
    ids=["stale", "future", "missing"],
)
def test_stale_cited_board_cannot_complete(tmp_path, cited_day):
    from test_hourly_collection import control

    report = complete_report()
    api = control()
    state = tmp_path / "control.json"
    api.initialize(state, report)
    assert api.claim_round(state, report, "2026-11-23T08:30:00+08:00")["slot"] == 0
    local, delivered = tmp_path / "local.html", tmp_path / "delivered.html"
    current_html = api.render_html(report)
    local.write_text(current_html, encoding="utf-8")
    delivered.write_text(current_html, encoding="utf-8")
    api.confirm_publication(state, report, local, delivered)
    assert api.claim_round(state, report, "2026-11-23T10:30:00+08:00") is None

    # The cited board date and its matching value agree with each other, while
    # evidence metadata still claims the target day. The citation must win.
    if cited_day is None:
        report.pop("board_date")
    else:
        report["board_date"] = cited_day
    report["collection"]["evidence"]["/board_date"]["value"] = cited_day
    html = render(tmp_path, report, "cited-board")
    assert "数据收集中" in html and "数据已完备" not in html
    assert (
        f"榜图：目标 2026-11-22，实际 {cited_day or '待核'}"
        in api.assess(report)["pending"]
    )
    assert "大类数据日期 2026-11-22" in html
    assert "2026-11-23/v1" not in html
    if cited_day:
        assert report["board_date"] == cited_day
        assert "官方榜图。" not in html
    else:
        assert "board_date" not in report
        assert "榜图：目标 2026-11-22，实际 待核" in api.assess(report)["pending"]

    round_ = api.claim_round(state, report, "2026-11-23T10:30:00+08:00")
    assert round_["markets"] == ["BOARD"]
    assert "/board_date" in round_["pending_paths"]
    assert round_["changed"] == ["/board_date"]
    # A later slot cannot replay an already claimed round, nor can publication
    # acknowledge this incomplete projection even when file bytes match it.
    assert api.claim_round(state, report, "2026-11-23T10:31:00+08:00") is None
    local.write_text(html, encoding="utf-8")
    delivered.write_text(html, encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        api.confirm_publication(state, report, local, delivered)


def test_wrong_field_date_cannot_rank_or_publish_current_value(tmp_path):
    from copy import deepcopy

    base = {
        "code": "A",
        "name": "合成甲",
        "market": "US",
        "instrument_type": "ETF",
        "instrument_id": "a",
        "data_date": "2026-11-20",
        "platform_match_verified": True,
        "direct_verified": True,
        "strength": "99",
        "local_strength": "77",
        "trend": "热",
        "signal_events": ["开香槟"],
        "watch_eligible": True,
    }
    other = {
        **base,
        "code": "B",
        "name": "合成乙",
        "instrument_id": "b",
        "strength": "40",
        "local_strength": "20",
        "watch_eligible": False,
    }
    source = {
        "date": "2026-11-23",
        "long": [{"seat": 1, "name": "合成比较", "direct": [base, other]}],
        "collection": {"targets": {"US": "2026-11-20"}, "evidence": {}},
    }
    for i, asset in enumerate((base, other)):
        for field in ("trend", "signal_events", "strength", "local_strength"):
            source["collection"]["evidence"][f"/long/0/direct/{i}/{field}"] = {
                "state": "verified",
                "data_date": "2026-11-20",
                "version": "v1",
                "identity": asset["instrument_id"],
                "market": "US",
                "instrument_type": "ETF",
                "source": "fixture-field-response",
                "value": asset[field],
            }
    path = "/long/0/direct/0/strength"
    for case in (
        "future",
        "bad-date",
        "old",
        "identity",
        "market",
        "type",
        "value",
        "UNKNOWN",
        "missing-proof",
        "null-proof",
        "withdrawn",
        "split-revival",
    ):
        report = deepcopy(source)
        proof = report["collection"]["evidence"][path]
        if case in ("future", "bad-date", "old"):
            proof["data_date"] = {
                "future": "2026-11-24",
                "bad-date": "bad",
                "old": "2026-11-19",
            }[case]
        elif case in ("identity", "market", "type", "value"):
            key = {"type": "instrument_type"}.get(case, case)
            proof[key] = "OTHER"
        elif case == "UNKNOWN":
            proof["state"] = "UNKNOWN"
        elif case == "null-proof":
            report["collection"]["evidence"][path] = None
        elif case == "missing-proof":
            report["collection"]["evidence"].pop(path)
        else:
            proof["state"] = "withdrawn"
        if case == "split-revival":
            a = report["long"][0]["direct"][0]
            a["strength_scope"] = {"data_date": "2026-11-20", "version": "v1"}
            alias = {
                **base,
                "direct_verified": False,
                "strength": "100",
                "strength_evidence": {
                    "strength": {
                        "data_date": "2026-11-20",
                        "version": "v1",
                        "state": "verified",
                    }
                },
            }
            report["long"][0]["searchable"] = [alias]
            report["collection"]["evidence"]["/long/0/searchable/0/strength"] = {
                **source["collection"]["evidence"][path],
                "value": "100",
            }
        html = render(tmp_path, report, "field-" + case)
        cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
        assert re.findall(r'<h4 class="reader-name">(\S+)', html) == ["B", "A"], case
        a = next(card for card in cards if 'reader-name">A ' in card)
        assert "<dt>全局强度</dt><dd>待核</dd>" in a, case
        assert "<dt>同品种强度</dt><dd>77 / 100</dd>" in a, case
        assert "99 / 100" not in a and "100 / 100" not in a
        assert "应建观察仓" not in a
        if case == "old":
            from test_hourly_collection import control
            assert (
                "A strength：目标 2026-11-20，实际 2026-11-19"
                in control().assess(report)["pending"]
            )
            assert "全局强度日期" not in a
    for field, hidden in (("trend", "热"), ("signal_events", "开香槟")):
        report = deepcopy(source)
        report["collection"]["evidence"]["/long/0/direct/0/" + field]["identity"] = (
            "other"
        )
        html = render(tmp_path, report, "other-field-" + field)
        a = next(
            card
            for card in re.findall(
                r'<li class="asset reader-card">.*?</li>', html, re.S
            )
            if 'reader-name">A ' in card
        )
        assert f"<dd>{hidden}</dd>" not in a if field == "trend" else hidden not in a
        assert "<dt>同品种强度</dt><dd>77 / 100</dd>" in a

    # A missing preferred field can still use an independently verified current
    # split source. An invalid donor must not revive it or mask a valid peer.
    for valid in (True, False):
        report = deepcopy(source)
        preferred = report["long"][0]["direct"][0]
        preferred.pop("strength")
        preferred["strength_scope"] = {"data_date": "2026-11-20", "version": "v1"}
        report["collection"]["evidence"].pop(path)
        donor = {
            **base,
            "direct_verified": False,
            "strength": "88",
            "strength_evidence": {
                "strength": {
                    "data_date": "2026-11-20",
                    "version": "v1",
                    "state": "verified",
                }
            },
        }
        report["long"][0]["searchable"] = [donor]
        report["collection"]["evidence"]["/long/0/searchable/0/strength"] = {
            **source["collection"]["evidence"][path],
            "value": "88",
            "data_date": "2026-11-20" if valid else "2026-11-24",
        }
        html = render(tmp_path, report, f"split-{valid}")
        a = next(
            card
            for card in re.findall(
                r'<li class="asset reader-card">.*?</li>', html, re.S
            )
            if 'reader-name">A ' in card
        )
        assert "<dt>同品种强度</dt><dd>77 / 100</dd>" in a
        assert ("<dt>全局强度</dt><dd>88 / 100</dd>" in a) is valid
        assert re.findall(r'<h4 class="reader-name">(\S+)', html) == (
            ["A", "B"] if valid else ["B", "A"]
        )


def test_explicit_collection_missing_lookup_evidence_stays_pending(tmp_path):
    from copy import deepcopy

    asset = {
        "code": "MISS",
        "name": "合成查询",
        "market": "US",
        "instrument_type": "ETF",
        "instrument_id": "miss-id",
        "data_date": "2026-11-20",
        "lookup": "miss",
    }
    source = {
        "date": "2026-11-23",
        "long": [{"seat": 1, "name": "合成查询类", "related": [asset]}],
        "collection": {"targets": {"US": "2026-11-20"}, "evidence": {}},
    }
    path = "/long/0/related/0/lookup"
    proof = {
        "state": "verified",
        "data_date": "2026-11-20",
        "version": "v1",
        "identity": "miss-id",
        "market": "US",
        "instrument_type": "ETF",
        "source": "fixture-full-catalog",
        "value": "miss",
        "full_lookup": True,
    }
    for case in (
        "no-proof",
        "null-to-null",
        "no-target",
        "no-id",
        "no-market",
        "no-type",
        "no-source",
        "not-full",
    ):
        report = deepcopy(source)
        card = report["long"][0]["related"][0]
        evidence = deepcopy(proof)
        if case != "no-proof":
            report["collection"]["evidence"][path] = evidence
        if case == "null-to-null":
            card["instrument_id"] = evidence["identity"] = None
            report["collection"]["targets"]["US"] = evidence["data_date"] = None
        elif case == "no-target":
            report["collection"]["targets"]["US"] = None
        elif case == "no-id":
            card["instrument_id"] = None
        elif case == "no-market":
            card.pop("market")
        elif case == "no-type":
            card.pop("instrument_type")
        elif case == "no-source":
            evidence.pop("source")
        elif case == "not-full":
            evidence["full_lookup"] = False
        html = render(tmp_path, report, "lookup-" + case)
        assert "查不到" not in html and "待核" in html, case
        assert "数据收集中" in html and "数据已完备" not in html
    report = deepcopy(source)
    report["collection"]["evidence"][path] = proof
    html = render(tmp_path, report, "full-miss")
    assert "查不到" in html
    # Explicit old-format compatibility is retained, without inventing proof.
    report.pop("collection")
    html = render(tmp_path, report, "legacy-miss")
    assert "查不到" in html


def test_same_scope_negative_lookup_requires_applicable_collection_proof(tmp_path):
    from copy import deepcopy

    from test_hourly_collection import control

    source = complete_report()
    group = source["long"][0]
    negative = group["searchable"].pop(0)
    negative.update(lookup="miss", platform_match_verified=False,
                    lookup_scope="2026-11-22/v1")
    group["related"] = [negative]
    group["direct"] = [{**negative, "lookup": "pending",
                        "direct_verified": True, "reason": "STALE-PENDING"}]
    group["coverage"]["instrument_ids"].append("btc")
    manifest = source["collection"]
    old_prefix = "/long/0/searchable/0/"
    manifest["worklist"] = [item for item in manifest["worklist"]
                            if not item["path"].startswith(old_prefix)]
    manifest["evidence"] = {path: proof
                            for path, proof in manifest["evidence"].items()
                            if not path.startswith(old_prefix)}
    manifest["evidence"]["/long/0/coverage"]["value"] = deepcopy(group["coverage"])
    path = "/long/0/related/0/lookup"
    for section in ("direct", "related"):
        manifest["worklist"].append({
            "path": f"/long/0/{section}/0/lookup", "label": "BTC lookup",
            "market": "CRYPTO", "target_date": "2026-11-22"})
    manifest["evidence"][path] = {
        "state": "verified", "data_date": "2026-11-22", "version": "v1",
        "identity": "btc", "market": "CRYPTO", "instrument_type": "spot",
        "source": "fixture-full-negative", "value": "miss", "full_lookup": True}
    api = control()
    for case in ("valid", "missing", "invalid", "not-full", "wrong-date",
                 "wrong-identity"):
        report = deepcopy(source)
        proofs = report["collection"]["evidence"]
        if case == "missing":
            proofs.pop(path)
        elif case == "invalid":
            proofs[path] = None
        elif case == "not-full":
            proofs[path]["full_lookup"] = False
        elif case == "wrong-date":
            proofs[path]["data_date"] = "2026-11-21"
        elif case == "wrong-identity":
            proofs[path]["identity"] = "other-id"
        html = render(tmp_path, report, "dedup-proof-" + case)
        cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
        btc = next(card for card in cards if 'reader-name">BTC ' in card)
        expected = "查不到" if case == "valid" else "待核"
        for label in ("当前趋势", "全局强度", "同品种强度"):
            assert f"<dt>{label}</dt><dd>{expected}</dd>" in btc
        assert '<dd class="reader-event">待查</dd>' in btc
        assert "应建观察仓" not in btc
        if case == "valid":
            assert "STALE-PENDING" not in btc
        # One applicable proof can serve the eligible deduplicated card, but
        # cannot complete the separate unresolved direct lookup obligation.
        assert "数据收集中" in html and "数据已完备" not in html
        assert not api.assess(report)["complete"]
        if case != "valid":
            assert "查不到" not in btc
        else:
            # With the unresolved alias removed, the full negative is complete.
            report["long"][0]["direct"] = []
            report["long"][0]["coverage"]["instrument_ids"].pop()
            report["collection"]["evidence"]["/long/0/coverage"]["value"] = (
                deepcopy(report["long"][0]["coverage"]))
            report["collection"]["worklist"] = [
                item for item in report["collection"]["worklist"]
                if item["path"] != "/long/0/direct/0/lookup"]
            assert api.assess(report)["complete"]
            assert "数据已完备" in render(tmp_path, report, "full-negative-complete")


@pytest.mark.parametrize("case,scope,proof_scope,version,expected", [
    ("stale-version", "2026-11-22/v2", None, "v1", "待核"),
    ("canonical", "2026-11-22/v2", None, "v2", "查不到"),
    ("explicit-canonical", "2026-11-22/v2", "2026-11-22/v2", "v2", "查不到"),
    ("explicit-contradiction", "2026-11-22/v2", "2026-11-22/v1", "v2", "待核"),
    ("receipt-field-contradiction", "2026-11-22/v2", "2026-11-22/v2", "v1", "待核"),
    ("opaque-bound", "private-cutoff/catalog", "private-cutoff/catalog",
     "v2", "查不到"),
    ("opaque-unbound", "private-cutoff/catalog", None, "v2", "待核"),
    ("opaque-partial", "private-cutoff/catalog", "catalog", "v2", "待核"),
])
def test_negative_lookup_proof_must_cover_declared_scope(
    tmp_path, case, scope, proof_scope, version, expected
):
    from copy import deepcopy

    from test_hourly_collection import control

    api = control()
    for mode in ("pending-first", "negative-first", "single-negative"):
        report = complete_report()
        group = report["long"][0]
        negative = group["searchable"].pop(0)
        negative.update(lookup="miss", platform_match_verified=False,
                        lookup_scope=scope, trend="PRIVATE-STALE-QUOTE",
                        strength=98, local_strength=97,
                        signal_events=["开香槟"], watch_eligible=True)
        pending = {**negative, "lookup": "pending", "direct_verified": True}
        if mode == "single-negative":
            negative["direct_verified"] = True
            rows = [negative]
        else:
            rows = ([pending, negative] if mode == "pending-first"
                    else [negative, pending])
        group["direct"] = rows
        group["coverage"]["instrument_ids"] = ["btc"] * len(rows)
        manifest = report["collection"]
        old = "/long/0/searchable/0/"
        manifest["worklist"] = [item for item in manifest["worklist"]
                                if not item["path"].startswith(old)]
        manifest["evidence"] = {path: proof
                                for path, proof in manifest["evidence"].items()
                                if not path.startswith(old)}
        manifest["evidence"]["/long/0/coverage"]["value"] = deepcopy(group["coverage"])
        for index, row in enumerate(rows):
            path = f"/long/0/direct/{index}/lookup"
            manifest["worklist"].append({
                "path": path, "label": "BTC lookup", "market": "CRYPTO",
                "target_date": "2026-11-22"})
            if row["lookup"] != "miss":
                continue
            manifest["evidence"][path] = {
                "state": "verified", "data_date": "2026-11-22",
                "identity": "btc", "market": "CRYPTO", "instrument_type": "spot",
                "source": "PRIVATE-RECEIPT", "version": version,
                "value": "miss", "full_lookup": True}
            if proof_scope is not None:
                manifest["evidence"][path]["lookup_scope"] = proof_scope
        html = render(tmp_path, report, f"scope-proof-{case}-{mode}")
        row = next(row for row in re.findall(r"<details\b.*?</details>", html, re.S)
                   if 'reader-name">BTC ' in row)
        sections = re.split(
            r"<h3>(?:大类直属|趋势动物里面能够查到|相关的标的)</h3>", row
        )[1:]
        assert row.count('class="asset reader-card"') == 1
        assert 'class="asset reader-card"' in sections[0]
        for label in ("当前趋势", "全局强度", "同品种强度"):
            assert f"<dt>{label}</dt><dd>{expected}</dd>" in row
        assert '<dd class="reader-event">待查</dd>' in row
        for hidden in ("PRIVATE-", scope, "lookup_scope", "开香槟",
                       "98 / 100", "97 / 100", "应建观察仓", "趋势动物可查</span>"):
            assert hidden not in row
        complete = mode == "single-negative" and expected == "查不到"
        assert api.assess(report)["complete"] is complete
        assert ("数据已完备" in html) is complete
        assert ("数据收集中" in html) is not complete
        if expected == "待核":
            assert "查不到" not in html


def test_reconciled_lookup_conflict_prevents_completion(tmp_path):
    from copy import deepcopy

    from test_hourly_collection import control

    def with_alias(kind, foreign=False):
        report = complete_report()
        group = report["long"][0]
        found = group["searchable"][0]
        found["lookup_scope"] = "2026-11-22/v1"
        alias = deepcopy(found)
        if kind == "miss":
            alias.update(lookup="miss", platform_match_verified=False)
        if foreign:
            alias.update(market="US", instrument_type="ETF", data_date="2026-11-20",
                         lookup_scope="2026-11-20/v1")
        group["searchable"].append(alias)
        group["coverage"]["instrument_ids"].append("btc")
        collection = report["collection"]
        collection["evidence"]["/long/0/coverage"]["value"] = deepcopy(
            group["coverage"]
        )
        for field in (
            ("lookup",)
            if kind == "miss"
            else ("trend", "strength", "local_strength", "signal_events")
        ):
            path = "/long/0/searchable/1/" + field
            collection["worklist"].append(
                {
                    "path": path,
                    "label": "BTC " + field,
                    "market": alias["market"],
                    "target_date": alias["data_date"],
                }
            )
            collection["evidence"][path] = {
                "state": "verified",
                "data_date": alias["data_date"],
                "version": "v1",
                "identity": "btc",
                "market": alias["market"],
                "instrument_type": alias["instrument_type"],
                "source": "fixture-alias-result",
                "value": alias[field],
                **({"full_lookup": True} if field == "lookup" else {}),
            }
        return report

    api = control()
    report = with_alias("miss")
    html = render(tmp_path, report, "lookup-conflict")
    assert "数据收集中" in html and "数据已完备" not in html
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
    btc = next(card for card in cards if 'reader-name">BTC ' in card)
    assert "<dt>当前趋势</dt><dd>待核</dd>" in btc
    assert "查不到" not in btc and "应建观察仓" not in btc
    assert not api.assess(report)["complete"]
    state = tmp_path / "control.json"
    api.initialize(state, report)
    round_ = api.claim_round(state, report, "2026-11-23T08:30:00+08:00")
    assert round_["markets"] == ["CRYPTO"]
    assert "/long/0/searchable/1/lookup" in round_["pending_paths"]
    local, delivered = tmp_path / "local.html", tmp_path / "delivered.html"
    local.write_text(html, encoding="utf-8")
    delivered.write_text(html, encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        api.confirm_publication(state, report, local, delivered)
    assert api.claim_round(state, report, "2026-11-23T09:30:00+08:00")["markets"] == [
        "CRYPTO"
    ]

    # Compatible duplicates and a full miss in another actual market retain
    # completion. Same code/id alone cannot create a cross-market conflict.
    for kind, foreign in (("match", False), ("miss", True)):
        valid = with_alias(kind, foreign)
        assert api.assess(valid)["complete"]
        valid_html = render(tmp_path, valid, f"compatible-{kind}-{foreign}")
        assert "数据已完备" in valid_html
        assert ("查不到" in valid_html) is foreign


def test_null_strength_preserves_independent_watch_eligibility(tmp_path):
    from copy import deepcopy

    from test_hourly_collection import control

    def missing_report(field, mode, proof_mode):
        report = complete_report()
        group = report["long"][0]
        asset = group["searchable"][0]
        path = "/long/0/searchable/0/" + field
        if mode == "null":
            asset[field] = None
        else:
            asset.pop(field)
        if proof_mode == "null-proof":
            report["collection"]["evidence"][path]["value"] = None
        else:
            report["collection"]["evidence"].pop(path)
        comparator = {
            **deepcopy(asset),
            "code": "CMP",
            "name": "合成比较",
            "instrument_id": "cmp",
            "strength": "50",
            "local_strength": "30",
            "watch_eligible": False,
        }
        group["searchable"].append(comparator)
        group["coverage"]["instrument_ids"].append("cmp")
        report["collection"]["evidence"]["/long/0/coverage"]["value"] = deepcopy(
            group["coverage"]
        )
        for metric in ("trend", "signal_events", "strength", "local_strength"):
            proof_path = "/long/0/searchable/1/" + metric
            report["collection"]["worklist"].append(
                {
                    "path": proof_path,
                    "label": "CMP " + metric,
                    "market": "CRYPTO",
                    "target_date": "2026-11-22",
                }
            )
            report["collection"]["evidence"][proof_path] = {
                "state": "verified",
                "data_date": "2026-11-22",
                "version": "v1",
                "identity": "cmp",
                "market": "CRYPTO",
                "instrument_type": "spot",
                "source": "fixture-comparator",
                "value": comparator[metric],
            }
        return report

    api = control()
    for field, label, other_label, other_value in (
        ("strength", "全局强度", "同品种强度", "90"),
        ("local_strength", "同品种强度", "全局强度", "80"),
    ):
        for mode in ("null", "absent"):
            for proof_mode in ("no-proof", "null-proof"):
                report = missing_report(field, mode, proof_mode)
                html = render(tmp_path, report, f"missing-{field}-{mode}-{proof_mode}")
                row = re.search(r"<details\b.*?合成加密.*?</details>", html, re.S)[0]
                cards = re.findall(r'<li class="asset reader-card">.*?</li>', row, re.S)
                btc = next(card for card in cards if 'reader-name">BTC ' in card)
                assert "应建观察仓" in btc, (field, mode, proof_mode)
                assert f"<dt>{label}</dt><dd>待核</dd>" in btc
                assert f"<dt>{other_label}</dt><dd>{other_value} / 100</dd>" in btc
                assert re.findall(r'<h4 class="reader-name">(\S+)', row) == (
                    ["CMP", "BTC"] if field == "strength" else ["BTC", "CMP"]
                )
                assert "数据收集中" in html and not api.assess(report)["complete"]
    for bad in ("withdrawn", "contradiction", "stale-trend", "future-card", "lookup"):
        report = missing_report("strength", "null", "no-proof")
        asset = report["long"][0]["searchable"][0]
        evidence = report["collection"]["evidence"]
        if bad in ("withdrawn", "contradiction"):
            evidence["/long/0/searchable/0/strength"] = {
                **evidence["/long/0/searchable/0/local_strength"],
                "state": "withdrawn" if bad == "withdrawn" else "verified",
                "value": None if bad == "withdrawn" else "99",
            }
        elif bad == "stale-trend":
            evidence["/long/0/searchable/0/trend"]["data_date"] = "2026-11-21"
        elif bad == "future-card":
            asset["data_date"] = "2026-11-24"
        else:
            asset["lookup"] = "pending"
        html = render(tmp_path, report, "protected-" + bad)
        btc = next(
            card
            for card in re.findall(
                r'<li class="asset reader-card">.*?</li>', html, re.S
            )
            if 'reader-name">BTC ' in card
        )
        if bad == "withdrawn":
            # hourly-b06: a same-scope null strength withdrawal affects that
            # metric only; the independently verified watch is retained.
            assert "应建观察仓" in btc
        else:
            assert "应建观察仓" not in btc, bad


@pytest.mark.parametrize(
    "field", ["strength", "local_strength"], ids=["global", "local"]
)
def test_final_strength_conflict_blocks_collection_completion(tmp_path, field):
    from copy import deepcopy

    from test_hourly_collection import control

    def duplicated(agree):
        report = complete_report()
        group = report["long"][0]
        first = group["searchable"][0]
        scope = {"data_date": "2026-11-22", "version": "fixture-v1"}
        first["strength_scope"] = scope
        alias = deepcopy(first)
        if not agree:
            alias[field] = "81" if field == "strength" else "91"
        alias["strength_evidence"] = {
            metric: {**scope, "state": "verified"}
            for metric in ("strength", "local_strength")
        }
        group["searchable"].append(alias)
        group["coverage"]["instrument_ids"].append("btc")
        collection = report["collection"]
        collection["evidence"]["/long/0/coverage"]["value"] = deepcopy(
            group["coverage"]
        )
        for metric in ("trend", "signal_events", "strength", "local_strength"):
            path = "/long/0/searchable/1/" + metric
            collection["worklist"].append(
                {
                    "path": path,
                    "label": "BTC " + metric,
                    "market": "CRYPTO",
                    "target_date": "2026-11-22",
                }
            )
            collection["evidence"][path] = {
                "state": "verified",
                **scope,
                "identity": "btc",
                "market": "CRYPTO",
                "instrument_type": "spot",
                "source": "fixture-duplicate-strength",
                "value": alias[metric],
            }
        return report

    api = control()
    report = duplicated(False)
    html = render(tmp_path, report, "final-conflict-" + field)
    cards = re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
    btc = next(card for card in cards if 'reader-name">BTC ' in card)
    label, other_label, other_value = (
        ("全局强度", "同品种强度", "90")
        if field == "strength"
        else ("同品种强度", "全局强度", "80")
    )
    assert f"<dt>{label}</dt><dd>待核</dd>" in btc
    assert f"<dt>{other_label}</dt><dd>{other_value} / 100</dd>" in btc
    assert "数据收集中" in html and "数据已完备" not in html
    assert not api.assess(report)["complete"]
    state = tmp_path / "control.json"
    api.initialize(state, report)
    round_ = api.claim_round(state, report, "2026-11-23T08:30:00+08:00")
    assert round_["markets"] == ["CRYPTO"]
    assert "/long/0/searchable/0/" + field in round_["pending_paths"]
    assert all(path.endswith("/" + field) for path in round_["pending_paths"])
    local, delivered = tmp_path / "local.html", tmp_path / "delivered.html"
    local.write_text(html, encoding="utf-8")
    delivered.write_text(html, encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        api.confirm_publication(state, report, local, delivered)
    assert api.claim_round(state, report, "2026-11-23T09:30:00+08:00")["markets"] == [
        "CRYPTO"
    ]

    agreed = duplicated(True)
    assert api.assess(agreed)["complete"]
    final = render(tmp_path, agreed, "agreed-" + field)
    assert "数据已完备" in final
    local.write_text(final, encoding="utf-8")
    delivered.write_text(final, encoding="utf-8")
    api.confirm_publication(state, agreed, local, delivered)
    assert api.claim_round(state, agreed, "2026-11-23T10:30:00+08:00") is None

    # A current quote for a different explicit identity remains a separate card,
    # even with the same visible code and a different value of this metric.
    separate = duplicated(False)
    group = separate["long"][0]
    group["searchable"][1]["instrument_id"] = "other-btc"
    group["coverage"]["instrument_ids"][1] = "other-btc"
    collection = separate["collection"]
    collection["evidence"]["/long/0/coverage"]["value"] = deepcopy(group["coverage"])
    for metric in ("trend", "signal_events", "strength", "local_strength"):
        collection["evidence"]["/long/0/searchable/1/" + metric]["identity"] = (
            "other-btc"
        )
    assert api.assess(separate)["complete"]
    separate_html = render(tmp_path, separate, "separate-" + field)
    assert "数据已完备" in separate_html
    assert re.findall(r'<h4 class="reader-name">(\S+)', separate_html).count("BTC") == 2


@pytest.mark.parametrize(
    "card_day",
    [None, "bad-date", "2026-11-21", "2026-11-24"],
    ids=["unknown", "invalid", "stale", "future"],
)
def test_invalid_card_date_cannot_rank_current_fields(tmp_path, card_day):
    from copy import deepcopy

    from test_hourly_collection import control

    def with_rows(day, duplicate):
        report = complete_report()
        group = report["long"][0]
        asset = group["searchable"][0]
        asset["signal_events"] = ["开香槟"]
        report["collection"]["evidence"]["/long/0/searchable/0/signal_events"][
            "value"
        ] = ["开香槟"]
        asset["strength_scope"] = {"data_date": "2026-11-22", "version": "fixture-v1"}
        comparator = {
            **deepcopy(asset),
            "code": "CMP",
            "name": "合成比较",
            "instrument_id": "cmp",
            "strength": "50",
            "local_strength": "30",
            "signal_events": [],
            "watch_eligible": False,
        }
        extra = [comparator]
        if duplicate:
            donor = deepcopy(asset)
            donor["strength_evidence"] = {
                field: {
                    "state": "verified",
                    "data_date": "2026-11-22",
                    "version": "fixture-v1",
                }
                for field in ("strength", "local_strength")
            }
            extra.append(donor)
        asset["data_date"] = day
        for i, row in enumerate(extra, 1):
            group["searchable"].append(row)
            group["coverage"]["instrument_ids"].append(row["instrument_id"])
            for field in ("trend", "signal_events", "strength", "local_strength"):
                path = f"/long/0/searchable/{i}/" + field
                report["collection"]["worklist"].append(
                    {
                        "path": path,
                        "label": row["code"] + " " + field,
                        "market": "CRYPTO",
                        "target_date": "2026-11-22",
                    }
                )
                report["collection"]["evidence"][path] = {
                    "state": "verified",
                    "data_date": "2026-11-22",
                    "version": "fixture-v1",
                    "identity": row["instrument_id"],
                    "market": "CRYPTO",
                    "instrument_type": "spot",
                    "source": "fixture-current-card-fields",
                    "value": row[field],
                }
        report["collection"]["evidence"]["/long/0/coverage"]["value"] = deepcopy(
            group["coverage"]
        )
        return report

    api = control()
    for duplicate in (False, True):
        report = with_rows(card_day, duplicate)
        original = deepcopy(report)
        html = render(tmp_path, report, f"bad-card-{duplicate}")
        row = next(
            row
            for row in re.findall(r"<details\b.*?</details>", html, re.S)
            if "合成加密" in row
        )
        btc = next(
            card
            for card in re.findall(r'<li class="asset reader-card">.*?</li>', row, re.S)
            if 'reader-name">BTC ' in card
        )
        assert "<dt>全局强度</dt><dd>待核</dd>" in btc
        assert "<dt>同品种强度</dt><dd>待核</dd>" in btc
        assert "<dt>当前趋势</dt><dd>待核</dd>" in btc
        assert '<dd class="reader-event">待查</dd>' in btc
        assert all(
            value not in btc
            for value in ("80 / 100", "90 / 100", "开香槟", "应建观察仓")
        )
        assert re.findall(r'<h4 class="reader-name">(\S+)', row) == ["CMP", "BTC"]
        assert (
            "行情截至 " + (card_day if card_day and card_day != "bad-date" else "待核")
            in btc
        )
        if card_day == "2026-11-21":
            assert "历史数据" in btc
        assert "数据收集中" in html and not api.assess(report)["complete"]
        assert report == original  # original dates, values and proofs are retained
        state = tmp_path / f"state-{duplicate}.json"
        api.initialize(state, report)
        assert api.claim_round(state, report, "2026-11-23T08:30:00+08:00")[
            "markets"
        ] == ["CRYPTO"]
        local, delivered = tmp_path / "local.html", tmp_path / "delivered.html"
        local.write_text(html, encoding="utf-8")
        delivered.write_text(html, encoding="utf-8")
        with pytest.raises(ValueError, match="incomplete"):
            api.confirm_publication(state, report, local, delivered)
    current = with_rows("2026-11-22", True)
    html = render(tmp_path, current, "current-card")
    btc = next(
        card
        for card in re.findall(r'<li class="asset reader-card">.*?</li>', html, re.S)
        if 'reader-name">BTC ' in card
    )
    assert "80 / 100" in btc and "90 / 100" in btc
    assert "开香槟" in btc and "应建观察仓" in btc
    assert api.assess(current)["complete"] and "数据已完备" in html


@pytest.mark.parametrize(
    "field", ["strength", "local_strength"], ids=["global", "local"]
)
def test_withdrawn_strength_retains_independent_watch(tmp_path, field):
    from copy import deepcopy

    from test_hourly_collection import control

    def withdrawn_report(position):
        report = complete_report()
        group = report["long"][0]
        first = group["searchable"][0]
        first["signal_events"] = ["开香槟"]
        report["collection"]["evidence"]["/long/0/searchable/0/signal_events"][
            "value"
        ] = ["开香槟"]
        if position != "single":
            group["searchable"].append(deepcopy(first))
        index = 1 if position == "alias" else 0
        group["searchable"][index][field] = None
        comparator = {
            **deepcopy(first),
            "code": "CMP",
            "name": "合成比较",
            "instrument_id": "cmp",
            "strength": "50",
            "local_strength": "30",
            "watch_eligible": False,
            "signal_events": [],
        }
        group["searchable"].append(comparator)
        collection = report["collection"]
        group["coverage"]["instrument_ids"] = [
            row["instrument_id"] for row in group["searchable"]
        ]
        collection["evidence"]["/long/0/coverage"]["value"] = deepcopy(
            group["coverage"]
        )
        for i, row in enumerate(group["searchable"]):
            for metric in ("trend", "signal_events", "strength", "local_strength"):
                path = f"/long/0/searchable/{i}/" + metric
                if i:
                    collection["worklist"].append(
                        {
                            "path": path,
                            "label": row["code"] + " " + metric,
                            "market": "CRYPTO",
                            "target_date": "2026-11-22",
                        }
                    )
                collection["evidence"][path] = {
                    "state": "withdrawn"
                    if i == index and metric == field
                    else "verified",
                    "data_date": "2026-11-22",
                    "version": "fixture-v1",
                    "identity": row["instrument_id"],
                    "market": "CRYPTO",
                    "instrument_type": "spot",
                    "source": "fixture-field-withdrawal",
                    "value": row[metric],
                }
        return report

    api = control()
    label, other_label, other_value = (
        ("全局强度", "同品种强度", "90")
        if field == "strength"
        else ("同品种强度", "全局强度", "80")
    )
    for position in ("single", "selected", "alias"):
        report = withdrawn_report(position)
        html = render(tmp_path, report, "withdrawal-" + position)
        row = next(
            row
            for row in re.findall(r"<details\b.*?</details>", html, re.S)
            if "合成加密" in row
        )
        btc = next(
            card
            for card in re.findall(r'<li class="asset reader-card">.*?</li>', row, re.S)
            if 'reader-name">BTC ' in card
        )
        assert "应建观察仓" in btc, (field, position)
        assert "开香槟" in btc and "<dt>当前趋势</dt><dd>热</dd>" in btc
        assert f"<dt>{label}</dt><dd>待核</dd>" in btc
        assert f"<dt>{other_label}</dt><dd>{other_value} / 100</dd>" in btc
        assert re.findall(r'<h4 class="reader-name">(\S+)', row) == (
            ["CMP", "BTC"] if field == "strength" else ["BTC", "CMP"]
        )
        assert "数据收集中" in html and not api.assess(report)["complete"]
        state = tmp_path / f"withdrawn-{position}.json"
        api.initialize(state, report)
        assert api.claim_round(state, report, "2026-11-23T08:30:00+08:00")[
            "markets"
        ] == ["CRYPTO"]
        local, delivered = tmp_path / "local.html", tmp_path / "delivered.html"
        local.write_text(html, encoding="utf-8")
        delivered.write_text(html, encoding="utf-8")
        with pytest.raises(ValueError, match="incomplete"):
            api.confirm_publication(state, report, local, delivered)

        invalid_signal = deepcopy(report)
        invalid_signal["collection"]["evidence"]["/long/0/searchable/0/signal_events"][
            "state"
        ] = "UNKNOWN"
        invalid_html = render(tmp_path, invalid_signal, "invalid-signal-" + position)
        invalid_btc = next(
            card
            for card in re.findall(
                r'<li class="asset reader-card">.*?</li>', invalid_html, re.S
            )
            if 'reader-name">BTC ' in card
        )
        assert "应建观察仓" not in invalid_btc
        assert "开香槟" not in invalid_btc


@pytest.mark.parametrize(
    "field", ["strength", "local_strength"], ids=["global", "local"]
)
def test_three_source_withdrawal_cannot_revive_omitted_strength(tmp_path, field):
    from copy import deepcopy
    from itertools import permutations

    def report_with_sources(order, withdrawal_scope):
        report = complete_report()
        group = report["long"][0]
        preferred = group.pop("searchable")[0]
        preferred.update(
            direct_verified=True,
            signal_events=["开香槟"],
            strength_scope={"data_date": "2026-11-22", "version": "fixture-v1"},
        )
        donor = deepcopy(preferred)
        donor.update(direct_verified=False, watch_eligible=False)
        donor[field] = "80" if field == "strength" else "88"
        donor["strength_evidence"] = {
            field: {
                "state": "verified",
                "data_date": "2026-11-22",
                "version": "fixture-v1",
            }
        }
        withdrawn = deepcopy(donor)
        withdrawn[field] = None
        withdrawn["strength_evidence"] = {}
        withdrawal_version = (
            "fixture-v2" if withdrawal_scope == "version" else "fixture-v1"
        )
        if withdrawal_scope == "day":
            withdrawn["data_date"] = "2026-11-21"
        elif withdrawal_scope == "market":
            withdrawn["market"] = "CRYPTO-ALT"
            report["collection"]["targets"]["CRYPTO-ALT"] = "2026-11-22"
        elif withdrawal_scope == "identity":
            withdrawn["instrument_id"] = "other-btc"
        preferred.pop(field)
        comparator = {
            **deepcopy(donor),
            "code": "CMP",
            "name": "合成比较",
            "instrument_id": "cmp",
            "direct_verified": True,
            "strength": "50",
            "local_strength": "30",
            "signal_events": [],
        }
        group["direct"] = [preferred, comparator]
        group["searchable"] = [
            deepcopy(withdrawn if role == "withdrawn" else donor) for role in order
        ]
        group["coverage"]["instrument_ids"] = [
            asset["instrument_id"]
            for section in ("direct", "searchable")
            for asset in group[section]
        ]
        collection = report["collection"]
        collection["worklist"] = [
            item
            for item in collection["worklist"]
            if not item["path"].startswith("/long/0/searchable/")
        ]
        collection["evidence"] = {
            path: proof
            for path, proof in collection["evidence"].items()
            if not path.startswith("/long/0/searchable/")
        }
        collection["evidence"]["/long/0/coverage"]["value"] = deepcopy(
            group["coverage"]
        )
        for section in ("direct", "searchable"):
            for i, asset in enumerate(group[section]):
                role = order[i] if section == "searchable" else "preferred"
                for metric in ("trend", "signal_events", "strength", "local_strength"):
                    path = f"/long/0/{section}/{i}/" + metric
                    collection["worklist"].append(
                        {
                            "path": path,
                            "label": asset["code"] + " " + metric,
                            "market": asset["market"],
                            "target_date": collection["targets"][asset["market"]],
                        }
                    )
                    if metric not in asset:
                        continue  # the preferred row genuinely has no field scope
                    collection["evidence"][path] = {
                        "state": "withdrawn"
                        if role == "withdrawn" and metric == field
                        else "verified",
                        "data_date": asset["data_date"],
                        "version": withdrawal_version
                        if role == "withdrawn"
                        else "fixture-v1",
                        "identity": asset["instrument_id"],
                        "market": asset["market"],
                        "instrument_type": "spot",
                        "source": "fixture-" + role,
                        "value": asset[metric],
                    }
        return report

    examples = [
        (order, scope)
        for roles in (("donor", "withdrawn"), ("donor", "withdrawn", "extra-donor"))
        for order in permutations(roles)
        for scope in ("same", "version")
    ]
    examples += [
        (("donor", "withdrawn"), scope) for scope in ("day", "market", "identity")
    ]
    for index, (order, scope) in enumerate(examples):
        report = report_with_sources(order, scope)
        html = render(tmp_path, report, f"three-source-{index}")
        row = next(
            row
            for row in re.findall(r"<details\b.*?</details>", html, re.S)
            if "合成加密" in row
        )
        direct = row.split("<h3>大类直属</h3>")[1].split(
            "<h3>趋势动物里面能够查到</h3>"
        )[0]
        btc = next(
            card
            for card in re.findall(
                r'<li class="asset reader-card">.*?</li>', direct, re.S
            )
            if 'reader-name">BTC ' in card
        )
        label, other_label, other_value = (
            ("全局强度", "同品种强度", "90")
            if field == "strength"
            else ("同品种强度", "全局强度", "80")
        )
        value = (
            "待核"
            if scope == "same"
            else ("80 / 100" if field == "strength" else "88 / 100")
        )
        assert f"<dt>{label}</dt><dd>{value}</dd>" in btc, (field, order, scope)
        assert f"<dt>{other_label}</dt><dd>{other_value} / 100</dd>" in btc
        assert "应建观察仓" in btc and "开香槟" in btc
        assert re.findall(r'<h4 class="reader-name">(\S+)', direct) == (
            ["CMP", "BTC"]
            if field == "strength" and scope == "same"
            else ["BTC", "CMP"]
        )
        assert "数据收集中" in html


def freshness_report():
    """Hypothetical Monday: authenticated fixture facts, never a live report."""
    report = complete_report()
    report['collection']['target_evidence'] = {
        'CRYPTO': dict(state='verified', report_date='2026-11-23',
                       basis_date='2026-11-22', source='offline-calendar',
                       kind='continuous'),
        'US': dict(state='verified', report_date='2026-11-23',
                   basis_date='2026-11-22', source='offline-calendar', kind='equity',
                   sessions=['2026-11-20'], covered_through='2026-11-22'),
    }
    # The quote is current; the signal has not been checked.
    del report['collection']['evidence']['/long/0/searchable/0/signal_events']
    return report


def reader_cards(html):
    return {re.search(r'reader-name">(\S+)', card)[1]: card
            for card in re.findall(
                r'<li class="asset reader-card">.*?</li>', html, re.S)}


def test_card_freshness_before_and_after_crypto_release(tmp_path):
    for phase, day, css, label in (
        ('before', '2026-11-21', 'waiting', '待更新 · 等 2026-11-22'),
        ('after', '2026-11-22', 'fresh', '本期最新'),
    ):
        report = freshness_report()
        btc = report['long'][0]['searchable'][0]
        btc['data_date'] = day
        html = render(tmp_path, report, phase)
        cards = reader_cards(html)
        assert f'class="reader-surface {css}"' in cards['BTC']
        assert label in cards['BTC']
        assert f'行情截至 {day}' in cards['BTC']
        assert 'class="reader-surface fresh"' in cards['ETF']
        assert '行情截至 2026-11-20' in cards['ETF']
        assert '特殊信号' in cards['BTC'] and '待查' in cards['BTC']
        assert '数据收集中' in html and '数据已完备' not in html
        if phase == 'before':
            assert '80 / 100' not in cards['BTC']
            assert '应建观察仓' not in cards['BTC']


def test_card_with_unknown_or_future_date_is_yellow(tmp_path):
    for case, day in [('unknown', None), ('invalid', 'bad-date'),
                      ('future', '2026-11-24'), ('contradictory', '2026-11-22'),
                      ('no-calendar', '2026-11-22')]:
        report = freshness_report()
        asset = report['long'][0]['searchable'][0]
        asset['data_date'] = day
        if case == 'contradictory':
            report['collection']['target_evidence']['CRYPTO']['kind'] = 'equity'
        if case == 'no-calendar':
            del report['collection']['target_evidence']['CRYPTO']
        html = render(tmp_path, report, case)
        card = reader_cards(html)['BTC']
        assert 'class="reader-surface waiting"' in card
        assert '日期待核' in card and '本期最新' not in card
        assert '待更新 · 等' not in card  # targets appear only for known stale dates
        if case in ('unknown', 'invalid', 'future', 'contradictory'):
            assert all(v not in card for v in ('80 / 100', '90 / 100', '应建观察仓'))


def test_report_lists_pending_only_on_affected_cards(tmp_path):
    report = freshness_report()
    report['long'][0]['coverage_note'] = '成员范围待查'
    html = render(tmp_path, report, 'local-pending')
    header = html.split('<main')[0]
    assert '<h1>趋势动物观察报告<time' in html
    assert html.count('2026 年 11 月 23 日') == 1
    assert '下次核实：2026-11-23T10:30:00+08:00' in header
    assert all(word not in header for word in (
        'signal_events', '/long/', '缺少', '目标', '榜图日期'))
    assert '成员范围待查' in html
    cards = reader_cards(html)
    assert '<dd class="reader-event">待查</dd>' in cards['BTC']
    assert '<dd class="reader-event">无</dd>' in cards['ETF']
    assert cards['BTC'].count('行情截至') == 1
    assert '全局强度日期' not in html and '特殊信号日期' not in html
    complete = render(tmp_path, complete_report(), 'local-complete')
    assert '核实时间：2026-11-23T10:12:00+08:00' in complete
    assert '下次核实' not in complete


def test_conflicting_or_absent_signal_stays_pending(tmp_path):
    from copy import deepcopy

    cases = ('mismatch', 'missing', 'empty', 'identity', 'invalid-date',
             'future-date', 'lookup-conflict', 'duplicate')
    for case in cases:
        report = complete_report()
        asset = report['long'][0]['searchable'][0]
        proof = report['collection']['evidence'][
            '/long/0/searchable/0/signal_events']
        if case in ('mismatch', 'duplicate'):
            asset['signal_events'] = ['开香槟']
        elif case == 'missing':
            del asset['signal_events']
        elif case == 'identity':
            proof['identity'] = 'other'
        elif case in ('invalid-date', 'future-date'):
            asset['data_date'] = ('bad-date' if case == 'invalid-date'
                                  else '2026-11-24')
        elif case == 'lookup-conflict':
            asset['lookup'] = 'miss'
        if case == 'duplicate':
            donor = deepcopy(asset)
            donor['signal_events'] = []
            report['long'][0]['related'] = [donor]
            report['collection']['evidence'][
                '/long/0/related/0/signal_events'] = deepcopy(proof)
        original = deepcopy(report)
        html = render(tmp_path, report, 'signal-' + case)
        card = reader_cards(html)['BTC']
        expected = '无' if case == 'empty' else '待查'
        assert f'<dd class="reader-event">{expected}</dd>' in card, case
        assert '开香槟' not in card
        if case != 'empty':
            assert '应建观察仓' not in card
            assert '数据收集中' in html and '数据已完备' not in html
        if case in ('invalid-date', 'future-date'):
            assert '80 / 100' not in card and '90 / 100' not in card
        elif case in ('mismatch', 'missing', 'identity', 'duplicate'):
            assert '80 / 100' in card and '90 / 100' in card
        assert report == original


def test_stale_membership_evidence_shows_group_pending(tmp_path):
    from copy import deepcopy

    from test_hourly_collection import control

    for case in ('added', 'removed', 'missing-id', 'stale-group', 'unknown-target',
                 'future-target', 'calendar-conflict', 'matching', 'existing-hint'):
        report = complete_report()
        group = report['long'][0]
        note = ('成员范围待查。' if case == 'existing-hint'
                else '已核实部分成员，仍保留本类关联说明。')
        group['coverage_note'] = note
        if case in ('added', 'existing-hint'):
            member = deepcopy(group['searchable'][0])
            member.update(code='NEW', instrument_id='new-member')
            group['searchable'].append(member)
        elif case == 'removed':
            group['searchable'] = []
        elif case == 'missing-id':
            group['searchable'][0].pop('instrument_id')
        elif case == 'stale-group':
            group['data_date'] = '2026-11-21'
        elif case == 'unknown-target':
            report['collection']['targets']['BOARD'] = None
        elif case == 'future-target':
            report['collection']['targets']['BOARD'] = '2026-11-24'
            group['data_date'] = '2026-11-24'
            report['collection']['evidence']['/long/0/coverage'][
                'data_date'] = '2026-11-24'
        elif case == 'calendar-conflict':
            report['collection']['target_evidence'] = {
                'BOARD': dict(state='verified', report_date='2026-11-23',
                              basis_date='2026-11-22', kind='equity',
                              sessions=['2026-11-20'],
                              covered_through='2026-11-22', source='fixture-calendar')}
        original = deepcopy(report)
        html = render(tmp_path, report, 'members-' + case)
        row = next(row for row in re.findall(r'<details\b.*?</details>', html, re.S)
                   if '合成加密' in row)
        assert note in row
        if case == 'matching':
            assert '成员范围待查' not in row
            assert control().assess(report)['complete']
        else:
            assert row.count('成员范围待查') == 1, case
            assert not control().assess(report)['complete']
            assert '数据收集中' in html
        header = html.split('<main')[0]
        assert all(raw not in header for raw in (
            '/long/', 'coverage', '成分及查询覆盖', '目标', '实际'))
        assert report == original


def test_verified_paper_status_is_current_and_redacted(tmp_path):
    report = {
        "date": "2040-01-01",
        "paper_entry": {
            "schema": "trend-v3-paper-entry.v1", "date": "2040-01-01",
            "report_version": "v02",
            "results": [
                {"code": "US.ABC", "state": "active", "filled": 4, "remaining": 6,
                 "active": True, "account": "private-id",
                 "order": "private-order"},
                {"code": "US.DEF", "state": "unknown", "active": None,
                 "reason": "broker_receipt_unverified"},
                {"code": "<script>alert(1)</script>", "state": "filled"},
            ],
            "skipped": [{"code": "BTCUSD", "reason": "market_order_unsupported"}],
        },
    }
    html = render(tmp_path, report, "paper-current", report_revision="v02")
    simulation = html.split('id="simulation"', 1)[1].split("</section>", 1)[0]
    assert "US.ABC · 已提交 · 已成交 4 · 剩余 6 · 有活动委托" in simulation
    assert "US.DEF · 回执待核 · 未核实券商回执 · 活动委托待核" in simulation
    assert "BTCUSD · 已跳过 · 仅限价或市价不可用" in simulation
    hidden = ("private-id", "private-order", "<script>")
    assert all(value not in html for value in hidden)
    assert html.count("未接入") == 3

    report["paper_entry"]["date"] = "2039-12-31"
    assert_formal_records(
        render(tmp_path, report, "paper-stale", report_revision="v02")
    )


def test_simulation_requires_matching_current_report_revision(tmp_path):
    from test_hourly_collection import control

    report = complete_report()
    report["paper_entry"] = {
        "schema": "trend-v3-paper-entry.v1",
        "date": report["date"],
        "report_version": "v01",
        "results": [
            {"code": "US.ABC", "state": "pending", "order": "private-order"},
        ],
        "skipped": [],
    }
    for name, revision in (
        ("missing", None), ("mismatched", "v02"), ("unknown", "UNKNOWN"),
    ):
        html = render(tmp_path, report, name, report_revision=revision)
        assert_formal_records(html)
        assert "US.ABC" not in html

    report["paper_entry"]["report_version"] = "v02"
    html = render(tmp_path, report, "matching", report_revision="v02")
    simulation = html.split('id="simulation"', 1)[1].split("</section>", 1)[0]
    assert "US.ABC · 待执行" in simulation
    assert "未接入" not in simulation
    assert "private-order" not in html
    assert control().render_html(report, report_revision="v02") == html
    state = tmp_path / "state.json"
    control().initialize(state, report)
    delivered = tmp_path / "delivered.html"
    delivered.write_text(html, encoding="utf-8")
    confirm = [
        sys.executable, str(SKILL / "collection.py"), "confirm",
        str(tmp_path / "matching.json"), "--state", str(state),
        "--local", str(tmp_path / "matching.html"), "--delivered", str(delivered),
    ]
    mismatch = subprocess.run(confirm, capture_output=True, text=True, timeout=10)
    assert mismatch.returncode == 2
    assert "publication readback mismatch" in mismatch.stderr
    verified = subprocess.run(
        confirm + ["--report-revision", "v02"],
        capture_output=True, text=True, timeout=10,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == {"publication_verified": True}

    report["paper_entry"]["report_version"] = "UNKNOWN"
    assert_formal_records(
        render(tmp_path, report, "unknown-both", report_revision="UNKNOWN")
    )


def test_signal_board_hit_renders_direct_without_watch_upgrade(tmp_path):
    from test_signal_board_search import fixture

    payload = fixture()
    payload["board"]["groups"] = [dict(name="弱类", strength=20)]
    payload["group_markets"][0]["group"] = "弱类"
    payload["memberships"] = [dict(payload["memberships"][0], group="弱类")]
    signal = payload["boards"][0]
    signal.update(
        board_id="cold-us", title="美股凉转寒", direction="short", label="凉转寒"
    )
    signal["members"][0].update(labels=["凉转寒"], name="合成一号")
    signal["members"].append(
        dict(
            signal["members"][0],
            instrument_id="unknown",
            code="UNKNOWN-MEMBER",
            name="无归属",
        )
    )
    data = {
        "date": "2026-11-25",
        "board_date": "2026-11-24",
        "board_version": "2026-11-25",
        "cutoff": payload["cutoff"],
        "signal_search": payload,
        "short": [
            dict(
                seat=1,
                name="弱类",
                board_strength="20",
                direct=[
                    dict(
                        instrument_id="p1",
                        code="ONE",
                        market="US",
                        instrument_type="stock",
                        name="合成一号",
                        data_date="2026-11-24",
                        trend="冻",
                        direct_verified=True,
                        signal_events=["凉转寒"],
                    )
                ],
            )
        ],
    }
    html = render(tmp_path, data, "signal-hit")
    assert "美股凉转寒 · 凉转寒 · 数据日 2026-11-24" in html
    assert html.count("合成一号") == 1
    assert '<span class="reader-marker">直属</span>' in html
    assert "应建观察仓" not in html and "UNKNOWN-MEMBER" not in html
    assert "冻" in html

    data["board_version"] = "other-edition"
    html = render(tmp_path, data, "signal-wrong-edition")
    assert "信号榜：" not in html

    data["board_version"] = "2026-11-25"
    from copy import deepcopy

    for change in ["stale-card", "lookup-conflict", "market-target"]:
        revised = deepcopy(data)
        asset = revised["short"][0]["direct"][0]
        if change == "stale-card":
            asset["data_date"] = "2026-11-20"
        elif change == "lookup-conflict":
            asset.update(platform_match_verified=True)
            revised["short"][0]["searchable"] = [
                dict(asset, lookup="miss", platform_match_verified=False)
            ]
        else:
            revised["signal_search"]["targets"] = {"US": "2026-11-20"}
        html = render(tmp_path, revised, "signal-" + change)
        assert "信号榜：" not in html, change


def test_verified_own_event_without_previous(tmp_path):
    """Normal CLI consumes hashed own events without inferring prior temperature."""
    from copy import deepcopy

    base = freshness_report()
    asset = base["long"][0]["searchable"][0]
    asset.update(data_date="2026-11-22", signal_events=[], watch_eligible=False)
    base["board_version"] = "2026-11-23"
    prefix = "/long/0/searchable/0/"
    source = "_evidence/2026-11-23/v01/visual-observations.json#stock_details[0]"
    base["collection"]["evidence"][prefix + "trend"].update(
        source=source, version="2026-11-23"
    )
    for case in (
        "current",
        "identity",
        "market",
        "day",
        "version",
        "product",
        "digest",
        "board-only",
        "unrelated-proof",
        "conflict",
        "escape",
    ):
        root = tmp_path / case / "v01"
        root.mkdir(parents=True)
        report = deepcopy(base)
        own = dict(
            identity="btc",
            code="BTC",
            name="合成比特币",
            market="CRYPTO",
            instrument_type="spot",
            data_date="2026-11-22",
            temperature="热",
            previous_temperature=None,
            labels=["温转热", "市值龙头"],
        )
        if case in ("identity", "market"):
            own[case] = "other"
        if case == "day":
            own["data_date"] = "2026-11-20"
        if case == "product":
            own["instrument_type"] = "other"
        if case == "board-only":
            own["labels"] = ["市值龙头"]
        raw = json.dumps(dict(stock_details=[own]), ensure_ascii=False)
        (root / "visual-observations.json").write_text(raw)
        draft = dict(
            date=base["date"],
            board=dict(version="2026-11-23"),
            sources=dict(visual=source.split("#")[0]),
            source_sha256=dict(visual=hashlib.sha256(raw.encode()).hexdigest()),
            field_records=dict(
                R1=dict(source=source, version="2026-11-23", record=own)
            ),
            instruments=[
                dict(
                    id="btc",
                    code="BTC",
                    market="CRYPTO",
                    instrument_type="spot",
                    data_date="2026-11-22",
                    field_refs=dict(labels="R1"),
                )
            ],
        )
        if case == "digest":
            draft["source_sha256"]["visual"] = "0" * 64
        if case == "version":
            draft["field_records"]["R1"]["version"] = "old"
        if case == "escape":
            draft["sources"]["visual"] = "../../visual-observations.json"
        if case == "unrelated-proof":
            report["collection"]["evidence"][prefix + "signal_events"] = dict(
                report["collection"]["evidence"][prefix + "trend"],
                identity="other",
                value=[],
            )
        if case == "conflict":
            report["collection"]["evidence"][prefix + "signal_events"] = dict(
                report["collection"]["evidence"][prefix + "trend"], value=[]
            )
        (root / "selection-draft.json").write_text(json.dumps(draft))
        (root / "reader-projection.json").write_text(json.dumps(report))
        run = subprocess.run(
            [
                sys.executable,
                str(RENDERER),
                str(root / "reader-projection.json"),
                str(root / "out.html"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if case in ("conflict", "escape", "digest"):
            assert run.returncode == 2 and "own-event" in run.stderr
            continue
        assert run.returncode == 0, run.stderr
        card = reader_cards((root / "out.html").read_text())["BTC"]
        expected = "温转热" if case in ("current", "unrelated-proof") else "待查"
        assert f'<dd class="reader-event">{expected}</dd>' in card
        assert "应建观察仓" not in card

        if case == "current":
            assessed = subprocess.run(
                [
                    sys.executable,
                    str(SKILL / "collection.py"),
                    "assess",
                    str(root / "reader-projection.json"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert assessed.returncode == 0, assessed.stderr
            pending = json.loads(assessed.stdout)["pending"]
            assert not any("BTC" in p and "特殊信号" in p for p in pending)

            import shutil

            revision = root.parent / "v02"
            revision.mkdir()
            for original in root.glob("*.json"):
                shutil.copyfile(original, revision / original.name)
            replay = subprocess.run(
                [
                    sys.executable,
                    str(RENDERER),
                    str(revision / "reader-projection.json"),
                    str(revision / "compat.html"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert replay.returncode == 0, replay.stderr
            assert (
                '<dd class="reader-event">温转热</dd>'
                in reader_cards((revision / "compat.html").read_text())["BTC"]
            )

            namesake_report = deepcopy(report)
            namesake = dict(
                asset, instrument_id="namesake", market="US", data_date="2026-11-20"
            )
            namesake_report["long"][0]["related"] = [namesake]
            (root / "reader-projection.json").write_text(json.dumps(namesake_report))
            scoped = subprocess.run(
                [
                    sys.executable,
                    str(RENDERER),
                    str(root / "reader-projection.json"),
                    str(root / "namesake.html"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert scoped.returncode == 0, scoped.stderr
            cells = re.findall(
                r'<dd class="reader-event">(.*?)</dd>',
                (root / "namesake.html").read_text(),
            )
            assert "温转热" in cells and "待查" in cells
