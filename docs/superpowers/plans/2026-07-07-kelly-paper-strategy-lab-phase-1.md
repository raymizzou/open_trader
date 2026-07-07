# Kelly Paper Strategy Lab Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only Kelly strategy experiment foundation: local artifacts, dashboard experiment display, holdings-table `凯利` detail entry, and pytest + Playwright verification.

**Architecture:** Phase 1 is fixture/artifact driven and does not connect to Futu trading APIs. A focused `kelly_lab` module loads and validates strategy templates and locked experiments from `data/latest/*.json`; `dashboard.py` attaches Kelly lab state to the existing dashboard payload; `dashboard.js` renders a read-only strategy lab panel and a `kelly` detail mode beside `交易决策` and `做T`.

**Tech Stack:** Python 3.12, dataclasses/JSON artifacts, existing `ThreadingHTTPServer` dashboard, pytest, existing Node-based dashboard JS tests, Playwright Chromium for end-to-end UI verification.

---

## Files

- Create: `src/open_trader/kelly_lab.py`  
  Loads strategy templates and experiments, validates locked experiment shape, indexes experiments by market/symbol, and returns JSON-safe dashboard details.

- Modify: `src/open_trader/dashboard.py`  
  Loads Kelly lab artifacts and attaches `kelly_lab` top-level dashboard state plus per-holding `kelly` detail data.

- Modify: `src/open_trader/dashboard_static/index.html`  
  Adds a read-only `kelly-lab-panel` section above the holdings panel.

- Modify: `src/open_trader/dashboard_static/dashboard.js`  
  Adds `kelly` detail mode, renders the experiment panel, renders the per-holding Kelly detail row, and keeps existing `decision` / `t_signal` modes intact.

- Modify: `src/open_trader/dashboard_static/dashboard.css`  
  Adds compact styles for the Kelly lab panel, experiment cards, and Kelly detail section using the existing dashboard visual language.

- Create: `tests/test_kelly_lab.py`  
  Unit tests for artifact loading, validation, lock semantics, participant indexing, and missing-artifact fallback.

- Modify: `tests/test_dashboard.py`  
  Verifies `load_dashboard_state()` exposes Kelly lab data and per-holding Kelly experiment associations.

- Modify: `tests/test_dashboard_web.py`  
  Adds Node-level checks that `renderHoldings()` shows the `凯利` button and opens Kelly detail mode.

- Create: `package.json`  
  Adds Playwright scripts without changing Python packaging.

- Create: `playwright.config.ts`  
  Defines a fixture web server and Chromium-only Playwright project.

- Create: `tests/e2e/serve_dashboard_fixture.py`  
  Serves dashboard static files and fixture `/api/dashboard` / `/api/quotes` responses for browser tests.

- Create: `tests/e2e/fixtures/kelly-dashboard.json`  
  Dashboard payload fixture with one running Kelly experiment and two holdings.

- Create: `tests/e2e/kelly-lab.spec.ts`  
  Browser test for strategy lab rendering and holdings-row `凯利` detail entry.

---

### Task 1: Kelly Lab Artifact Loader

**Files:**
- Create: `src/open_trader/kelly_lab.py`
- Create: `tests/test_kelly_lab.py`

- [ ] **Step 1: Write failing tests for valid artifacts and missing fallback**

