# Chinese Premarket Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `reports/premarket/<YYYY-MM-DD>.md` fully Chinese, easier to scan, and explicit about why each symbol is important.

**Architecture:** Keep `PremarketAction` and CSV output unchanged. Localize only Markdown rendering in `src/open_trader/advice/report.py` and update the classifier prompt so future free-text fields are Chinese. Tests lock the report structure, enum localization, empty states, and prompt requirement.

**Tech Stack:** Python 3.12, pytest, Markdown text generation.

---

## File Structure

- Modify `src/open_trader/advice/report.py`: localize Markdown report output and add small formatting helpers.
- Modify `tests/test_premarket_report.py`: assert Chinese report structure and empty states.
- Modify `src/open_trader/advice/prompts/change_classifier.md`: require Chinese free-text output.
- Add or modify tests in `tests/test_premarket_report.py` to verify prompt text contains the Chinese-output requirement.

## Task 1: Localize Premarket Markdown Report

**Files:**
- Modify: `tests/test_premarket_report.py`
- Modify: `src/open_trader/advice/report.py`

- [ ] **Step 1: Write failing report structure assertions**

Update `test_write_premarket_outputs_writes_actions_csv_and_markdown()` to assert:

```python
assert "# 开盘前交易简报 - 2026-06-16" in markdown
assert "## 今日需要关注" in markdown
assert "| 标的 | 重要性 | 当前仓位 | 建议动作 |" in markdown
assert "| AAPL | 高 | 5.10% | 减仓 |" in markdown
assert "| MSFT | 中 | 7.00% | 减仓 |" in markdown
assert "## 详细说明" in markdown
assert "| 变化类型 | 建议动作变化 |" in markdown
assert "**为什么重要：** AAPL 今日建议相对上次发生变化，需要优先确认。" in markdown
assert "**摘要：** 建议开盘前重点复核 AAPL 的仓位和风险。" in markdown
assert "**观察条件：** 若开盘后触发计划价位，应优先处理。" in markdown
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_report.py::test_write_premarket_outputs_writes_actions_csv_and_markdown -v
```

Expected: FAIL because the report still uses English headings and raw enum values.

- [ ] **Step 3: Implement Chinese report rendering**

In `src/open_trader/advice/report.py`:

- Change title to `# 开盘前交易简报 - <run_date>`.
- Add summary table under `## 今日需要关注`.
- Add detail sections under `## 详细说明`.
- Add helper functions:
  - `_severity_text(value: str) -> str`
  - `_change_type_text(value: str) -> str`
  - `_suggested_action_text(value: str) -> str`
  - `_escape_table_cell(value: str) -> str`
- Use `rationale` for `**为什么重要：**`.

- [ ] **Step 4: Run report test to verify it passes**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_report.py::test_write_premarket_outputs_writes_actions_csv_and_markdown -v
```

Expected: PASS.

## Task 2: Localize Empty States

**Files:**
- Modify: `tests/test_premarket_report.py`
- Modify: `src/open_trader/advice/report.py`

- [ ] **Step 1: Write failing empty-state assertions**

Update `test_write_premarket_outputs_handles_no_actions()` to assert:

```python
assert "# 开盘前交易简报 - 2026-06-16" in markdown
assert "今日没有需要特别关注的交易建议变化。" in markdown
assert "No material trading advice changes" not in markdown
```

Add a new test:

```python
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
```

- [ ] **Step 2: Run empty-state tests to verify they fail**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_report.py::test_write_premarket_outputs_handles_no_actions tests/test_premarket_report.py::test_write_premarket_outputs_handles_no_eligible_symbols -v
```

Expected: FAIL because empty states still use English.

- [ ] **Step 3: Implement Chinese empty states**

In `_render_markdown()`:

- For `no_eligible=True`, output `没有找到符合条件的美股或 ETF 标的。`
- For empty actions, output `今日没有需要特别关注的交易建议变化。`

- [ ] **Step 4: Run all premarket report tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_report.py -v
```

Expected: PASS.

## Task 3: Require Chinese Classifier Free Text

**Files:**
- Modify: `tests/test_premarket_report.py`
- Modify: `src/open_trader/advice/prompts/change_classifier.md`

- [ ] **Step 1: Write failing prompt test**

Add:

```python
def test_change_classifier_prompt_requires_chinese_output() -> None:
    prompt = (
        Path(__file__).resolve().parents[1]
        / "src/open_trader/advice/prompts/change_classifier.md"
    ).read_text(encoding="utf-8")

    assert "suggested_action、summary、rationale、watch_trigger 必须使用中文" in prompt
    assert "不要在报告字段中混用英文枚举值" in prompt
```

- [ ] **Step 2: Run prompt test to verify it fails**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_report.py::test_change_classifier_prompt_requires_chinese_output -v
```

Expected: FAIL because the prompt does not yet require Chinese free-text output.

- [ ] **Step 3: Update classifier prompt**

In `src/open_trader/advice/prompts/change_classifier.md`, add a concise Chinese-output requirement after the JSON key list:

```markdown
For report readability, `suggested_action`, `summary`, `rationale`, and
`watch_trigger` must be written in Chinese. Do not mix English enum values into
report-facing fields. Keep enum fields (`change_type`, `severity`) in the
required machine-readable English values.

报告可读性要求：suggested_action、summary、rationale、watch_trigger 必须使用中文。
不要在报告字段中混用英文枚举值；change_type 和 severity 仍使用 schema 要求的英文枚举。
```

- [ ] **Step 4: Run prompt test**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_report.py::test_change_classifier_prompt_requires_chinese_output -v
```

Expected: PASS.

## Task 4: Verification And Commit

**Files:**
- All touched files.

- [ ] **Step 1: Run targeted premarket tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_premarket_report.py tests/test_premarket_pipeline.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest
```

Expected: PASS.

- [ ] **Step 3: Commit implementation**

Run:

```bash
git add src/open_trader/advice/report.py src/open_trader/advice/prompts/change_classifier.md tests/test_premarket_report.py docs/superpowers/plans/2026-06-17-chinese-premarket-report.md
git commit -m "feat: localize premarket report"
```
