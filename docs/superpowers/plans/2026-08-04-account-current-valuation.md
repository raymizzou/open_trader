# Account Current Valuation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make accepted OpenD prices and complete USD/HKD valuations authoritative for every quoteable real Account position and every Trend simulated position, while preserving the frozen Account v1 fail-closed behavior.

**Architecture:** Keep Account Sync Worker as the only Account/quote adapter caller and publisher. Reuse its existing accepted Account state, OpenD quote service, FX inputs, Account API snapshot, Gateway route, and Dashboard renderer; add no service, endpoint, cache, dependency, or client-side valuation. Trend keeps ownership of simulated positions and publishes the same five-field valuation shape from its existing service.

**Tech Stack:** Python 3.12, `Decimal`, pytest, vanilla JavaScript, Playwright-based Dashboard acceptance, launchd.

## Global Constraints

- Start implementation from this branch, which is based on merged Issue #21 at `main@ff2b2f05`.
- Preserve Account v1 price precedence: current accepted OpenD quote, then retained accepted quote; if neither exists for a required instrument, return `503`. Never promote a statement/account snapshot price to a current quote.
- Preserve every existing flat v1 field's meaning. In particular, flat `market_value_usd` remains empty for non-USD positions.
- The optional public compatibility object is exactly `current_valuation` with `price`, `price_kind`, `price_as_of`, `market_value_usd`, and `market_value_hkd`. A new Worker publication supplies all five fields for every quoteable stock/ETF/fund/option/unknown US/HK/CN position or supplies no new projection. `cash` and `money_market_fund` positions remain unchanged.
- Accepted Account positions are the only production quote-universe source. `DashboardQuoteService.refresh` has no no-argument or `portfolio.csv` fallback.
- HK/CN quote time comes from OpenD `update_time`, normalized with the market timezone; use refresh time only when it is absent.
- Runtime may serve retained quotes as `200 stale` without a new hard age cap, but release acceptance requires one fresh complete OpenD refresh from the candidate Worker.
- A newly accepted position may create a brief fail-closed `503` until its first quote cycle; the browser must keep the previous snapshot and recover automatically.
- Do not change Gateway routing, Dashboard layout, Account polling, Trend strategy/report/allocation/execution behavior, cash valuation, or rollback topology. The only Dashboard interaction change is the approved transient update highlight on the three existing valuation cells.
- Do not add an abstraction layer. Reuse `FutuQuoteUniverse`, `DashboardQuoteService`, Account projection validation, existing `Decimal` money rounding, and the current browser row renderer.
- Write each behavior test first, observe the named failure, implement only enough to pass, and commit at the end of each task.
- Use the repository interpreter consistently:

  ```bash
  export OPEN_TRADER_ROOT=/Users/ray/projects/open_trader/.worktrees/account-current-valuation-plan
  export OPEN_TRADER_PYTHON="${OPEN_TRADER_PYTHON:-/Users/ray/projects/open_trader/.venv/bin/python}"
  cd "$OPEN_TRADER_ROOT"
  test -x "$OPEN_TRADER_PYTHON"
  export PYTHONPATH="$OPEN_TRADER_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  ```

---

## Task 1: Derive the OpenD quote universe from accepted Account positions

**Interfaces**

- Consumes: normalized `account_sync_state.json` returned by `load_account_sync_state`.
- Produces: the existing `FutuQuoteUniverse`; `DashboardQuoteService.refresh(universe)` requires it from Account Sync Worker.
- Removes: the CSV quote-universe loader, statement-only CSV exclusion, and no-argument production fallback. `portfolio.csv` itself remains available to its other owners.

**Files**

- Modify: `src/open_trader/futu_universe.py`
- Modify: `src/open_trader/dashboard_quotes.py:143` (`DashboardQuoteService.refresh`)
- Modify: `src/open_trader/account_sync_worker.py:180` (`sync_quotes_once`)
- Test: `tests/test_futu_universe.py`
- Test: `tests/test_dashboard_quotes.py`
- Test: `tests/test_account_sync_worker.py`

