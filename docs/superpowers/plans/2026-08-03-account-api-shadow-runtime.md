# Account API Shadow Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a loopback-only, read-only Account API shadow process that serves the frozen v1 snapshot contract and proves live equivalence to the raw Account publication.

**Architecture:** Add a deep `account_snapshot` read module over the existing atomic JSON publication and a thin stdlib `account_api` HTTP module. Route both operator commands lazily in `open_trader.__main__`, keeping the process free of broker and Dashboard imports; launchd, parity, and operator proof remain independent of Gateway.

**Tech Stack:** Python 3.12, stdlib `dataclasses`, `datetime`, `hashlib`, `http.server`, `json`, `urllib`, macOS launchd shell scripts, pytest.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-03-account-api-shadow-runtime-design.md` and the normative `docs/superpowers/specs/2026-08-03-account-v1-contract.md`.
- Account Sync Worker remains the sole writer; Account API must not write files, call brokers, trigger sync, or publish a second snapshot file.
- Operator runtime is fixed to `127.0.0.1:8768` with `mode: shadow`; no production switch or operator port override exists in R2.
- The only routes are `GET /healthz` and `GET /api/v1/account/snapshot`; add no Gateway route, Dashboard behavior, CORS, WebSocket, delta endpoint, or static page.
- Stable reads use exact `A1/Q1/heartbeat/A2/Q2` ordering, byte equality, `quote_as_of == last_success_at`, and at most three attempts.
- Raw `account_sync_state.json.dashboard_projection` is the only parity truth source; Legacy `/api/dashboard` is not a dependency or gate.
- API and Worker must run the same 40-character lowercase Git SHA before snapshot returns live `200`.
- Use only existing dependencies and stdlib; do not add a shared HTTP or launchd framework.
- Preserve the user's dirty root checkout. Work only in `.worktrees/issue-20-account-api-shadow` until the verified fast-forward integration step.
- Add and commit the dated operator-facing `CHANGELOG.md` entry before merging to local `main`.
- This task has no Dashboard or Gateway behavior change: do not run `make acceptance` and do not capture screenshots.
- Leave the shadow API running for operator review; do not close #20 or unlock R3 before confirmation.

---

## File Map

- `src/open_trader/account_sync_state.py`: expose one strict public predicate for the already-owned Account publication schema.
- `src/open_trader/account_snapshot.py`: stable reads, v1 mapping, freshness, errors, IDs, generations, and ETag.
- `src/open_trader/account_api.py`: loopback HTTP, liveness, lazy command parsers, and independent live parity.
- `src/open_trader/__main__.py`: route `account-api` and `account-api-parity` before importing the monolithic CLI.
- `tests/test_account_api.py`: publication fixtures plus snapshot, HTTP, import-boundary, CLI, and parity behavior.
- `ops/launchd/com.open-trader.account-api.plist.template`: fixed shadow process definition.
- `scripts/install_account_api_launchd.sh`: independent render, install, bootout-wait, and exact-runtime readiness.
- `scripts/uninstall_account_api_launchd.sh`: safe, idempotent Account API removal.
- `tests/test_account_api_launchd.py`: plist and launchd lifecycle contract.
- `docs/operations/account-api-shadow-runtime.md`: install, inspect, parity, stop, and rollback runbook.
- `README.md`: link the Account worker chain to the new shadow read process without presenting it as a browser entry.
- `CHANGELOG.md`: dated operator-facing R2 entry and verification evidence.

---

### Task 1: Build deterministic Account v1 snapshots

**Files:**
- Create: `src/open_trader/account_snapshot.py`
- Create: `tests/test_account_api.py`
- Modify: `src/open_trader/account_sync_state.py:1032-1042`

**Interfaces:**
- Consumes: existing `ACCOUNT_STATE_VERSION`, `REQUIRED_BROKERS`, `effective_source_status`, and the current `dashboard_projection` persistence value.
- Produces: `is_valid_account_publication(value: object) -> bool`, `SnapshotResult(status_code: int, payload: dict[str, object], etag: str | None)`, and `load_account_snapshot(data_dir: Path, *, api_git_sha: str, now: datetime) -> SnapshotResult`.

- [ ] **Step 1: Add one failing happy-path contract test and its real publication fixture**

Create `tests/test_account_api.py` with a fixture helper that uses current writer functions rather than inventing another persistence schema:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from open_trader.account_snapshot import load_account_snapshot
from open_trader.account_sync_state import (
    LIVE_BROKERS,
    REQUIRED_BROKERS,
    BrokerAccountCandidate,
    accept_candidate,
    empty_account_sync_state,
    with_dashboard_projection,
    write_json_atomic,
)
from open_trader.models import AssetClass, CashBalance, Market, Position


SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime.fromisoformat("2026-08-03T12:00:05+08:00")


def _write_publication(data_dir: Path, *, worker_sha: str = SHA) -> None:
    account_as_of = "2026-08-03T12:00:00+08:00"
    quote_as_of = "2026-08-03T12:00:04+08:00"
    state = empty_account_sync_state()
    for index, broker in enumerate(REQUIRED_BROKERS):
        live = broker in LIVE_BROKERS
        alias = f"{broker}_main"
        state = accept_candidate(
            state,
            BrokerAccountCandidate(
                broker=broker,
                source_kind="live" if live else "statement",
                data_as_of=account_as_of if live else "2026-07-31",
                period="2026-08" if live else "2026-07",
                positions=(
                    Position(
                        statement_id="" if live else f"2026-07-31-{broker}",
                        broker=broker,
                        account_alias=alias,
                        market=Market.US,
                        asset_class=AssetClass.STOCK,
                        symbol=f"TEST{index}",
                        name=f"Test {index}",
                        currency="USD",
                        quantity=Decimal("1"),
                        cost_price=Decimal("10"),
                        last_price=Decimal("11"),
                        market_value=Decimal("11"),
                        cost_value=Decimal("10"),
                        unrealized_pnl=Decimal("1"),
                        confidence="high",
                        notes="",
                    ),
                ),
                cash=(
                    CashBalance(
                        statement_id="" if live else f"2026-07-31-{broker}",
                        broker=broker,
                        account_alias=alias,
                        currency="USD",
                        cash_balance=Decimal("5"),
                        available_balance=Decimal("4"),
                        confidence="high",
                        notes="",
                    ),
                ),
                fx_rates=(
                    {"account_alias": alias, "currency": "USD", "rate_to_hkd": "7.8"},
                ) if live else (),
                summary={"position_count": 1, "cash_count": 1},
            ),
            attempted_at=account_as_of,
        )
    quotes = {
        "status": "ok",
        "requested_count": 2,
        "quote_count": 2,
        "missing_count": 0,
        "fetched_at": quote_as_of,
        "last_success_at": quote_as_of,
        "stale": False,
        "quotes": {
            f"US.TEST{index}": {
                "market": "US",
                "symbol": f"TEST{index}",
                "status": "ok",
                "last_price": "11",
                "price_session": "regular",
                "price_time": quote_as_of,
                "fetched_at": quote_as_of,
                "stale": False,
            }
            for index in (0, 1)
        },
        "diagnostic": {},
    }
    state = with_dashboard_projection(state, quotes, generated_at=quote_as_of)
    write_json_atomic(data_dir / "latest/account_sync_state.json", state)
    write_json_atomic(data_dir / "latest/quotes.json", quotes)
    write_json_atomic(
        data_dir / "account_sync/controller_status.json",
        {
            "schema_version": "open_trader.account_sync.controller.v1",
            "pid": 123,
            "started_at": account_as_of,
            "working_directory": "/tmp/open-trader",
            "git_sha": worker_sha,
            "heartbeat_at": quote_as_of,
            "phase": "idle",
            "account_loop": {"status": "ok"},
            "quote_loop": {"status": "ok"},
            "blocker": None,
        },
    )


def test_snapshot_maps_current_publication_to_frozen_v1_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["schema_version"] == 1
    assert result.payload["status"] == "healthy"
    assert result.payload["stale"] is False
    assert result.payload["generated_at"] == "2026-08-03T12:00:04+08:00"
    assert result.payload["quote_as_of"] == "2026-08-03T12:00:04+08:00"
    assert result.payload["release"] == {"api_git_sha": SHA, "worker_git_sha": SHA}
    assert result.payload["sources"]["account"]["as_of"] == "2026-08-03T12:00:00+08:00"
    assert [row["broker"] for row in result.payload["broker_summaries"]] == sorted(REQUIRED_BROKERS)
    assert result.payload["positions"] == sorted(
        result.payload["positions"],
        key=lambda row: (
            row["broker"], row["account_alias"], row["market"],
            row["asset_class"], row["symbol"], row["position_id"],
        ),
    )
    assert result.payload["cash_balances"] == sorted(
        result.payload["cash_balances"],
        key=lambda row: (row["broker"], row["account_alias"], row["currency"]),
    )
    position = next(row for row in result.payload["positions"] if row["broker"] == "futu")
    canonical = json.dumps(["US", "stock", "TEST0"], ensure_ascii=False, separators=(",", ":"))
    instrument_id = "ins_" + hashlib.sha256(canonical.encode()).hexdigest()
    assert position["instrument_id"] == instrument_id
    assert position["position_id"] == "pos_" + hashlib.sha256(
        json.dumps(["futu", "futu_main", instrument_id], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert result.payload["account_generation"].startswith("sha256:")
    assert result.payload["snapshot_generation"].startswith("sha256:")
    assert result.etag == f'"account-v1-{result.payload["snapshot_generation"].removeprefix("sha256:")}"'
    account_input = {
        "summary": result.payload["summary"],
        "broker_summaries": result.payload["broker_summaries"],
        "positions": result.payload["positions"],
        "cash_balances": result.payload["cash_balances"],
        "accepted_account_as_of": result.payload["sources"]["account"]["as_of"],
        "accepted_broker_data_as_of": {
            broker: result.payload["sources"]["account"]["brokers"][broker]["data_as_of"]
            for broker in sorted(REQUIRED_BROKERS)
        },
    }
    assert result.payload["account_generation"] == _contract_sha(account_input)
    visible = dict(result.payload)
    visible.pop("snapshot_generation")
    assert result.payload["snapshot_generation"] == _contract_sha(visible)
    assert not ({"risk_flag", "actionable", "decision_plan"} & result.payload.keys())
```

