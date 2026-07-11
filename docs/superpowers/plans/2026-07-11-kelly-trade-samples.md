# Kelly Trade Samples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Kelly trade samples and parameter stats from synced Futu paper orders, then surface those derived parameters in the existing Kelly Lab UI.

**Architecture:** Add a focused `kelly_trade_samples` module that turns experiment definitions and paper-order artifacts into a new `kelly_trade_samples.json` artifact. Kelly Lab loads that artifact when present and overlays per-experiment stats onto existing experiment state. The dashboard reuses the current parameter derivation section and adds sample-source fields.

**Tech Stack:** Python stdlib, `Decimal`, existing Open Trader CLI, pytest, dashboard static JavaScript tests, Playwright e2e fixtures.

---

## File Structure

- Create `src/open_trader/kelly_trade_samples.py`
  - Owns schema constants, pure sample-building logic, stat calculation, artifact validation, load/write helpers.
- Create `tests/test_kelly_trade_samples.py`
  - Unit tests for pairing, diagnostics, stats, and artifact IO.
- Create `tests/test_kelly_trade_samples_cli.py`
  - CLI parser and command wiring tests.
- Modify `src/open_trader/cli.py`
  - Import builder/write helpers.
  - Add `kelly build-trade-samples`.
  - Wire command to load Kelly Lab without strategy capital, load paper orders, build payload, write artifact, and print counts.
- Modify `src/open_trader/kelly_lab.py`
  - Add schema constant and optional loader for `kelly_trade_samples.json`.
  - Overlay valid `stats_by_experiment` onto experiment `stats`.
  - Return unavailable state on invalid sample artifact.
- Modify `tests/test_kelly_lab.py`
  - Cover missing artifact, valid overlay, invalid schema, market separation.
- Modify `src/open_trader/dashboard_static/dashboard.js`
  - Render `parameter_source` and `skipped_order_count` in parameter derivation.
- Modify `tests/test_dashboard_web.py`
  - Cover new source/skipped rows.
- Modify `tests/e2e/fixtures/kelly-dashboard.json`
  - Add sample-source fields to Kelly fixture stats.
- Modify Playwright e2e test if needed to assert source/skipped rows.
- Modify `CHANGELOG.md`
  - Record the trade sample artifact and UI stats source.

---

### Task 1: Core Trade Sample Builder

**Files:**
- Create: `src/open_trader/kelly_trade_samples.py`
- Create: `tests/test_kelly_trade_samples.py`

- [ ] **Step 1: Write failing unit tests for pairing**

Add `tests/test_kelly_trade_samples.py`:

```python
from __future__ import annotations

from open_trader.kelly_trade_samples import build_kelly_trade_samples_payload


def _experiment(experiment_id: str = "trend_us") -> dict[str, object]:
    return {
        "experiment_id": experiment_id,
        "experiment_name": "Trend US",
        "market": "US",
        "stats": {},
        "participants": [
            {
                "market": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "source": "watchlist",
                "locked": True,
                "per_symbol_budget": "10000",
                "budget_currency": "USD",
            }
        ],
    }


def test_build_trade_samples_pairs_filled_buy_and_sell_as_win() -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "synced_at": "2026-07-11 09:30",
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-1",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "sell",
                    "submitted_at": "2026-07-12 10:00",
                    "filled_qty": "10",
                    "avg_fill_price": "106",
                    "status": "filled",
                    "order_id": "SELL-1",
                },
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    assert payload["schema_version"] == "open_trader.kelly_trade_samples.v1"
    assert payload["sample_count"] == 1
    assert payload["open_position_count"] == 0
    assert payload["skipped_order_count"] == 0
    assert payload["source_orders_synced_at"] == "2026-07-11 09:30"
    assert payload["samples"] == [
        {
            "experiment_id": "trend_us",
            "market": "US",
            "symbol": "AAPL",
            "entry_order_id": "BUY-1",
            "exit_order_id": "SELL-1",
            "entry_submitted_at": "2026-07-11 09:31",
            "exit_submitted_at": "2026-07-12 10:00",
            "entry_price": "100",
            "exit_price": "106",
            "quantity": "10",
            "entry_notional": "1000",
            "exit_notional": "1060",
            "gross_pnl": "60",
            "net_pnl_pct": "6%",
            "result": "win",
        }
    ]
    assert payload["stats_by_experiment"]["trend_us"]["completed_samples"] == 1
    assert payload["stats_by_experiment"]["trend_us"]["winning_samples"] == 1
    assert payload["stats_by_experiment"]["trend_us"]["parameter_source"] == "futu_paper_order_samples"


def test_build_trade_samples_keeps_unmatched_buy_as_open_position() -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "synced_at": "2026-07-11 09:30",
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-1",
                }
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    assert payload["sample_count"] == 0
    assert payload["open_position_count"] == 1
    assert payload["open_positions"][0]["entry_order_id"] == "BUY-1"
    assert payload["stats_by_experiment"]["trend_us"]["completed_samples"] == 0
    assert payload["stats_by_experiment"]["trend_us"]["open_samples"] == 1
    assert payload["stats_by_experiment"]["trend_us"]["suggested_position_pct"] == "0%"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_kelly_trade_samples.py -q
```

