# Futu Skill Facts News Sentiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first Futu Skills facts layer slice and generate a Futu-backed `news_sentiment` artifact without changing trade action generation.

**Architecture:** Add a focused `open_trader.futu_skill_facts` module that owns schema validation, artifact paths, indexing, and a pluggable extractor protocol. Add one CLI command, `extract-futu-skill-facts`, that reads portfolio symbols and writes dated/latest JSON facts for the `news_sentiment` module. Dashboard integration reads the artifact and lets the existing `News / Sentiment` card prefer the Futu-backed module when present.

**Tech Stack:** Python 3.12, argparse, JSON artifacts, pytest, existing dashboard static JavaScript.

---

### Task 1: Core Facts Contract

**Files:**
- Create: `src/open_trader/futu_skill_facts.py`
- Test: `tests/test_futu_skill_facts.py`

- [x] **Step 1: Write failing tests for paths, validation, generation, and indexing**

Add tests that import `FUTU_SKILL_FACTS_SCHEMA_VERSION`, `FutuSkillNewsSentimentExtractor`, `generate_futu_skill_facts`, `futu_skill_facts_run_path`, `futu_skill_facts_latest_path`, `index_futu_skill_facts_by_market_symbol`, `load_futu_skill_facts_cache`, and `validate_futu_skill_fact_record`.

Test behaviors:

```python
def test_generate_futu_skill_facts_writes_news_sentiment_artifact(tmp_path: Path) -> None:
    portfolio = tmp_path / "data/latest/portfolio.csv"
    write_portfolio(portfolio, [{"market": "US", "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock"}])
    extractor = FakeExtractor()

    result = generate_futu_skill_facts(
        portfolio_path=portfolio,
        data_dir=tmp_path / "data",
        run_date="2026-07-01",
        market="US",
        extractor=extractor,
        update_latest=True,
    )

    payload = json.loads(result.run_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == FUTU_SKILL_FACTS_SCHEMA_VERSION
    assert payload["records"][0]["news_sentiment"]["signal"] == "supportive"
    assert result.records == 1
    assert result.generated == 1
    assert result.failed == 0
    assert result.latest_path.read_text(encoding="utf-8") == result.run_path.read_text(encoding="utf-8")
```

Also test that invalid module statuses raise `ValueError`, missing symbols are skipped, and `index_futu_skill_facts_by_market_symbol()` returns `{("US", "NVDA"): record}`.

- [x] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py -v
```

Expected: FAIL because `open_trader.futu_skill_facts` does not exist.

- [x] **Step 3: Implement the minimal module**

Create `src/open_trader/futu_skill_facts.py` with:

- `FUTU_SKILL_FACTS_SCHEMA_VERSION = "open_trader.futu_skill_facts.v1"`
- enums for `status`, `signal`, `confidence`, and `suggested_constraint`
- `FutuSkillFactResult`
- `FutuSkillNewsSentimentExtractor` protocol
- path helpers mirroring existing market-scoped artifacts
- portfolio row loading that skips cash rows and rows without market/symbol
- record validation
- `generate_futu_skill_facts()` with atomic JSON writes

- [x] **Step 4: Run tests and verify they pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py -v
```

Expected: PASS.

### Task 2: CLI Wiring

**Files:**
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_premarket_cli.py`

- [x] **Step 1: Write failing CLI tests**

Add tests like the existing `extract-decision-facts` tests:

```python
def test_extract_futu_skill_facts_help_includes_expected_options() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "open_trader", "extract-futu-skill-facts", "--help"],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--portfolio" in result.stdout
    assert "--market" in result.stdout
    assert "--update-latest" in result.stdout
```

and a monkeypatched wiring test that asserts `generate_futu_skill_facts()` receives `portfolio_path`, `data_dir`, `run_date`, `market`, `extractor`, and `update_latest`.

- [x] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_premarket_cli.py::test_extract_futu_skill_facts_help_includes_expected_options tests/test_premarket_cli.py::test_extract_futu_skill_facts_main_wires_generator -v
```

Expected: FAIL because the command is not registered.

- [x] **Step 3: Add CLI command**

Import `LLMFutuNewsSentimentExtractor` and `generate_futu_skill_facts`. Add parser `extract-futu-skill-facts` with `--portfolio`, `--data-dir`, `--date`, `--market`, and `--update-latest`. In `main()`, instantiate the extractor, call the generator, and print `run_date`, `futu_skill_facts`, `generated`, `failed`, `futu_skill_facts_json`, and `latest`.

- [x] **Step 4: Run CLI tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_premarket_cli.py::test_extract_futu_skill_facts_help_includes_expected_options tests/test_premarket_cli.py::test_extract_futu_skill_facts_main_wires_generator -v
```

Expected: PASS.

### Task 3: Dashboard Payload Integration

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

- [x] **Step 1: Write failing dashboard payload test**

Add a test that writes `data/latest/US/futu_skill_facts.json`, loads the dashboard state, and asserts:

```python
holding = state["holdings"][0]
assert holding["futu_skill_facts"]["news_sentiment"]["available"] is True
assert holding["futu_skill_facts"]["news_sentiment"]["signal"] == "supportive"
```

- [x] **Step 2: Write failing frontend rendering test**

Extend the static asset tests to assert that JavaScript references `futu_skill_facts` and renders source evidence for the News / Sentiment card.

- [x] **Step 3: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py::test_load_dashboard_state_attaches_futu_skill_facts tests/test_dashboard_web.py::test_dashboard_static_assets_include_futu_skill_facts -v
```

Expected: FAIL because dashboard does not load or render Futu skill facts.

- [x] **Step 4: Implement dashboard loader and frontend preference**

Load `futu_skill_facts.json` with scoped/unscoped precedence like `decision_facts.json`. Attach `holding["futu_skill_facts"]`. Update the `News / Sentiment` card so it uses Futu skill facts when available, otherwise falls back to existing `decision_facts.news_sentiment`.

- [x] **Step 5: Run dashboard tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py::test_load_dashboard_state_attaches_futu_skill_facts tests/test_dashboard_web.py::test_dashboard_static_assets_include_futu_skill_facts -v
```

Expected: PASS.

### Task 4: Focused Regression

**Files:**
- Test existing touched files.

- [x] **Step 1: Run focused tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py tests/test_premarket_cli.py tests/test_dashboard.py tests/test_dashboard_web.py -v
```

Expected: PASS.

- [x] **Step 2: Commit**

Run:

```bash
git add src/open_trader/futu_skill_facts.py src/open_trader/cli.py src/open_trader/dashboard.py src/open_trader/dashboard_static/dashboard.js tests/test_futu_skill_facts.py tests/test_premarket_cli.py tests/test_dashboard.py tests/test_dashboard_web.py docs/superpowers/plans/2026-07-01-futu-skill-facts-news-sentiment.md
git commit -m "feat: add futu skill facts news sentiment layer"
```