Add `_contract_sha(value)` in the test as an independent canonical JSON SHA-256 helper; do not import the production hash helper.

- [ ] **Step 2: Run the test to verify the missing module is the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py::test_snapshot_maps_current_publication_to_frozen_v1_contract -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'open_trader.account_snapshot'`.

- [ ] **Step 3: Expose the existing strict publication predicate**

Add this public wrapper beside `_is_valid_state` in `account_sync_state.py`; do not duplicate the broker or projection field validators:

```python
def is_valid_account_publication(value: object) -> bool:
    return (
        _is_valid_state(value)
        and isinstance(value, dict)
        and _is_valid_dashboard_projection(value.get("dashboard_projection"))
    )
```

- [ ] **Step 4: Implement the minimal successful snapshot builder**

Create `account_snapshot.py` with this public surface and deterministic helpers:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .account_sync_state import (
    REQUIRED_BROKERS,
    effective_source_status,
    is_valid_account_publication,
)


@dataclass(frozen=True)
class SnapshotResult:
    status_code: int
    payload: dict[str, object]
    etag: str | None


def load_account_snapshot(
    data_dir: Path, *, api_git_sha: str, now: datetime
) -> SnapshotResult:
    account, quotes, worker_sha = _load_stable_publication(data_dir)
    return _build_snapshot(account, quotes, api_git_sha, worker_sha, now)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _opaque_id(prefix: str, values: list[str]) -> str:
    return prefix + hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
```

Implement `_build_snapshot` by copying only Account-owned projection fields, adding IDs, sorting exactly as the R1 contract requires, and deriving `sources`. Compute `accepted_account_as_of` from the latest broker `last_success_at`, and use that same value for `sources.account.as_of`; do not use persistence `generation`, because `record_source_failure` changes that compatibility token even when accepted facts do not change:

```python
accepted_account_as_of = max(
    (account["brokers"][broker]["last_success_at"] for broker in REQUIRED_BROKERS),
    key=datetime.fromisoformat,
)
account_generation_input = {
    "summary": summary,
    "broker_summaries": broker_summaries,
    "positions": positions,
    "cash_balances": cash_balances,
    "accepted_account_as_of": accepted_account_as_of,
    "accepted_broker_data_as_of": {
        broker: account["brokers"][broker]["data_as_of"]
        for broker in sorted(REQUIRED_BROKERS)
    },
}
account_generation = _sha256(account_generation_input)
payload_without_snapshot_generation = {
    "schema_version": 1,
    "account_generation": account_generation,
    "generated_at": projection["generated_at"],
    "quote_as_of": projection["quote_as_of"],
    "status": "healthy",
    "stale": False,
    "sources": sources,
    "release": {"api_git_sha": api_git_sha, "worker_git_sha": worker_sha},
    "summary": summary,
    "broker_summaries": broker_summaries,
    "positions": positions,
    "cash_balances": cash_balances,
    "errors": [],
}
snapshot_generation = _sha256(payload_without_snapshot_generation)
payload_tail = dict(payload_without_snapshot_generation)
schema_version = payload_tail.pop("schema_version")
payload = {
    "schema_version": schema_version,
    "snapshot_generation": snapshot_generation,
    **payload_tail,
}
etag = f'"account-v1-{snapshot_generation.removeprefix("sha256:")}"'
```