Expected: import failure for `open_trader.kelly_trade_samples`.

- [ ] **Step 3: Implement the minimal builder**

Create `src/open_trader/kelly_trade_samples.py` with:

```python
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


TRADE_SAMPLES_SCHEMA_VERSION = "open_trader.kelly_trade_samples.v1"


def build_kelly_trade_samples_payload(
    experiments: list[dict[str, Any]],
    paper_orders_payload: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = generated_at or _current_timestamp()
    experiment_index = _experiment_index(experiments)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []

    for order in _orders_from_payload(paper_orders_payload):
        experiment_id = _text(order.get("experiment_id"))
        market = _text(order.get("market")).upper()
        symbol = _text(order.get("symbol")).upper()
        key = (experiment_id, market, symbol)
        reason = _order_skip_reason(order, experiment_index)
        if reason:
            skipped.append(_diagnostic(order, reason))
            continue
        groups.setdefault(key, []).append(order)

    samples: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []
    group_skips: list[dict[str, Any]] = []

    for key, orders in sorted(groups.items()):
        paired = _pair_group(key, orders)
        samples.extend(paired["samples"])
        open_positions.extend(paired["open_positions"])
        group_skips.extend(paired["skipped_orders"])

    diagnostics = {"skipped_orders": skipped + group_skips}
    stats = _stats_by_experiment(
        experiments,
        samples,
        open_positions,
        diagnostics["skipped_orders"],
        timestamp,
    )
    return {
        "schema_version": TRADE_SAMPLES_SCHEMA_VERSION,
        "generated_at": timestamp,
        "source_orders_synced_at": _text(paper_orders_payload.get("synced_at")),
        "sample_count": len(samples),
        "open_position_count": len(open_positions),
        "skipped_order_count": len(diagnostics["skipped_orders"]),
        "samples": samples,
        "open_positions": open_positions,
        "stats_by_experiment": stats,
        "diagnostics": diagnostics,
    }
```

Add helpers in the same file:

```python
def _pair_group(
    key: tuple[str, str, str],
    orders: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    experiment_id, market, symbol = key
    samples: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    open_entry: dict[str, Any] | None = None

    for order in sorted(orders, key=_order_sort_key):
        side = _text(order.get("side")).lower()
        if side == "buy":
            if open_entry is not None:
                skipped.append(_diagnostic(order, "repeated_entry_not_supported"))
                continue
            open_entry = order
            continue
        if side == "sell":
            if open_entry is None:
                skipped.append(_diagnostic(order, "sell_without_open_entry"))
                continue
            entry_qty = _decimal(open_entry.get("filled_qty"))
            exit_qty = _decimal(order.get("filled_qty"))
            if entry_qty is None or exit_qty is None or entry_qty != exit_qty:
                skipped.append(_diagnostic(order, "exit_quantity_mismatch"))
                continue
            samples.append(_completed_sample(experiment_id, market, symbol, open_entry, order))
            open_entry = None

    if open_entry is not None:
        open_positions.append(_open_position(experiment_id, market, symbol, open_entry))
    return {"samples": samples, "open_positions": open_positions, "skipped_orders": skipped}


def _completed_sample(
    experiment_id: str,
    market: str,
    symbol: str,
    entry: dict[str, Any],
    exit_order: dict[str, Any],
) -> dict[str, Any]:
    quantity = _decimal(entry.get("filled_qty")) or Decimal("0")
    entry_price = _order_price(entry)
    exit_price = _order_price(exit_order)
    entry_notional = entry_price * quantity
    exit_notional = exit_price * quantity
    gross_pnl = exit_notional - entry_notional
    net_pnl_pct = Decimal("0") if entry_notional == 0 else gross_pnl / entry_notional
    return {
        "experiment_id": experiment_id,
        "market": market,
        "symbol": symbol,
        "entry_order_id": _stable_order_id(entry),
        "exit_order_id": _stable_order_id(exit_order),
        "entry_submitted_at": _text(entry.get("submitted_at")),
        "exit_submitted_at": _text(exit_order.get("submitted_at")),
        "entry_price": _decimal_text(entry_price),
        "exit_price": _decimal_text(exit_price),
        "quantity": _decimal_text(quantity),
        "entry_notional": _decimal_text(entry_notional),
        "exit_notional": _decimal_text(exit_notional),
        "gross_pnl": _decimal_text(gross_pnl),
        "net_pnl_pct": _pct_text(net_pnl_pct),
        "result": "win" if gross_pnl > 0 else "loss" if gross_pnl < 0 else "flat",
    }
```