- [ ] **Step 1: Add failing accepted-state universe tests**

  Add tests proving that the new builder:

  - includes accepted Eastmoney CN and Phillips HK positions;
  - includes stock/ETF/fund/option/unknown positions with non-zero quantity;
  - excludes cash, money-market funds, zero quantity, unsupported markets, and invalid symbols with explicit skip reasons;
  - returns duplicate Account rows and lets the existing quote service canonical-symbol map request one symbol once;
  - requires an explicit universe at every quote refresh call;
  - retains HK/CN OpenD `update_time` as an offset-aware market timestamp and falls back to `fetched_at` only when it is absent.

  Use the intended public helper directly:

  ```python
  universe = build_account_quote_universe({
      "brokers": {
          "eastmoney": {
              "positions": [{
                  "market": "CN", "asset_class": "stock", "symbol": "600519",
                  "name": "贵州茅台", "quantity": "100",
              }],
          },
          "phillips": {
              "positions": [{
                  "market": "HK", "asset_class": "etf", "symbol": "02800",
                  "name": "盈富基金", "quantity": "500",
              }],
          },
      },
  })
  assert [item.futu_symbol for item in universe.items] == ["SH.600519", "HK.02800"]
  ```

- [ ] **Step 2: Run the focused tests and confirm the red state**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_futu_universe.py \
    tests/test_dashboard_quotes.py \
    tests/test_account_sync_worker.py
  ```

  Expected: failure because `build_account_quote_universe` does not exist, refresh still accepts no universe, and HK/CN quote rows discard OpenD `update_time`.

- [ ] **Step 3: Implement the accepted-state builder by reusing the existing row rules**

  Replace the CSV loader with one Account-state function in `futu_universe.py`; retain the existing dataclasses and do not add a new model:

  ```python
  def build_account_quote_universe(
      state: Mapping[str, object],
  ) -> FutuQuoteUniverse:
      items: list[FutuUniverseItem] = []
      skipped: list[SkippedFutuUniverseRow] = []
      row_number = 0
      brokers = state.get("brokers")
      for broker in sorted(brokers) if isinstance(brokers, Mapping) else ():
          source = brokers[broker]
          positions = source.get("positions") if isinstance(source, Mapping) else None
          for row in positions if isinstance(positions, list) else ():
              row_number += 1
              # Normalize Account fields, apply the existing market/asset/
              # quantity/symbol checks, and append the existing item or
              # skipped-row dataclass. Broker source kind is not an input.
      return FutuQuoteUniverse(items=items, skipped=skipped)
  ```

  Delete `load_futu_quote_universe`, the `csv` import, `STATEMENT_BROKERS`, the brokers parameter on `_skip_reason`, and their CSV-only tests after `rg` confirms there is no production caller. Replace quote-service test setup with a local `FutuQuoteUniverse` fixture helper rather than retaining production fallback code for tests.

- [ ] **Step 4: Require the Worker-owned universe and preserve real quote time**

  Make the existing service boundary explicit:

  ```python
  def refresh(
      self, universe: FutuQuoteUniverse,
  ) -> QuoteRefreshResult:
      fetched_at = _now_text()
  ```

  For non-US quote rows, normalize `snapshot.update_time` with
  `ZoneInfo("Asia/Hong_Kong")` for HK and `ZoneInfo("Asia/Shanghai")` for CN.
  Store it in the existing `price_time` field. Leave it empty when OpenD omits
  the value so `_dashboard_position_row` uses `fetched_at` as the defined
  fallback. Keep the existing US session/time selection unchanged.

  In `sync_quotes_once`, load state once before refreshing and reuse the same object for projection:

  ```python
  state_path = self.config.data_dir / "latest" / "account_sync_state.json"
  state = load_account_sync_state(state_path)
  universe = build_account_quote_universe(state)
  payload = self._quote_service.refresh(universe).to_dict()
  write_json_atomic(self._quotes_path(), payload)
  if payload["status"] == "failed":
      return payload
  write_json_atomic(
      state_path,
      with_dashboard_projection(state, payload, generated_at=self.now_text()),
  )
  ```

  Update every quote-service test call to pass an explicit universe. Update Worker test doubles to require the universe and assert that accepted statement positions are requested without reading `portfolio.csv`.

- [ ] **Step 5: Run focused tests and commit**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_futu_universe.py \
    tests/test_dashboard_quotes.py \
    tests/test_account_sync_worker.py
  git diff --check
  git add src/open_trader/futu_universe.py \
    src/open_trader/dashboard_quotes.py \
    src/open_trader/account_sync_worker.py \
    tests/test_futu_universe.py tests/test_dashboard_quotes.py \
    tests/test_account_sync_worker.py
  git commit -m "fix: quote every accepted Account holding"
  ```

  Expected: all three focused files pass; one canonical Futu symbol produces one OpenD request regardless of how many brokers hold it, no production refresh can fall back to CSV, and HK/CN quote times preserve OpenD provenance.