Add `tests/test_kelly_lab.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from open_trader.kelly_lab import (
    index_kelly_experiments_by_market_symbol,
    load_kelly_lab_state,
)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_kelly_lab_state_returns_locked_experiments(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "trend_pullback_20d",
                    "strategy_name": "趋势回调 20D",
                    "strategy_version": "v1",
                    "entry_rule_description": "价格回调到 20 日均线附近。",
                    "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                    "max_holding_days": 20,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "trend_pullback_20d_exp_20260707",
                    "experiment_name": "趋势回调 20D 第一批",
                    "strategy_id": "trend_pullback_20d",
                    "strategy_version": "v1",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "100000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "50",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "US",
                            "symbol": "AAPL",
                            "name": "Apple Inc.",
                            "source": "holding+watchlist",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        },
                        {
                            "market": "HK",
                            "symbol": "00700",
                            "name": "腾讯控股",
                            "source": "watchlist",
                            "locked": True,
                            "per_symbol_budget": "25000",
                            "budget_currency": "USD",
                        },
                    ],
                    "stats": {
                        "completed_samples": 0,
                        "open_samples": 0,
                        "observed_win_rate": "",
                        "sample_stage": "insufficient",
                    },
                }
            ],
        },
    )

    state = load_kelly_lab_state(data_dir).to_dict()

    assert state["available"] is True
    assert state["template_count"] == 1
    assert state["experiment_count"] == 1
    experiment = state["experiments"][0]
    assert experiment["experiment_id"] == "trend_pullback_20d_exp_20260707"
    assert experiment["locked"] is True
    assert experiment["template"]["strategy_name"] == "趋势回调 20D"
    assert experiment["participants"][0]["symbol"] == "AAPL"
    assert experiment["stats"]["sample_stage"] == "insufficient"


def test_load_kelly_lab_state_missing_files_is_unavailable(tmp_path: Path) -> None:
    state = load_kelly_lab_state(tmp_path / "data").to_dict()

    assert state["available"] is False
    assert state["template_count"] == 0
    assert state["experiment_count"] == 0
    assert state["experiments"] == []
    assert "kelly_strategy_templates.json not found" in state["error"]


def test_index_kelly_experiments_by_market_symbol(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_json(
        data_dir / "latest" / "kelly_strategy_templates.json",
        {
            "schema_version": "open_trader.kelly_strategy_templates.v1",
            "templates": [
                {
                    "strategy_id": "breakout_10d",
                    "strategy_name": "突破 10D",
                    "strategy_version": "v1",
                    "entry_rule_description": "突破区间。",
                    "exit_rule_description": "跌回突破位或 10 天到期。",
                    "max_holding_days": 10,
                    "order_type": "limit",
                    "market_session": "regular",
                }
            ],
        },
    )
    write_json(
        data_dir / "latest" / "kelly_experiments.json",
        {
            "schema_version": "open_trader.kelly_experiments.v1",
            "experiments": [
                {
                    "experiment_id": "breakout_10d_exp_20260707",
                    "experiment_name": "突破 10D 第一批",
                    "strategy_id": "breakout_10d",
                    "strategy_version": "v1",
                    "start_date": "2026-07-07",
                    "paper_account": "futu_simulate",
                    "experiment_budget": "50000",
                    "budget_currency": "USD",
                    "capital_utilization_pct": "40",
                    "allocation_mode": "equal_weight",
                    "max_open_position_per_symbol": 1,
                    "status": "running",
                    "locked": True,
                    "participants": [
                        {
                            "market": "US",
                            "symbol": "MSFT",
                            "name": "Microsoft",
                            "source": "holding",
                            "locked": True,
                            "per_symbol_budget": "20000",
                            "budget_currency": "USD",
                        }
                    ],
                    "stats": {
                        "completed_samples": 12,
                        "open_samples": 1,
                        "observed_win_rate": "58.33%",
                        "sample_stage": "insufficient",
                    },
                }
            ],
        },
    )
    state = load_kelly_lab_state(data_dir)

    indexed = index_kelly_experiments_by_market_symbol(state.experiments)

    assert list(indexed) == [("US", "MSFT")]
    assert indexed[("US", "MSFT")][0]["experiment_id"] == "breakout_10d_exp_20260707"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'open_trader.kelly_lab'`.

- [ ] **Step 3: Implement `kelly_lab.py`**

Create `src/open_trader/kelly_lab.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TEMPLATES_SCHEMA_VERSION = "open_trader.kelly_strategy_templates.v1"
EXPERIMENTS_SCHEMA_VERSION = "open_trader.kelly_experiments.v1"

ALLOWED_EXPERIMENT_STATUSES = {"draft", "running", "paused", "completed", "failed"}


@dataclass(frozen=True)
class KellyLabState:
    available: bool
    templates: list[dict[str, Any]]
    experiments: list[dict[str, Any]]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "template_count": len(self.templates),
            "experiment_count": len(self.experiments),
            "templates": self.templates,
            "experiments": self.experiments,
            "error": self.error,
        }


def load_kelly_lab_state(data_dir: Path) -> KellyLabState:
    templates_path = data_dir / "latest" / "kelly_strategy_templates.json"
    experiments_path = data_dir / "latest" / "kelly_experiments.json"
    if not templates_path.is_file():
        return KellyLabState(False, [], [], "kelly_strategy_templates.json not found")
    if not experiments_path.is_file():
        return KellyLabState(False, [], [], "kelly_experiments.json not found")

    templates_payload = _read_json_object(templates_path)
    experiments_payload = _read_json_object(experiments_path)
    if templates_payload.get("schema_version") != TEMPLATES_SCHEMA_VERSION:
        raise ValueError("kelly strategy templates schema_version is invalid")
    if experiments_payload.get("schema_version") != EXPERIMENTS_SCHEMA_VERSION:
        raise ValueError("kelly experiments schema_version is invalid")

    templates = [_validated_template(item) for item in _list_value(templates_payload, "templates")]
    templates_by_key = {
        (template["strategy_id"], template["strategy_version"]): template
        for template in templates
    }
    experiments = [
        _validated_experiment(item, templates_by_key)
        for item in _list_value(experiments_payload, "experiments")
    ]
    return KellyLabState(True, templates, experiments)


def index_kelly_experiments_by_market_symbol(
    experiments: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for experiment in experiments:
        for participant in experiment.get("participants", []):
            if not isinstance(participant, dict):
                continue
            market = str(participant.get("market") or "").strip().upper()
            symbol = str(participant.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            indexed.setdefault((market, symbol), []).append(experiment)
    return indexed


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _list_value(payload: dict[str, Any], key: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"kelly artifact field {key} must be a list")
    return value


def _validated_template(item: object) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("kelly strategy template must be an object")
    required = (
        "strategy_id",
        "strategy_name",
        "strategy_version",
        "entry_rule_description",
        "exit_rule_description",
        "max_holding_days",
        "order_type",
        "market_session",
    )
    template = {key: item.get(key) for key in required}
    missing = [key for key, value in template.items() if str(value or "").strip() == ""]
    if missing:
        raise ValueError(f"kelly strategy template missing field(s): {', '.join(missing)}")
    return {**item, **{key: str(template[key]).strip() for key in required if key != "max_holding_days"}}


def _validated_experiment(
    item: object,
    templates_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("kelly experiment must be an object")
    required = (
        "experiment_id",
        "experiment_name",
        "strategy_id",
        "strategy_version",
        "start_date",
        "paper_account",
        "experiment_budget",
        "budget_currency",
        "capital_utilization_pct",
        "allocation_mode",
        "max_open_position_per_symbol",
        "status",
    )
    missing = [key for key in required if str(item.get(key) or "").strip() == ""]
    if missing:
        raise ValueError(f"kelly experiment missing field(s): {', '.join(missing)}")
    status = str(item.get("status") or "").strip()
    if status not in ALLOWED_EXPERIMENT_STATUSES:
        raise ValueError(f"kelly experiment status is invalid: {status}")
    if item.get("locked") is not True and status != "draft":
        raise ValueError("running Kelly experiments must be locked")
    template_key = (
        str(item.get("strategy_id") or "").strip(),
        str(item.get("strategy_version") or "").strip(),
    )
    template = templates_by_key.get(template_key)
    if template is None:
        raise ValueError("kelly experiment references an unknown strategy template")
    participants = [_validated_participant(participant) for participant in _experiment_participants(item)]
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    return {
        **item,
        "status": status,
        "template": template,
        "participants": participants,
        "stats": {
            "completed_samples": stats.get("completed_samples", 0),
            "open_samples": stats.get("open_samples", 0),
            "observed_win_rate": str(stats.get("observed_win_rate") or ""),
            "sample_stage": str(stats.get("sample_stage") or "insufficient"),
        },
    }


def _experiment_participants(item: dict[str, Any]) -> list[object]:
    participants = item.get("participants")
    if not isinstance(participants, list):
        raise ValueError("kelly experiment participants must be a list")
    return participants


def _validated_participant(item: object) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("kelly experiment participant must be an object")
    required = ("market", "symbol", "name", "source", "per_symbol_budget", "budget_currency")
    missing = [key for key in required if str(item.get(key) or "").strip() == ""]
    if missing:
        raise ValueError(f"kelly participant missing field(s): {', '.join(missing)}")
    if item.get("locked") is not True:
        raise ValueError("kelly experiment participant must be locked")
    return {
        **item,
        "market": str(item.get("market") or "").strip().upper(),
        "symbol": str(item.get("symbol") or "").strip().upper(),
    }
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py -q
```