The final payload must be assembled in contract field order even though canonical generation hashing sorts object keys. `_load_stable_publication` in this task may perform the final five reads immediately; Task 2 adds race and unavailable coverage.

- [ ] **Step 5: Run the focused test and existing Account state tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py::test_snapshot_maps_current_publication_to_frozen_v1_contract tests/test_account_sync_state.py -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the deterministic snapshot slice**

```bash
git add src/open_trader/account_snapshot.py src/open_trader/account_sync_state.py tests/test_account_api.py
git commit -m "feat: build Account v1 snapshots (#20)"
```

---

### Task 2: Enforce stable reads and frozen status semantics

**Files:**
- Modify: `src/open_trader/account_snapshot.py`
- Modify: `tests/test_account_api.py`

**Interfaces:**
- Consumes: `load_account_snapshot(data_dir, *, api_git_sha, now)` from Task 1.
- Produces: final three-attempt stable reader and all contract `200 stale` / `503` results without changing the public function signature.

- [ ] **Step 1: Add parameterized unavailable and stale tests**

Add exact cases for missing Account, malformed Account JSON, unsupported Account version, missing Quotes, invalid Quotes, release mismatch, broker failure with retained facts, quote failure with complete retained facts, and quote `partial` with zero missing instruments. Also cover the two no-partial-data boundaries: an Account source with no accepted `last_success_at` returns `503 account_publication_missing`, and Quotes `partial` with `missing_count > 0` or `failed` without complete retained coverage returns `503 quotes_publication_missing`:

```python
import pytest
from open_trader.account_sync_state import record_source_failure, write_json_atomic


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda data: (data / "latest/account_sync_state.json").unlink(), "account_publication_missing"),
        (lambda data: (data / "latest/account_sync_state.json").write_text("{bad", encoding="utf-8"), "account_publication_invalid"),
        (lambda data: _rewrite_json(data / "latest/account_sync_state.json", {"version": 2}), "account_schema_unsupported"),
        (lambda data: (data / "latest/quotes.json").unlink(), "quotes_publication_missing"),
        (lambda data: (data / "latest/quotes.json").write_text("[]", encoding="utf-8"), "quotes_publication_invalid"),
    ],
)
def test_snapshot_returns_contract_503_for_invalid_publication(tmp_path: Path, mutate, code: str) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    mutate(data_dir)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["status"] == "unavailable"
    assert result.payload["errors"][0]["code"] == code


def test_snapshot_returns_stale_with_retained_broker_facts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    before = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)
    path = data_dir / "latest/account_sync_state.json"
    account = json.loads(path.read_text(encoding="utf-8"))
    account = record_source_failure(
        account,
        "futu",
        attempted_at="2026-08-03T12:00:03+08:00",
        message="secret upstream response",
    )
    account["dashboard_projection"] = json.loads(path.read_text(encoding="utf-8"))["dashboard_projection"]
    write_json_atomic(path, account)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["status"] == "stale"
    assert result.payload["stale"] is True
    assert result.payload["account_generation"] == before.payload["account_generation"]
    assert result.payload["errors"] == [{
        "code": "broker_refresh_failed",
        "source": "futu",
        "message": "Latest broker refresh failed; serving last accepted account facts",
        "retryable": True,
    }]
    assert "secret" not in json.dumps(result.payload)


def test_quote_age_alone_does_not_stale_a_successful_publication(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    account = json.loads(account_path.read_text(encoding="utf-8"))
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    old_quote = "2026-08-02T16:00:00+08:00"
    account["dashboard_projection"]["quote_as_of"] = old_quote
    quotes["last_success_at"] = old_quote
    quotes["fetched_at"] = old_quote
    for row in quotes["quotes"].values():
        row["price_time"] = old_quote
        row["fetched_at"] = old_quote
    write_json_atomic(account_path, account)
    write_json_atomic(quotes_path, quotes)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["sources"]["quotes"]["status"] == "healthy"


def test_live_account_age_stales_account_but_not_statement_sources(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)

    result = load_account_snapshot(
        data_dir,
        api_git_sha=SHA,
        now=datetime.fromisoformat("2026-08-03T12:03:01+08:00"),
    )

    assert result.status_code == 200
    assert result.payload["status"] == "stale"
    assert result.payload["sources"]["account"]["brokers"]["futu"]["status"] == "stale"
    assert result.payload["sources"]["account"]["brokers"]["phillips"]["status"] == "healthy"
```

Add `_rewrite_json(path, updates)` as a concrete test helper that loads an object, applies `dict.update(updates)`, and writes it with `write_json_atomic`.

- [ ] **Step 2: Add deterministic race tests through the private file-read seam**

Keep the public interface unchanged and monkeypatch only a private `_read_bytes(path)` wrapper:

```python
import open_trader.account_snapshot as account_snapshot


def test_snapshot_retries_one_account_quote_read_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    real_read = Path.read_bytes
    account_path = data_dir / "latest/account_sync_state.json"
    calls = 0

    def racing_read(path: Path) -> bytes:
        nonlocal calls
        body = real_read(path)
        if path == account_path:
            calls += 1
            if calls == 2:
                changed = json.loads(body)
                changed["generation"] = "2026-08-03T12:00:01+08:00"
                return json.dumps(changed, sort_keys=True).encode()
        return body

    monkeypatch.setattr(account_snapshot, "_read_bytes", racing_read)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert calls >= 4


def test_snapshot_returns_unstable_after_three_races(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    real_read = Path.read_bytes
    counter = 0

    def always_changing(path: Path) -> bytes:
        nonlocal counter
        body = real_read(path)
        if path.name == "account_sync_state.json":
            counter += 1
            return body + str(counter).encode()
        return body

    monkeypatch.setattr(account_snapshot, "_read_bytes", always_changing)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.payload["errors"][0]["code"] == "account_publication_unstable"
    assert counter == 6
```