Also add `_open_position`, `_stats_by_experiment`, `_order_skip_reason`,
`_orders_from_payload`, `_experiment_index`, `_order_price`, `_decimal`,
`_decimal_text`, `_pct_text`, `_text`, `_stable_order_id`, `_order_sort_key`,
`_diagnostic`, and `_current_timestamp`.

Required semantics:

- `_order_skip_reason()` returns `unsupported_status` unless status is `filled`.
- `_order_skip_reason()` returns `partial_fill_not_supported` if status contains
  `partial` or `filled_qty != order_qty` when both quantities are present.
- `_order_skip_reason()` returns `missing_price_or_quantity` if filled quantity
  or fill price is missing, zero, negative, or non-numeric.
- `_order_skip_reason()` returns `unknown_experiment` if `experiment_id` is not
  in the current experiment list.
- `_order_skip_reason()` returns `market_mismatch` if the order market does not
  match the experiment market.

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
pytest tests/test_kelly_trade_samples.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/kelly_trade_samples.py tests/test_kelly_trade_samples.py
git commit -m "feat: build kelly trade samples"
```

---

### Task 2: Stats Edge Cases and Artifact IO

**Files:**
- Modify: `src/open_trader/kelly_trade_samples.py`
- Modify: `tests/test_kelly_trade_samples.py`

- [ ] **Step 1: Add failing tests for diagnostics and stats**

Append tests:

```python
def test_build_trade_samples_skips_unsupported_order_patterns() -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "synced_at": "2026-07-11 09:30",
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:31",
                    "filled_qty": "0",
                    "order_qty": "10",
                    "avg_fill_price": "-",
                    "status": "submitted",
                    "order_id": "SUBMITTED-1",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:32",
                    "filled_qty": "5",
                    "order_qty": "10",
                    "avg_fill_price": "100",
                    "status": "partial_filled",
                    "order_id": "PARTIAL-1",
                },
                {
                    "experiment_id": "missing_exp",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:33",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "UNKNOWN-1",
                },
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    reasons = [item["reason"] for item in payload["diagnostics"]["skipped_orders"]]
    assert reasons == [
        "unsupported_status",
        "partial_fill_not_supported",
        "unknown_experiment",
    ]
    assert payload["skipped_order_count"] == 3


def test_build_trade_samples_computes_loss_and_shrunk_kelly_stats() -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {
            "schema_version": "open_trader.kelly_paper_orders.v1",
            "synced_at": "2026-07-11 09:30",
            "orders": [
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-11 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-1",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "sell",
                    "submitted_at": "2026-07-11 10:00",
                    "filled_qty": "10",
                    "avg_fill_price": "110",
                    "status": "filled",
                    "order_id": "SELL-1",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "buy",
                    "submitted_at": "2026-07-12 09:31",
                    "filled_qty": "10",
                    "avg_fill_price": "100",
                    "status": "filled",
                    "order_id": "BUY-2",
                },
                {
                    "experiment_id": "trend_us",
                    "market": "US",
                    "symbol": "AAPL",
                    "side": "sell",
                    "submitted_at": "2026-07-12 10:00",
                    "filled_qty": "10",
                    "avg_fill_price": "95",
                    "status": "filled",
                    "order_id": "SELL-2",
                },
            ],
        },
        generated_at="2026-07-12 10:01",
    )

    stats = payload["stats_by_experiment"]["trend_us"]
    assert stats["completed_samples"] == 2
    assert stats["winning_samples"] == 1
    assert stats["losing_samples"] == 1
    assert stats["raw_win_rate"] == "50%"
    assert stats["adjusted_win_rate"] == "50%"
    assert stats["avg_net_win_pct"] == "10%"
    assert stats["avg_net_loss_pct"] == "5%"
    assert stats["payoff_ratio"] == "2"
    assert stats["full_kelly_pct"] == "25%"
    assert stats["fractional_kelly_pct"] == "6.25%"
    assert stats["suggested_position_pct"] == "4%"
    assert stats["sample_stage"] == "insufficient"
    assert stats["sample_adjustment"] == "样本少于 200，向 50% 收缩"
    assert stats["last_sample_closed_at"] == "2026-07-12 10:00"
    assert stats["last_recomputed_at"] == "2026-07-12 10:01"
