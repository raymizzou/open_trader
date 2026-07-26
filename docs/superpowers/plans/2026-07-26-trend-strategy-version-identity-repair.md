# Trend Strategy Version Identity Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the ETF-enabled strategy parameters under CN v10 and US/HK v7 while preserving audited drawdown and Kelly history.

**Architecture:** Reuse the existing versioned snapshot, explicit Kelly identity, and approved drawdown predecessor mechanisms. Update only their version tables and guards; keep historical v9/v6 behavior replayable. Repoint the merged notification tests at malformed frozen artifacts so missing artifacts remain the accepted `SKIPPED` case.

**Tech Stack:** Python 3.12, pytest, existing JSON drawdown state and trend snapshot modules.

## Global Constraints

- CN v10, US v7, and HK v7 are effective from 2026-07-27.
- New drawdown records inherit only from CN v9, US v6, and HK v6 respectively.
- New Kelly identities inherit only the explicitly approved same-market identities.
- Historical snapshots remain replayable and unchanged.
- Missing frozen baselines remain `SKIPPED`; malformed baselines remain `FAIL`.
- Do not add a same-version compatibility override, live NAV rebase, abstraction, dependency, or configuration layer.
- Run `make acceptance` only once as the final Dashboard gate.

---

### Task 1: Reconcile Merged Alert Tests With Skippable Baselines

**Files:**
- Modify: `tests/test_drawdown_preflight.py`
- Test: `tests/test_drawdown_preflight.py`

**Interfaces:**
- Consumes: `write_report(root: Path, market: str, state_status: str) -> Path`
- Produces: notification tests whose failure trigger is `baseline_invalid`

- [ ] **Step 1: Preserve the current red evidence**

Run:

```bash
.venv/bin/pytest -q tests/test_drawdown_preflight.py tests/test_strategy_drawdown_cli.py
```

Expected: three notification tests fail because an absent baseline now returns
`ready` with market status `skipped`.

- [ ] **Step 2: Make the notification fixtures genuinely invalid**

Before the failing request in each of these tests, write a matching-date report
without the required strategy/account baseline fields:

```python
for market in ("CN", "HK", "US"):
    write_report(tmp_path, market, "missing")
```

Apply this to:

```python
test_failure_alert_is_grouped_deduplicated_and_rearmed_after_recovery
test_notification_failure_does_not_change_fail_closed_result
test_null_notifier_does_not_record_alert_delivery
```

In the grouped-alert expectation, use:

```python
"- CN v4：回撤预检失败",
"- HK v4：回撤预检失败",
"- US v4：回撤预检失败",
```

and:

```python
[
    "CN|v4|baseline_invalid",
    "HK|v4|baseline_invalid",
    "US|v4|baseline_invalid",
]
```

- [ ] **Step 3: Run the focused merged tests**

Run:

```bash
.venv/bin/pytest -q tests/test_drawdown_preflight.py tests/test_strategy_drawdown_cli.py
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_drawdown_preflight.py
git commit -m "test: alert only on invalid drawdown baselines"
```

---

### Task 2: Publish New Snapshot and Kelly Identities

**Files:**
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_trend_kelly.py`
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/trend_kelly.py`

**Interfaces:**
- Consumes: `live_trend_strategy_snapshot(...) -> dict[str, object]`
- Consumes: `trend_kelly_identity_matches(sample_identity, target_identity) -> bool`
- Produces: current CN v10 and US/HK v7 snapshots with exact same-market Kelly inheritance

- [ ] **Step 1: Update snapshot tests first**

Change the current-version expectations to:

```python
("CN", "v10", ("v4", "v7", "v8", "v9", "v10"))
("US", "v7", ("v4", "v5", "v6", "v7"))
("HK", "v7", ("v4", "v5", "v6", "v7"))
```

Rename the default-version tests accordingly, keep:

```python
assert snapshot["effective_from"] == "2026-07-27"
```

and extend replay coverage to:

```python
@pytest.mark.parametrize("version", ["v4", "v6", "v7", "v8", "v9", "v10"])
```

for CN, and:

```python
@pytest.mark.parametrize("version", ["v4", "v5", "v6", "v7"])
```

for US/HK. Preserve historical behavior with:

```python
for market, version, pools in (
    ("CN", "v9", (622466, 697199)),
    ("US", "v6", (622460,)),
    ("HK", "v6", (622494,)),
):
    snapshot = trend_module.live_trend_strategy_snapshot(
        market, "abc123", pools, strategy_version=version
    )
    assert snapshot["effective_from"] == "2026-07-27"
    assert snapshot["parameters"]["candidate_pool_ids"] == list(pools)
    assert snapshot["parameters"]["exit_reasons"] == [
        "danger", "left_right_side", "temperature_to_flat", "protection",
    ]
    if market == "CN":
        assert snapshot["parameters"]["allowed_assets"] == ["A股", "ETF基金"]
```