- [ ] **Step 3: Run the new tests to verify the missing semantics**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py -v
```

Expected: the new stale, unavailable, and race assertions fail against Task 1 behavior.

- [ ] **Step 4: Implement exact publication classification and retry behavior**

Implement `_read_bytes` and the five-read loop:

```python
MAX_STABLE_READ_ATTEMPTS = 3


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _load_stable_publication(data_dir: Path) -> tuple[dict[str, object], dict[str, object], str]:
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    heartbeat_path = data_dir / "account_sync/controller_status.json"
    for _attempt in range(MAX_STABLE_READ_ATTEMPTS):
        account_first = _read_required(account_path, "account")
        quotes_first = _read_required(quotes_path, "quotes")
        heartbeat = _read_required(heartbeat_path, "heartbeat")
        account_second = _read_required(account_path, "account")
        quotes_second = _read_required(quotes_path, "quotes")
        if account_first != account_second or quotes_first != quotes_second:
            continue
        account = _parse_account(account_first)
        quotes = _parse_quotes(quotes_first)
        worker_sha = _worker_sha(heartbeat)
        if account["dashboard_projection"]["quote_as_of"] != quotes["last_success_at"]:
            continue
        return account, quotes, worker_sha
    raise PublicationUnavailable("account_publication_unstable")
```

Use one private `PublicationUnavailable(code: str)` exception to carry the fixed machine code. `load_account_snapshot` catches it and returns the R1 unavailable envelope with `retryable: True`; it never forwards exception text.

Classify before normalization:

- absent Account path -> `account_publication_missing`;
- Account JSON parse/type failure -> `account_publication_invalid`;
- `version != ACCOUNT_STATE_VERSION` -> `account_schema_unsupported`;
- failed `is_valid_account_publication` -> `account_publication_invalid`;
- absent Quotes path -> `quotes_publication_missing`;
- Quotes parse/type/required-field failure -> `quotes_publication_invalid`;
- missing, invalid, or SHA-less heartbeat -> `account_release_mismatch`;
- unequal API/Worker SHA -> `account_release_mismatch`.

Map source freshness exactly:

```python
broker_stale = source["status"] == "failed" or effective_source_status(source, now=now) == "stale"
quotes_healthy = quotes["status"] == "ok" or (
    quotes["status"] == "partial" and quotes["missing_count"] == 0
)
quotes_stale = quotes["status"] == "failed" and bool(quotes["last_success_at"]) and bool(quotes["quotes"])
```

Before using those status booleans, derive the required quote `(market, symbol)` pairs from positions owned by `live` broker sources and require a valid `status == "ok"` quote row for every pair. A failed refresh is stale only when `last_success_at` exists and that complete retained coverage remains; a partial refresh with any missing instrument, or a failed refresh without complete retained coverage, is unavailable. Set `sources.account.as_of = accepted_account_as_of`, `sources.quotes.as_of = quote_as_of`, and use only `broker_refresh_failed` / `quotes_refresh_failed` as stale reason and error codes with fixed sanitized messages.

Do not use `dashboard_quotes.load_published_quotes`: that module imports broker quote code and applies a 15-second wall-clock rule that conflicts with the frozen closed-market semantics.

- [ ] **Step 5: Run Account API and persistence regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py tests/test_account_sync_state.py tests/test_account_sync_worker.py -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit stable reading and status semantics**

```bash
git add src/open_trader/account_snapshot.py tests/test_account_api.py
git commit -m "feat: enforce Account publication semantics (#20)"
```

---

### Task 3: Serve the loopback shadow HTTP interface without domain imports

**Files:**
- Create: `src/open_trader/account_api.py`
- Modify: `src/open_trader/__main__.py:6-13`
- Modify: `tests/test_account_api.py`

**Interfaces:**
- Consumes: `SnapshotResult` and `load_account_snapshot` from Task 2.
- Produces: `create_account_api(data_dir: Path, *, host: str, port: int, runtime_metadata: Mapping[str, object] | None = None) -> ThreadingHTTPServer`, `serve_account_api(data_dir: Path) -> None`, and `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Add HTTP, ETag, health, loopback, and import-boundary tests**

Add a `_running(server)` context manager that starts `server.serve_forever` in a daemon thread and always calls `shutdown` and `server_close`. Then add:

```python
from contextlib import contextmanager
from http import HTTPStatus
from http.server import ThreadingHTTPServer
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import open_trader.account_api as account_api


def test_account_api_health_snapshot_etag_and_not_found(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    server = account_api.create_account_api(
        data_dir,
        host="127.0.0.1",
        port=0,
        runtime_metadata={
            "pid": 321,
            "started_at": "2026-08-03T12:00:00+08:00",
            "cwd": "/tmp/open-trader",
            "api_git_sha": SHA,
        },
    )
    with _running(server):
        base = f"http://127.0.0.1:{server.server_address[1]}"
        health = _get_json(base + "/healthz")
        assert health["module"] == "account_api"
        assert health["mode"] == "shadow"
        assert health["release_match"] is True
        with urllib.request.urlopen(base + "/api/v1/account/snapshot") as response:
            payload = json.load(response)
            etag = response.headers["ETag"]
            assert response.headers.get("Access-Control-Allow-Origin") is None
        request = urllib.request.Request(
            base + "/api/v1/account/snapshot",
            headers={"If-None-Match": etag},
        )
        with pytest.raises(urllib.error.HTTPError) as unchanged:
            urllib.request.urlopen(request)
        assert unchanged.value.code == HTTPStatus.NOT_MODIFIED
        assert unchanged.value.read() == b""
        assert payload["snapshot_generation"].removeprefix("sha256:") in etag
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/api/unknown")
        assert missing.value.code == HTTPStatus.NOT_FOUND
        assert json.load(missing.value)["code"] == "not_found"


def test_account_api_rejects_non_loopback_listener(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        account_api.create_account_api(tmp_path, host="0.0.0.0", port=8768)


def test_account_api_entrypoint_does_not_import_domain_adapters() -> None:
    script = '''
import runpy, sys
sys.argv = ["open_trader", "account-api", "--help"]
try:
    runpy.run_module("open_trader", run_name="__main__")
except SystemExit as error:
    assert error.code == 0
print("LOADED_MODULES")
print("\\n".join(sorted(sys.modules)))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = completed.stdout.split("LOADED_MODULES\n", 1)[1].splitlines()
    for forbidden in (
        "open_trader.account_sync_worker",
        "open_trader.frontend_gateway",
        "open_trader.dashboard_web",
        "open_trader.futu_account",
        "open_trader.tiger_account",
        "open_trader.futu_quote",
    ):
        assert forbidden not in loaded
```