Expected: PASS, `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/kelly_lab.py tests/test_kelly_lab.py
git commit -m "feat: add kelly lab artifact loader"
```

---

### Task 2: Dashboard Payload Integration

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing dashboard state test**

Append to `tests/test_dashboard.py`:

```python
def test_load_dashboard_state_exposes_kelly_lab_and_holding_detail(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    latest = tmp_path / "data" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "kelly_strategy_templates.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_strategy_templates.v1",
                "templates": [
                    {
                        "strategy_id": "trend_pullback_20d",
                        "strategy_name": "趋势回调 20D",
                        "strategy_version": "v1",
                        "entry_rule_description": "价格回调到 20 日均线附近。",
                        "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                        "max_holding_days": 20,
                        "order_type": "limit",
                        "market_session": "regular",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (latest / "kelly_experiments.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_experiments.v1",
                "experiments": [
                    {
                        "experiment_id": "trend_pullback_20d_exp_20260707",
                        "experiment_name": "趋势回调 20D 第一批",
                        "strategy_id": "trend_pullback_20d",
                        "strategy_version": "v1",
                        "start_date": "2026-07-07",
                        "paper_account": "futu_simulate",
                        "experiment_budget": "100000",
                        "budget_currency": "USD",
                        "capital_utilization_pct": "50",
                        "allocation_mode": "equal_weight",
                        "max_open_position_per_symbol": 1,
                        "status": "running",
                        "locked": True,
                        "participants": [
                            {
                                "market": "US",
                                "symbol": "VIXY",
                                "name": "ProShares VIX",
                                "source": "holding+watchlist",
                                "locked": True,
                                "per_symbol_budget": "25000",
                                "budget_currency": "USD",
                            }
                        ],
                        "stats": {
                            "completed_samples": 0,
                            "open_samples": 0,
                            "observed_win_rate": "",
                            "sample_stage": "insufficient",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    assert state["kelly_lab"]["available"] is True
    assert state["kelly_lab"]["experiment_count"] == 1
    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["kelly"]["available"] is True
    assert vixy["kelly"]["experiment_count"] == 1
    assert vixy["kelly"]["experiments"][0]["experiment_id"] == "trend_pullback_20d_exp_20260707"
    qqq = next(row for row in state["holdings"] if row["symbol"] == "QQQ")
    assert qqq["kelly"]["available"] is False
    assert qqq["kelly"]["experiment_count"] == 0
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py::test_load_dashboard_state_exposes_kelly_lab_and_holding_detail -q
```

Expected: FAIL with `KeyError: 'kelly_lab'` or `KeyError: 'kelly'`.

- [ ] **Step 3: Integrate Kelly state into dashboard payload**

Modify `src/open_trader/dashboard.py`:

Add imports:

```python
from .kelly_lab import (
    index_kelly_experiments_by_market_symbol,
    load_kelly_lab_state,
)
```

Add a `kelly_lab` field to `DashboardState` and `to_dict()`:

```python
@dataclass(frozen=True)
class DashboardState:
    config: DashboardConfig
    broker_detail_month: str
    detail_available: bool
    broker_source_statuses: list[dict[str, Any]]
    summary: dict[str, Any]
    holdings: list[dict[str, Any]]
    broker_summaries: list[dict[str, Any]]
    cash_details: list[dict[str, str]]
    kelly_lab: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_path": str(self.config.portfolio_path),
            "broker_detail_month": self.broker_detail_month,
            "detail_available": self.detail_available,
            "broker_source_statuses": self.broker_source_statuses,
            "summary": self.summary,
            "holdings": self.holdings,
            "broker_summaries": self.broker_summaries,
            "cash_details": self.cash_details,
            "kelly_lab": self.kelly_lab,
        }
```

Inside `load_dashboard_state()` after other latest artifact loading:

```python
    kelly_lab_state = load_kelly_lab_state(config.data_dir)
    kelly_experiments_by_holding = index_kelly_experiments_by_market_symbol(
        kelly_lab_state.experiments
    )
```

Pass `kelly_experiments_by_holding` into `_holding_detail()`, add a parameter:

```python
    kelly_experiments_by_holding: dict[tuple[str, str], list[dict[str, Any]]],
```

Set:

```python
    holding["kelly"] = _kelly_detail(
        kelly_experiments_by_holding.get(key, []) if key is not None else []
    )
```

Add helper:

```python
def _kelly_detail(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    if not experiments:
        return {
            "available": False,
            "experiment_count": 0,
            "experiments": [],
            "status": "missing_experiment",
            "message": "该标的未参与任何已锁定的 Kelly 策略实验。",
        }
    return {
        "available": True,
        "experiment_count": len(experiments),
        "experiments": experiments,
        "status": "available",
        "message": "该标的已关联 Kelly 策略实验。",
    }
```

When returning `DashboardState`, include:

```python
        kelly_lab=kelly_lab_state.to_dict(),
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py tests/test_dashboard.py::test_load_dashboard_state_exposes_kelly_lab_and_holding_detail -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: expose kelly lab dashboard state"
```

---

### Task 3: Read-Only Kelly Lab Panel

**Files:**
- Modify: `src/open_trader/dashboard_static/index.html`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write failing static and JS tests**

Append to `tests/test_dashboard_web.py`:

```python
def test_dashboard_static_contains_kelly_lab_panel_mount() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="kelly-lab-panel"' in html


def test_dashboard_js_renders_kelly_lab_panel() -> None:
    run_dashboard_js(
        """
state.dashboard = {
  kelly_lab: {
    available: true,
    experiment_count: 1,
    experiments: [{
      experiment_id: "trend_pullback_20d_exp_20260707",
      experiment_name: "趋势回调 20D 第一批",
      status: "running",
      locked: true,
      experiment_budget: "100000",
      budget_currency: "USD",
      capital_utilization_pct: "50",
      template: {
        strategy_id: "trend_pullback_20d",
        strategy_name: "趋势回调 20D",
        strategy_version: "v1",
        entry_rule_description: "价格回调到 20 日均线附近。",
        exit_rule_description: "目标价、止损或 20 个交易日到期。"
      },
      participants: [
        {market: "US", symbol: "AAPL", name: "Apple Inc.", source: "holding+watchlist", per_symbol_budget: "25000", budget_currency: "USD"}
      ],
      stats: {completed_samples: 0, open_samples: 0, observed_win_rate: "", sample_stage: "insufficient"}
    }]
  }
};
const html = renderKellyLabPanel();
if (!html.includes("模拟盘策略实验室") || !html.includes("趋势回调 20D 第一批")) {
  throw new Error("kelly lab panel missing experiment identity: " + html);
}
if (!html.includes("样本不足") || !html.includes("AAPL")) {
  throw new Error("kelly lab panel missing sample stage or participant: " + html);
}
"""
    )
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_contains_kelly_lab_panel_mount tests/test_dashboard_web.py::test_dashboard_js_renders_kelly_lab_panel -q
```

Expected: FAIL because `kelly-lab-panel` and `renderKellyLabPanel()` do not exist.

- [ ] **Step 3: Add HTML mount point**

Modify `src/open_trader/dashboard_static/index.html` inside `<main class="workspace-grid">`, before `<section class="holdings-panel">`:

```html
        <section id="kelly-lab-panel" class="kelly-lab-panel" aria-label="Kelly 模拟盘策略实验室"></section>
```

- [ ] **Step 4: Add JS rendering**

Modify `bindElements()` in `src/open_trader/dashboard_static/dashboard.js` to include:

```javascript
    "kelly-lab-panel",
```

Modify `renderDashboard()` to call the new renderer before `renderDashboardViews()`:

```javascript
  renderKellyLab();
```

Add functions near other render helpers:

```javascript
function renderKellyLab() {
  if (!elements["kelly-lab-panel"]) {
    return;
  }
  elements["kelly-lab-panel"].innerHTML = renderKellyLabPanel();
}

function renderKellyLabPanel() {
  const lab = state.dashboard && state.dashboard.kelly_lab && typeof state.dashboard.kelly_lab === "object"
    ? state.dashboard.kelly_lab
    : {};
  if (lab.available !== true) {
    return `
      <div class="section-heading compact-heading">
        <div>
          <h1>模拟盘策略实验室</h1>
          <p>${escapeHtml(formatPlain(lab.error || "Kelly 实验数据尚未生成。"))}</p>
        </div>
      </div>
    `;
  }
  const experiments = Array.isArray(lab.experiments) ? lab.experiments : [];
  return `
    <div class="section-heading compact-heading">
      <div>
        <h1>模拟盘策略实验室</h1>
        <p>只读展示已锁定的策略实验；阶段 1 不连接富途模拟盘，不自动下单。</p>
      </div>
      <span class="count-pill">${escapeHtml(formatPlain(lab.experiment_count || experiments.length))} 个实验</span>
    </div>
    <div class="kelly-experiment-grid">
      ${experiments.length ? experiments.map(renderKellyExperimentCard).join("") : `<article class="kelly-experiment-card"><p class="muted-copy">暂无 Kelly 策略实验。</p></article>`}
    </div>
  `;
}

function renderKellyExperimentCard(experiment) {
  const template = experiment && experiment.template && typeof experiment.template === "object" ? experiment.template : {};
  const stats = experiment && experiment.stats && typeof experiment.stats === "object" ? experiment.stats : {};
  const participants = Array.isArray(experiment.participants) ? experiment.participants : [];
  return `
    <article class="kelly-experiment-card">
      <div class="kelly-experiment-header">
        <div>
          <span>${escapeHtml(formatPlain(template.strategy_id || experiment.strategy_id))}</span>
          <h2>${escapeHtml(formatPlain(experiment.experiment_name || experiment.experiment_id))}</h2>
        </div>
        <b>${escapeHtml(kellyExperimentStatusLabel(experiment.status))}</b>
      </div>
      <p>${escapeHtml(formatPlain(template.entry_rule_description || "-"))}</p>
      <div class="kelly-stat-grid">
        <div><span>完成样本</span><strong>${escapeHtml(formatPlain(stats.completed_samples || 0))}</strong></div>
        <div><span>未平仓</span><strong>${escapeHtml(formatPlain(stats.open_samples || 0))}</strong></div>
        <div><span>观察胜率</span><strong>${escapeHtml(formatPlain(stats.observed_win_rate || "-"))}</strong></div>
        <div><span>样本阶段</span><strong>${escapeHtml(kellySampleStageLabel(stats.sample_stage))}</strong></div>
      </div>
      <div class="kelly-participant-list">
        ${participants.map((participant) => `<span>${escapeHtml(formatPlain(participant.market))}.${escapeHtml(formatPlain(participant.symbol))}</span>`).join("")}
      </div>
    </article>
  `;
}

function kellyExperimentStatusLabel(value) {
  const labels = {draft: "草稿", running: "运行中", paused: "已暂停", completed: "已完成", failed: "失败"};
  return labels[value] || formatPlain(value || "-");
}

function kellySampleStageLabel(value) {
  const labels = {
    insufficient: "样本不足",
    observing: "观察中",
    usable_conservative: "保守可用",
    usable: "可用",
    paused: "已暂停",
  };
  return labels[value] || formatPlain(value || "-");
}
```

- [ ] **Step 5: Add CSS**

Append to `src/open_trader/dashboard_static/dashboard.css`:

```css
.kelly-lab-panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
  display: grid;
  gap: 12px;
  min-width: 0;
  padding: 14px;
}

.compact-heading {
  margin-bottom: 0;
}

.kelly-experiment-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.kelly-experiment-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 10px;
  min-width: 0;
  padding: 12px;
}

.kelly-experiment-header {
  align-items: start;
  display: flex;
  gap: 10px;
  justify-content: space-between;
}

.kelly-experiment-header span,
.kelly-experiment-card p,
.kelly-stat-grid span {
  color: var(--muted);
  font-size: 12px;
}

.kelly-experiment-header h2 {
  font-size: 16px;
  margin: 3px 0 0;
}

.kelly-experiment-header b {
  color: var(--accent);
  white-space: nowrap;
}

.kelly-stat-grid {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.kelly-stat-grid div {
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 7px;
  padding: 8px;
}

.kelly-stat-grid strong {
  display: block;
  font-size: 16px;
  margin-top: 3px;
}

.kelly-participant-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.kelly-participant-list span {
  background: #e4efe9;
  border-radius: 999px;
  color: var(--accent-strong);
  font-size: 12px;
  font-weight: 700;
  padding: 4px 8px;
}
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_static_contains_kelly_lab_panel_mount tests/test_dashboard_web.py::test_dashboard_js_renders_kelly_lab_panel -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/dashboard_static/index.html src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: render kelly lab experiment panel"
```

---

### Task 4: Holdings `凯利` Detail Mode

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Write failing JS detail-mode test**

Add this assertion block to the existing `test_dashboard_header_filters_and_cash_view_helpers()` script in `tests/test_dashboard_web.py`, near the current `做T` detail assertions:

```javascript
state.selectedHoldingDetail = "kelly";
state.dashboard.holdings[1].kelly = {
  available: true,
  experiment_count: 1,
  status: "available",
  message: "该标的已关联 Kelly 策略实验。",
  experiments: [{
    experiment_id: "trend_pullback_20d_exp_20260707",
    experiment_name: "趋势回调 20D 第一批",
    status: "running",
    template: {strategy_id: "trend_pullback_20d", strategy_name: "趋势回调 20D"},
    stats: {completed_samples: 0, open_samples: 0, observed_win_rate: "", sample_stage: "insufficient"}
  }]
};
renderHoldings();
for (const required of ["凯利仓位 ·", "趋势回调 20D 第一批", "样本不足", "阶段 1 不计算 Kelly 仓位"]) {
  if (!elements["holdings-body"].innerHTML.includes(required)) {
    throw new Error("kelly detail missing " + required + ": " + elements["holdings-body"].innerHTML);
  }
}
```