- [ ] **Step 2: Add exact Kelly identity tests**

Add a parameterized test:

```python
@pytest.mark.parametrize(
    ("market", "target_version", "accepted_versions"),
    [
        ("CN", "v10", ("v4", "v7", "v8", "v9", "v10")),
        ("US", "v7", ("v4", "v5", "v6", "v7")),
        ("HK", "v7", ("v4", "v5", "v6", "v7")),
    ],
)
def test_repaired_strategy_kelly_identity_inheritance_is_exact(
    market: str,
    target_version: str,
    accepted_versions: tuple[str, ...],
) -> None:
    target = (
        market,
        f"trend_animals_warm_to_hot/{market}/{target_version}",
        target_version,
    )
    for version_number in range(1, 11):
        version = f"v{version_number}"
        assert trend_kelly_identity_matches(
            (
                market,
                f"trend_animals_warm_to_hot/{market}/{version}",
                version,
            ),
            target,
        ) is (version in accepted_versions)
    assert not trend_kelly_identity_matches(
        (
            "US" if market != "US" else "HK",
            f"trend_animals_warm_to_hot/{'US' if market != 'US' else 'HK'}/v4",
            "v4",
        ),
        target,
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_a_share_trend.py \
  tests/test_trend_kelly.py
```

Expected: failures show current snapshots still publish v9/v6 and the new
Kelly target identities are not registered.

- [ ] **Step 4: Implement the minimal snapshot version changes**

In `src/open_trader/a_share_trend.py`:

```python
CURRENT_TREND_STRATEGY_VERSIONS = {"CN": "v10", "US": "v7", "HK": "v7"}
CURRENT_EXIT_DISCIPLINES = frozenset({
    ("CN", "v9"),
    ("CN", "v10"),
    ("US", "v6"),
    ("US", "v7"),
    ("HK", "v6"),
    ("HK", "v7"),
})
```

Allow v10 only for CN and v7 for all three markets:

```python
if (
    version not in {"v4", "v5", "v6", "v7", "v8", "v9", "v10"}
    or version in {"v8", "v9", "v10"} and market != "CN"
    or version == "v5" and market == "CN"
):
    raise ValueError("unsupported live trend strategy version")
```

Add v10 to the existing version sets in
`_expected_report_strategy_snapshot`, report Kelly calculation, the drawdown
entry gate, Markdown Kelly rendering, `validate_report_strategy_snapshot`,
risk-contract selection, drawdown-decision validation, and Kelly-capped sizing.
Map v10 to `valid_v4_risk_contract`. Preserve the existing v9/v6 exit
discipline and 2026-07-27 effective date through
`CURRENT_EXIT_DISCIPLINES`.

Use these explicit parameter rules:

```python
if market == "CN" and version in {"v9", "v10"}:
    parameters["allowed_assets"] = ["A股", "ETF基金"]

if market == "CN" and version == "v7":
    inherited_versions = ("v4",)

if market == "CN" and version == "v10":
    inherited_versions = ("v4", "v7", "v8", "v9", "v10")

if market in {"US", "HK"} and version == "v7":
    inherited_versions = ("v4", "v5", "v6", "v7")
```

Ensure `_candidate_reasons` admits `ETF基金` for both CN v9 and v10. Keep the
ranking/audit rows enabled for CN v10 and US/HK v7.

- [ ] **Step 5: Extend the explicit Kelly map**

In `src/open_trader/trend_kelly.py`, add `CN_V10_KELLY_IDENTITY`,
`US_V7_KELLY_IDENTITY`, and `HK_V7_KELLY_IDENTITY`, then add:

```python
CN_V10_KELLY_IDENTITY: frozenset({
    CN_V4_KELLY_IDENTITY,
    CN_V7_KELLY_IDENTITY,
    CN_V8_KELLY_IDENTITY,
    CN_V9_KELLY_IDENTITY,
    CN_V10_KELLY_IDENTITY,
})
```

and equivalent US/HK v7 sets containing their v4, v5, v6, and v7 identities.

- [ ] **Step 6: Run focused snapshot and Kelly tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_a_share_trend.py \
  tests/test_trend_kelly.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/trend_kelly.py \
  tests/test_a_share_trend.py \
  tests/test_trend_kelly.py
