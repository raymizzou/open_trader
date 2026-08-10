# Standard Polymarket WebSocket Eligibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the full Top 20 display universe and five-minute REST diagnostics while limiting standard YES/NO WebSocket ownership to markets admitted by the existing execution metadata policy.

**Architecture:** Add one shared WebSocket eligibility predicate to `PolymarketMonitor` and use it in full-universe and targeted metadata refreshes. Preserve the three-layer token union, guard a prior non-empty pool from ambiguous empty replacement, expose accurate counts, and reuse the existing Dashboard metric.

**Tech Stack:** Python 3.12, asyncio, pytest, vanilla JavaScript, launchd, Playwright acceptance

## Global Constraints

- Eligibility is exactly `fees_enabled is False` and `neg_risk is not True`.
- Eligible markets retain second-level WS discovery and paired REST confirmation.
- Top 20 display and five-minute paired-book refresh stay unchanged.
- Add no profit, liquidity, volume, book-availability, or one-leg filter.
- Relation, cross-venue, execution, notification, readiness, and account policy stay unchanged.
- Another layer may retain a token removed from standard ownership.
- Preserve a prior non-empty pool only for an ambiguous empty replacement.
- Add no toggle, config, persistence, dependency, component, or fixed traffic target.
- UI copy is exactly `市场 / 实时 Token` and `不可参与市场定时刷新`.
- Run `make acceptance` only as the final Dashboard gate.
- Commit `CHANGELOG.md` before merge.

## File Map

- `src/open_trader/polymarket_monitor.py`: eligibility, ownership, anomaly guard, metrics.
- `tests/test_polymarket_monitor.py`: monitor regressions.
- `src/open_trader/dashboard_static/dashboard.js`: existing metric value and copy.
- `tests/test_dashboard_web.py`: UI truthfulness regression.
- `CHANGELOG.md`: merge log.

---