Update the existing row-button assertion from:

```javascript
if (!elements["holdings-body"].innerHTML.includes("交易决策") || !elements["holdings-body"].innerHTML.includes(">做T<") || elements["holdings-body"].innerHTML.includes(">详情<")) {
```

to:

```javascript
if (!elements["holdings-body"].innerHTML.includes("交易决策") || !elements["holdings-body"].innerHTML.includes(">做T<") || !elements["holdings-body"].innerHTML.includes(">凯利<") || elements["holdings-body"].innerHTML.includes(">详情<")) {
```

- [ ] **Step 2: Run JS test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_header_filters_and_cash_view_helpers -q
```

Expected: FAIL because `normalizeHoldingDetailMode()` rejects `kelly` and no Kelly button/detail exists.

- [ ] **Step 3: Add `凯利` button and detail routing**

In `src/open_trader/dashboard_static/dashboard.js`, update holdings row action cell:

```javascript
          <td><button class="expand-button" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="decision">交易决策</button><button class="${escapeHtml(tSignalClass)}" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="t_signal">做T</button><button class="expand-button kelly-button" type="button" data-detail-key="${escapeHtml(rowKey)}" data-detail-mode="kelly">凯利</button></td>
```

Update selected detail rendering:

```javascript
                ${selectedDetail === "t_signal"
                  ? renderTSignalDetail(selected.holding)
                  : selectedDetail === "kelly"
                    ? renderKellyDetail(selected.holding)
                    : renderSymbolDetail(selected.holding, selected.index)}
```

Update `normalizeHoldingDetailMode()`:

```javascript
function normalizeHoldingDetailMode(mode) {
  return ["t_signal", "kelly"].includes(mode) ? mode : "decision";
}
```

Add `renderKellyDetail()` near `renderTSignalDetail()`:

```javascript
function renderKellyDetail(holding) {
  const title = `${formatPlain(holding.market)}.${formatPlain(holding.symbol)}`;
  const detail = holding && holding.kelly && typeof holding.kelly === "object" ? holding.kelly : {};
  const experiments = Array.isArray(detail.experiments) ? detail.experiments : [];
  return `
    <div class="detail-header trading-decision-header">
      <div>
        <button class="raw-toggle" type="button" data-back-to-holdings>返回持仓列表</button>
        <h2>凯利仓位 · ${escapeHtml(title)}</h2>
        <p>${escapeHtml(formatPlain(detail.message || "该标的暂无 Kelly 策略实验。"))}</p>
      </div>
      <button class="raw-toggle" type="button" data-back-to-holdings>收起</button>
    </div>
    <div class="kelly-detail-layout">
      <section class="detail-section kelly-detail-section">
        <h3>关联实验</h3>
        ${experiments.length ? experiments.map(renderKellyDetailExperiment).join("") : `<p class="muted-copy">该标的未参与任何已锁定的 Kelly 策略实验。</p>`}
      </section>
      <section class="detail-section kelly-detail-section">
        <h3>阶段 1 说明</h3>
        <p class="muted-copy">阶段 1 不计算 Kelly 仓位，只展示实验关联和样本阶段。模拟盘订单、样本结算和 Kelly 统计将在后续阶段接入。</p>
      </section>
    </div>
  `;
}

function renderKellyDetailExperiment(experiment) {
  const template = experiment && experiment.template && typeof experiment.template === "object" ? experiment.template : {};
  const stats = experiment && experiment.stats && typeof experiment.stats === "object" ? experiment.stats : {};
  return `
    <article class="kelly-detail-experiment">
      <div>
        <span>${escapeHtml(formatPlain(template.strategy_id || experiment.strategy_id))}</span>
        <strong>${escapeHtml(formatPlain(experiment.experiment_name || experiment.experiment_id))}</strong>
      </div>
      <dl class="compact-kv">
        ${renderCompactKv("状态", kellyExperimentStatusLabel(experiment.status))}
        ${renderCompactKv("样本阶段", kellySampleStageLabel(stats.sample_stage))}
        ${renderCompactKv("完成样本", formatPlain(stats.completed_samples || 0))}
        ${renderCompactKv("未平仓", formatPlain(stats.open_samples || 0))}
        ${renderCompactKv("观察胜率", formatPlain(stats.observed_win_rate || "-"))}
      </dl>
    </article>
  `;
}
```

- [ ] **Step 4: Add CSS**

Append to `src/open_trader/dashboard_static/dashboard.css`:

```css
.kelly-button {
  margin-left: 4px;
}

.kelly-detail-layout {
  display: grid;
  gap: 12px;
}

.kelly-detail-experiment {
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 10px;
  padding: 10px;
}

.kelly-detail-experiment span {
  color: var(--muted);
  display: block;
  font-size: 12px;
  margin-bottom: 3px;
}

.kelly-detail-experiment strong {
  font-size: 16px;
}
```

- [ ] **Step 5: Run focused JS test**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py::test_dashboard_header_filters_and_cash_view_helpers -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py
git commit -m "feat: add kelly holding detail mode"
```