---

## Task 2: Publish complete real-position current valuations

**Interfaces**

- Consumes: accepted Account positions, accepted quote rows, native-currency-to-HKD rates, and the accepted USD-to-HKD rate.
- Produces: a complete `current_valuation` object on every quoteable US/HK/CN position in a newly built projection.
- Preserves: flat fields, summaries, weights, P/L, cash rows, and the last accepted projection when any required quote or FX fact is missing.

**Files**

- Modify: `src/open_trader/account_sync_state.py:42-62` (field constants)
- Modify: `src/open_trader/account_sync_state.py:418-557` (projection and position row)
- Modify: `src/open_trader/account_sync_state.py:582-596` (FX lookup)
- Modify: `src/open_trader/account_sync_state.py:991-1003` (projection validation)
- Test: `tests/test_account_sync_state.py:496`
- Test: `tests/test_account_sync_worker.py`

- [ ] **Step 1: Add failing projection tests for statement quote override and both currencies**

  Extend the existing mixed live/statement projection fixture with valid OpenD quote rows for every quote-required position, including Eastmoney and Phillips. Assert one concrete row end to end:

  ```python
  row = next(
      row for row in projection["broker_positions"]
      if row["broker"] == "eastmoney" and row["symbol"] == "600519"
  )
  assert row["last_price"] == "1500"
  assert row["price_kind"] == "live"
  assert row["market_value_usd"] == ""
  assert row["current_valuation"] == {
      "price": "1500",
      "price_kind": "live",
      "price_as_of": "2026-08-04T15:00:00+08:00",
      "market_value_usd": "20769.23",
      "market_value_hkd": "162000.00",
  }
  ```

  Calculate expected figures from the fixture's actual quantity and accepted FX rates, rather than changing production rounding to fit this example. Also assert broker/portfolio summaries, weights, and P/L use the quoted native market value.

  Add failure cases for:

  - missing current and retained quote on a required statement position;
  - zero, negative, or non-finite quote price;
  - missing native-to-HKD FX;
  - missing USD-to-HKD denominator;
  - cash and money-market positions retain their old flat shape and do not gain `current_valuation`;
  - a complete old projection with no `current_valuation` still validates for independent rollback compatibility;
  - a partially present object never validates.

