# Kelly Strategy Stats Single-Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `kelly_strategy_stats.json` as the only runtime source for Kelly parameters used by the dashboard, order intents, and risk audit fields.

**Architecture:** Extract per-experiment Kelly calculation into a focused `kelly_strategy_stats` module that consumes validated trade evidence. Keep `stats_by_experiment` in the trade-sample artifact temporarily as compatibility output, but switch every runtime consumer to the new strategy-stats artifact and fail closed when it is missing, stale, malformed, or incomplete.

**Tech Stack:** Python 3, `Decimal`, argparse CLI, JSON artifacts, pytest, vanilla JavaScript dashboard, Playwright.

## Global Constraints

- `kelly_paper_orders.json` remains the normalized Futu SIMULATE order source of record.
- `kelly_trade_samples.json` owns trade-pair evidence and diagnostics, not runtime decisions.
- `kelly_strategy_stats.json` is the only runtime Kelly-parameter source.
- Zero completed samples produce `sample_stage = "insufficient"` and `suggested_position_pct = "0%"`.
- Runtime consumers never fall back to stats embedded in `kelly_experiments.json`.
- Missing, malformed, stale, or experiment-incomplete strategy stats fail closed for new entries.
- Exit intents remain available when entry sizing is zero or unavailable.
- No strategy-rule, live-account, mixed-market, or portfolio-Kelly changes are in scope.
- Use `.venv/bin/python -m pytest`, not bare `pytest`.
- For behavior changes, verify the real CLI workflow and restart/check the dashboard process in addition to automated tests.

---

## File Map

- Create `src/open_trader/kelly_strategy_stats.py`: build, validate, load, and atomically write the unified stats artifact.
- Create `tests/test_kelly_strategy_stats.py`: calculation, schema, coverage, and staleness tests.
- Create `tests/test_kelly_strategy_stats_cli.py`: CLI parser and artifact-generation tests.
- Modify `src/open_trader/kelly_trade_samples.py`: delegate compatibility stats calculation to the new module.
- Modify `src/open_trader/cli.py`: add `kelly build-strategy-stats` and update producer loading flags.
- Modify `src/open_trader/kelly_lab.py`: replace trade-sample stats overlay with required strategy-stats loading.
- Modify `src/open_trader/kelly_order_intents.py`: consume unified stats and copy provenance into intents.
- Modify `src/open_trader/kelly_order_risk.py`: preserve stats provenance in risk-check output.
- Modify `src/open_trader/dashboard_static/dashboard.js`: display sample state and stale/error provenance clearly.
- Modify `tests/test_kelly_trade_samples.py`, `tests/test_kelly_lab.py`, `tests/test_kelly_order_intents.py`, `tests/test_kelly_order_risk.py`, `tests/test_dashboard_web.py`: integration expectations.
- Modify `tests/e2e/fixtures/kelly-dashboard.json` and `tests/e2e/kelly-lab.spec.ts`: sufficient, insufficient, and error display.
- Create/update `data/latest/kelly_strategy_stats.json`: current generated artifact.
- Modify `CHANGELOG.md`: record the single-source migration.

### Task 1: Unified Strategy-Stats Builder

**Files:**
- Create: `src/open_trader/kelly_strategy_stats.py`
- Create: `tests/test_kelly_strategy_stats.py`

**Interfaces:**
- Consumes: `build_kelly_strategy_stats_payload(experiments: list[dict[str, Any]], trade_samples_payload: dict[str, Any], *, generated_at: str | None = None) -> dict[str, Any]`
- Produces: `STRATEGY_STATS_SCHEMA_VERSION`, `build_kelly_strategy_stats_payload`, `validate_kelly_strategy_stats_payload`, `load_kelly_strategy_stats`, and `write_kelly_strategy_stats`.

- [ ] **Step 1: Write failing calculation and zero-sample tests**

Create tests with one completed win and one configured experiment with no samples:

```python
from open_trader.kelly_strategy_stats import build_kelly_strategy_stats_payload


def test_builds_stats_for_every_configured_experiment() -> None:
    payload = build_kelly_strategy_stats_payload(
        [
            {"experiment_id": "trend_us", "market": "US"},
            {"experiment_id": "breakout_hk", "market": "HK"},
        ],
        {
            "schema_version": "open_trader.kelly_trade_samples.v1",
            "generated_at": "2026-07-11 12:00",
            "samples": [
                {
                    "experiment_id": "trend_us",
                    "result": "win",
                    "net_pnl_pct": "10%",
                    "exit_submitted_at": "2026-07-11 11:59",
                }
            ],
            "open_positions": [],
            "diagnostics": {"skipped_orders": []},
        },
        generated_at="2026-07-11 12:01",
    )

    assert payload["schema_version"] == "open_trader.kelly_strategy_stats.v1"
    assert payload["source_trade_samples_generated_at"] == "2026-07-11 12:00"
    assert payload["stats_by_experiment"]["trend_us"]["completed_samples"] == 1
    assert payload["stats_by_experiment"]["breakout_hk"]["completed_samples"] == 0
    assert payload["stats_by_experiment"]["breakout_hk"]["suggested_position_pct"] == "0%"
    assert payload["stats_by_experiment"]["breakout_hk"]["sample_stage"] == "insufficient"
```

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_stats.py -q
```

Expected: FAIL with `ModuleNotFoundError: open_trader.kelly_strategy_stats`.

- [ ] **Step 3: Implement the builder and calculation helpers**

Create the module with these public shapes:

```python
STRATEGY_STATS_SCHEMA_VERSION = "open_trader.kelly_strategy_stats.v1"
TRADE_SAMPLES_SCHEMA_VERSION = "open_trader.kelly_trade_samples.v1"