```

Add IO test:

```python
from open_trader.kelly_trade_samples import (
    load_kelly_trade_samples,
    write_kelly_trade_samples,
)


def test_write_and_load_kelly_trade_samples(tmp_path) -> None:
    payload = build_kelly_trade_samples_payload(
        [_experiment()],
        {"schema_version": "open_trader.kelly_paper_orders.v1", "orders": []},
        generated_at="2026-07-12 10:01",
    )

    path = write_kelly_trade_samples(tmp_path / "data", payload)
    loaded = load_kelly_trade_samples(tmp_path / "data")

    assert path == tmp_path / "data" / "latest" / "kelly_trade_samples.json"
    assert loaded == payload
```

- [ ] **Step 2: Run tests and verify failures**

Run:

```bash
pytest tests/test_kelly_trade_samples.py -q
```

Expected: failures for missing IO helpers or incomplete stats formatting.

- [ ] **Step 3: Complete stats and IO implementation**

In `src/open_trader/kelly_trade_samples.py`, add public helpers:

```python
def write_kelly_trade_samples(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / "latest" / "kelly_trade_samples.json"
    _write_json_atomic(path, payload)
    return path


def load_kelly_trade_samples(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "latest" / "kelly_trade_samples.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != TRADE_SAMPLES_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version must be {TRADE_SAMPLES_SCHEMA_VERSION!r}",
        )
    if not isinstance(payload.get("stats_by_experiment"), dict):
        raise ValueError(f"{path.name} must contain stats_by_experiment")
    return payload
```

Implement `_stats_by_experiment()` with these rules:

```python
def _stats_by_experiment(
    experiments: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    skipped_orders: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for experiment in experiments:
        experiment_id = _text(experiment.get("experiment_id"))
        exp_samples = [sample for sample in samples if sample["experiment_id"] == experiment_id]
        exp_open = [item for item in open_positions if item["experiment_id"] == experiment_id]
        exp_skipped = [item for item in skipped_orders if item.get("experiment_id") == experiment_id]
        stats[experiment_id] = _experiment_stats(
            exp_samples,
            exp_open,
            exp_skipped,
            generated_at,
        )
    return stats
```

`_experiment_stats()` must compute the fields listed in the spec. Cap
`suggested_position_pct` at `4%` for this stage, and return `0%` when Kelly is
blank or non-positive.

Add `_write_json_atomic()` using the same temp-file pattern used in other Kelly
modules:

```python
def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp_path.replace(path)
```

- [ ] **Step 4: Run targeted tests**

Run:

```bash
pytest tests/test_kelly_trade_samples.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/kelly_trade_samples.py tests/test_kelly_trade_samples.py
git commit -m "feat: compute kelly trade sample stats"
```

---

### Task 3: CLI Command

**Files:**
- Modify: `src/open_trader/cli.py`
- Create: `tests/test_kelly_trade_samples_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_kelly_trade_samples_cli.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader import cli


def test_kelly_build_trade_samples_parser_accepts_generated_at() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "kelly",
            "build-trade-samples",
            "--generated-at",
            "2026-07-11 11:00",
        ]
    )

    assert args.command == "kelly"
    assert args.kelly_command == "build-trade-samples"
    assert args.generated_at == "2026-07-11 11:00"