- [ ] **Step 2: Run the focused tests and confirm the red state**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_account_sync_state.py \
    tests/test_account_sync_worker.py
  ```

  Expected: statement rows retain statement prices, no nested valuation is published, and missing USD FX is not yet rejected.

- [ ] **Step 3: Apply valid quotes regardless of broker source kind**

  In `_dashboard_position_row`, determine whether the position is quote-required from its existing market, asset class, quantity, and canonical symbol rules. For a required position, require a quote row with `status == "ok"` and a finite positive price; otherwise raise `PortfolioBuildError` so `with_dashboard_projection` cannot replace the accepted projection.

  Remove only the `source["source_kind"] == "live"` guard around the existing quote overlay. Keep session selection, option multiplier, quote time, native market value, summary, weight, and P/L calculations in their present shared path.

- [ ] **Step 4: Generalize the existing FX lookup just enough for USD conversion**

  Replace the value-bound lookup with one small currency lookup and keep the statement defaults, including the approved fixed `USD/HKD = 7.8` for Eastmoney and Phillips:

  ```python
  def _dashboard_fx_rate(
      account_alias: str,
      currency: str,
      source: Mapping[str, object],
      source_rates: Mapping[tuple[str, str], Decimal],
  ) -> Decimal:
      normalized = currency.upper()
      rate = source_rates.get((account_alias, normalized))
      if rate is not None:
          return rate
      if source["source_kind"] == "statement":
          fallback = _STATEMENT_FX_TO_HKD.get(normalized)
          if fallback is not None:
              return fallback
      raise PortfolioBuildError(
          f"live FX missing for {account_alias}.{normalized}"
      )
  ```

  Update the two existing position/cash call sites. Do not add a provider or cache.

- [ ] **Step 5: Build and validate the all-or-nothing object**

  Compute once from the selected native market value:

  ```python
  native_to_hkd = _dashboard_fx_rate(
      position.account_alias, position.currency, source, source_rates
  )
  usd_to_hkd = _dashboard_fx_rate(
      position.account_alias, "USD", source, source_rates
  )
  market_value_hkd = money(market_value * native_to_hkd)
  current_valuation = {
      "price": number(last_price),
      "price_kind": price_kind,
      "price_as_of": price_as_of,
      "market_value_usd": money(Decimal(market_value_hkd) / usd_to_hkd),
      "market_value_hkd": market_value_hkd,
  }
  ```

  Add the object only for positions accepted by `build_account_quote_universe`; cash and money-market funds remain unchanged. Keep flat `market_value_hkd` exactly equal to the nested value and flat `market_value_usd` unchanged. USD and HKD outputs use the existing money rounding independently; do not add a round-trip equality check for USD-native rows.

  Change affected projection annotations from `dict[str, str]`/`Mapping[str, str]` to `dict[str, object]`/`Mapping[str, object]`; do not hide the nested object behind casts.

  Extend `_is_valid_dashboard_position` so:

  - complete absence remains valid for the accepted Issue #21 rollback publication;
  - presence requires exactly the five string fields, valid price kind, finite positive price, finite USD/HKD values, and equality with the corresponding flat price/kind/time/HKD fields;
  - partial presence is invalid.

- [ ] **Step 6: Prove failed refresh cannot replace the accepted projection, then commit**

  In the Worker test, seed a complete accepted projection, return a `partial` quote publication missing one newly required statement symbol, and assert the state file bytes are unchanged and the result is `dashboard_projection_failed`.

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_account_sync_state.py \
    tests/test_account_sync_worker.py
  git diff --check
  git add src/open_trader/account_sync_state.py \
    tests/test_account_sync_state.py tests/test_account_sync_worker.py
  git commit -m "feat: publish Account current valuations"
  ```

---

## Task 3: Extend the frozen Account v1 read contract additively

**Interfaces**

- Consumes: the Worker's accepted projection and existing quote publication.
- Produces: optional `current_valuation` in Account API positions, included in response bytes, generation hashes, ETag, and parity comparison.
- Preserves: exact Issue #21 response when the object is absent; missing required quote for live or statement positions remains contract-shaped `503`.

**Files**

- Modify: `src/open_trader/account_snapshot.py:485-536`
- Modify: `src/open_trader/account_snapshot.py:583-610`
- Modify: `src/open_trader/account_api.py:42-57`
- Modify: `src/open_trader/account_api.py:464-490`
- Test: `tests/test_account_api.py:779`
- Test: `tests/test_account_api.py:1091`
- Modify: `docs/superpowers/specs/2026-08-03-account-v1-contract.md`

- [ ] **Step 1: Add failing API, parity, generation, and 503 tests**

  Add assertions that:

  - a complete nested object is copied into `positions` unchanged except for the existing public US quote-time normalization;
  - nested and flat `price_as_of` normalize identically;
  - changing presence/value of `current_valuation` changes `account_generation`, `snapshot_generation`, response bytes, and ETag;
  - API parity compares the nested object and fails on a nested mismatch;
  - the existing `test_snapshot_rejects_incomplete_retained_quotes` still returns `503`;
  - an accepted Eastmoney or Phillips quote is now included in required quote coverage;
  - cash and money-market positions neither require quotes nor expose the optional object;
  - a fully absent object remains readable as the accepted v1 compatibility shape.