Add a CLI-default test by monkeypatching `serve_account_api` and asserting `main(["--data-dir", str(data_dir)])` passes the path and no host/port/mode arguments.

- [ ] **Step 2: Run the HTTP tests to verify the module is missing**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py -k 'http or loopback or entrypoint or cli' -v
```

Expected: collection fails because `open_trader.account_api` does not exist.

- [ ] **Step 3: Implement the thin HTTP module**

Use `BaseHTTPRequestHandler` and `ThreadingHTTPServer`. The factory validates `ipaddress.ip_address(host).is_loopback`, captures one startup runtime mapping, and defines only `do_GET`:

```python
def do_GET(self) -> None:
    path = urlsplit(self.path).path
    if path == "/healthz":
        worker_sha = load_worker_git_sha(data_dir)
        self._send_json({
            "schema_version": "open_trader.account_api.health.v1",
            "module": "account_api",
            "status": "ok",
            "mode": "shadow",
            "pid": runtime["pid"],
            "started_at": runtime["started_at"],
            "api_git_sha": runtime["api_git_sha"],
            "worker_git_sha": worker_sha,
            "release_match": bool(worker_sha) and worker_sha == runtime["api_git_sha"],
            "source": "account_sync_worker_publication",
        })
        return
    if path == "/api/v1/account/snapshot":
        result = load_account_snapshot(
            data_dir,
            api_git_sha=str(runtime["api_git_sha"]),
            now=datetime.now().astimezone(),
        )
        if result.etag and self.headers.get("If-None-Match") == result.etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", result.etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(result.payload, result.status_code, etag=result.etag)
        return
    self._send_json({
        "schema_version": "open_trader.account_api.error.v1",
        "code": "not_found",
        "message": "Not found",
    }, HTTPStatus.NOT_FOUND)
```

Suppress default request logging, handle broken pipes, set `daemon_threads = True`, and print exactly one startup record:

```text
account_api_runtime: {"schema_version":"open_trader.account_api.runtime.v1","module":"account_api","mode":"shadow","pid":321,"started_at":"2026-08-03T12:00:00+08:00","cwd":"/tmp/open-trader","api_git_sha":"0123456789abcdef0123456789abcdef01234567","host":"127.0.0.1","port":8768}
```

`main` parses only `--data-dir` with default `Path("data")`; `serve_account_api` always binds `127.0.0.1:8768`.

Build default runtime metadata locally in `account_api.py` from `os.getpid()`, one process start timestamp, `Path.cwd().resolve()`, and `git -C <cwd> rev-parse HEAD`. Validate the SHA as 40 lowercase hex before serving snapshots; do not import the Gateway runtime helper or add a shared runtime abstraction for one second caller.

Add `load_worker_git_sha(data_dir: Path) -> str` to `account_snapshot.py`; it returns an empty string for missing, invalid, unsupported, or SHA-less heartbeat so health remains live and reports `release_match: false`.

- [ ] **Step 4: Add lazy command dispatch before monolithic CLI import**

Modify `__main__.py`:

```python
if args[:1] == ["account-api"]:
    from .account_api import main as account_api_main

    return account_api_main(args[1:])
```

Keep this branch beside the existing `frontend-gateway` lazy branch and before `from .cli import main as cli_main`.

- [ ] **Step 5: Run HTTP, import-boundary, and existing Gateway tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py tests/test_frontend_gateway.py tests/test_frontend_gateway_cli.py -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the shadow HTTP slice**

```bash
git add src/open_trader/account_api.py src/open_trader/account_snapshot.py src/open_trader/__main__.py tests/test_account_api.py
git commit -m "feat: serve shadow Account API (#20)"
```

---

### Task 4: Prove live parity against raw publication

**Files:**
- Modify: `src/open_trader/account_api.py`
- Modify: `src/open_trader/__main__.py`
- Modify: `tests/test_account_api.py`

**Interfaces:**
- Consumes: fixed HTTP snapshot route and raw Account/Quotes files.
- Produces: `ParityResult(status: Literal["PASS", "FAIL", "BLOCKED"], reason: str, account_generation: str, quote_as_of: str)`, `check_account_api_parity(data_dir: Path, *, base_url: str = "http://127.0.0.1:8768", attempts: int = 3) -> ParityResult`, and `parity_main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Add PASS, deterministic FAIL, and continuous-churn BLOCKED tests**

Use the real temporary HTTP server for PASS. For FAIL, monkeypatch `_fetch_snapshot` to corrupt one returned `position_id`; for BLOCKED, monkeypatch `_read_parity_bytes` to change Account bytes on every second read:

```python
def test_live_parity_passes_against_raw_publication(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    server = account_api.create_account_api(
        data_dir,
        host="127.0.0.1",
        port=0,
        runtime_metadata={"pid": 1, "started_at": NOW.isoformat(), "cwd": str(tmp_path), "api_git_sha": SHA},
    )
    with _running(server):
        result = account_api.check_account_api_parity(
            data_dir,
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
        )
    assert result.status == "PASS"
    assert result.account_generation.startswith("sha256:")
    assert result.quote_as_of == "2026-08-03T12:00:04+08:00"


def test_live_parity_fails_on_wrong_opaque_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    payload, etag = _snapshot_payload_and_etag(data_dir)
    payload["positions"][0]["position_id"] = "pos_wrong"
    monkeypatch.setattr(account_api, "_fetch_snapshot", lambda _url: (200, payload, etag))

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "FAIL"
    assert result.reason == "position_id_mismatch"


def test_live_parity_blocks_when_raw_publication_never_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    real_read = Path.read_bytes
    counter = 0

    def changing(path: Path) -> bytes:
        nonlocal counter
        body = real_read(path)
        if path.name == "account_sync_state.json":
            counter += 1
            return body + str(counter).encode()
        return body

    monkeypatch.setattr(account_api, "_read_parity_bytes", changing)

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "BLOCKED"
    assert result.reason == "publication_changed_during_parity"
```

Add a CLI test asserting `parity_main` prints `PASS`, `FAIL`, or `BLOCKED` as the first token and returns exit `0`, `1`, or `2` respectively.