def test_kelly_build_trade_samples_main_loads_inputs_and_writes_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True)
    paper_orders_payload = {
        "schema_version": "open_trader.kelly_paper_orders.v1",
        "orders": [],
    }
    (latest_dir / "kelly_paper_orders.json").write_text(
        json.dumps(paper_orders_payload),
        encoding="utf-8",
    )
    latest_path = latest_dir / "kelly_trade_samples.json"

    class FakeKellyLabState:
        available = True
        experiments = [{"experiment_id": "trend_us"}]
        error = ""

    def fake_load_kelly_lab_state(
        data_dir_arg: Path,
        *,
        include_strategy_capital: bool = True,
    ) -> FakeKellyLabState:
        captured["load_data_dir"] = data_dir_arg
        captured["include_strategy_capital"] = include_strategy_capital
        return FakeKellyLabState()

    def fake_build(
        experiments: list[dict[str, object]],
        paper_orders_payload_arg: dict[str, object],
        *,
        generated_at: str | None,
    ) -> dict[str, object]:
        captured["experiments"] = experiments
        captured["paper_orders_payload"] = paper_orders_payload_arg
        captured["generated_at"] = generated_at
        return {
            "schema_version": "open_trader.kelly_trade_samples.v1",
            "sample_count": 2,
            "open_position_count": 1,
            "skipped_order_count": 3,
            "stats_by_experiment": {},
        }

    def fake_write(data_dir_arg: Path, payload: dict[str, object]) -> Path:
        captured["write_data_dir"] = data_dir_arg
        captured["payload"] = payload
        return latest_path

    monkeypatch.setattr(cli, "load_kelly_lab_state", fake_load_kelly_lab_state)
    monkeypatch.setattr(cli, "build_kelly_trade_samples_payload", fake_build)
    monkeypatch.setattr(cli, "write_kelly_trade_samples", fake_write)

    result = cli.main(
        [
            "kelly",
            "build-trade-samples",
            "--data-dir",
            str(data_dir),
            "--generated-at",
            "2026-07-11 11:00",
        ]
    )

    assert result == 0
    assert captured["load_data_dir"] == data_dir
    assert captured["include_strategy_capital"] is False
    assert captured["experiments"] == [{"experiment_id": "trend_us"}]
    assert captured["paper_orders_payload"] == paper_orders_payload
    assert captured["generated_at"] == "2026-07-11 11:00"
    assert captured["write_data_dir"] == data_dir
    output = capsys.readouterr().out
    assert "samples: 2" in output
    assert "open_positions: 1" in output
    assert "skipped_orders: 3" in output
    assert f"latest: {latest_path}" in output
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_kelly_trade_samples_cli.py -q
```

Expected: parser rejects `build-trade-samples`.

- [ ] **Step 3: Wire CLI**

In `src/open_trader/cli.py`, add imports near existing Kelly imports:

```python
from .kelly_trade_samples import (
    build_kelly_trade_samples_payload,
    write_kelly_trade_samples,
)
```

Add parser after `build-strategy-capital`:

```python
    kelly_build_trade_samples_parser = kelly_subparsers.add_parser(
        "build-trade-samples",
        help="Build Kelly trade samples and stats from synced paper orders",
    )
    kelly_build_trade_samples_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    kelly_build_trade_samples_parser.add_argument(
        "--generated-at",
        help="Override sample generation timestamp for deterministic local demos",
    )
```

Add command handler before `check-order-risk`:

```python
    if args.command == "kelly" and args.kelly_command == "build-trade-samples":
        try:
            lab_state = load_kelly_lab_state(
                args.data_dir,
                include_strategy_capital=False,
            )
            if not lab_state.available:
                raise ValueError(lab_state.error)
            latest_dir = args.data_dir / "latest"
            paper_orders_payload = _load_optional_json(
                latest_dir / "kelly_paper_orders.json",
            )
            if paper_orders_payload is None:
                raise FileNotFoundError(latest_dir / "kelly_paper_orders.json")
            payload = build_kelly_trade_samples_payload(
                lab_state.experiments,
                paper_orders_payload,
                generated_at=args.generated_at,
            )
            latest_path = write_kelly_trade_samples(args.data_dir, payload)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(f"samples: {payload['sample_count']}")
        print(f"open_positions: {payload['open_position_count']}")
        print(f"skipped_orders: {payload['skipped_order_count']}")
        print(f"latest: {latest_path}")
        return 0