def build_kelly_strategy_stats_payload(
    experiments: list[dict[str, Any]],
    trade_samples_payload: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    _validate_trade_samples_payload(trade_samples_payload)
    timestamp = generated_at or _current_timestamp()
    stats_by_experiment = {
        experiment_id: _experiment_stats(
            samples=_for_experiment(trade_samples_payload["samples"], experiment_id),
            open_positions=_for_experiment(
                trade_samples_payload["open_positions"], experiment_id
            ),
            skipped_orders=_for_experiment(
                trade_samples_payload["diagnostics"]["skipped_orders"],
                experiment_id,
            ),
            generated_at=timestamp,
            market=market,
        )
        for experiment_id, market in _configured_experiments(experiments)
    }
    return {
        "schema_version": STRATEGY_STATS_SCHEMA_VERSION,
        "generated_at": timestamp,
        "source_trade_samples_generated_at": trade_samples_payload["generated_at"],
        "experiment_count": len(stats_by_experiment),
        "stats_by_experiment": stats_by_experiment,
    }
```

Move the existing `Decimal` calculation behavior from `kelly_trade_samples.py`
without changing its formulas: 200-sample shrinkage threshold, quarter Kelly,
and 4% position cap. Add `parameter_source = "futu_paper_order_samples"` and
`source_trade_samples_generated_at = trade_samples_payload["generated_at"]` to
every record.

- [ ] **Step 4: Add validation, atomic write, load, and stale/coverage checks**

Implement:

```python
def validate_kelly_strategy_stats_payload(
    payload: object,
    *,
    artifact_name: str = "kelly_strategy_stats.json",
    expected_experiment_ids: set[str] | None = None,
    expected_trade_samples_generated_at: str | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError(f"{artifact_name} must contain a JSON object")
    if payload.get("schema_version") != STRATEGY_STATS_SCHEMA_VERSION:
        raise ValueError(
            f"{artifact_name} schema_version must be "
            f"{STRATEGY_STATS_SCHEMA_VERSION!r}"
        )
    generated_at = payload.get("generated_at")
    source_generated_at = payload.get("source_trade_samples_generated_at")
    stats = payload.get("stats_by_experiment")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError(f"{artifact_name} must contain generated_at")
    if not isinstance(source_generated_at, str) or not source_generated_at.strip():
        raise ValueError(
            f"{artifact_name} must contain source_trade_samples_generated_at"
        )
    if not isinstance(stats, dict):
        raise ValueError(f"{artifact_name} must contain stats_by_experiment")
    if (
        expected_trade_samples_generated_at is not None
        and source_generated_at != expected_trade_samples_generated_at
    ):
        raise ValueError(f"{artifact_name} is stale")
    if expected_experiment_ids is not None and set(stats) != expected_experiment_ids:
        raise ValueError(f"{artifact_name} experiment coverage mismatch")
    required = {
        "completed_samples",
        "sample_stage",
        "suggested_position_pct",
        "parameter_source",
        "last_recomputed_at",
        "source_trade_samples_generated_at",
    }
    for experiment_id, item in stats.items():
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError(f"{artifact_name} contains invalid experiment id")
        if not isinstance(item, dict):
            raise ValueError(
                f"{artifact_name} stats for {experiment_id} must be an object"
            )
        missing = required - set(item)
        if missing:
            raise ValueError(
                f"{artifact_name} stats for {experiment_id} missing "
                f"{sorted(missing)}"
            )
    return copy.deepcopy(stats)


def write_kelly_strategy_stats(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_strategy_stats.json"
    validate_kelly_strategy_stats_payload(payload)
    _write_json_atomic(path, payload)
    return path


def load_kelly_strategy_stats(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_strategy_stats.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    validate_kelly_strategy_stats_payload(payload, artifact_name=path.name)
    return payload
```

Add tests asserting:

```python
with pytest.raises(ValueError, match="experiment coverage"):
    validate_kelly_strategy_stats_payload(
        payload,
        expected_experiment_ids={"trend_us", "breakout_hk"},
    )

with pytest.raises(ValueError, match="stale"):
    validate_kelly_strategy_stats_payload(
        payload,
        expected_trade_samples_generated_at="2026-07-11 12:02",
    )
```

- [ ] **Step 5: Run builder tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_stats.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the builder**

```bash
git add src/open_trader/kelly_strategy_stats.py tests/test_kelly_strategy_stats.py
git commit -m "feat: add kelly strategy stats artifact"
```

### Task 2: Trade-Sample Compatibility and Stats CLI

**Files:**
- Modify: `src/open_trader/kelly_trade_samples.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_kelly_trade_samples.py`
- Create: `tests/test_kelly_strategy_stats_cli.py`

**Interfaces:**
- Consumes: Task 1 `build_kelly_strategy_stats_payload()` and `write_kelly_strategy_stats()`.
- Produces: `open-trader kelly build-strategy-stats --data-dir data --generated-at "2026-07-11 12:01"`.

- [ ] **Step 1: Write failing compatibility and CLI tests**

Add a compatibility assertion showing trade samples still contain the same stats
during migration, and a CLI test that writes the new artifact:

```python
def test_build_strategy_stats_cli_writes_latest_artifact(tmp_path: Path) -> None:
    result = main([
        "kelly",
        "build-strategy-stats",
        "--data-dir",
        str(tmp_path / "data"),
        "--generated-at",
        "2026-07-11 12:01",
    ])
    payload = json.loads(
        (tmp_path / "data/latest/kelly_strategy_stats.json").read_text()
    )
    assert result == 0
    assert payload["generated_at"] == "2026-07-11 12:01"
```

Fixture setup must write valid templates, experiments, and trade samples before
calling `main()`.

- [ ] **Step 2: Run tests and confirm parser/command failure**

```bash
.venv/bin/python -m pytest tests/test_kelly_trade_samples.py tests/test_kelly_strategy_stats_cli.py -q
```

Expected: CLI test FAILS because `build-strategy-stats` is not registered.

- [ ] **Step 3: Delegate compatibility stats to the new module**

In `build_kelly_trade_samples_payload()`, construct the evidence fields first,
then call the new builder and copy its map only for compatibility:

```python
evidence = {
    "schema_version": TRADE_SAMPLES_SCHEMA_VERSION,
    "generated_at": timestamp,
    "source_orders_synced_at": _text(paper_orders_payload.get("synced_at")),
    "sample_count": len(samples),
    "open_position_count": len(open_positions),
    "skipped_order_count": len(diagnostics["skipped_orders"]),
    "samples": samples,
    "open_positions": open_positions,
    "diagnostics": diagnostics,
}
strategy_stats = build_kelly_strategy_stats_payload(
    experiments,
    evidence,
    generated_at=timestamp,
)
return {**evidence, "stats_by_experiment": strategy_stats["stats_by_experiment"]}
```

Delete the duplicate stats-calculation helpers after tests prove output parity.

- [ ] **Step 4: Register and implement `build-strategy-stats`**

Add parser arguments matching `build-trade-samples`, then add a handler that:

```python
lab_state = load_kelly_lab_state(
    args.data_dir,
    include_strategy_capital=False,
    include_strategy_stats=False,
)
trade_samples_payload = load_kelly_trade_samples(args.data_dir)
payload = build_kelly_strategy_stats_payload(
    lab_state.experiments,
    trade_samples_payload,
    generated_at=args.generated_at,
)
latest_path = write_kelly_strategy_stats(args.data_dir, payload)
print(f"experiments: {payload['experiment_count']}")
print(f"latest: {latest_path}")
```

Rename the Kelly Lab opt-out argument as part of Task 3; until that task lands,
use the then-current opt-out and update it atomically with Task 3.

- [ ] **Step 5: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_kelly_trade_samples.py tests/test_kelly_trade_samples_cli.py tests/test_kelly_strategy_stats.py tests/test_kelly_strategy_stats_cli.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit compatibility and CLI work**

```bash
git add src/open_trader/kelly_trade_samples.py src/open_trader/cli.py tests/test_kelly_trade_samples.py tests/test_kelly_strategy_stats_cli.py
git commit -m "feat: build unified kelly strategy stats"
```

### Task 3: Kelly Lab Single-Source Loading

**Files:**
- Modify: `src/open_trader/kelly_lab.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_kelly_lab.py`
- Modify: `tests/test_kelly_trade_samples_cli.py`
- Modify: `tests/test_kelly_strategy_capital_cli.py`

**Interfaces:**
- Consumes: Task 1 validation and `kelly_strategy_stats.json`.
- Produces: `load_kelly_lab_state(data_dir, include_strategy_capital=True, include_strategy_stats=True)` with stats attached exclusively from the new artifact.

- [ ] **Step 1: Write failing lab-source tests**

Add tests proving embedded and compatibility stats are ignored:

```python
def test_lab_uses_strategy_stats_instead_of_embedded_or_sample_stats(tmp_path: Path) -> None:
    write_lab_fixtures(tmp_path, embedded_position="9%", sample_position="8%")
    write_strategy_stats_fixture(tmp_path, position="3%")

    state = load_kelly_lab_state(tmp_path / "data")

    assert state.available is True
    assert state.experiments[0]["stats"]["suggested_position_pct"] == "3%"
```

Also test missing, malformed, stale, and incomplete artifacts. Each must return
`available=False` with an error naming `kelly_strategy_stats.json`; no test may
expect fallback to embedded stats.

- [ ] **Step 2: Run the lab tests and confirm old-source behavior fails**

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py -q
```

Expected: new source-selection and fail-closed tests FAIL.

- [ ] **Step 3: Replace the loader and overlay**

Change the signature and loading sequence:

```python
def load_kelly_lab_state(
    data_dir: Path,
    *,
    include_strategy_capital: bool = True,
    include_strategy_stats: bool = True,
) -> KellyLabState:
```

When enabled, load both `kelly_trade_samples.json` and
`kelly_strategy_stats.json`; call `validate_kelly_strategy_stats_payload()` with:

```python
expected_experiment_ids={item["experiment_id"] for item in experiments}
expected_trade_samples_generated_at=trade_samples_payload["generated_at"]
```

Replace `_attach_trade_sample_stats_to_experiments()` with
`_attach_strategy_stats_to_experiments()`. Set `normalized["stats"]` to a deep
copy of the unified record rather than merging embedded fields.

- [ ] **Step 4: Update all producer opt-outs**

Use `include_strategy_stats=False` only for commands that produce prerequisites
or the stats artifact itself:

```python
# build-trade-samples, build-strategy-stats, build-strategy-capital
load_kelly_lab_state(
    data_dir,
    include_strategy_capital=False,
    include_strategy_stats=False,
)
```

Do not opt out in dashboard loading or order-intent generation.

- [ ] **Step 5: Run Lab and producer tests**

```bash
.venv/bin/python -m pytest tests/test_kelly_lab.py tests/test_kelly_trade_samples_cli.py tests/test_kelly_strategy_capital_cli.py tests/test_dashboard.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the consumer migration**

```bash
git add src/open_trader/kelly_lab.py src/open_trader/cli.py tests/test_kelly_lab.py tests/test_kelly_trade_samples_cli.py tests/test_kelly_strategy_capital_cli.py tests/test_dashboard.py
git commit -m "refactor: load kelly stats from one source"
```

### Task 4: Order Sizing and Risk Provenance

**Files:**
- Modify: `src/open_trader/kelly_order_intents.py`
- Modify: `src/open_trader/kelly_order_risk.py`
- Modify: `tests/test_kelly_order_intents.py`
- Modify: `tests/test_kelly_order_intents_cli.py`
- Modify: `tests/test_kelly_order_risk.py`

**Interfaces:**
- Consumes: unified `experiment["stats"]` from Task 3.
- Produces: intents/checks carrying `strategy_stats_generated_at`, `strategy_stats_source_samples_generated_at`, and `parameter_source`.

- [ ] **Step 1: Write failing intent provenance and fail-closed tests**

Assert an entry intent copies the exact unified values:

```python
assert intent["suggested_position_pct"] == "3%"
assert intent["parameter_source"] == "futu_paper_order_samples"
assert intent["strategy_stats_generated_at"] == "2026-07-11 12:01"
assert intent["strategy_stats_source_samples_generated_at"] == "2026-07-11 12:00"
```

Add a zero-sample entry test expecting an intent with `0%`, followed by a risk
result with `risk_status == "blocked"`. Add an exit test proving it remains
`approved` with `0%` or absent sizing.

- [ ] **Step 2: Run focused tests and confirm provenance is missing**

```bash
.venv/bin/python -m pytest tests/test_kelly_order_intents.py tests/test_kelly_order_risk.py -q
```

Expected: provenance assertions FAIL.

- [ ] **Step 3: Make order intents load unified stats and copy provenance**

Remove `include_trade_samples=False` from `build_kelly_order_intents()`. Add these
fields to every generated intent:

```python
"suggested_position_pct": str(stats.get("suggested_position_pct", "")).strip(),
"parameter_source": str(stats.get("parameter_source", "")).strip(),
"strategy_stats_generated_at": str(stats.get("last_recomputed_at", "")).strip(),
"strategy_stats_source_samples_generated_at": str(
    stats.get("source_trade_samples_generated_at", "")
).strip(),
```

Ensure Task 3 attaches `source_trade_samples_generated_at` to each stats record
when loading the artifact.

- [ ] **Step 4: Preserve provenance in risk-check output**

Extend `_base_check()` so each check contains the same four fields copied from
the intent. Do not recalculate Kelly values in `kelly_order_risk.py`.

- [ ] **Step 5: Run order-chain tests**

```bash
.venv/bin/python -m pytest tests/test_kelly_order_intents.py tests/test_kelly_order_intents_cli.py tests/test_kelly_order_risk.py tests/test_kelly_order_risk_cli.py tests/test_kelly_order_execution.py tests/test_kelly_order_execution_cli.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit sizing and provenance**

```bash
git add src/open_trader/kelly_order_intents.py src/open_trader/kelly_order_risk.py tests/test_kelly_order_intents.py tests/test_kelly_order_intents_cli.py tests/test_kelly_order_risk.py
git commit -m "feat: size kelly orders from unified stats"
```

### Task 5: Dashboard States and Playwright Coverage

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/e2e/fixtures/kelly-dashboard.json`
- Modify: `tests/e2e/kelly-lab.spec.ts`

**Interfaces:**
- Consumes: unified stats fields attached by Task 3.
- Produces: direct UI presentation for sufficient, insufficient, stale, and invalid stats states.

- [ ] **Step 1: Write failing renderer tests**

Add Node-backed renderer assertions:

```python
assert "样本不足" in html
assert "建议仓位" in html
assert "0%" in html
assert "富途模拟盘订单样本" in html
```

Add a dashboard-unavailable assertion that the error names
`kelly_strategy_stats.json`. Extend Playwright fixture experiments so one is
`sufficient`, one is `insufficient`, and an unavailable fixture represents stale
stats.

- [ ] **Step 2: Run renderer and Playwright tests and confirm missing labels**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
npm run test:e2e:kelly
```

Expected: the new state-label assertions FAIL before renderer changes.

- [ ] **Step 3: Render explicit sample state**

In `renderKellyParameterDerivation(stats)`, add:

```javascript
const sampleStageLabel = item.sample_stage === "sufficient"
  ? "样本充足"
  : item.sample_stage === "insufficient"
    ? "样本不足"
    : item.sample_stage;
```

Include rows for `样本状态`, completed/open samples, source sample timestamp,
and latest calculation time. Keep direct display; do not add a button.

- [ ] **Step 4: Run UI tests at desktop and mobile viewports**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
npm run test:e2e:kelly
```

Expected: all tests PASS, no overlapping or truncated parameter fields.

- [ ] **Step 5: Commit UI coverage**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py tests/e2e/fixtures/kelly-dashboard.json tests/e2e
git commit -m "feat: show unified kelly stats states"
```

### Task 6: Migration Artifact, Workflow Verification, and Changelog

**Files:**
- Create: `data/latest/kelly_strategy_stats.json`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: all builders and consumers from Tasks 1-5.
- Produces: a runnable local artifact chain and documented release state.

- [ ] **Step 1: Run the complete focused automated suite**

```bash
.venv/bin/python -m pytest tests/test_kelly_strategy_stats.py tests/test_kelly_strategy_stats_cli.py tests/test_kelly_trade_samples.py tests/test_kelly_trade_samples_cli.py tests/test_kelly_lab.py tests/test_kelly_order_intents.py tests/test_kelly_order_intents_cli.py tests/test_kelly_order_risk.py tests/test_kelly_order_risk_cli.py tests/test_kelly_order_execution.py tests/test_kelly_order_execution_cli.py tests/test_dashboard.py tests/test_dashboard_web.py -q
npm run test:e2e:kelly
.venv/bin/python -m compileall src/open_trader
git diff --check
```

Expected: pytest and Playwright report all PASS; compileall and diff check exit 0.

- [ ] **Step 2: Run the real producer workflow in dependency order**

```bash
.venv/bin/python -m open_trader kelly build-trade-samples --data-dir data --generated-at "2026-07-11 13:00"
.venv/bin/python -m open_trader kelly build-strategy-stats --data-dir data --generated-at "2026-07-11 13:01"
.venv/bin/python -m open_trader kelly build-order-intents --data-dir data --created-at "2026-07-11 13:02"
.venv/bin/python -m open_trader kelly check-order-risk --data-dir data --checked-at "2026-07-11 13:03"
```

Expected: each command prints its count and latest artifact path. Verify with a
read-only JSON query that every intent percentage equals the corresponding
strategy-stats percentage and every risk check preserves the same provenance.

- [ ] **Step 3: Verify fail-closed behavior directly**

Using a temporary data directory, copy valid required fixtures but omit
`kelly_strategy_stats.json`; run `build-order-intents` and expect a non-zero exit
whose error names the missing artifact. Repeat with a stale source timestamp and
expect a non-zero exit containing `stale`.

- [ ] **Step 4: Restart and verify the live dashboard**

Inspect the current dashboard process and service ownership:

```bash
screen -ls
launchctl list | rg open_trader
ps aux | rg "open_trader|dashboard"
```

Restart the actual dashboard using the repository's existing launch command.
Record the new PID and start timestamp, then verify fresh logs and request the
dashboard API. Confirm the API's stats, rendered UI position, generated intent,
and risk provenance all match `data/latest/kelly_strategy_stats.json`.

- [ ] **Step 5: Update the changelog**

Add a dated entry stating that Kelly trade evidence and runtime strategy stats
are separated, UI/order sizing now share one source, and invalid or stale stats
fail closed for entries.

- [ ] **Step 6: Commit migration artifacts and documentation**

```bash
git add data/latest/kelly_strategy_stats.json CHANGELOG.md
git commit -m "docs: record unified kelly stats workflow"
```

- [ ] **Step 7: Review the final branch**

Invoke `superpowers:requesting-code-review`, address findings with focused tests,
then invoke `superpowers:verification-before-completion`. Report exact test
counts, Playwright results, real command outputs, and the restarted dashboard
PID/timestamp before claiming completion.