git commit -m "fix: publish ETF strategy version identities"
```

---

### Task 3: Inherit Audited Drawdown State

**Files:**
- Modify: `tests/test_drawdown_preflight.py`
- Modify: `src/open_trader/drawdown_preflight.py`

**Interfaces:**
- Consumes: `APPROVED_DRAWDOWN_PREDECESSORS`
- Produces: exact new-to-old mapping for the existing `inherit_from` bootstrap path

- [ ] **Step 1: Update the predecessor inheritance test**

Change:

```python
target_versions = {"CN": "v10", "HK": "v7", "US": "v7"}
```

and seed:

```python
old_versions = {"CN": "v9", "HK": "v6", "US": "v6"}
```

Keep the assertions that high-water marks remain `100`, `200`, and `300`, and
current equity remains `94`, `188`, and `282`.

Update the missing-predecessor test to request CN v10 while only CN v8 exists.
Keep the existing assertion for
`approved predecessor drawdown state is unavailable` and unchanged state
bytes.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_drawdown_preflight.py::test_new_strategy_versions_inherit_approved_predecessor_high_water_marks \
  tests/test_drawdown_preflight.py::test_missing_approved_predecessor_fails_closed_without_writing_state
```

Expected: FAIL because v10/v7 have no approved predecessor mapping.

- [ ] **Step 3: Add the exact mappings**

In `src/open_trader/drawdown_preflight.py`, retain historical mappings and add:

```python
("CN", "v10"): ("trend_animals_warm_to_hot/CN/v9", "v9"),
("US", "v7"): ("trend_animals_warm_to_hot/US/v6", "v6"),
("HK", "v7"): ("trend_animals_warm_to_hot/HK/v6", "v6"),
```

- [ ] **Step 4: Run the full drawdown preflight suites**

Run:

```bash
.venv/bin/pytest -q tests/test_drawdown_preflight.py tests/test_strategy_drawdown_cli.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/drawdown_preflight.py tests/test_drawdown_preflight.py
git commit -m "fix: inherit drawdown state into repaired versions"
```

---

### Task 4: Verify and Prepare the Final Dashboard Gate

**Files:**
- Modify: `CHANGELOG.md`
- Test: full repository and live drawdown preflight

**Interfaces:**
- Consumes: current strategy snapshots and persisted drawdown state
- Produces: review-ready commit only if the final Dashboard gate returns `PASS`

- [ ] **Step 1: Run all focused tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_a_share_trend.py \
  tests/test_trend_kelly.py \
  tests/test_drawdown_preflight.py \
  tests/test_strategy_drawdown_cli.py
```

Expected: all tests pass.

- [ ] **Step 2: Run the full automated suite**

Run:

```bash
make test
```

Expected: exit 0 with no failures.

- [ ] **Step 3: Run the real preflight**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-drawdown-preflight \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --repo /Users/ray/projects/open_trader/.worktrees/acceptance-skip-missing-baseline \
  --actor acceptance
```

Expected: top-level `ready`; CN v10 and US/HK v7 are `bootstrapped` or `ready`,
with inherited high-water marks and no `parameter_mismatch`.

- [ ] **Step 4: Update and commit the operator log**

Add to the existing `## 2026-07-26` section in `CHANGELOG.md`:

```markdown
- Published the ETF-enabled parameters under CN v10 and US/HK v7, inheriting
  audited drawdown high-water marks and approved Kelly samples from v9/v6;
  missing frozen baselines remain skippable while malformed baselines fail.
```

Then:

```bash
git add CHANGELOG.md
git commit -m "docs: log repaired trend strategy versions"
```

- [ ] **Step 5: Run the final Dashboard gate**

Run only after all source and documentation commits:

```bash
make acceptance
```

Expected: literal `PASS`. On `FAIL`, fix and rerun. On `BLOCKED`, report the
external blocker and do not substitute mocks, curl, fixtures, or screenshots.

- [ ] **Step 6: Redeploy the exact accepted SHA**

After `PASS`, record the accepted SHA and current log size, replace the existing
Dashboard screen with the accepted worktree, and verify:

```bash
trend_accepted_sha=$(git rev-parse HEAD)
trend_log_size=$(stat -f '%z' /tmp/open_trader_dashboard_8766.log 2>/dev/null || echo 0)
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/acceptance-skip-missing-baseline && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
dashboard_review_pid=$(lsof -tiTCP:8766 -sTCP:LISTEN)
test "$(git rev-parse HEAD)" = "$trend_accepted_sha"
ps -o pid=,lstart=,command= -p "$dashboard_review_pid"
lsof -a -p "$dashboard_review_pid" -d cwd
tail -c "+$((trend_log_size + 1))" /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8766/
```

Require cwd
`/Users/ray/projects/open_trader/.worktrees/acceptance-skip-missing-baseline`,
the exact accepted SHA, fresh logs without a traceback, and HTTP 200 before
giving `http://127.0.0.1:8766/` to the user.
