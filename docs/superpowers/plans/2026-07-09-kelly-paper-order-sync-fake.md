# Kelly Paper Order Sync Fake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fake Futu paper-order sync path that writes `data/latest/kelly_paper_orders.json` for the Kelly Lab UI.

**Architecture:** Add a small backend module with a paper-order client protocol, a fake client, and a sync function. The sync function only accepts `SIMULATE` clients, normalizes a list of order dictionaries, and atomically writes the optional artifact that `kelly_lab.py` already loads.

**Tech Stack:** Python, JSON artifact files, pytest, existing Kelly Lab dashboard and Playwright tests.

---

## Files

- Create: `src/open_trader/kelly_paper_order_sync.py`
  - Define a `PaperOrderClient` protocol.
  - Define `FakeFutuPaperOrderClient`.
  - Define `sync_kelly_paper_orders(data_dir, client, synced_at=None)`.
  - Atomically write `data/latest/kelly_paper_orders.json`.
- Create: `tests/test_kelly_paper_order_sync.py`
  - Cover successful artifact writes from the fake client.
  - Cover the hard gate that rejects non-`SIMULATE` clients.
  - Cover order validation for missing `experiment_id`.
- Modify: `data/latest/kelly_paper_orders.json`
  - Regenerate local ignored mock data through the new fake sync function for UI verification.

## Task 1: Sync Module Tests

- [ ] **Step 1: Write failing tests**

Create `tests/test_kelly_paper_order_sync.py` with tests for:

```python
def test_sync_kelly_paper_orders_writes_latest_artifact_from_fake_client(tmp_path: Path) -> None:
    client = FakeFutuPaperOrderClient(
        orders=(
            {
                "experiment_id": "trend_pullback_20d_exp_20260707",
                "market": "US",
                "symbol": "RAM",
                "side": "buy",
                "submitted_at": "2026-07-08 10:01",
                "order_price": "12.34",
                "order_qty": "800",
                "filled_qty": "800",
                "avg_fill_price": "12.34",
                "status": "filled",
                "order_id": "SIM-10001",
            },
        )
    )

    payload = sync_kelly_paper_orders(
        tmp_path / "data",
        client,
        synced_at="2026-07-09 10:30",
    )

    assert payload["environment"] == "SIMULATE"
```

Also add tests for rejecting a `REAL` environment and rejecting orders without `experiment_id`.

- [ ] **Step 2: Verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync.py -q
```

Expected: import failure because `open_trader.kelly_paper_order_sync` does not exist.

## Task 2: Fake Sync Implementation

- [ ] **Step 1: Implement the sync module**

Add a focused module that imports `PAPER_ORDERS_SCHEMA_VERSION` from `open_trader.kelly_lab`, deep-copies fake client orders, validates `experiment_id`, and writes the artifact with a temp file plus `Path.replace()`.

- [ ] **Step 2: Verify green**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync.py -q
```

Expected: all tests pass.

## Task 3: Local Demo Artifact

- [ ] **Step 1: Regenerate ignored mock artifact**

Run a short Python snippet that calls `sync_kelly_paper_orders(Path("data"), FakeFutuPaperOrderClient(...))` with the current mock strategy orders.

- [ ] **Step 2: Verify backend and UI**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
npm run test:e2e:kelly
```

Expected: pytest and Playwright pass, and the Kelly Lab still displays order rows from the artifact.

## Self-Review

- Scope stays local to fake sync and artifact writing.
- No real Futu SDK, no automatic trading, no UI redesign in this phase.
- The non-`SIMULATE` gate prevents this path from accidentally being used for live trading.