- [ ] **Step 2: Run parity tests to verify the interface is absent**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py -k parity -v
```

Expected: failures report missing `check_account_api_parity`, `ParityResult`, or `parity_main`.

- [ ] **Step 3: Implement independent raw comparison**

For each of three attempts:

1. read Account and Quotes exact bytes;
2. fetch `/api/v1/account/snapshot`;
3. read Account and Quotes exact bytes again;
4. retry if either raw byte sequence changed;
5. compare only when both raw files are pinned.

Parse the pinned `dashboard_projection` directly. Compare summary, broker summaries after sorting, every common position/cash field, `generated_at`, `quote_as_of`, broker source kind/times, Account generation, snapshot generation, and ETag.

Independently compute IDs inside the parity implementation rather than calling the snapshot builder's `_opaque_id` helper:

```python
canonical = json.dumps(
    [row["market"].strip().upper(), row["asset_class"].strip().lower(), row["symbol"].strip().upper()],
    ensure_ascii=False,
    separators=(",", ":"),
)
instrument_id = "ins_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
position_id = "pos_" + hashlib.sha256(
    json.dumps(
        [row["broker"].strip().lower(), row["account_alias"].strip(), instrument_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
```

Map network errors, non-200 responses other than retryable `account_publication_unstable`, malformed JSON, missing ETag, and deterministic field mismatches to `FAIL`. Map API `503 account_publication_unstable` and exhausted raw churn to `BLOCKED`.

- [ ] **Step 4: Route and parse the parity operator command lazily**

Add to `__main__.py` before importing `.cli`:

```python
if args[:1] == ["account-api-parity"]:
    from .account_api import parity_main

    return parity_main(args[1:])
```

`parity_main` accepts only `--data-dir` with default `data` and always checks `http://127.0.0.1:8768`. Tests call `check_account_api_parity` directly when they need a temporary port.

- [ ] **Step 5: Run all Account API tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit live parity**

```bash
git add src/open_trader/account_api.py src/open_trader/__main__.py tests/test_account_api.py
git commit -m "feat: verify live Account API parity (#20)"
```

---

### Task 5: Install Account API as an independent launchd job

**Files:**
- Create: `ops/launchd/com.open-trader.account-api.plist.template`
- Create: `scripts/install_account_api_launchd.sh`
- Create: `scripts/uninstall_account_api_launchd.sh`
- Create: `tests/test_account_api_launchd.py`

**Interfaces:**
- Consumes: `open-trader account-api --data-dir PATH`, fixed health schema, and launchd's `bootout`, `print`, and `bootstrap` operations.
- Produces: label `com.open-trader.account-api`, logs `logs/account_api/launchd.out.log` and `launchd.err.log`, and independent install/uninstall commands.

- [ ] **Step 1: Add failing plist and dry-run tests**

Create `tests/test_account_api_launchd.py`:

```python
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_account_api_launchd.sh"
UNINSTALLER = ROOT / "scripts/uninstall_account_api_launchd.sh"
TEMPLATE = ROOT / "ops/launchd/com.open-trader.account-api.plist.template"
LABEL = "com.open-trader.account-api"


def test_account_api_template_runs_only_fixed_shadow_command() -> None:
    payload = plistlib.loads(TEMPLATE.read_bytes())
    assert payload["Label"] == LABEL
    assert payload["WorkingDirectory"] == "OPEN_TRADER_REPO"
    assert payload["ProgramArguments"] == [
        "OPEN_TRADER_PYTHON", "-m", "open_trader", "account-api",
        "--data-dir", "OPEN_TRADER_DATA_DIR",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["StandardOutPath"] == "OPEN_TRADER_REPO/logs/account_api/launchd.out.log"
    assert payload["StandardErrorPath"] == "OPEN_TRADER_REPO/logs/account_api/launchd.err.log"


def test_account_api_installer_dry_run_renders_repo_and_runtime_paths(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    result = subprocess.run(
        [
            str(INSTALLER), "--dry-run", "--repo-root", str(ROOT),
            "--runtime-root", str(runtime), "--python", sys.executable,
            "--launch-agents-dir", str(agents),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = plistlib.loads(result.stdout.encode())
    assert payload["EnvironmentVariables"]["PYTHONPATH"] == str(ROOT / "src")
    assert str(runtime / "data") in payload["ProgramArguments"]
    assert "frontend-gateway" not in result.stdout
    assert "account-sync-worker" not in result.stdout
```

- [ ] **Step 2: Run the launchd tests to verify all files are missing**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_api_launchd.py -v
```

Expected: failures report the missing template and scripts.

- [ ] **Step 3: Add the fixed plist template**

Use this exact process shape:

```xml
<key>Label</key><string>com.open-trader.account-api</string>
<key>WorkingDirectory</key><string>OPEN_TRADER_REPO</string>
<key>EnvironmentVariables</key>
<dict><key>PYTHONPATH</key><string>OPEN_TRADER_REPO/src</string></dict>
<key>ProgramArguments</key>
<array>
  <string>OPEN_TRADER_PYTHON</string>
  <string>-m</string><string>open_trader</string><string>account-api</string>
  <string>--data-dir</string><string>OPEN_TRADER_DATA_DIR</string>
</array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ProcessType</key><string>Interactive</string>
<key>ThrottleInterval</key><integer>5</integer>
<key>StandardOutPath</key><string>OPEN_TRADER_REPO/logs/account_api/launchd.out.log</string>
<key>StandardErrorPath</key><string>OPEN_TRADER_REPO/logs/account_api/launchd.err.log</string>
```

Include the standard XML/plist declaration and enclosing `<plist><dict>` tags used by existing templates.

- [ ] **Step 4: Implement independent installer readiness**

The installer accepts `--dry-run`, `--repo-root`, `--runtime-root`, `--python`, `--launch-agents-dir`, and `--wait-seconds`. Use:

```bash
LABEL="com.open-trader.account-api"
DATA_DIR="$RUNTIME_ROOT/data"
OUT_LOG="$REPO_ROOT/logs/account_api/launchd.out.log"
ERR_LOG="$REPO_ROOT/logs/account_api/launchd.err.log"
CURL_BIN="${CURL_BIN:-/usr/bin/curl}"
```

Render only `OPEN_TRADER_PYTHON`, `OPEN_TRADER_DATA_DIR`, and `OPEN_TRADER_REPO`. Lint with `plutil`. On install:

1. write the rendered plist;
2. `bootout gui/$UID/$LABEL` and tolerate only absence;
3. poll `launchctl print` until `Could not find service` before bootstrap;
4. truncate only Account API logs;
5. call `bootstrap` once after absence is confirmed;
6. poll launchd PID plus health until exact runtime metadata matches.

Health readiness must parse JSON and require:

```python
valid = (
    health.get("schema_version") == "open_trader.account_api.health.v1"
    and health.get("module") == "account_api"
    and health.get("status") == "ok"
    and health.get("mode") == "shadow"
    and health.get("pid") == int(expected_pid)
    and health.get("api_git_sha") == expected_sha
    and health.get("worker_git_sha") == expected_sha
    and health.get("release_match") is True
)
```

If readiness expires, boot out only `com.open-trader.account-api`, wait until absent, print `Account API did not publish matching shadow health`, and return nonzero.

- [ ] **Step 5: Add lifecycle tests for bootout waiting, exact health, and isolation**

Extend `tests/test_account_api_launchd.py` with fake `launchctl` and `curl` executables. Model asynchronous removal with a `pending-removal` file exactly as current launchd tests do, but assert the call log contains only this label and sequence:

```python
assert calls == [
    f"bootout gui/{os.getuid()}/{LABEL}",
    f"print gui/{os.getuid()}/{LABEL}",
    f"print gui/{os.getuid()}/{LABEL}",
    f"bootstrap gui/{os.getuid()} {agents / f'{LABEL}.plist'}",
    f"print gui/{os.getuid()}/{LABEL}",
]
assert "frontend-gateway" not in "\n".join(calls)
assert "legacy-dashboard" not in "\n".join(calls)
assert "account-sync-controller" not in "\n".join(calls)
```

Add the same safe uninstaller contract as the Worker: if `launchctl print` still finds the label after bootout, preserve the plist and return nonzero; otherwise remove only `$LABEL.plist`. Also assert repeated uninstall reports `launchd agent not installed` and succeeds.

- [ ] **Step 6: Run script syntax, plist, and launchd tests**

Run:

```bash
bash -n scripts/install_account_api_launchd.sh scripts/uninstall_account_api_launchd.sh
plutil -lint ops/launchd/com.open-trader.account-api.plist.template
.venv/bin/python -m pytest tests/test_account_api_launchd.py tests/test_account_sync_launchd.py tests/test_dashboard_launchd_stack.py -v
```

Expected: shell syntax and plist lint succeed; all selected tests pass with clean stderr.

- [ ] **Step 7: Commit the independent runtime**

```bash
git add ops/launchd/com.open-trader.account-api.plist.template scripts/install_account_api_launchd.sh scripts/uninstall_account_api_launchd.sh tests/test_account_api_launchd.py
git commit -m "ops: install Account API shadow runtime (#20)"
```

---

### Task 6: Publish the operator runbook and merge log

**Files:**
- Create: `docs/operations/account-api-shadow-runtime.md`
- Modify: `README.md:193-231`
- Modify: `CHANGELOG.md:6-12`

**Interfaces:**
- Consumes: final commands, paths, label, port, health fields, and parity exits from Tasks 1-5.
- Produces: exact install/inspect/parity/uninstall workflow and dated operator-facing merge entry.

- [ ] **Step 1: Write the shadow runtime runbook with executable commands**

Document this exact operator sequence:

```bash
scripts/install_account_api_launchd.sh --dry-run
scripts/install_account_api_launchd.sh
launchctl print gui/$(id -u)/com.open-trader.account-api
lsof -nP -iTCP:8768 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8768/healthz
R2_PROBE_DIR="$(mktemp -d)"
curl -fsS -D "$R2_PROBE_DIR/headers" \
  -o "$R2_PROBE_DIR/snapshot.json" \
  http://127.0.0.1:8768/api/v1/account/snapshot
.venv/bin/python -m open_trader account-api-parity --data-dir data
tail -n 100 logs/account_api/launchd.out.log
tail -n 100 logs/account_api/launchd.err.log
scripts/uninstall_account_api_launchd.sh
```

Explain that `8768` is loopback shadow only, Frontend Gateway has no R2 route, the API never calls brokers or writes publication, exit `2` means parity proof is blocked by source churn, and Worker/API SHA mismatch intentionally makes snapshot unavailable while health remains `200`.

- [ ] **Step 2: Link the runbook from the Account workflow**

After the current Worker compatibility paragraph in README, add a short shadow-reader chain:

```text
Account Sync Worker → raw Account publication → Account API shadow (127.0.0.1:8768)
```

State that browsers still use only Frontend Gateway and link `docs/operations/account-api-shadow-runtime.md`.

- [ ] **Step 3: Add the dated CHANGELOG entry before any merge**

Under `## 2026-08-03`, add an operator-facing bullet that says R2 adds a loopback-only read-only Account API on `8768`, a strong-ETag v1 snapshot, stable publication reads, independent live parity and launchd operations; Worker remains the sole writer, Gateway/Dashboard remain unchanged, and final verification uses exact-SHA Worker/API runtime proof.

- [ ] **Step 4: Verify documentation names and exclusions**

Run:

```bash
rg -n "account-api|account-api-parity|com\.open-trader\.account-api|127\.0\.0\.1:8768|PASS|FAIL|BLOCKED" README.md CHANGELOG.md docs/operations/account-api-shadow-runtime.md
rg -n "Gateway.*route|browser|sole writer|唯一.*writer|只读" README.md CHANGELOG.md docs/operations/account-api-shadow-runtime.md
git diff --check
```

Expected: all runtime tokens and boundaries are documented; diff check is clean.

- [ ] **Step 5: Commit runbook and merge log**

```bash
git add README.md CHANGELOG.md docs/operations/account-api-shadow-runtime.md
git commit -m "docs: publish Account API shadow runbook (#20)"
```

---

### Task 7: Verify, integrate, deploy exact SHA, and submit review evidence

**Files:**
- Verify only: all Task 1-6 files
- Runtime data: `/Users/ray/projects/open_trader/data`
- Runtime logs: `logs/account_api/launchd.*.log`, `logs/account_sync/launchd.*.log`

**Interfaces:**
- Consumes: candidate branch, local `main`, both launchd installers, fixed HTTP routes, and parity command.
- Produces: clean tests, local-main integration, exact-SHA live processes, immutable publication proof, and #20 operator-review evidence.

- [ ] **Step 1: Run focused Account and runtime suites**

```bash
.venv/bin/python -m pytest \
  tests/test_account_api.py \
  tests/test_account_api_launchd.py \
  tests/test_account_sync_state.py \
  tests/test_account_sync_worker.py \
  tests/test_account_sync_launchd.py \
  tests/test_frontend_gateway.py \
  tests/test_frontend_gateway_cli.py \
  tests/test_dashboard_launchd_stack.py -v
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the complete candidate suite**

```bash
.venv/bin/python -m pytest
```

Expected: all tests pass. Record the exact count and duration. Do not run `make acceptance`.

- [ ] **Step 3: Reconcile a concurrently advanced local main inside the clean feature worktree**

Check ancestry:

```bash
git merge-base --is-ancestor main HEAD
```

If the ancestry check succeeds, keep the candidate unchanged. If it fails because local `main` advanced, merge `main` non-destructively inside this clean worktree:

```bash
git merge --no-edit main
.venv/bin/python -m pytest
```

Resolve no unrelated dirty-root files. Any merge conflict must be handled with the resolving-merge-conflicts skill, followed by the full suite again.

- [ ] **Step 4: Fast-forward local main only after CHANGELOG and tests are complete**

From `/Users/ray/projects/open_trader`:

```bash
R2_ROOT_AUDIT="$(mktemp -d)"
git status --short --untracked-files=all > "$R2_ROOT_AUDIT/before.status"
git diff -- .gitignore > "$R2_ROOT_AUDIT/before.gitignore.diff"
git merge --ff-only codex/issue-20-account-api-shadow
.venv/bin/python -m pytest
git status --short --untracked-files=all > "$R2_ROOT_AUDIT/after.status"
git diff -- .gitignore > "$R2_ROOT_AUDIT/after.gitignore.diff"
diff -u "$R2_ROOT_AUDIT/before.status" "$R2_ROOT_AUDIT/after.status"
diff -u "$R2_ROOT_AUDIT/before.gitignore.diff" "$R2_ROOT_AUDIT/after.gitignore.diff"
```

Expected: local `main` equals the candidate SHA and the full suite passes. Confirm the pre-existing `.gitignore` and untracked root files remain unchanged.

- [ ] **Step 5: Deploy Worker and Account API separately from the exact merged SHA**

From the issue worktree, whose HEAD now equals local `main`:

```bash
scripts/install_account_sync_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python "$PWD/.venv/bin/python"
scripts/install_account_api_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python "$PWD/.venv/bin/python"
```

Expected: each installer reports only its own label installed with no stderr. Account API installer never restarts Worker; the explicit Worker install is a separate release-match step.

- [ ] **Step 6: Prove processes, listener, health, ETag, and live parity**

```bash
R2_SHA="$(git rev-parse HEAD)"
launchctl print gui/$UID/com.open-trader.account-sync-controller
launchctl print gui/$UID/com.open-trader.account-api
lsof -nP -iTCP:8768 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8768/healthz
R2_VERIFY_DIR="$(mktemp -d)"
curl -fsS -D "$R2_VERIFY_DIR/headers" \
  -o "$R2_VERIFY_DIR/snapshot.json" \
  http://127.0.0.1:8768/api/v1/account/snapshot
R2_ETAG="$(awk 'BEGIN{IGNORECASE=1} /^ETag:/{sub(/\r$/, "", $2); print $2}' "$R2_VERIFY_DIR/headers")"
curl -sS -o "$R2_VERIFY_DIR/unchanged.body" \
  -w '%{http_code}\n' \
  -H "If-None-Match: $R2_ETAG" \
  http://127.0.0.1:8768/api/v1/account/snapshot
.venv/bin/python -m open_trader account-api-parity \
  --data-dir /Users/ray/projects/open_trader/data
```

Expected:

- Account API health is `200`, `mode` is `shadow`, both SHA fields equal `$R2_SHA`, and `release_match` is true;
- the only `8768` listener is `127.0.0.1` and its PID equals launchd health PID;
- snapshot is `200` with a strong ETag;
- the conditional request prints `304` and `unchanged.body` is empty;
- parity prints `PASS` and exits `0`.

- [ ] **Step 7: Prove repeated GET is read-only**

```bash
shasum -a 256 \
  /Users/ray/projects/open_trader/data/latest/account_sync_state.json \
  /Users/ray/projects/open_trader/data/latest/quotes.json \
  > "$R2_VERIFY_DIR/before.sha"
stat -f '%N %m' \
  /Users/ray/projects/open_trader/data/latest/account_sync_state.json \
  /Users/ray/projects/open_trader/data/latest/quotes.json \
  > "$R2_VERIFY_DIR/before.mtime"
for _request in 1 2 3 4 5 6 7 8 9 10; do
  curl -fsS -o /dev/null http://127.0.0.1:8768/api/v1/account/snapshot
done
shasum -a 256 \
  /Users/ray/projects/open_trader/data/latest/account_sync_state.json \
  /Users/ray/projects/open_trader/data/latest/quotes.json \
  > "$R2_VERIFY_DIR/after.sha"
stat -f '%N %m' \
  /Users/ray/projects/open_trader/data/latest/account_sync_state.json \
  /Users/ray/projects/open_trader/data/latest/quotes.json \
  > "$R2_VERIFY_DIR/after.mtime"
diff -u "$R2_VERIFY_DIR/before.sha" "$R2_VERIFY_DIR/after.sha"
diff -u "$R2_VERIFY_DIR/before.mtime" "$R2_VERIFY_DIR/after.mtime"
```

Run this proof during a quiet interval shorter than the Worker's 5-second quote cycle, or first compare the pinned generation before and after. If Worker publication changes naturally, rerun until one interval pins; do not stop the Writer or use fixtures to manufacture the proof.

- [ ] **Step 8: Verify PID, cwd, SHA, fresh logs, and clean stderr**

```bash
tail -n 20 logs/account_api/launchd.out.log
tail -n 20 logs/account_api/launchd.err.log
tail -n 20 logs/account_sync/launchd.out.log
tail -n 20 logs/account_sync/launchd.err.log
test ! -s logs/account_api/launchd.err.log
test ! -s logs/account_sync/launchd.err.log
```

Use the two launchd PIDs with `ps -p PID -o pid=,lstart=,command=` and `lsof -a -p PID -d cwd -Fn`. Expected: both processes run from this worktree at `$R2_SHA`, startup logs are fresh, and stderr is empty.

- [ ] **Step 9: Post evidence without closing the review gate**

Post one #20 comment containing:

- candidate/local-main SHA;
- focused and full pytest counts/durations;
- Worker and Account API labels, PIDs, cwd, SHA, start times;
- listener and health result;
- snapshot `200`, ETag `304`, parity `PASS`;
- publication hash/mtime proof;
- fresh stderr sizes;
- explicit statement that Gateway/Dashboard were unchanged and `make acceptance` was intentionally not run.

Then remove `ready-for-agent` from #20 because implementation is awaiting operator review. Keep #20 open and do not add `ready-for-agent` to R3.

---

## Execution Handoff

Plan execution must stop at the #20 operator-review gate after exact-SHA evidence. No push, issue close, R3 unlock, or worktree cleanup is authorized by this plan.