- [ ] **Step 2: Run the focused API tests and confirm the red state**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q tests/test_account_api.py
  ```

  Expected: the public whitelist drops `current_valuation`, parity ignores it, and statement quote coverage is not enforced.

- [ ] **Step 3: Copy and normalize the optional object at the existing public boundary**

  In `account_snapshot.py`, keep the flat whitelist and add only this optional copy:

  ```python
  valuation = row.get("current_valuation")
  if isinstance(valuation, Mapping):
      position["current_valuation"] = {
          field: valuation[field]
          for field in (
              "price", "price_kind", "price_as_of",
              "market_value_usd", "market_value_hkd",
          )
      }
      position["current_valuation"]["price_as_of"] = (
          _normalize_public_price_as_of(
              position["market"],
              position["current_valuation"]["price_as_of"],
          )
      )
  ```

  Let the existing generation and ETag builders include the returned position naturally; do not add another generation.

  Update `_public_position` and `_position_row` annotations to return `dict[str, object]`, matching the additive nested object.

- [ ] **Step 4: Mirror the optional field in independent API parity**

  Extend `_parity_position` with the same five-field copy and the same existing time normalization. Keep the parity implementation independent rather than importing snapshot internals, because it verifies the boundary.

  In `_has_complete_quote_coverage`, derive the required `(market, symbol)` set by calling `build_account_quote_universe({"brokers": brokers})`. This reuses the exact market, asset-class, quantity, and canonical-symbol rules from Task 1 for live and statement sources. Delete `_is_quote_required_position` if `rg` confirms it has no remaining caller. Continue using `_is_valid_quote_row`; do not accept statement/account prices as quote coverage.

- [ ] **Step 5: Update the frozen contract documentation**

  Add the exact optional object to the Positions section and example. Document:

  - all five fields and decimal-string types;
  - nested USD is the cross-currency display value while flat non-USD `market_value_usd` stays empty;
  - object absence is valid only for release compatibility;
  - feature-enabled publications are all-or-nothing;
  - cash and money-market positions remain outside the object;
  - required missing quote still produces `503`.

- [ ] **Step 6: Run API tests and commit**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_account_api.py tests/test_account_sync_state.py
  git diff --check
  git add src/open_trader/account_snapshot.py src/open_trader/account_api.py \
    tests/test_account_api.py \
    docs/superpowers/specs/2026-08-03-account-v1-contract.md
  git commit -m "feat: expose current valuation in Account v1"
  ```

---

## Task 4: Publish the same valuation shape for Trend simulated positions

**Interfaces**

- Consumes: each existing OpenD simulated account snapshot and the service's existing `fx_to_hkd` mapping.
- Produces: the same complete five-field `current_valuation` object on every returned simulated position.
- Preserves: Trend ownership, broker endpoints, account/report attribution, caching, unavailable response shape, and no-submit execution behavior.

**Files**

- Modify: `src/open_trader/trend_simulate_positions.py:73-104`
- Modify: `src/open_trader/trend_simulate_positions.py:121-204`
- Test: `tests/test_trend_simulate_positions.py:229-330`

- [ ] **Step 1: Add failing US/HK/CN valuation tests**

  Parameterize the existing broker-route test over Tiger/USD, Phillips/HKD, and Eastmoney/CNY. For each row assert:

  ```python
  assert position["current_valuation"] == {
      "price": position["last_price"],
      "price_kind": "account_snapshot",
      "price_as_of": payload["synced_at"],
      "market_value_usd": expected_usd,
      "market_value_hkd": position["market_value_hkd"],
  }
  ```

  Add explicit unavailable cases for missing/invalid native FX, missing/invalid USD-to-HKD, invalid/non-positive price, and invalid market value. Keep the existing attribution assertions.

- [ ] **Step 2: Run the focused tests and confirm the red state**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q tests/test_trend_simulate_positions.py
  ```

  Expected: simulated rows have no USD equivalent or nested object.

- [ ] **Step 3: Capture one snapshot time and calculate both currencies**

  Capture `synced_at` once before projection and pass it into `_project_positions`; use the same value in the response. Retain the parsed `last_price` instead of discarding it.

  Require finite positive native and USD FX rates, then reuse `_money`:

  ```python
  market_value_hkd = _money(market_value * native_to_hkd)
  valuation = {
      "price": last_price_text,
      "price_kind": "account_snapshot",
      "price_as_of": synced_at,
      "market_value_usd": _money(
          Decimal(market_value_hkd) / usd_to_hkd
      ),
      "market_value_hkd": market_value_hkd,
  }
  ```

  If any row cannot form the complete object, let the existing exception path return that broker's existing `available: false` response. Do not return partial rows.

- [ ] **Step 4: Run tests and commit**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q tests/test_trend_simulate_positions.py
  git diff --check
  git add src/open_trader/trend_simulate_positions.py \
    tests/test_trend_simulate_positions.py
  git commit -m "feat: value simulated holdings in USD and HKD"
  ```