### Task 1: Gate the full-universe standard token map

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py:1216-1267, 3186-3242`
- Test: `tests/test_polymarket_monitor.py:3436-3483, 3790-3825`

**Interfaces:**
- Consumes: normalized rows from `_normalize_market(...)`.
- Produces: `_standard_market_websocket_eligible(market: Mapping[str, object]) -> bool`.

- [ ] **Step 1: Replace the broad-subscription regression**

Replace `test_only_exact_active_binary_markets_are_subscribed_and_books_match_by_token_id` with:

```python
def test_only_execution_eligible_active_binary_markets_are_subscribed(
    tmp_path: Path,
) -> None:
    good = market("good", yes="yes-good", no="no-good")
    fee = market("fee", yes="yes-fee", no="no-fee", fees_enabled=True)
    unknown = market(
        "unknown", yes="yes-unknown", no="no-unknown", fees_enabled=None
    )
    neg = market("neg", yes="yes-neg", no="no-neg", neg_risk=True)
    malformed = ns(
        id="malformed",
        state=ns(active=True, closed=False),
        outcomes=[ns(label="YES", token_id="yes")],
    )
    setup_public([event("e", markets=(good, fee, unknown, neg, malformed))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    snapshot = monitor.snapshot()
    assert FakePublicClient.book_calls == [
        ["yes-good", "no-good"],
        ["yes-fee", "no-fee"],
        ["yes-unknown", "no-unknown"],
        ["yes-neg", "no-neg"],
    ]
assert tuple(FakePublicClient.subscribe_specs[-1].token_ids) == (
    "no-good",
    "yes-good",
)
rows = {row["market_id"]: row for row in snapshot["events"][0]["markets"]}
assert set(rows) == {"good", "fee", "unknown", "neg"}
assert rows["fee"]["eligibility_reason"] == "fee_unverified_or_enabled"
assert rows["unknown"]["eligibility_reason"] == "fee_unverified_or_enabled"
assert rows["neg"]["eligibility_reason"] == "neg_risk"
assert snapshot["diagnostics"]["malformed_markets"] == 1
```

- [ ] **Step 2: Prove candidate distance and book failure do not shrink eligible ownership**

```python
def test_execution_eligible_market_stays_subscribed_without_threshold_candidate(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m", yes="yes-m", no="no-m"),))])
    FakePublicClient.books["yes-m"].asks = [
        ns(price=Decimal("0.50"), size=Decimal("20"))
    ]
    FakePublicClient.books["no-m"].asks = [
        ns(price=Decimal("0.50"), size=Decimal("20"))
    ]
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert monitor.snapshot()["events"][0]["markets"][0]["eligibility_reason"] == "no_threshold_candidate"
    assert tuple(FakePublicClient.subscribe_specs[-1].token_ids) == ("no-m", "yes-m")


def test_execution_eligible_market_stays_subscribed_when_book_read_fails(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m", yes="yes-m", no="no-m"),))])
    FakePublicClient.fail_get_order_books = True
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert tuple(FakePublicClient.subscribe_specs[-1].token_ids) == ("no-m", "yes-m")


def test_execution_eligible_websocket_tick_still_confirms_paired_books(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    FakePublicClient.book_calls.clear()
    asyncio.run(monitor._process_stream_event(
        FakePublicClient(),
        ns(type="price_change", payload=ns(asset_id="yes-1", price_changes=())),
    ))
    assert FakePublicClient.book_calls == [["yes-1", "no-1"]]
```

- [ ] **Step 3: Add ambiguous-empty and legitimate-empty tests**

```python
def test_ambiguous_empty_standard_pool_preserves_prior_subscription(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m", yes="yes-old", no="no-old"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    previous_tokens = dict(monitor._market_by_token)
    setup_public([event("e", markets=(market(
        "m", yes="yes-unknown", no="no-unknown", fees_enabled=None
    ),))])
    monitor.refresh_once()
    snapshot = monitor.snapshot()
    assert monitor._market_by_token == previous_tokens
    assert snapshot["health"]["status"] == "degraded"
    assert "universe_refresh_failed" in snapshot["health"]["degraded_reasons"]


def test_explicitly_ineligible_universe_accepts_empty_standard_pool(tmp_path: Path) -> None:
    setup_public([event("e", markets=(
        market("fee", fees_enabled=True),
        market("neg", yes="yes-neg", no="no-neg", neg_risk=True),
    ))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    assert monitor._market_by_token == {}
    assert FakePublicClient.subscribe_specs == []
    assert monitor.snapshot()["health"]["status"] == "healthy"


def test_unchanged_universe_token_union_does_not_reconnect(tmp_path: Path) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path, relation_discovery=None)
    client = FakePublicClient()
    asyncio.run(monitor._refresh_universe(client))
    first_handle = monitor._stream_handle
    asyncio.run(monitor._refresh_universe(client))
    assert len(FakePublicClient.subscribe_specs) == 1
    assert monitor._stream_handle is first_handle
```

- [ ] **Step 4: Run new tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_polymarket_monitor.py -k \
  'only_execution_eligible or stays_subscribed or eligible_websocket_tick or ambiguous_empty or explicitly_ineligible_universe or unchanged_universe_token_union'
```

Expected: broad ownership and ambiguous-empty replacement violate assertions.

- [ ] **Step 5: Implement the predicate and gated map**

```python
@staticmethod
def _standard_market_websocket_eligible(market: Mapping[str, object]) -> bool:
    return market.get("fees_enabled") is False and market.get("neg_risk") is not True
```

In `_refresh_universe`:

```python
markets[market_id] = market_row
if self._standard_market_websocket_eligible(market_row):
    token_map[str(market_row["yes_token_id"])] = market_id
    token_map[str(market_row["no_token_id"])] = market_id
```

Before publishing replacement maps, capture the old three-layer union; after
publishing, set `_subscription_dirty` only when the union changed:

```python
explicitly_ineligible = bool(markets) and all(
    row.get("fees_enabled") is True or row.get("neg_risk") is True
    for row in markets.values()
)
with self._lock:
    prior_standard_pool = bool(self._market_by_token)
    previous_union = (
        set(self._market_by_token)
        | set(self._relation_by_token)
        | self._cross_venue_tokens
    )
if prior_standard_pool and not token_map and not explicitly_ineligible:
    raise RuntimeError("ambiguous empty standard websocket pool")

with self._lock:
    previous_opportunity_rows = copy.deepcopy(self._opportunities)
    previous_opportunities = set(previous_opportunity_rows)
    self._events = {str(item["event_id"]): item for item in normalized}
    self._markets = markets
    self._market_by_token = token_map
    self._diagnostics["malformed_events"] = malformed_events
    self._universe_at = self._now()
    self._universe_failed = False
    current_union = (
        set(self._market_by_token)
        | set(self._relation_by_token)
        | self._cross_venue_tokens
    )
    if current_union != previous_union:
        self._subscription_dirty = True
```

Leave `asyncio.gather` paired-book confirmation unfiltered. Replace the final
direct `_subscribe(client)` call with `_refresh_subscription_if_dirty(client)`
so an unchanged union keeps its current stream.

- [ ] **Step 6: Keep chunking coverage meaningful**

Remove `fees_enabled=True` from `test_large_token_universe_is_subscribed_in_websocket_safe_chunks`; preserve its `[250, 2]` assertions.

- [ ] **Step 7: Run and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_polymarket_monitor.py
git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
git commit -m "perf: gate standard websocket subscriptions"
```

Expected: all monitor tests pass.

---

### Task 2: Keep targeted metadata refresh consistent

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py:857-911`
- Test: `tests/test_polymarket_monitor.py:3555-3571`

**Interfaces:**
- Consumes: `_standard_market_websocket_eligible(...)` from Task 1.
- Produces: promotion/demotion that dirties only a changed three-layer union.

- [ ] **Step 1: Extend targeted demotion**

Set `monitor._subscription_dirty = False` before `refresh_opportunity` and append:

```python
assert monitor._market_by_token == {}
assert monitor._subscription_dirty is True
```

- [ ] **Step 2: Add promotion and shared-ownership tests**

```python
def test_targeted_metadata_refresh_promotes_newly_eligible_market(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m", fees_enabled=True),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    current = dict(monitor._markets["m"])
    monitor._subscription_dirty = False
    FakePublicClient.events = [event("e", markets=(market("m"),))]
    asyncio.run(monitor._refresh_standard_market_metadata(FakePublicClient(), current))
    assert set(monitor._market_by_token) == {"yes-1", "no-1"}
    assert monitor._subscription_dirty is True


def test_targeted_demotion_does_not_reconnect_when_other_layer_owns_tokens(
    tmp_path: Path,
) -> None:
    setup_public([event("e", markets=(market("m"),))])
    monitor = make_monitor(tmp_path)
    monitor.refresh_once()
    current = dict(monitor._markets["m"])
    monitor._relation_by_token = {
        "yes-1": {"relation"},
        "no-1": {"relation"},
    }
    monitor._subscription_dirty = False
    FakePublicClient.events = [event("e", markets=(market("m", fees_enabled=True),))]
    asyncio.run(monitor._refresh_standard_market_metadata(FakePublicClient(), current))
    assert monitor._market_by_token == {}
    assert monitor._subscription_dirty is False
```

- [ ] **Step 3: Run tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_polymarket_monitor.py -k \
  'targeted_standard or targeted_metadata or targeted_demotion'
```

Expected: current code always re-adds standard ownership.

- [ ] **Step 4: Gate ownership and compare final union**

Inside the existing lock:

```python
previous_union = (
    set(self._market_by_token)
    | set(self._relation_by_token)
    | self._cross_venue_tokens
)
previous = self._markets.get(market_id, {})
for token in (
    str(previous.get("yes_token_id", "")),
    str(previous.get("no_token_id", "")),
):
    if self._market_by_token.get(token) == market_id:
        self._market_by_token.pop(token, None)
self._markets[market_id] = market_row
if self._standard_market_websocket_eligible(market_row):
    self._market_by_token[str(market_row["yes_token_id"])] = market_id
    self._market_by_token[str(market_row["no_token_id"])] = market_id
current_union = (
    set(self._market_by_token)
    | set(self._relation_by_token)
    | self._cross_venue_tokens
)
if current_union != previous_union:
    self._subscription_dirty = True
```

Keep event-row replacement in the same lock. Do not subscribe directly here.

- [ ] **Step 5: Run and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_polymarket_monitor.py -k 'targeted_standard or targeted_metadata or subscription'
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_polymarket_monitor.py
git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
git commit -m "fix: refresh standard websocket ownership safely"
```

Expected: both test commands pass.

---

### Task 3: Report truthful subscription metrics

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py:687-699`
- Modify: `src/open_trader/dashboard_static/dashboard.js:2650-2671`
- Test: `tests/test_polymarket_monitor.py:582-614`
- Test: `tests/test_dashboard_web.py:3602-3668`

**Interfaces:**
- Consumes: all three token maps.
- Produces: `standard_subscribed_tokens: int` and corrected three-layer `subscribed_tokens: int`.

- [ ] **Step 1: Add monitor assertions**

Append to `test_cross_venue_tokens_join_existing_subscription_and_refresh_once`:

```python
websocket = monitor.snapshot()["relation_discovery"]["websocket"]
assert websocket["standard_subscribed_tokens"] == 1
assert websocket["subscribed_tokens"] == 4
```

- [ ] **Step 2: Add Dashboard assertions**

Put `standard_subscribed_tokens:46` in the healthy payload's websocket state and assert:

```python
assert "市场 / 实时 Token" in rendered["healthyMetrics"]
assert "240 / 46" in rendered["healthyMetrics"]
assert "不可参与市场定时刷新" in rendered["healthyMetrics"]
assert "不可参与市场仍持续监控" not in rendered["healthyMetrics"]
```

- [ ] **Step 3: Run both tests and verify RED**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_polymarket_monitor.py::test_cross_venue_tokens_join_existing_subscription_and_refresh_once \
  tests/test_dashboard_web.py::test_prediction_market_layout_a_uses_binary_health_and_four_truthful_metrics
```

Expected: missing standard count, undercounted union, old UI copy.

- [ ] **Step 4: Correct monitor state**

```python
standard_tokens = set(self._market_by_token)
combined_tokens = (
    standard_tokens | set(self._relation_by_token) | self._cross_venue_tokens
)
return {
    "status": "connected" if connected else "disconnected",
    "standard_subscribed_tokens": len(standard_tokens),
    "subscribed_tokens": len(combined_tokens),
    "last_message_at": self._stream_message_at,
    "last_message_age_seconds": _display_age(_age(now, self._stream_message_at)),
    "connected_at": self._stream_connected_at,
}
```

- [ ] **Step 5: Reuse the existing Dashboard card**

```javascript
const standardTokenCount = payload?.relation_discovery?.websocket?.standard_subscribed_tokens;
const tokenCount = predictionHasValue(standardTokenCount)
  ? predictionNumber(standardTokenCount)
  : "-";
```

Change available and unavailable labels to `市场 / 实时 Token` and healthy helper to `不可参与市场定时刷新`. Do not change card count, CSS, layout, or interaction.

- [ ] **Step 6: Run and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_polymarket_monitor.py tests/test_dashboard_web.py
git add \
  src/open_trader/polymarket_monitor.py \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py
git commit -m "feat: report standard realtime subscriptions"
```

Expected: both affected test files pass.

---

### Task 4: Verify and satisfy the merge-log gate

**Files:**
- Modify: `CHANGELOG.md:6-25`
- Verify: implementation and test files from Tasks 1-3.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: clean committed branch with exact evidence.

- [ ] **Step 1: Run diff guards**

```bash
git diff --check
git status --short
git diff main...HEAD -- \
  src/open_trader/polymarket_monitor.py \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_polymarket_monitor.py \
  tests/test_dashboard_web.py
```

Expected: no whitespace errors or unrelated implementation files.

- [ ] **Step 2: Run affected and full Python suites**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_polymarket_monitor.py tests/test_dashboard_web.py
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
```

Expected: both pass; record exact counts and warnings.

- [ ] **Step 3: Run branch public diagnostic**

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python \
  -m open_trader prediction-arb monitor-once \
  --config /Users/ray/projects/open_trader/config/prediction_arbitrage.json \
  --data-dir /Users/ray/projects/open_trader/data \
  --timeout 30
```

Expected: `mutations: 0` and `result: PASS`.

- [ ] **Step 4: Add dated changelog entry**

After Step 2 and Step 3 pass, add this bullet under `## 2026-08-10`:

```markdown
- 普通 YES/NO WebSocket 只保留当前执行规则允许的免手续费、非明确 neg-risk 市场；Top 20 展示与五分钟 REST 诊断保持不变，关系和跨所 token 仍独立保留。看板现显示标准层实时 Token，异常空池保留上一订阅并 fail-closed。验证：标准监控与 Dashboard 聚焦测试、完整 Python 套件及非变更 public monitor diagnostic 均通过。
```

- [ ] **Step 5: Commit before merge**

```bash
git add CHANGELOG.md
git commit -m "docs: log standard websocket eligibility"
git status --short --branch
```

Expected: clean branch.

---

### Task 5: Merge, accept, redeploy, and classify traffic

**Files:**
- Verify: `CHANGELOG.md`
- Verify: `logs/frontend_gateway/launchd.out.log`
- Verify: `logs/legacy_dashboard/launchd.out.log`
- Verify: `http://127.0.0.1:8766/api/prediction-arbitrage/state`

**Interfaces:**
- Consumes: clean implementation branch and launchd stack.
- Produces: local `main` on exact accepted SHA, HTTP 200, audited counts, traffic classification. No push.

- [ ] **Step 1: Capture old state and three equal traffic windows**

```bash
curl -fsS --max-time 30 \
  http://127.0.0.1:8766/api/prediction-arbitrage/state \
  | jq '{status, market_count, token_count, websocket: .relation_discovery.websocket, activity: .relation_discovery.activity.status}'
launchctl print "gui/$(id -u)/com.open-trader.legacy-dashboard" \
  | rg 'pid =|working directory|program ='
```

For each of three 60-second windows, record activity state before and after:

```bash
curl -fsS --max-time 30 http://127.0.0.1:8766/api/prediction-arbitrage/state \
  | jq -r '.relation_discovery.activity.status'
ssh -o BatchMode=yes root@43.129.247.179 '
  sample_rx_start=$(tr -d "\n" < /sys/class/net/eth0/statistics/rx_bytes)
  sample_tx_start=$(tr -d "\n" < /sys/class/net/eth0/statistics/tx_bytes)
  sleep 60
  sample_rx_end=$(tr -d "\n" < /sys/class/net/eth0/statistics/rx_bytes)
  sample_tx_end=$(tr -d "\n" < /sys/class/net/eth0/statistics/tx_bytes)
  awk -v bytes="$((sample_rx_end-sample_rx_start+sample_tx_end-sample_tx_start))" \
    "BEGIN { printf \"%.2f KiB/s\\n\", bytes / 60 / 1024 }"
'
curl -fsS --max-time 30 http://127.0.0.1:8766/api/prediction-arbitrage/state \
  | jq -r '.relation_discovery.activity.status'
```

Do not compare scan-heavy with steady windows.

- [ ] **Step 2: Merge after checking both worktrees and changelog**

```bash
git status --short --branch
git -C /Users/ray/projects/open_trader status --short --branch
git -C /Users/ray/projects/open_trader merge --no-ff \
  codex/standard-market-websocket-gate-design
```

Expected: conflict-free local merge with changelog. Do not push.

- [ ] **Step 3: Deploy merged candidate**

```bash
/Users/ray/projects/open_trader/scripts/install_dashboard_launchd.sh \
  --mode stack \
  --repo-root /Users/ray/projects/open_trader \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: installer checks pass and HTTP is `200`.

- [ ] **Step 4: Run final acceptance once**

```bash
cd /Users/ray/projects/open_trader
PYTHON_BIN=/Users/ray/projects/open_trader/.venv/bin/python make acceptance
```

Expected: `PASS`. On `FAIL`, fix and restart Task 4 verification. On `BLOCKED`, report the blocker and do not substitute curl, fixtures, mocks, tests, or screenshots.

- [ ] **Step 5: Redeploy exact accepted SHA and verify identity**

```bash
accepted_sha=$(git -C /Users/ray/projects/open_trader rev-parse HEAD)
/Users/ray/projects/open_trader/scripts/install_dashboard_launchd.sh \
  --mode stack \
  --repo-root /Users/ray/projects/open_trader \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
launchctl print "gui/$(id -u)/com.open-trader.frontend-gateway" | rg 'pid =|state ='
launchctl print "gui/$(id -u)/com.open-trader.legacy-dashboard" | rg 'pid =|state ='
curl -fsS --max-time 30 http://127.0.0.1:8766/api/prediction-arbitrage/state \
  | jq '{status, websocket: .relation_discovery.websocket}'
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
rg -n "$accepted_sha|dashboard_runtime|frontend_gateway_runtime" \
  /Users/ray/projects/open_trader/logs/frontend_gateway/launchd.out.log \
  /Users/ray/projects/open_trader/logs/legacy_dashboard/launchd.out.log | tail -20
```

Expected: fresh PIDs, exact cwd/SHA in fresh logs, healthy state with `standard_subscribed_tokens`, HTTP `200`.

- [ ] **Step 6: Repeat post-deploy windows and classify**

Repeat Step 1 exactly. Compare steady with steady and scanning with scanning using average or median:

- `effective`: comparable post-deploy windows consistently lower;
- `inconclusive`: ranges overlap or direction reverses;
- `ineffective`: comparable post-deploy windows consistently not lower.

Do not claim effectiveness from token counts alone. If inconclusive or ineffective, report remaining relation/REST/other baseline and stop before adding another optimization.

## Rollback

Do not add a feature flag. Resolve the prior accepted SHA from pre-deploy evidence, create a detached deployment worktree for it, deploy it through `scripts/install_dashboard_launchd.sh --mode stack` with root data as `--runtime-root`, then verify fresh PIDs, prior SHA, and HTTP `200`. Leave `main` history intact.