```

- [ ] **Step 4: Run targeted tests**

Run:

```bash
pytest tests/test_kelly_trade_samples_cli.py tests/test_kelly_trade_samples.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/cli.py tests/test_kelly_trade_samples_cli.py
git commit -m "feat: add kelly trade sample cli"
```

---

### Task 4: Kelly Lab Stats Overlay

**Files:**
- Modify: `src/open_trader/kelly_lab.py`
- Modify: `tests/test_kelly_lab.py`

- [ ] **Step 1: Write failing Kelly Lab tests**

Append to `tests/test_kelly_lab.py`:

```python
def test_load_kelly_lab_state_overlays_trade_sample_stats(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True)
    _write_minimal_kelly_templates(latest_dir)
    _write_minimal_kelly_experiments(latest_dir)
    (latest_dir / "kelly_trade_samples.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_trade_samples.v1",
                "generated_at": "2026-07-11 11:00",
                "stats_by_experiment": {
                    "trend_us": {
                        "completed_samples": 2,
                        "open_samples": 1,
                        "observed_win_rate": "50%",
                        "sample_stage": "insufficient",
                        "parameter_source": "futu_paper_order_samples",
                        "skipped_order_count": 3,
                        "last_recomputed_at": "2026-07-11 11:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = load_kelly_lab_state(data_dir)

    assert state.available is True
    stats = state.experiments[0]["stats"]
    assert stats["completed_samples"] == 2
    assert stats["open_samples"] == 1
    assert stats["parameter_source"] == "futu_paper_order_samples"
    assert stats["skipped_order_count"] == 3


def test_load_kelly_lab_state_rejects_invalid_trade_sample_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True)
    _write_minimal_kelly_templates(latest_dir)
    _write_minimal_kelly_experiments(latest_dir)
    (latest_dir / "kelly_trade_samples.json").write_text(
        json.dumps({"schema_version": "wrong", "stats_by_experiment": {}}),
        encoding="utf-8",
    )

    state = load_kelly_lab_state(data_dir)

    assert state.available is False
    assert "kelly_trade_samples.json schema_version" in state.error
```

If helpers do not exist, add these local helpers near other test helpers:

```python
def _write_minimal_kelly_templates(latest_dir: Path) -> None:
    latest_dir.joinpath("kelly_strategy_templates.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_strategy_templates.v1",
                "templates": [
                    {
                        "strategy_id": "trend_pullback_20d",
                        "strategy_name": "Trend Pullback",
                        "strategy_version": "v1",
                        "entry_rule_description": "Entry",
                        "exit_rule_description": "Exit",
                        "max_holding_days": 20,
                        "order_type": "limit",
                        "market_session": "regular",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_minimal_kelly_experiments(latest_dir: Path) -> None:
    latest_dir.joinpath("kelly_experiments.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_experiments.v1",
                "experiments": [
                    {
                        "experiment_id": "trend_us",
                        "experiment_name": "Trend US",
                        "strategy_id": "trend_pullback_20d",
                        "strategy_version": "v1",
                        "market": "US",
                        "start_date": "2026-07-07",
                        "paper_account": "futu_simulate_us",
                        "experiment_budget": "30000",
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
                                "name": "Apple",
                                "source": "watchlist",
                                "locked": True,
                                "per_symbol_budget": "30000",
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
            }
        ),
        encoding="utf-8",
    )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_kelly_lab.py::test_load_kelly_lab_state_overlays_trade_sample_stats tests/test_kelly_lab.py::test_load_kelly_lab_state_rejects_invalid_trade_sample_schema -q
```

Expected: overlay test still sees mock stats or invalid schema is ignored.

- [ ] **Step 3: Implement optional loader and overlay**

In `src/open_trader/kelly_lab.py`, add:

```python
TRADE_SAMPLES_SCHEMA_VERSION = "open_trader.kelly_trade_samples.v1"
```

In `load_kelly_lab_state()`, add `trade_samples_path` and wrap optional loads in
the existing unavailable-state pattern:

```python
    trade_samples_path = latest_dir / "kelly_trade_samples.json"
```

After strategy capital attach:

```python
    try:
        trade_sample_stats = _load_optional_trade_sample_stats(trade_samples_path)
        experiments = _attach_trade_sample_stats_to_experiments(
            experiments,
            trade_sample_stats,
        )
    except (ValueError, FileNotFoundError) as exc:
        return KellyLabState(available=False, error=str(exc))
```

Add helpers:

```python
def _load_optional_trade_sample_stats(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = _load_json_object(path)
    _validate_schema_version(
        payload,
        path,
        expected_schema_version=TRADE_SAMPLES_SCHEMA_VERSION,
    )
    stats_by_experiment = payload.get("stats_by_experiment")
    if not isinstance(stats_by_experiment, dict):
        raise ValueError(f"{path.name} must contain stats_by_experiment")
    validated: dict[str, dict[str, Any]] = {}
    for experiment_id, stats in stats_by_experiment.items():
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError(f"{path.name} contains invalid experiment id")
        if not isinstance(stats, dict):
            raise ValueError(f"{path.name} stats for {experiment_id} must be an object")
        validated[experiment_id] = copy.deepcopy(stats)
    return validated


def _attach_trade_sample_stats_to_experiments(
    experiments: list[dict[str, Any]],
    stats_by_experiment: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not stats_by_experiment:
        return experiments
    attached: list[dict[str, Any]] = []
    for experiment in experiments:
        normalized = copy.deepcopy(experiment)
        experiment_id = normalized.get("experiment_id")
        if isinstance(experiment_id, str) and experiment_id in stats_by_experiment:
            current_stats = normalized.get("stats")
            if not isinstance(current_stats, dict):
                current_stats = {}
            merged = copy.deepcopy(current_stats)
            merged.update(copy.deepcopy(stats_by_experiment[experiment_id]))
            normalized["stats"] = merged
        attached.append(normalized)
    return attached
```

- [ ] **Step 4: Run targeted tests**

Run:

```bash
pytest tests/test_kelly_lab.py::test_load_kelly_lab_state_overlays_trade_sample_stats tests/test_kelly_lab.py::test_load_kelly_lab_state_rejects_invalid_trade_sample_schema tests/test_kelly_lab.py::test_latest_kelly_experiments_are_single_market -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/kelly_lab.py tests/test_kelly_lab.py
git commit -m "feat: overlay kelly trade sample stats"
```

---

### Task 5: Dashboard Parameter Source

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`

- [ ] **Step 1: Add failing dashboard test**

Add or update a dashboard web test so a Kelly experiment stats object contains:

```javascript
stats: {
  completed_samples: 2,
  open_samples: 1,
  observed_win_rate: "50%",
  sample_stage: "insufficient",
  raw_win_rate: "50%",
  adjusted_win_rate: "50%",
  avg_net_win_pct: "10%",
  avg_net_loss_pct: "5%",
  payoff_ratio: "2",
  full_kelly_pct: "25%",
  fractional_kelly_pct: "6.25%",
  suggested_position_pct: "4%",
  sample_adjustment: "样本少于 200，向 50% 收缩",
  last_sample_closed_at: "2026-07-12 10:00",
  last_recomputed_at: "2026-07-12 10:01",
  parameter_source: "futu_paper_order_samples",
  skipped_order_count: 3
}
```

Assert rendered HTML includes:

```python
assert "参数来源" in html
assert "富途模拟盘订单样本" in html
assert "跳过订单" in html
assert "3" in html
```

- [ ] **Step 2: Run dashboard test and verify failure**

Run:

```bash
pytest tests/test_dashboard_web.py -q
```

Expected: new source/skipped text is missing.

- [ ] **Step 3: Update dashboard rendering**

In `src/open_trader/dashboard_static/dashboard.js`, update
`renderKellyParameterDerivation(stats)`:

```javascript
  const sourceLabel = item.parameter_source === "futu_paper_order_samples"
    ? "富途模拟盘订单样本"
    : item.parameter_source;
```

Add rows before latest sample rows:

```javascript
    ["参数来源", sourceLabel],
    ["跳过订单", item.skipped_order_count],
```

Also include `item.parameter_source` and `item.skipped_order_count` in
`hasDerivation`.

- [ ] **Step 4: Run dashboard tests**

Run:

```bash
pytest tests/test_dashboard_web.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: show kelly sample parameter source"
```

---

### Task 6: Fixtures, Playwright, and Checked-In Data

**Files:**
- Modify: `tests/e2e/fixtures/kelly-dashboard.json`
- Modify: relevant Playwright Kelly dashboard test file
- Add or update: `data/latest/kelly_trade_samples.json`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Generate sample artifact from current data**

Run:

```bash
python -m open_trader.cli kelly build-trade-samples --data-dir data --generated-at "2026-07-11 12:00"
```

Expected output includes:

```text
samples:
open_positions:
skipped_orders:
latest: data/latest/kelly_trade_samples.json
```

Because current synced paper orders may be empty, it is acceptable for sample
counts to be zero. The artifact must still exist and have valid stats for each
experiment.

- [ ] **Step 2: Update e2e fixture**

Use the existing fixture update process used by the repository. If no helper is
available, manually update `tests/e2e/fixtures/kelly-dashboard.json` so at least
one strategy has:

```json
{
  "parameter_source": "futu_paper_order_samples",
  "skipped_order_count": 0,
  "last_recomputed_at": "2026-07-11 12:00"
}
```

inside its `stats` object.

- [ ] **Step 3: Add Playwright assertion**

Find the Kelly e2e test with:

```bash
rg -n "Kelly|kelly|参数推导|模拟盘策略实验室" tests/e2e
```

Add assertions equivalent to:

```typescript
await expect(page.getByText("参数来源")).toBeVisible();
await expect(page.getByText("富途模拟盘订单样本")).toBeVisible();
await expect(page.getByText("跳过订单")).toBeVisible();
```

- [ ] **Step 4: Update changelog**

Add an entry to `CHANGELOG.md` under the current unreleased section:

```markdown
- Added Kelly trade sample generation from synced Futu paper orders, including
  derived win rate, payoff ratio, Kelly sizing stats, and dashboard source
  visibility.
```

- [ ] **Step 5: Run Playwright Kelly test**

Run the focused command for the Kelly e2e file. If the exact file is unknown
after `rg`, run:

```bash
npx playwright test tests/e2e
```

Expected: Playwright passes and the Kelly page visibly includes parameter source
and skipped-order count.

- [ ] **Step 6: Commit**

```bash
git add data/latest/kelly_trade_samples.json tests/e2e/fixtures/kelly-dashboard.json tests/e2e CHANGELOG.md
git commit -m "test: cover kelly trade sample dashboard"
```

---

### Task 7: Full Verification and Cleanup

**Files:**
- No planned code edits unless verification exposes a defect.

- [ ] **Step 1: Run Kelly-specific tests**

Run:

```bash
pytest tests/test_kelly_trade_samples.py tests/test_kelly_trade_samples_cli.py tests/test_kelly_lab.py tests/test_dashboard_web.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run CLI workflow directly**

Run:

```bash
python -m open_trader.cli kelly build-trade-samples --data-dir data --generated-at "2026-07-11 12:30"
python -m open_trader.cli dashboard --data-dir data >/tmp/open_trader_dashboard_check.html
rg -n "富途模拟盘订单样本|跳过订单|参数来源" /tmp/open_trader_dashboard_check.html
```

Expected:

- first command prints sample/open/skipped counts and output path
- generated dashboard HTML contains parameter source and skipped-order labels

- [ ] **Step 3: Run broad Python verification**

Run:

```bash
python -m compileall src/open_trader
pytest tests/test_kelly_trade_samples.py tests/test_kelly_trade_samples_cli.py tests/test_kelly_lab.py tests/test_dashboard_web.py tests/test_dashboard.py -q
git diff --check
```

Expected: compileall succeeds, pytest passes, and `git diff --check` has no output.

- [ ] **Step 4: Inspect background process risk**

This feature changes generated artifacts and dashboard rendering, not a
long-running watcher. Still run:

```bash
screen -ls || true
launchctl list | rg -i "open_trader|kelly|trader" || true
```

Expected: no stale Kelly dashboard or automation process needs restart for this
feature. If one is found and points at this repo, restart it before claiming live
behavior changed.

- [ ] **Step 5: Final commit if verification caused fixes**

Only if Step 1-4 required edits:

```bash
git status --short
git add src/open_trader/kelly_trade_samples.py src/open_trader/kelly_lab.py src/open_trader/cli.py src/open_trader/dashboard_static/dashboard.js tests/test_kelly_trade_samples.py tests/test_kelly_trade_samples_cli.py tests/test_kelly_lab.py tests/test_dashboard_web.py tests/e2e/fixtures/kelly-dashboard.json CHANGELOG.md data/latest/kelly_trade_samples.json
git commit -m "fix: stabilize kelly trade sample workflow"
```

- [ ] **Step 6: Report completion**

Summarize:

- commits created
- exact test commands and pass output
- direct CLI output counts
- Playwright result
- whether any background process was found or restarted

---

## Self-Review Notes

Spec coverage:

- Artifact schema: Task 1 and Task 2.
- Pairing rule and diagnostics: Task 1 and Task 2.
- Stats formula and small-sample shrinkage: Task 2.
- Kelly Lab overlay: Task 4.
- Dashboard source/skipped UI: Task 5.
- CLI: Task 3.
- Tests and Playwright: Task 1 through Task 7.

No FIFO, partial fills, split exits, repeated entries, or real-money execution are
included in this plan.