---

### Task 5: Playwright Verification Harness

**Files:**
- Create: `package.json`
- Create: `playwright.config.ts`
- Create: `tests/e2e/serve_dashboard_fixture.py`
- Create: `tests/e2e/fixtures/kelly-dashboard.json`
- Create: `tests/e2e/kelly-lab.spec.ts`

- [ ] **Step 1: Add Playwright package metadata**

Create `package.json`:

```json
{
  "private": true,
  "scripts": {
    "test:e2e": "playwright test",
    "test:e2e:kelly": "playwright test tests/e2e/kelly-lab.spec.ts"
  },
  "devDependencies": {
    "@playwright/test": "^1.45.0"
  }
}
```

Create `playwright.config.ts`:

```typescript
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: 'http://127.0.0.1:8766',
    trace: 'on-first-retry',
  },
  webServer: {
    command: '.venv/bin/python tests/e2e/serve_dashboard_fixture.py --port 8766',
    url: 'http://127.0.0.1:8766',
    reuseExistingServer: !process.env.CI,
    timeout: 10_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
```

- [ ] **Step 2: Add fixture dashboard server**

Create `tests/e2e/serve_dashboard_fixture.py`:

```python
from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "src" / "open_trader" / "dashboard_static"
FIXTURE_PATH = Path(__file__).with_name("fixtures") / "kelly-dashboard.json"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/static/dashboard.css":
            self._send_file(STATIC_DIR / "dashboard.css", "text/css; charset=utf-8")
            return
        if path == "/static/dashboard.js":
            self._send_file(STATIC_DIR / "dashboard.js", "application/javascript; charset=utf-8")
            return
        if path == "/api/dashboard":
            self._send_json(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
            return
        if path == "/api/quotes":
            self._send_json(
                {
                    "status": "ok",
                    "requested_count": 0,
                    "quote_count": 0,
                    "missing_count": 0,
                    "fetched_at": "2026-07-07T15:30:00+08:00",
                    "last_success_at": "2026-07-07T15:30:00+08:00",
                    "stale": False,
                    "quotes": {},
                    "diagnostic": {},
                }
            )
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fixture_dashboard_url: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Add dashboard fixture**

Create `tests/e2e/fixtures/kelly-dashboard.json`:

```json
{
  "portfolio_path": "tests/e2e/fixtures/portfolio.csv",
  "broker_detail_month": "",
  "detail_available": false,
  "broker_source_statuses": [],
  "summary": {
    "portfolio_value_hkd": "1000000.00",
    "holding_value_hkd": "1000000.00",
    "cash_like_value_hkd": "0.00",
    "cash_like_weight_hkd": "0.00%",
    "holding_count": 2
  },
  "broker_summaries": [],
  "cash_details": [],
  "kelly_lab": {
    "available": true,
    "template_count": 1,
    "experiment_count": 1,
    "error": "",
    "templates": [],
    "experiments": [
      {
        "experiment_id": "trend_pullback_20d_exp_20260707",
        "experiment_name": "趋势回调 20D 第一批",
        "strategy_id": "trend_pullback_20d",
        "strategy_version": "v1",
        "start_date": "2026-07-07",
        "paper_account": "futu_simulate",
        "experiment_budget": "100000",
        "budget_currency": "USD",
        "capital_utilization_pct": "50",
        "allocation_mode": "equal_weight",
        "max_open_position_per_symbol": 1,
        "status": "running",
        "locked": true,
        "template": {
          "strategy_id": "trend_pullback_20d",
          "strategy_name": "趋势回调 20D",
          "strategy_version": "v1",
          "entry_rule_description": "价格回调到 20 日均线附近。",
          "exit_rule_description": "目标价、止损或 20 个交易日到期。",
          "max_holding_days": 20,
          "order_type": "limit",
          "market_session": "regular"
        },
        "participants": [
          {
            "market": "US",
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "source": "holding+watchlist",
            "locked": true,
            "per_symbol_budget": "25000",
            "budget_currency": "USD"
          }
        ],
        "stats": {
          "completed_samples": 0,
          "open_samples": 0,
          "observed_win_rate": "",
          "sample_stage": "insufficient"
        }
      }
    ]
  },
  "holdings": [
    {
      "market": "US",
      "asset_class": "stock",
      "symbol": "AAPL",
      "name": "Apple Inc.",
      "currency": "USD",
      "total_quantity": "10",
      "avg_cost_price": "180",
      "last_price": "210",
      "market_value": "2100",
      "market_value_hkd": "16380.00",
      "portfolio_weight_hkd": "1.64%",
      "unrealized_pnl_pct": "16.67%",
      "brokers": "futu",
      "broker_detail_count": 0,
      "broker_details": [],
      "agent_report": {"available": false},
      "tradingagents_summary": {"available": false},
      "strategy": {"available": false},
      "premarket_action": {"available": false},
      "trade_action": {"available": false},
      "technical_facts": {"available": false},
      "decision_facts": {"kline": {"available": false}, "news_sentiment": {"available": false}},
      "futu_skill_facts": {},
      "t_signal": {"available": false},
      "research_view": {},
      "kelly": {
        "available": true,
        "experiment_count": 1,
        "status": "available",
        "message": "该标的已关联 Kelly 策略实验。",
        "experiments": [
          {
            "experiment_id": "trend_pullback_20d_exp_20260707",
            "experiment_name": "趋势回调 20D 第一批",
            "status": "running",
            "template": {"strategy_id": "trend_pullback_20d", "strategy_name": "趋势回调 20D"},
            "stats": {"completed_samples": 0, "open_samples": 0, "observed_win_rate": "", "sample_stage": "insufficient"}
          }
        ]
      }
    },
    {
      "market": "US",
      "asset_class": "stock",
      "symbol": "MSFT",
      "name": "Microsoft",
      "currency": "USD",
      "total_quantity": "5",
      "avg_cost_price": "420",
      "last_price": "500",
      "market_value": "2500",
      "market_value_hkd": "19500.00",
      "portfolio_weight_hkd": "1.95%",
      "unrealized_pnl_pct": "19.05%",
      "brokers": "futu",
      "broker_detail_count": 0,
      "broker_details": [],
      "agent_report": {"available": false},
      "tradingagents_summary": {"available": false},
      "strategy": {"available": false},
      "premarket_action": {"available": false},
      "trade_action": {"available": false},
      "technical_facts": {"available": false},
      "decision_facts": {"kline": {"available": false}, "news_sentiment": {"available": false}},
      "futu_skill_facts": {},
      "t_signal": {"available": false},
      "research_view": {},
      "kelly": {
        "available": false,
        "experiment_count": 0,
        "status": "missing_experiment",
        "message": "该标的未参与任何已锁定的 Kelly 策略实验。",
        "experiments": []
      }
    }
  ]
}
```

- [ ] **Step 4: Add Playwright test**

Create `tests/e2e/kelly-lab.spec.ts`:

```typescript
import { expect, test } from '@playwright/test';

