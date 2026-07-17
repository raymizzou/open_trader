# Futu Unsupported Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Represent Futu SDK `err_code=-12301` as a non-blocking `not_applicable` module without weakening handling for any other source failure.

**Architecture:** Keep the distinction at the existing Futu anomaly boundary with a dedicated exception, normalize it into the existing fixed signal-module shape, and let the shared source-availability helper recognize the new state. Preserve recognition of legacy cached `error` records whose summary starts with `富途接口不支持`.

**Tech Stack:** Python 3.12, pytest, existing Futu OpenD client and Dashboard JavaScript status renderer.

## Global Constraints

- Only Futu SDK `err_code=-12301` becomes `not_applicable`.
- Connection failures, malformed responses, missing fields, and stale data remain blocking.
- Do not synthesize a replacement signal or indicator.
- Keep legacy cached unsupported records readable.

---

### Task 1: Normalize Explicitly Unsupported Futu Modules

**Files:**
- Modify: `src/open_trader/futu_skill_facts.py`
- Test: `tests/test_futu_skill_facts.py`

**Interfaces:**
- Consumes: `FutuAnomalyScriptClient._run_native(module, stock_symbol, window_days)` and Futu payload `{"err_code": -12301}`.
- Produces: validated signal modules with `status="not_applicable"`, neutral signal, low confidence, empty constraint, and one category whose state is `not_applicable`.

- [ ] **Step 1: Write failing tests**

Update the native-client test to require a dedicated `FutuAnomalyUnsupportedError`, then add a generation test whose technical extractor raises that error and assert:

```python
assert result.failed == 0
assert record["error"] == ""
assert record["technical_anomaly"] == {
    "status": "not_applicable",
    "signal": "neutral",
    "confidence": "low",
    "suggested_constraint": "",
    "window_days": 7,
    "summary": "富途接口不支持技术异动：US.BOTZ",
    "categories": [{
        "name": "技术异动",
        "state": "not_applicable",
        "direction": "",
        "detail": "富途接口不支持技术异动：US.BOTZ",
        "evidence_date": "",
    }],
}
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_futu_skill_facts.py::test_futu_anomaly_client_reports_native_sdk_unsupported_reason \
  tests/test_futu_skill_facts.py::test_generate_futu_skill_facts_marks_native_unsupported_as_not_applicable
```

Expected: FAIL because the dedicated exception and `not_applicable` module do not exist.

- [ ] **Step 3: Implement the minimum state conversion**

In `src/open_trader/futu_skill_facts.py`:

```python
class FutuAnomalyUnsupportedError(RuntimeError):
    pass
```

Raise it only for `err_code == -12301`, add `not_applicable` to `VALID_MODULE_STATUSES`, and construct this fixed module:

```python
def _unsupported_signal_module(module_name: str, window_days: int, reason: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "signal": "neutral",
        "confidence": "low",
        "suggested_constraint": "",
        "window_days": _validate_window_days(window_days),
        "summary": reason,
        "categories": [{
            "name": _default_error_category_name(module_name),
            "state": "not_applicable",
            "direction": "",
            "detail": reason,
            "evidence_date": "",
        }],
    }
```

Catch this exception before the existing generic exception in each anomaly module. Assign `_unsupported_signal_module(...)` and do not append to the record error list.

- [ ] **Step 4: Verify GREEN and the surrounding file**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_futu_skill_facts.py
```

Expected: all tests PASS.

### Task 2: Accept and Display the New State

**Files:**
- Modify: `src/open_trader/decision_source_availability.py`
- Test: `tests/test_decision_source_availability.py`
- Verify existing native label: `src/open_trader/dashboard_static/dashboard.js`

**Interfaces:**
- Consumes: a Futu module mapping with `status="not_applicable"`.
- Produces: `futu_module_unsupported(module) is True`; source completeness passes it while Dashboard keeps the existing `not_applicable: "不适用"` label.

- [ ] **Step 1: Write the failing availability test**

Change the canonical unsupported test module to:

```python
{
    "status": "not_applicable",
    "summary": "富途接口不支持技术异动：US.MSFT",
}
```

Add a second assertion that the existing legacy `status="error"` plus the same summary is still accepted.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_decision_source_availability.py::test_evaluate_required_sources_accepts_explicit_futu_unsupported_module
```

Expected: FAIL because `not_applicable` is not yet recognized by `futu_module_unsupported`.

- [ ] **Step 3: Extend the shared helper only**

Change `futu_module_unsupported` to return true for the new canonical status or the legacy cached shape:

```python
return bool(
    isinstance(module, dict)
    and (
        module.get("status") == "not_applicable"
        or (
            module.get("status") == "error"
            and str(module.get("summary") or "").startswith("富途接口不支持")
        )
    )
)
```

Do not change `futu_module_available`; unsupported remains distinguishable from usable signal data.

- [ ] **Step 4: Verify GREEN and focused integration**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_decision_source_availability.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py
```

Expected: all tests PASS and the existing JavaScript translation renders `not_applicable` as `不适用`.

- [ ] **Step 5: Commit Tasks 1-2**

```bash
git add \
  src/open_trader/futu_skill_facts.py \
  src/open_trader/decision_source_availability.py \
  tests/test_futu_skill_facts.py \
  tests/test_decision_source_availability.py
git commit -m "fix: classify unsupported Futu anomaly modules"
```

### Task 3: Refresh Live Data and Run the Acceptance Gate

**Files:**
- Regenerate: `data/runs/2026-07-17/HK/futu_skill_facts.json`
- Regenerate: `data/latest/HK/futu_skill_facts.json`
- Inspect: `data/runs/2026-07-17/HK/daily_run_status.json`
- Inspect: `/tmp/open_trader_dashboard_8766.log`

**Interfaces:**
- Consumes: current portfolio, configured credentials, Futu OpenD, and Dashboard on port 8766.
- Produces: real `not_applicable` modules, a fresh Dashboard process, and a final acceptance status.

- [ ] **Step 1: Run all automated tests**

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 2: Refresh real HK Futu skill facts**

```bash
set -a
source config/daily_premarket.env
set +a
PYTHONPATH=src .venv/bin/python -m open_trader extract-futu-skill-facts \
  --portfolio data/runs/2026-07-17/portfolio.csv \
  --data-dir data --date 2026-07-17 --market HK --update-latest
```

Expected: `00200`, `02623`, and `02824` technical anomaly modules have `status=not_applicable` and do not add record errors.

- [ ] **Step 3: Restart and verify the Dashboard candidate**

Stop the process listening on 8766, start the existing screen command from `/Users/ray/projects/open_trader`, then verify PID, cwd, current Git SHA, fresh log timestamp, and HTTP 200.

- [ ] **Step 4: Run the final acceptance gate**

```bash
make acceptance
```

Expected: `PASS`. If real `technical_facts` for `02824` is still incomplete, rerun the existing `extract-technical-facts` workflow; do not synthesize or hand-edit its data.

- [ ] **Step 5: Redeploy the accepted SHA**

After `PASS`, restart the Dashboard from the exact accepted SHA and verify a new PID, correct cwd, matching SHA, fresh logs, and HTTP 200 before providing the review URL.