---

## Task 5: Render owner-published values and make acceptance prove them

**Interfaces**

- Consumes: real and simulated position objects returned by their existing endpoints.
- Produces: existing Dashboard table cells and DOM controller attributes populated from `current_valuation` when present, plus a one-shot update class on the three valuation cells when an already-rendered accepted valuation changes.
- Preserves: flat-field fallback only when the object is completely absent; the `实时价` column label; no per-row OpenD badge, `/api/quotes` call, FX arithmetic, layout, permanent copy, or scroll/focus change.

**Files**

- Modify: `src/open_trader/dashboard_static/dashboard.js:5378-5399`
- Modify: `src/open_trader/dashboard_static/dashboard.js:5537-5586`
- Modify: `src/open_trader/dashboard_static/dashboard.js:8897-8926`
- Modify: `src/open_trader/dashboard_static/dashboard.css:2889-2899`
- Modify: `src/open_trader/dashboard_acceptance.py:960-994`
- Modify: `src/open_trader/dashboard_acceptance.py:1098-1210`
- Modify: `src/open_trader/dashboard_acceptance.py:3153-3206`
- Test: `tests/test_dashboard_web.py:4266-4305`
- Test: `tests/test_dashboard_web.py:6418-6665`
- Test: `tests/test_dashboard_acceptance.py:5330-5420`

- [ ] **Step 1: Add failing renderer tests for preference and rollback fallback**

  Add one real row and one simulated row where nested values differ visibly from flat fixtures. Assert the DOM uses nested price, price kind/time, USD, and HKD. Add a second case with the object wholly absent and assert the existing flat display remains unchanged.

  For a successful second payload, assert that a changed accepted valuation adds
  the update class to `实时价`, `美元市值`, and `港元市值` only. Assert that first
  load, an unchanged payload/`304`, a failed/`503` poll, a plain rerender, and a
  cash or money-market row do not add it. The update marker is consumed by the
  immediate render so switching to a previously hidden broker cannot replay an
  old highlight.

  Add a polling regression where a valid Account snapshot is followed by the brief contract-shaped `503` caused by a newly accepted position, then a valid recovered snapshot. Assert the table keeps the previous rows during the failure and updates after recovery without losing scroll/focus state.

  Keep the existing fetch-spy assertion that the browser never calls `/api/quotes`.

- [ ] **Step 2: Add failing acceptance tests for nested controller truth**

  Extend `_controller_position` fixtures so `_check_controller_owned_rows` expects nested values in the DOM. Add `_account_snapshot_errors` cases for:

  - missing object on a quoteable real position;
  - cash or money-market rows remaining valid without the object;
  - partial object;
  - blank, non-finite, or non-positive values;
  - nested price/kind/time/HKD not matching flat facts.

  Extend simulated validation to require all five fields, assert nested price/time against the OpenD-backed response, and require finite USD/HKD values for US, HK, and CN.

