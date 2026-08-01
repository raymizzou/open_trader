# Predict BNB Testnet One-Order Canary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one separately invoked, hard-wired BNB Testnet canary that submits at most one minimum-size Predict.fun order and verifies the resulting testnet state without creating any Predict.fun mainnet execution capability.

**Architecture:** Keep the canary outside the Dashboard and long-running monitors. Reuse the read-only Predict config/API-key boundary, add one testnet-only module and CLI subcommand, and use the official Predict SDK solely for testnet authentication/signing/order construction. Require an exact confirmation flag, pre-existing token approvals, funded testnet balances, and a market whose minimum executable cost is at most 1 USDT. Never retry submission and never fall back to mainnet.

**Tech Stack:** Python 3.12, `predict-sdk==0.0.22`, existing Keychain helpers, stdlib JSON/Decimal, pytest, direct BNB Testnet workflow.

**Approved design:** `docs/superpowers/specs/2026-08-02-predict-cross-venue-yes-no-design.md`

## Global constraints

- Execute this plan only after the read-only plan has landed, credentials are allocated, and the user explicitly authorizes the live testnet canary run.
- Hard-code chain ID `97`, API base `https://api-testnet.predict.fun`, and maximum canary cost `1.00 USDT`. There is no URL, chain, or cost override.
- Submit at most one FOK limit BUY in one command invocation. No automatic retry, second leg, loop, daemon, Dashboard button, or scheduler.
- Never run approval transactions. If allowances are insufficient, return a sanitized blocked result.
- Never store the signer private key in a file or accept it as a CLI argument. Use a distinct Keychain account under `com.open-trader.predict`.
- Never log or persist the API key, private key, JWT, signature, signed payload, or full request body.
- This canary does not gate the read-only Dashboard's `make acceptance`; it has a separate live verification result.

## Locked file structure

New files:

- `src/open_trader/predict_testnet_canary.py`
- `tests/test_predict_testnet_canary.py`

Modified files:

- `pyproject.toml`
- `src/open_trader/predict_source.py`
- `src/open_trader/cli.py`
- `tests/test_predict_source.py`
- `CHANGELOG.md`

Do not modify Dashboard, monitor, store, notification, Polymarket execution, or launchd files.

---

### Task 1: Add the testnet signer boundary and hard safety constants

**Files:**

- Modify: `pyproject.toml`
- Modify: `src/open_trader/predict_source.py`
- Modify: `tests/test_predict_source.py`
- Create: `src/open_trader/predict_testnet_canary.py`
- Create: `tests/test_predict_testnet_canary.py`

**Interfaces:**

```python
TESTNET_CHAIN_ID = 97
TESTNET_API_BASE = "https://api-testnet.predict.fun"
CANARY_MAX_COST = Decimal("1.00")
PREDICT_TESTNET_SIGNER_ACCOUNT = "testnet-signer-private-key"


@dataclass(frozen=True, slots=True)
class CanaryResult:
    status: Literal["verified", "blocked", "failed"]
    market_id: str
    order_id: str
    transaction_hash: str
    filled_quantity: Decimal
    maximum_cost: Decimal
    reason: str
```

- [ ] **Step 1: Write failing constant, dependency, and secret tests**

Assert:

- the official SDK is pinned exactly to `predict-sdk==0.0.22`;
- the canary module contains only chain 97 and the testnet API base;
- no constructor or CLI parser accepts URL, chain ID, private key, JWT, or signature;
- testnet signer storage uses stdin and the separate Keychain account;
- all exceptions/results are sanitized even when fake SDK errors contain secrets.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_source.py tests/test_predict_testnet_canary.py -q
```

Expected: FAIL because the dependency, signer account, and canary module do not exist.

- [ ] **Step 2: Add the exact SDK pin and testnet-only boundaries**

Add `predict-sdk==0.0.22` to project dependencies. Extend the Predict Keychain account allowlist with `testnet-signer-private-key`; do not change the mainnet API-key account.

Implement constants and redacted result/error types in `predict_testnet_canary.py`. Add this permanent guard at module import:

```python
if TESTNET_CHAIN_ID != 97 or TESTNET_API_BASE != "https://api-testnet.predict.fun":
    raise RuntimeError("predict testnet safety constants changed")
```

- [ ] **Step 3: Install, run focused tests, and commit**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pip install -e '.[dev]'
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_source.py tests/test_predict_testnet_canary.py -q
git diff --check
git add pyproject.toml src/open_trader/predict_source.py \
  src/open_trader/predict_testnet_canary.py tests/test_predict_source.py \
  tests/test_predict_testnet_canary.py
git commit -m "feat: add Predict testnet canary boundary"
```

Expected: PASS.

---

### Task 2: Build one minimum-cost FOK order without submission retries

**Files:**

- Modify: `src/open_trader/predict_testnet_canary.py`
- Modify: `tests/test_predict_testnet_canary.py`

- [ ] **Step 1: Write failing selection and construction tests**

With fake REST/SDK objects, prove the canary:

1. rejects missing credentials, unfunded balances, insufficient allowances, and absent explicit confirmation before constructing an order;
2. lists only open standard binary, non-NegRisk, non-yield testnet markets;
3. selects the first market whose minimum order quantity at best ask costs at most `1.00 USDT`;
4. creates one `FOK`, `LIMIT`, `BUY` at executable best ask through `OrderBuilder.make(ChainId.BNB_TESTNET, private_key, OrderBuilderOptions(predict_account=wallet))`;
5. rejects any SDK object that reports a non-testnet chain or non-testnet API base;
6. never calls an approval method;
7. calls the submission fake at most once even after timeout, 429, network error, or rejected status.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_testnet_canary.py -k 'select or build or submit_once' -q
```

Expected: FAIL because selection and order construction are absent.

- [ ] **Step 2: Implement the linear one-shot workflow**

Keep one function with explicit sequential checks; do not create an order state machine:

```python
def run_testnet_canary(
    config: PredictConfig,
    *,
    api_key: str,
    signer_private_key: str,
    submit_one_order: bool,
    sdk: object,
    clock: Callable[[], datetime],
) -> CanaryResult:
    if not submit_one_order:
        return CanaryResult("blocked", "", "", "", Decimal("0"), Decimal("0"), "confirmation_required")
    return _run_confirmed_canary(config, api_key, signer_private_key, sdk, clock)
```

Authenticate against the testnet endpoint, check balances/allowances, select one market, build one FOK limit buy, and call the submit endpoint exactly once. Do not catch and retry submission.

- [ ] **Step 3: Run focused tests and commit**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_testnet_canary.py -q
git diff --check
git add src/open_trader/predict_testnet_canary.py \
  tests/test_predict_testnet_canary.py
git commit -m "feat: build one Predict testnet order"
```

Expected: PASS with submission call count never above one.

---

### Task 3: Verify the submitted order and expose an explicit CLI command

**Files:**

- Modify: `src/open_trader/predict_testnet_canary.py`
- Modify: `src/open_trader/cli.py:1254-1465`
- Modify: `tests/test_predict_testnet_canary.py`

- [ ] **Step 1: Write failing verification and CLI tests**

Assert:

- the CLI command is exactly `open-trader prediction-arb predict testnet-canary --config <path> --submit-one-order`;
- omitting `--submit-one-order` returns code 2 before auth/sign/order calls;
- the signer comes from Keychain and no private-key CLI option exists;
- after one submission, polling reads only that order/activity/position until success or timeout;
- `verified` requires transaction success plus the expected position or balance delta;
- timeout/rejection returns `blocked`/`failed` without a second submission;
- output contains only status, market ID, order ID, transaction hash, quantity, maximum cost, and sanitized reason.

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_testnet_canary.py -k 'verify or cli' -q
```

Expected: FAIL because verification and CLI dispatch are absent.

- [ ] **Step 2: Add bounded post-submit polling and CLI dispatch**

Poll for at most 60 seconds with a fixed two-second interval. Polling may retry reads; submission may not repeat. Compare pre/post position or balance snapshots for the exact market/outcome. Print one JSON result through the existing CLI output style and redact exceptions before rendering.

- [ ] **Step 3: Run focused and CLI help tests**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_predict_source.py tests/test_predict_testnet_canary.py \
  tests/test_polymarket_trading.py -q
open-trader prediction-arb predict testnet-canary --help
```

Expected: PASS; help exposes no secret, URL, chain, or mainnet switch.

- [ ] **Step 4: Commit**

```bash
git diff --check
git add src/open_trader/predict_testnet_canary.py src/open_trader/cli.py \
  tests/test_predict_testnet_canary.py
git commit -m "feat: verify one Predict testnet order"
```

---

### Task 4: Run safety scans, live canary, and record the result

**Files:**

- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run complete tests and mutation-boundary scans**

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" \
  /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q
rg -n 'api\.predict\.fun|chain.?id.?=.*56|BNB_MAINNET|--private-key|--api-key|--jwt' \
  src/open_trader tests
rg -n 'submit' src/open_trader/predict_testnet_canary.py \
  tests/test_predict_testnet_canary.py
git diff --check
```

Expected: full suite PASS; no mainnet base/chain or secret CLI option in the canary; tests prove the only submit call is bounded to one.

- [ ] **Step 2: Store credentials without exposing them**

Use the interactive setup command implemented by the read-only plan for the API key and the canary's interactive signer setup. Do not paste either secret into chat, shell history, files, environment variables, or command arguments.

- [ ] **Step 3: Run preflight without submission**

Invoke the command without `--submit-one-order` and confirm it returns `confirmation_required` before SDK/auth/order activity. Then inspect the funded testnet wallet and allowances through the read-only checks. If funding or approvals are insufficient, stop with `blocked`; do not send approval transactions.

- [ ] **Step 4: Obtain explicit user authorization and submit once**

Only after the user explicitly authorizes this live canary, run:

```bash
open-trader prediction-arb predict testnet-canary \
  --config config/prediction_arbitrage.json \
  --submit-one-order
```

Expected: exactly one sanitized result. `verified` requires transaction success and expected position/balance delta. `blocked` or `failed` ends the run; do not rerun automatically.

- [ ] **Step 5: Update the changelog before merge**

Add a dated operator-facing entry stating the chain, market/order IDs, maximum test cost, sanitized verification result, and that no mainnet execution path exists. Never include wallet secrets, signatures, JWTs, or request bodies.

```bash
git add CHANGELOG.md
git commit -m "docs: log Predict testnet canary"
git status --short
```

Expected: clean worktree.

- [ ] **Step 6: Self-review the hard boundary**

```bash
rg -n 'TODO|FIXME|NotImplemented|pass$|retry.*submit|mainnet' \
  src/open_trader/predict_testnet_canary.py tests/test_predict_testnet_canary.py
git diff --check main...HEAD
git log --oneline main..HEAD
```

Expected: no placeholder, no submission retry, no mainnet route, and only intentional commits.

## Completion boundary

The code portion is complete when automated tests and safety scans pass. The canary itself is verified only after one explicitly authorized testnet submission produces a successful transaction and expected account-state delta. Missing credentials, funding, allowance, suitable market, or external testnet confirmation is `blocked`, not a reason to weaken the boundary or retry the order.