test('renders Kelly lab and opens holding Kelly detail', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toBeVisible();
  await expect(page.getByText('趋势回调 20D 第一批')).toBeVisible();
  await expect(page.getByText('样本不足')).toBeVisible();
  await expect(page.getByText('US.AAPL')).toBeVisible();

  const aaplRow = page.getByRole('row').filter({ hasText: 'AAPL' }).first();
  await expect(aaplRow.getByRole('button', { name: '凯利' })).toBeVisible();
  await aaplRow.getByRole('button', { name: '凯利' }).click();

  await expect(page.getByRole('heading', { name: /凯利仓位 · US\.AAPL/ })).toBeVisible();
  await expect(page.getByText('阶段 1 不计算 Kelly 仓位')).toBeVisible();
  await expect(page.getByText('趋势回调 20D 第一批').last()).toBeVisible();
});
```

- [ ] **Step 5: Install Playwright dependencies if needed**

Run:

```bash
npm install
npx playwright install chromium
```

Expected: `node_modules/` is created, `package-lock.json` is created, Chromium browser installed. If network access fails during implementation, rerun with the required approval path for networked dependency installation.

- [ ] **Step 6: Run Playwright test**

Run:

```bash
npm run test:e2e:kelly
```

Expected: PASS, `1 passed`.

- [ ] **Step 7: Commit**

```bash
git add package.json package-lock.json playwright.config.ts tests/e2e/serve_dashboard_fixture.py tests/e2e/fixtures/kelly-dashboard.json tests/e2e/kelly-lab.spec.ts
git commit -m "test: add kelly lab playwright coverage"
```

---

### Task 6: Phase 1 Verification Pass

**Files:**
- No new files.

- [ ] **Step 1: Run backend and static tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py tests/test_dashboard.py::test_load_dashboard_state_exposes_kelly_lab_and_holding_detail tests/test_dashboard.py::test_load_dashboard_state_degrades_invalid_kelly_lab_artifacts tests/test_dashboard_web.py::test_dashboard_static_contains_kelly_lab_panel_mount tests/test_dashboard_web.py::test_dashboard_js_renders_kelly_lab_panel tests/test_dashboard_web.py::test_dashboard_header_filters_and_cash_view_helpers -q
```

Expected: all selected tests PASS.

- [ ] **Step 2: Run Playwright**

Run:

```bash
npm run test:e2e:kelly
```

Expected: PASS, `1 passed`.

- [ ] **Step 3: Run a broader dashboard regression slice**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: all tests PASS or existing environment skips only for missing optional tools. No failures.

- [ ] **Step 4: Check git status**

Run:

```bash
git status --short
```

Expected: only intended files are modified or untracked. Existing unrelated `data.bak.20260705151209/` and `tmp/` may remain untracked and must not be committed.

- [ ] **Step 5: Commit verification notes if any code changed**

If verification reveals small test-only fixes, commit them:

```bash
git add <fixed files>
git commit -m "test: verify kelly lab phase 1"
```

If no files changed, do not create an empty commit.

---

## Self-Review Checklist

- Spec coverage: Phase 1 covers local strategy template/experiment artifacts, locked participant display, holdings `凯利` entry, and Playwright verification. It intentionally excludes Futu order sync, simulated order placement, sample generation, and Kelly calculations.
- Placeholder scan: no task uses undefined "TBD" work; every implementation task includes concrete file paths, code snippets, commands, and expected outcomes.
- Type consistency: dashboard payload fields are `kelly_lab` top-level and `kelly` per holding; experiment identifiers use `experiment_id`, `strategy_id`, and `strategy_version` consistently.