- [ ] **Step 3: Run focused Dashboard tests and confirm the red state**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_dashboard_web.py \
    tests/test_dashboard_acceptance.py
  ```

  Expected: rows still render flat values and acceptance does not reject missing/partial valuations.

- [ ] **Step 4: Add one browser display projection helper and transient update marker**

  Add no new class. Reuse the existing `state` object for one short-lived set of
  changed position keys:

  ```javascript
  function accountPositionDisplay(position) {
    const valuation = position?.current_valuation;
    if (!valuation || typeof valuation !== "object") return position;
    return {
      ...position,
      last_price: valuation.price,
      price_kind: valuation.price_kind,
      price_as_of: valuation.price_as_of,
      market_value_usd: valuation.market_value_usd,
      market_value_hkd: valuation.market_value_hkd,
    };
  }
  ```

  Use it only when constructing `display` in `accountHoldingGroups()` and `simulatedAccountRows()`. Before replacing a successful Account payload, compare complete valuation signatures by the existing stable position key; mark only keys present in both payloads whose accepted valuation changed. Consume the set in the immediate account render and apply one CSS class to the existing price, USD, and HKD cells. Do not mark the initial payload or preserve markers across unrelated rerenders.

  Implement the effect with one CSS animation from the existing success tint to
  the normal table background. Under `prefers-reduced-motion: reduce`, disable
  the animation and background tint. Keep enrichment, selection keys, table
  layout, and all text unchanged. Do not calculate or infer missing child fields
  in JavaScript.

- [ ] **Step 5: Project the same display fields in acceptance**

  Add one Python helper mirroring the browser's pure field selection and call it before comparing `CONTROLLER_DOM_FIELDS`. Add `price_as_of: data-price-as-of` to that mapping. In `_account_snapshot_errors`, wrap the public positions as `{"brokers": {"public": {"positions": positions}}}`, pass that to `build_account_quote_universe`, require a complete object for every resulting `(market, symbol)` identity, leave cash/money-market rows outside the rule, and compare nested price/kind/time/HKD with the flat fields.

  Add one acceptance helper that reads a stable candidate Account/quotes pair, derives the same Account universe, and compares every quoteable API position with the matching accepted quote row. It must verify price and normalized `price_time or fetched_at`, canonical coverage, `quotes.status == "ok"`, `stale == false`, a last-success time newer than the candidate Worker start, and zero missing rows. This is the final provenance proof; do not issue a second price query that can race a moving market. Report unavailable OpenD/fresh refresh as the gate's external `BLOCKED` condition, while normal runtime still permits retained `200 stale` responses.

  In `_validate_simulated_positions`, require the complete object and compare `price` with the direct snapshot's selected accepted price plus `price_as_of` with response `synced_at`. Use existing `_position_decimal` for finite value checks.

- [ ] **Step 6: Run focused tests and commit**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_dashboard_web.py \
    tests/test_dashboard_acceptance.py \
    tests/test_trend_simulate_positions.py
  git diff --check
  git add src/open_trader/dashboard_static/dashboard.js \
    src/open_trader/dashboard_static/dashboard.css \
    src/open_trader/dashboard_acceptance.py \
    tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
  git commit -m "fix: render authoritative holding valuations"
  ```

---

## Task 6: Document, verify, accept, and deploy the exact release

**Interfaces**

- Consumes: the complete candidate branch and real local OpenD/launchd environment.
- Produces: operator runbook, dated changelog, automated proof, real Worker/API/Gateway/browser proof, one final `make acceptance` result, and exact-SHA review deployment.
- Preserves: one Account writer, one listener per service port, Issue #21 rollback, and the Dashboard acceptance gate.

**Files**

- Modify: `docs/operations/account-api-production-cutover.md`
- Modify: `CHANGELOG.md`
- Generated runtime evidence only: `data/latest/account_sync_state.json`, `data/latest/quotes.json`, runtime status/log files already owned by the application

- [ ] **Step 1: Update the operator runbook and changelog before merge**

  Add a 2026-08-04 operator-facing changelog entry covering:

  - Account-position-derived quote universe;
  - removal of the production `portfolio.csv` quote fallback;
  - OpenD overrides for Eastmoney/Phillips quoteable holdings;
  - optional complete real/simulated current valuation;
  - unchanged cash/money-market rows, fixed statement `USD/HKD = 7.8`, and independently rounded USD/HKD outputs;
  - truthful HK/CN OpenD quote time with no UI label or `实时价` copy change;
  - preserved `503` behavior when neither current nor retained quote exists;
  - accepted brief new-position `503` recovery and runtime stale behavior;
  - the focused/full/live/final verification commands; insert their exact results after Steps 2, 3, 5, and 6 and before the documentation commit.

  Extend the cutover runbook with commands that prove every quoteable Account position has the five-field object, matches the same fresh accepted quote publication, and uses a non-statement OpenD `price_kind`. Document the cash/money-market exclusion, brief new-position `503` recovery, runtime stale behavior, whole-stack rollback, and Account-only rollback.

- [ ] **Step 2: Run all focused behavior suites**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q \
    tests/test_futu_universe.py \
    tests/test_dashboard_quotes.py \
    tests/test_account_sync_worker.py \
    tests/test_account_sync_state.py \
    tests/test_account_api.py \
    tests/test_trend_simulate_positions.py \
    tests/test_dashboard_web.py \
    tests/test_dashboard_acceptance.py
  ```

  Expected: all focused tests pass. Record the exact count in `CHANGELOG.md`.

- [ ] **Step 3: Run the full automated suite**

  ```bash
  "$OPEN_TRADER_PYTHON" -m pytest -q
  ```

  Expected: full suite passes. Do not proceed on any failure.

- [ ] **Step 4: Commit docs and freeze the candidate SHA**

  ```bash
  git diff --check
  git add docs/operations/account-api-production-cutover.md CHANGELOG.md
  git commit -m "docs: document current valuation operations"
  git status --short
  export CANDIDATE_SHA="$(git rev-parse HEAD)"
  test -z "$(git status --short)"
  ```

  Expected: clean worktree and one immutable candidate SHA. Any later source or data-fixture edit creates a new candidate and repeats Steps 2-4.

- [ ] **Step 5: Deploy the candidate and verify fresh real publications before the final gate**

  Use a detached worktree at `CANDIDATE_SHA` and the existing installers in the runbook. The Worker installer must first stop the old writer and prove its lock is released. Then install Account API and Dashboard stack from the same detached root.

  Verify:

  ```bash
  launchctl print gui/$(id -u)/com.open-trader.account-sync-controller
  launchctl print gui/$(id -u)/com.open-trader.account-api
  launchctl print gui/$(id -u)/com.open-trader.frontend-gateway
  launchctl print gui/$(id -u)/com.open-trader.legacy-dashboard
  lsof -nP -iTCP:8766 -sTCP:LISTEN
  lsof -nP -iTCP:8767 -sTCP:LISTEN
  lsof -nP -iTCP:8768 -sTCP:LISTEN
  curl -fsS http://127.0.0.1:8766/healthz
  curl -fsS http://127.0.0.1:8766/api/v1/account/snapshot
  PYTHONPATH="$CUTOVER_ROOT/src" "$OPEN_TRADER_PYTHON" \
    -m open_trader account-api-parity --data-dir "$CUTOVER_ROOT/data"
  tail -n 100 "$CUTOVER_ROOT/logs/account_sync/launchd.out.log"
  tail -n 100 "$CUTOVER_ROOT/logs/account_api/launchd.out.log"
  ```

  Require fresh PID/start time, detached cwd, clean source, and `CANDIDATE_SHA` for Worker/API/Gateway/Legacy records. Require HTTP 200, Account parity, fresh Worker/API logs, and a candidate-Worker quote publication with `status == "ok"`, `stale == false`, zero missing rows, and `last_success_at` after Worker start.

  Inspect the real Gateway response and browser for:

  - every quoteable Account position matching its canonical accepted OpenD quote price and normalized quote time;
  - every quoteable Eastmoney and Phillips position using non-statement OpenD `price_kind`;
  - all quoteable real positions with complete nested values, while cash/money-market rows retain their old shape;
  - one US, one HK, and one CN simulated endpoint with complete nested values;
  - no browser request to `/api/quotes`.

  Current read-only preflight evidence on 2026-08-04 is 35 requested, 35 returned, and 35 valid OpenD prices. Repeat the candidate-owned proof rather than treating this planning probe as release evidence.

  If a required live quote is genuinely unavailable and no retained accepted quote exists, stop with the contract-shaped `503`; do not substitute a statement price, fixture, or mock. If only retained stale quotes are available, normal runtime remains valid but release acceptance is `BLOCKED` until a fresh OpenD refresh succeeds.

- [ ] **Step 6: Run `make acceptance` once as the final review gate**

  ```bash
  cd "$CUTOVER_ROOT"
  PYTHON_BIN="$OPEN_TRADER_PYTHON" make acceptance
  ```

  - `PASS`: continue.
  - `FAIL`: fix, create a new candidate SHA, repeat focused/full/live checks, then rerun the final gate.
  - `BLOCKED`: report the external/browser blocker; do not substitute curl, fixtures, mocks, or screenshots.

- [ ] **Step 7: Redeploy the exact accepted SHA and hand off for review**

  After `PASS`, rerun the existing Account Worker, Account API, and Dashboard stack installers from the same detached `CANDIDATE_SHA`. Do not edit source or data fixtures between acceptance and restart.

  Recheck new PIDs, cwd, exact Git SHA, clean state, fresh logs, one listener per port, and:

  ```bash
  curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
  ```

  Expected: `200`. Only then provide `http://127.0.0.1:8766/` for operator review.
