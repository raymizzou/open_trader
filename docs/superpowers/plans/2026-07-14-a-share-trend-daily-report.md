# A-Share Trend Daily Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a stable, advisory-only Eastmoney A-share trend workflow that produces one 17:00 Markdown/JSON/Feishu plan per trading day and sends intraday protection-line alerts without placing orders.

**Architecture:** Add one small Trend Animals HTTP/cache client, one A-share report module containing the account loader and deterministic discipline engine, and one session-aware watcher. Reuse `FutuQuoteClient`, `RunLock`, notifier construction, per-channel delivery results, the existing CLI, and the existing launchd installer; keep the first release independent of the Kelly execution modules and all non-CN strategies.

**Tech Stack:** Python 3.12 standard library (`urllib`, `csv`, `json`, `dataclasses`, `decimal`, `zoneinfo`), installed `futu-api`, pytest, macOS launchd, existing Feishu/macOS notifiers.

## Global Constraints

- Scope is only the Eastmoney A-share account: Shanghai/Shenzhen stocks and domestic ETFs; exclude Beijing Exchange, ST/*ST/delisting names, Hong Kong, US, futures, and convertible bonds.
- The system creates advice and alerts only. It never connects to Eastmoney order entry and never marks an action filled without the user's manual fill record.
- Official Trend Animals runtime documentation is authoritative. Fetch `getUpdateStatus` before market/trend data and `getSnapshotColumnBilling` before `getTickerSnapshot`; reject missing required fields instead of inventing them.
- Never write `TREND_ANIMALS_API_KEY` to reports, logs, exceptions, cache keys, JSON, Markdown, Git, or command output.
- Cache `searchTicker` exact symbol-to-`tmId` mappings persistently; cache paid responses by `asOfDate + endpoint + sorted parameters/fields`; a normal rerun returns the frozen report without paid calls.
- Candidate sources are exactly tmId `622466` (`温转热(A股)`) and tmId `697199` (`温转热(ETF基金个股)`), read from the ignored environment file rather than hard-coded in source.
- Candidate facts requested are exactly `tmId,tickerName,tickerSymbol,asset,asOfDate,tradableFlag,industryName,amount1d,isTrendRightSide,daysSinceTrendEntry,trendStrengthLocalCurr,stopwinFlagByDangerSignal`.
- Holding facts additionally request `stopwinFlagByBoilingTemperature,stopwinFlagByPopChampagne`; current holdings are checked even when absent from both candidate pools.
- Candidate hard gates are right-side `true`, strength `> 90`, right-side days `< 10`, tradable `true`, amount `>= 1` hundred-million CNY, danger `false`, eligible exchange/name, not already held, and valid ATR14 data.
- Deterministic order is strength descending, right-side days ascending, amount descending, code ascending; retain the first 10 as the candidate list regardless of slots or cash.
- Formal buys use only fresh account data, remaining slots up to 10, existing cash (never planned sale proceeds), fixed 1% of strategy net value, close-price estimates, and 100-share round lots. Skip an unaffordable lot and consider the next ranked candidate.
- Formal buy advice is valid only 09:30–10:00 on `executionDate`; the report labels shares as a close-price estimate and instructs the user to recalculate downward to a 100-share lot from the live Eastmoney price without exceeding the suggested amount. No morning report is generated.
- Do not use leverage, do not require minimum invested capital, and do not apply an industry cap. Display industry concentration as a fact.
- `portfolio.csv` mtime in `Asia/Shanghai` equal to `asOfDate` means fresh. A stale account still produces candidates and conditional holding actions, but no formal buy amount or quantity.
- Force a full sell when danger is `true` or right-side is `false`. Unknown holding fields are neither safe nor an automatic sell; keep the prior protection line and mark manual review.
- ATR14 uses 14 true ranges and therefore at least 15 valid QFQ daily bars. Initial protection is close/activation price minus `2 × ATR14`.
- Boiling/champagne raises the active protection line to at least the lowest low of the five complete bars strictly before `asOfDate`; an active line never decreases.
- Official report job starts at 17:00 Asia/Shanghai on weekdays. Futu `TradeDateMarket.CN` decides whether the date is an A-share trading day before paid API calls.
- A-share, ETF-individual, and holding snapshots must all have `asOfDate` equal to the current trading day. On not-ready or systemic Futu/API failure, notify and retry every 10 minutes through 18:00; after the 18:00 attempt, notify final failure and create no formal report.
- `executionDate` is the first Futu CN trading day strictly after `asOfDate`, obtained from the trading calendar rather than guessed from weekdays.
- Save frozen artifacts as `reports/trend_a_share/YYYY-MM-DD.md` and `.json`; `--revision` creates the next `YYYY-MM-DD-rN` pair and preserves every prior file.
- Feishu gets the full report. macOS gets only waiting/success/failure status and watcher interruption/recovery/trigger alerts. A Feishu delivery failure keeps the local artifacts and records `delivery_failed` without repurchasing data.
- The trial has no API daily-spend ceiling. Record requested endpoint, fields, row counts, cache status, billing-table estimate, and run-window balance delta.
- The intraday watcher runs only 09:30–11:30 and 13:00–15:00 on CN trading days, polls Futu every 5 seconds, alerts once per symbol per day on first `price <= active_line`, and never auto-orders.
- OpenD loss causes an immediate Feishu+macOS interruption alert, 60-second reconnect attempts, and one recovery alert. No missing quote may be represented as safe.
- Preserve unrelated dirty-worktree changes and stage only files named by the current task.
- Every source change requires targeted tests, a real workflow check where practical, process inspection/restart, fresh logs, and finally `make acceptance`. Only `PASS` is completion.
- After `make acceptance` returns `PASS`, install/restart launchd from the exact accepted Git SHA and verify PID/working directory/SHA/fresh log plus HTTP 200 from the review URL before asking for review.

---

## File Map

- Create `src/open_trader/trend_animals.py`: authenticated GET client, validation, rate-conscious request cache, symbol-to-tmId cache, and balance-delta cost facts.
- Modify `src/open_trader/futu_quote.py`: expose the installed SDK's CN trading-day query through the existing error model.
- Modify `src/open_trader/daily_premarket.py`: carry the three Trend Animals env values and add optional notification-channel filtering without changing existing callers.
- Create `src/open_trader/a_share_trend.py`: Eastmoney account snapshot, ATR/protection state, candidate filtering/ranking, report orchestration, rendering, persistence, retry, and notifications.
- Create `src/open_trader/a_share_trend_watch.py`: CN session clock, live price checks, daily alert de-duplication, and disconnect/recovery behavior.
- Modify `src/open_trader/cli.py`: add `trend-a-share-report` and `watch-trend-a-share` commands.
- Create `ops/launchd/com.open-trader.trend-a-share-report.plist.template`: weekday 17:00 report job.
- Create `ops/launchd/com.open-trader.trend-a-share-watch.plist.template`: weekday 09:25 watcher job.
- Modify `scripts/install_daily_premarket_launchd.sh` and `scripts/uninstall_daily_premarket_launchd.sh`: support `--market CN|all` while leaving HK/US jobs unchanged.
- Modify `config/daily_premarket.env.example`: document the three already-created Trend Animals variables and no secret values.
- Create `tests/test_trend_animals.py`, `tests/test_a_share_trend.py`, and `tests/test_a_share_trend_watch.py`; modify `tests/test_futu_quote.py`, `tests/test_premarket_cli.py`, and `tests/test_daily_premarket.py`.

---

### Task 1: CN Trading Calendar Through the Existing Futu Client

**Files:**
- Modify: `src/open_trader/futu_quote.py:71-230`
- Modify: `tests/test_futu_quote.py:1-300`

**Interfaces:**
- Consumes: installed SDK call `context.request_trading_days(market=TradeDateMarket.CN, start=date, end=date)`.
- Produces: `FutuQuoteClient.get_cn_trading_days(*, start: str, end: str) -> list[str]`.

- [ ] **Step 1: Write the failing calendar tests**

Add this method to `FakeOpenQuoteContext` and these tests:

```python
def request_trading_days(
    self, *, market: object, start: str, end: str
) -> tuple[int, object]:
    self.requested_trading_days = {"market": market, "start": start, "end": end}
    return 0, [
        {"time": "2026-07-14", "trade_date_type": "WHOLE"},
        {"time": "", "trade_date_type": "WHOLE"},
    ]


def test_futu_quote_client_returns_cn_trading_days() -> None:
    from futu import TradeDateMarket

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=FakeOpenQuoteContext,
        connectivity_checker=lambda host, port: True,
    )

    assert client.get_cn_trading_days(
        start="2026-07-14", end="2026-07-20"
    ) == ["2026-07-14"]
    assert client.context.requested_trading_days == {
        "market": TradeDateMarket.CN,
        "start": "2026-07-14",
        "end": "2026-07-20",
    }


def test_futu_quote_client_classifies_trading_calendar_failure() -> None:
    class FailingCalendarContext(FakeOpenQuoteContext):
        def request_trading_days(self, **kwargs: object) -> tuple[int, object]:
            return -1, "网络中断"

    client = FutuQuoteClient(
        host="127.0.0.1", port=11111,
        context_factory=FailingCalendarContext,
        connectivity_checker=lambda host, port: True,
    )

    with pytest.raises(FutuQuoteError) as exc_info:
        client.get_cn_trading_days(start="2026-07-14", end="2026-07-14")

    assert exc_info.value.error_type == "quote_server_interrupted"
```

- [ ] **Step 2: Run the tests and verify the missing method failure**

Run: `.venv/bin/python -m pytest tests/test_futu_quote.py -q`

Expected: FAIL with `AttributeError: 'FutuQuoteClient' object has no attribute 'get_cn_trading_days'`.

- [ ] **Step 3: Implement the minimum SDK adapter**

Add to `FutuQuoteClient`:

```python
def get_cn_trading_days(self, *, start: str, end: str) -> list[str]:
    try:
        from futu import TradeDateMarket
        market = TradeDateMarket.CN
    except ImportError:
        market = "CN"
    ret_code, data = self.context.request_trading_days(
        market=market, start=start, end=end
    )
    if ret_code != 0:
        message = str(data)
        raise FutuQuoteError(
            message,
            error_type=(
                "quote_server_interrupted" if "网络中断" in message
                else "snapshot_failed"
            ),
            next_step=(
                QUOTE_INTERRUPTED_NEXT_STEP if "网络中断" in message
                else SNAPSHOT_FAILED_NEXT_STEP
            ),
            opend_reachable=True,
            context_ok=True,
            snapshot_ok=False,
        )
    return [
        str(item.get("time", "")).strip()
        for item in data
        if str(item.get("time", "")).strip()
    ]
```

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m pytest tests/test_futu_quote.py -q`

Expected: PASS.

- [ ] **Step 5: Commit only the calendar adapter**

```bash
git add src/open_trader/futu_quote.py tests/test_futu_quote.py
git commit -m "feat: expose CN trading calendar"
```

---

### Task 2: Trend Animals Client With Secret-Safe Persistent Caching

**Files:**
- Create: `src/open_trader/trend_animals.py`
- Create: `tests/test_trend_animals.py`

**Interfaces:**
- Consumes: official GET endpoints under `https://www.trendtrader.cn/apiData/data/` and a secret API key supplied in memory.
- Produces: `TrendAnimalsClient.get_update_status()`, `get_snapshot_billing()`, `get_account_balance()`, `search_exact_symbol()`, `get_components()`, and `get_snapshots()`; all return validated `list[dict[str, object]]` except balance, which returns one mapping. `TrendAnimalsLookupError` distinguishes a valid no-unique-match response from transport/server failure.

- [ ] **Step 1: Write tests for validation, cache keys, exact tmId lookup, and secret redaction**

Create `tests/test_trend_animals.py` with a callable fake transport and these core cases:

```python
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from open_trader.trend_animals import TrendAnimalsClient, TrendAnimalsError


class FakeTransport:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def __call__(self, url: str, timeout: float) -> dict[str, object]:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.calls.append((parsed.path.rsplit("/", 1)[-1], params))
        return self.responses[parsed.path.rsplit("/", 1)[-1]]


def success(data: list[dict[str, object]]) -> dict[str, object]:
    return {"code": "00000", "msg": "操作成功", "success": True, "data": data}


def test_paid_response_cache_uses_date_endpoint_and_sorted_params(tmp_path: Path) -> None:
    transport = FakeTransport({
        "getComponentTicker": success([
            {"tmId": 1, "tickerSymbol": "600000.SH", "asOfDate": "2026-07-14"}
        ])
    })
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    first = client.get_components(tm_id=622466, expected_date="2026-07-14")
    second = client.get_components(tm_id=622466, expected_date="2026-07-14")

    assert first == second
    assert len(transport.calls) == 1
    cache_text = next((tmp_path / "responses").glob("*.json")).read_text()
    assert "secret-value" not in cache_text
    assert "secret-value" not in next((tmp_path / "responses").glob("*.json")).name


def test_search_exact_symbol_caches_tm_id_without_guessing(tmp_path: Path) -> None:
    transport = FakeTransport({
        "searchTicker": success([
            {"tmId": 7, "tickerSymbol": "600025.SH"},
            {"tmId": 8, "tickerSymbol": "600026.SH"},
        ])
    })
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    assert client.search_exact_symbol("600025") == 7
    assert client.search_exact_symbol("600025") == 7
    assert len(transport.calls) == 1


def test_snapshot_rejects_wrong_data_date(tmp_path: Path) -> None:
    transport = FakeTransport({
        "getTickerSnapshot": success([
            {"tmId": 7, "tickerSymbol": "600025.SH", "asOfDate": "2026-07-13"}
        ])
    })
    client = TrendAnimalsClient(
        api_key="secret-value", cache_dir=tmp_path, transport=transport
    )

    with pytest.raises(TrendAnimalsError, match="expected 2026-07-14") as exc_info:
        client.get_snapshots(
            tm_ids=[7], fields=("tmId", "tickerSymbol", "asOfDate"),
            expected_date="2026-07-14",
        )
    assert "secret-value" not in str(exc_info.value)
```

Also test non-`00000`, non-list `data`, missing billing field, exact symbol miss, and a corrupt local cache file. A corrupt response cache must raise `TrendAnimalsError` and never silently repurchase data in the same run.

- [ ] **Step 2: Run tests and verify import failure**

Run: `.venv/bin/python -m pytest tests/test_trend_animals.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'open_trader.trend_animals'`.

- [ ] **Step 3: Implement the validated GET client and two cache namespaces**

Create the module with these exact public shapes and cache rules:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import urlopen


BASE_URL = "https://www.trendtrader.cn/apiData/data"
Transport = Callable[[str, float], dict[str, object]]


class TrendAnimalsError(RuntimeError):
    pass


class TrendAnimalsLookupError(TrendAnimalsError):
    pass


def _default_transport(url: str, timeout: float) -> dict[str, object]:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class TrendAnimalsClient:
    def __init__(
        self, *, api_key: str, cache_dir: Path,
        transport: Transport = _default_transport, timeout_seconds: float = 20.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("TREND_ANIMALS_API_KEY is required")
        self._api_key = api_key
        self.cache_dir = cache_dir
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def get_update_status(self) -> list[dict[str, object]]:
        return self._get("getUpdateStatus", {})

    def get_snapshot_billing(self) -> list[dict[str, object]]:
        return self._get("getSnapshotColumnBilling", {})

    def get_account_balance(self) -> Mapping[str, object]:
        rows = self._get("getAccountBalance", {"viewLevel": "summary"})
        if len(rows) != 1:
            raise TrendAnimalsError("getAccountBalance returned no unique summary")
        return rows[0]

    def search_exact_symbol(self, symbol: str) -> int:
        normalized = symbol.strip().upper().split(".", 1)[0]
        cache_path = self.cache_dir / "symbols" / f"{normalized}.json"
        cached = self._read_cache(cache_path)
        if cached is not None:
            return int(cached["tmId"])
        rows = self._get("searchTicker", {"keyword": normalized})
        matches = [
            row for row in rows
            if str(row.get("tickerSymbol", "")).split(".", 1)[0].upper() == normalized
        ]
        if len(matches) != 1:
            raise TrendAnimalsLookupError(
                f"searchTicker found no unique exact match for {normalized}"
            )
        self._write_cache(cache_path, {"symbol": normalized, "tmId": int(matches[0]["tmId"])})
        return int(matches[0]["tmId"])

    def get_components(self, *, tm_id: int, expected_date: str) -> list[dict[str, object]]:
        return self._cached_rows(
            "getComponentTicker",
            {"tmId": str(tm_id), "getAllBasicComponentsFlag": "0"},
            expected_date,
        )

    def get_snapshots(
        self, *, tm_ids: Sequence[int], fields: Sequence[str], expected_date: str,
    ) -> list[dict[str, object]]:
        return self._cached_rows(
            "getTickerSnapshot",
            {"tmIds": ",".join(map(str, sorted(set(tm_ids)))),
             "fields": ",".join(sorted(set(fields)))},
            expected_date,
        )
```

Complete `_get` by building `url = f"{BASE_URL}/{endpoint}?{urlencode({'apiKey': self._api_key, **params})}"`, validating `success is True`, `code == "00000"`, and `data` is a list. Complete `_cached_rows` by hashing JSON containing only `date`, `endpoint`, and sorted non-secret params; validate every nonempty row's `asOfDate` equals `expected_date`. Complete `_write_cache` with `NamedTemporaryFile(delete=False, dir=path.parent)` followed by `Path(temp.name).replace(path)`. `_read_cache` returns `None` only when the file does not exist; malformed JSON raises `TrendAnimalsError`.

- [ ] **Step 4: Run client tests**

Run: `.venv/bin/python -m pytest tests/test_trend_animals.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the isolated client**

```bash
git add src/open_trader/trend_animals.py tests/test_trend_animals.py
git commit -m "feat: add cached Trend Animals client"
```

---

### Task 3: Pure A-Share Discipline Engine and Frozen Artifacts

**Files:**
- Create: `src/open_trader/a_share_trend.py`
- Create: `tests/test_a_share_trend.py`

**Interfaces:**
- Consumes: normalized API rows, `DailyKlineBar`, Eastmoney rows from `portfolio.csv`, and the prior protection-state JSON.
- Produces: `AccountSnapshot`, `CandidateDecision`, `HoldingDecision`, `TrendReport`, `load_eastmoney_account()`, `atr14()`, `evaluate_candidate()`, `build_report()`, `render_markdown()`, and `write_frozen_report()`.

- [ ] **Step 1: Write pure-rule tests before orchestration**

Create dataclass factories in `tests/test_a_share_trend.py` and cover the boundaries exactly:

```python
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from open_trader.a_share_trend import (
    CandidateInput, build_candidate_list, estimate_buy_actions,
    load_eastmoney_account, update_protection_line, write_frozen_report,
)


def candidate(
    symbol: str, *, strength: str = "96", days: int = 3,
    amount: str = "2", right_side: object = True,
    tradable: object = True, danger: object = False,
) -> CandidateInput:
    return CandidateInput(
        tm_id=int(symbol), symbol=symbol, exchange="SH",
        name=f"股票{symbol}", asset="A股",
        industry="电力", as_of_date="2026-07-14", tradable=tradable,
        amount=Decimal(amount), right_side=right_side, days=days,
        strength=Decimal(strength), danger=danger,
        close=Decimal("10"), atr=Decimal("0.5"),
    )


def test_candidates_filter_then_sort_deterministically() -> None:
    rows = [
        candidate("600004", strength="95", days=2, amount="3"),
        candidate("600003", strength="96", days=4, amount="2"),
        candidate("600002", strength="96", days=3, amount="1"),
        candidate("600001", strength="96", days=3, amount="2"),
        candidate("600005", strength="90"),
        candidate("600006", danger=True),
    ]

    decisions = build_candidate_list(rows, held_symbols={"600003"})

    assert [item.symbol for item in decisions.eligible[:10]] == [
        "600001", "600002", "600004"
    ]
    assert decisions.excluded["600003"] == ["already_held"]
    assert decisions.excluded["600005"] == ["strength_not_above_90"]
    assert decisions.excluded["600006"] == ["danger_signal"]


def test_buy_actions_use_one_percent_cash_slots_and_round_lots() -> None:
    ranked = [candidate("600001"), candidate("600002")]

    actions = estimate_buy_actions(
        ranked=ranked, account_fresh=True, net_value=Decimal("676549.55"),
        available_cash=Decimal("7000"), current_position_count=9,
    )

    assert [(item.symbol, item.target_amount, item.estimated_shares) for item in actions] == [
        ("600001", Decimal("6765.50"), 600)
    ]


def test_stale_account_has_no_formal_buys() -> None:
    assert estimate_buy_actions(
        ranked=[candidate("600001")], account_fresh=False,
        net_value=Decimal("676549.55"), available_cash=Decimal("405219.55"),
        current_position_count=5,
    ) == []


def test_overheat_line_uses_prior_five_lows_and_never_decreases() -> None:
    assert update_protection_line(
        old_line=Decimal("27.31"), boiling=True, champagne=False,
        prior_five_lows=[Decimal(value) for value in ["28", "29", "27.8", "28.5", "29"]],
    ) == Decimal("27.80")
    assert update_protection_line(
        old_line=Decimal("28.20"), boiling=True, champagne=False,
        prior_five_lows=[Decimal("27.80")] * 5,
    ) == Decimal("28.20")
```

Add cases for `.BJ`, `ST`, `*ST`, a name containing `退`, null booleans, days `9` versus `10`, amount `1` versus `0.999`, strength `90` versus `90.001`, fewer than 15 bars, candidate K-line failure, holding K-line failure with/without old line, danger/right-side full sell, all current holdings outside pools, more than 10 positions, insufficient 100-share lot promotion, no-action report text, source-fact versus strategy-judgment sections, stale mtime, only-Eastmoney account totals, idempotent base artifact, and `-r1/-r2` revisions.

- [ ] **Step 2: Run tests and verify import failure**

Run: `.venv/bin/python -m pytest tests/test_a_share_trend.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'open_trader.a_share_trend'`.

- [ ] **Step 3: Define the compact domain records and account loader**

Start `a_share_trend.py` with immutable dataclasses and a loader that filters `brokers` to exactly `eastmoney`:

```python
@dataclass(frozen=True)
class AccountPosition:
    symbol: str
    name: str
    asset_class: str
    quantity: Decimal
    avg_cost_price: Decimal | None


@dataclass(frozen=True)
class AccountSnapshot:
    source_date: str
    fresh: bool
    net_value: Decimal
    available_cash: Decimal
    positions: tuple[AccountPosition, ...]
    exceptions: tuple[str, ...]


def load_eastmoney_account(
    path: Path, *, expected_date: str,
    timezone: ZoneInfo = ZoneInfo("Asia/Shanghai"),
) -> AccountSnapshot:
    source_date = datetime.fromtimestamp(path.stat().st_mtime, timezone).date().isoformat()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    eastmoney = [row for row in rows if _broker_set(row.get("brokers", "")) == {"eastmoney"}]
    net_value = sum((_decimal(row["market_value"]) for row in eastmoney), Decimal("0"))
    cash = sum((
        _decimal(row["market_value"]) for row in eastmoney
        if row.get("market") == "CASH" and row.get("currency") == "CNY"
    ), Decimal("0"))
    positions = tuple(
        AccountPosition(
            symbol=row["symbol"].strip(), name=row["name"].strip(),
            asset_class=row["asset_class"].strip().lower(),
            quantity=_decimal(row["total_quantity"]),
            avg_cost_price=_optional_decimal(row.get("avg_cost_price", "")),
        )
        for row in eastmoney
        if row.get("market") == "CN"
        and row.get("asset_class") in {"stock", "etf"}
        and _decimal(row["total_quantity"]) > 0
    )
    return AccountSnapshot(
        source_date=source_date, fresh=source_date == expected_date,
        net_value=net_value, available_cash=cash, positions=positions,
        exceptions=tuple(_account_exceptions(eastmoney)),
    )
```

Mixed-broker rows containing `eastmoney` plus another broker raise `ValueError`; unsupported Eastmoney assets become `exceptions` and stay visible.

- [ ] **Step 4: Implement exact numeric, filter, rank, sizing, and protection functions**

Use `Decimal` through all money/price calculations:

```python
def atr14(bars: Sequence[DailyKlineBar]) -> Decimal | None:
    valid = [bar for bar in bars if None not in (bar.high, bar.low)]
    if len(valid) < 15:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(valid[-15:-1], valid[-14:]):
        high = Decimal(str(current.high))
        low = Decimal(str(current.low))
        previous_close = Decimal(str(previous.close))
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges, Decimal("0")) / Decimal("14")


def _candidate_reasons(item: CandidateInput, held_symbols: set[str]) -> list[str]:
    reasons: list[str] = []
    if item.right_side is not True: reasons.append("right_side_not_true")
    if item.strength is None or item.strength <= 90: reasons.append("strength_not_above_90")
    if item.days is None or item.days >= 10: reasons.append("right_side_days_not_below_10")
    if item.tradable is not True: reasons.append("not_tradable")
    if item.amount is None or item.amount < 1: reasons.append("amount_below_1")
    if item.danger is not False: reasons.append("danger_signal" if item.danger else "danger_unknown")
    if item.symbol in held_symbols: reasons.append("already_held")
    if item.exchange == "BJ" or _excluded_name(item.name): reasons.append("excluded_security")
    if item.atr is None: reasons.append("atr_unavailable")
    return reasons


def update_protection_line(
    *, old_line: Decimal, boiling: bool, champagne: bool,
    prior_five_lows: Sequence[Decimal],
) -> Decimal:
    if not (boiling or champagne) or len(prior_five_lows) != 5:
        return old_line
    return max(old_line, min(prior_five_lows))
```

Sort eligible rows with `key=lambda x: (-x.strength, x.days, -x.amount, x.symbol)` and slice only the displayed candidate list to 10. For buys, compute `target = (net_value * Decimal("0.01")).quantize(Decimal("0.01"))`, cap by remaining cash, and calculate `shares = int(target / close / 100) * 100`; only a positive lot consumes cash and a slot.

Normalize Trend Animals symbols once at the boundary: `600000.SH` becomes `symbol="600000", exchange="SH"`; `920000.BJ` retains `exchange="BJ"` long enough for the exclusion rule. Never infer exchange from the first digit when the API already returned a suffix.

- [ ] **Step 5: Implement holding decisions, state, rendering, and atomic freeze**

Persist state at `data/trend_a_share/protection_state.json` keyed by six-digit symbol:

```json
{
  "schema_version": 1,
  "positions": {
    "600900": {
      "initial_line": "27.31",
      "active_line": "27.31",
      "atr14": "0.5057142857142857",
      "updated_for": "2026-07-14"
    }
  }
}
```

Holding action precedence is exact:

```python
if symbol in previously_triggered_protection_lines:
    action, reason = "SELL_ALL", "protection_line_already_triggered"
elif snapshot is None or snapshot.right_side is None or snapshot.danger is None:
    action, reason = "MANUAL_REVIEW", "holding_signal_unknown"
elif snapshot.danger is True:
    action, reason = "SELL_ALL", "danger_signal"
elif snapshot.right_side is False:
    action, reason = "SELL_ALL", "left_trend_right_side"
else:
    action, reason = "HOLD", "trend_intact"
```

Replay `data/trend_a_share/watch_events.jsonl` while building the next report. A `protection_triggered` event remains `SELL_ALL` while the symbol is still present in `portfolio.csv`, even if the new API signal is unknown. Remove protection state only after the position disappears from the account. A current holding with no state is treated as a historical holding and receives `close - 2 × ATR14`; a formal buy row displays the same close-based line as an estimate, not a filled-order fact.

Render Markdown sections in this order: dates/account freshness; API facts; all holding decisions; top 10 candidates; formal next-session actions; industry concentration; excluded/account exceptions; data sources and estimated/actual API cost; disclaimer. Every formal buy row states the 09:30–10:00 validity window, close-based estimated shares, 1% target amount, and estimated initial line. Render the no-action sentence exactly `现金也是有效仓位，本日无需交易。`

`write_frozen_report(report, reports_dir, revision=False)` writes JSON and Markdown to temporary siblings and replaces both final files only after both temporary writes succeed. If the base pair exists and `revision=False`, load and return it unchanged. For `revision=True`, choose the first `-rN` where neither extension exists.

- [ ] **Step 6: Run the pure engine tests**

Run: `.venv/bin/python -m pytest tests/test_a_share_trend.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the discipline engine**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: build deterministic A-share trend report"
```

---

### Task 4: 17:00 Orchestrator, Retry Contract, CLI, and Feishu Delivery

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/cli.py:1-80,310-480,1260-1500`
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_premarket_cli.py`
- Modify: `config/daily_premarket.env.example`

**Interfaces:**
- Consumes: `load_env_config()`, `build_notifier()`, `send_notification_with_results()`, `RunLock`, `TrendAnimalsClient`, and `FutuQuoteClient`.
- Produces: `run_a_share_trend_report(...) -> AShareTrendRunResult` and CLI `open-trader trend-a-share-report [--date today] [--config PATH] [--revision]`.

- [ ] **Step 1: Write orchestrator tests for every terminal state**

Use injected clocks, sleep, API, quote, and notifier fakes. The ready-path assertion must include exact call order:

```python
def test_report_runner_checks_calendar_status_billing_then_paid_data(tmp_path: Path) -> None:
    calls: list[str] = []
    result = run_a_share_trend_report(
        config=config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 17, 0, tzinfo=SHANGHAI),
        sleep_fn=lambda seconds: None,
        api_factory=lambda **kwargs: ReadyApi(calls),
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        notifier=RecordingNotifier(),
    )

    assert result.status == "generated"
    assert calls[:5] == [
        "futu.calendar", "api.update_status", "api.balance_before",
        "api.components.622466", "api.components.697199",
    ]
    assert calls.index("api.billing") < calls.index("api.snapshots")
```

Add these terminal-state cases:

- weekend/holiday: `status == "holiday"`, no Trend Animals call, no notification;
- not ready at 17:00: immediate waiting notification, sleeps 600 seconds, calls only update status until ready;
- still not ready at 18:00: final failure notification, no `.md`/`.json`;
- systemic Futu failure: retry through 18:00, no formal report;
- one candidate with insufficient/invalid returned K-line rows: exclude only that candidate;
- one holding K-line failure with prior state: report generated and old line preserved;
- one holding K-line failure without prior state: `MANUAL_REVIEW`, report generated;
- snapshot date mismatch: final attempt follows the same retry/deadline rule;
- base report already exists: no API/Futu/notifier call;
- local write succeeds and Feishu fails: JSON contains `delivery_status: "delivery_failed"`, files remain, no second API call;
- API key never appears in captured logs, result errors, artifacts, or notification bodies.
- a valid `searchTicker` response without one exact holding match: mark only that holding `MANUAL_REVIEW`; a transport or non-success API response still blocks the report.
- `execution_date` equals the first returned CN trading date later than `as_of_date`, including across weekends and holidays.

- [ ] **Step 2: Run the focused tests and verify missing runner/CLI failures**

Run: `.venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_premarket_cli.py -q`

Expected: FAIL because `run_a_share_trend_report` and `trend-a-share-report` are not defined.

- [ ] **Step 3: Load the three Trend Animals settings without adding a second env parser**

Extend `DailyPremarketConfig` with secret/private configuration fields that are never rendered:

```python
trend_animals_api_key: str = ""
trend_animals_a_share_tm_id: int = 0
trend_animals_etf_tm_id: int = 0
```

Populate them in `load_env_config`:

```python
trend_animals_api_key=values.get("TREND_ANIMALS_API_KEY", ""),
trend_animals_a_share_tm_id=int(values.get("TREND_ANIMALS_WARM_TO_HOT_A_SHARE_TM_ID", "0")),
trend_animals_etf_tm_id=int(values.get("TREND_ANIMALS_WARM_TO_HOT_ETF_TM_ID", "0")),
```

Validate these three fields only when the A-share command starts, so existing HK/US commands remain backward compatible. Keep the example values empty and retain the explanatory comment; do not place a real key in the example.

- [ ] **Step 4: Implement one retry loop and one run lock**

Use a single lock at `data/runs/.trend_a_share_report.lock`. The loop shape is:

```python
deadline = datetime.combine(run_day, time(18, 0), tzinfo=SHANGHAI)
notified_waiting = False
while True:
    try:
        attempt = _attempt_report(...)
        if attempt.status in {"generated", "existing", "holiday"}:
            return attempt
    except (TrendAnimalsError, FutuQuoteError) as exc:
        last_error = str(exc)
    now = now_fn()
    if now >= deadline:
        _notify_status(notifier, "A股趋势计划失败", last_error)
        return AShareTrendRunResult(status="failed", report_path=None, json_path=None)
    if not notified_waiting:
        _notify_status(notifier, "A股趋势数据等待中", last_error)
        notified_waiting = True
    sleep_fn(min(600.0, max(1.0, (deadline - now).total_seconds())))
```

`_attempt_report` checks Futu calendar first, then `getUpdateStatus`; it requires the exact `asset` rows `A股` and `ETF基金` to both show the current date and does not call paid endpoints earlier. Query a 14-calendar-day CN range and choose the first returned trading date strictly later than the current date as `execution_date`; absence of that date is a Futu calendar failure. It captures `getAccountBalance` before and after paid work and stores both the billing-table estimate and nonnegative balance delta, labeling the delta as run-window actual cost.

Resolve holding tmIds with `search_exact_symbol`, catching only `TrendAnimalsLookupError` as a per-holding manual exception; merge all resolved IDs with component tmIds, call billing once, verify every requested field is present in the live billing response, and make one snapshot request with the sorted unique IDs. The official docs do not state a maximum `tmIds` count; do not invent chunk limits. A server rejection is a failed attempt under the retry contract.

- [ ] **Step 5: Deliver after freezing and record delivery outcome without refetching**

First extend `send_notification_with_results` with keyword-only `channels: set[str] | None = None`, filtering by `_notifier_channel(target)` before delivery. Add focused tests in `tests/test_daily_premarket.py` proving that `channels={"feishu", "feishu_app"}` excludes macOS and `channels={"macos"}` excludes Feishu.

After atomic local writes, send the Markdown body only to Feishu through the extended helper:

```python
attempts = send_notification_with_results(
    notifier, f"A股趋势操作计划 · {report.as_of_date}", markdown,
    channels={"feishu", "feishu_app"},
)
delivery_status = (
    "sent" if any(item.channel.startswith("feishu") and item.success for item in attempts)
    else "delivery_failed"
)
```

Patch only the local JSON delivery metadata atomically after notification. Do not rebuild the report or call Trend Animals/Futu again. Send a short macOS success/failure status, not a duplicate full report.

Record `process_version` as `git rev-parse HEAD` in the JSON and the first structured log line. If Git is unavailable, store `unknown` and treat the live deployment verification as failed rather than claiming the running version is known.

- [ ] **Step 6: Add the command parser and dispatch**

Parser:

```python
trend_report = subparsers.add_parser(
    "trend-a-share-report", help="Generate the Eastmoney A-share trend plan"
)
trend_report.add_argument("--date", default="today")
trend_report.add_argument(
    "--config", type=Path, default=Path("config/daily_premarket.env")
)
trend_report.add_argument("--revision", action="store_true")
```

Dispatch resolves `today` with `ZoneInfo(config.timezone)`, builds notifiers using the existing helper, calls `run_a_share_trend_report`, prints JSON containing status and artifact paths, and returns `0` for `generated`, `existing`, or `holiday`; return `1` for `failed` and `2` for invalid configuration.

- [ ] **Step 7: Run report and CLI tests**

Run: `.venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_premarket_cli.py tests/test_daily_premarket.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the runnable daily report**

```bash
git add config/daily_premarket.env.example src/open_trader/a_share_trend.py \
  src/open_trader/daily_premarket.py src/open_trader/cli.py \
  tests/test_a_share_trend.py tests/test_premarket_cli.py tests/test_daily_premarket.py
git commit -m "feat: run daily A-share trend plan"
```

---

### Task 5: Intraday Protection-Line Watcher

**Files:**
- Create: `src/open_trader/a_share_trend_watch.py`
- Create: `tests/test_a_share_trend_watch.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_premarket_cli.py`

**Interfaces:**
- Consumes: current Eastmoney positions, `protection_state.json`, `FutuQuoteClient.get_cn_trading_days()`, `get_snapshots()`, and existing notifiers.
- Produces: `watch_a_share_protection(...) -> AShareWatchResult` and CLI `open-trader watch-trend-a-share`.

- [ ] **Step 1: Write clock-driven watcher tests**

Use a sequence clock and no real sleeping. Cover these exact behaviors:

```python
def test_watcher_alerts_once_per_symbol_per_day(tmp_path: Path) -> None:
    quote = SequenceQuote([
        {"SH.600900": Decimal("27.30")},
        {"SH.600900": Decimal("27.20")},
    ])
    notifier = RecordingNotifier()

    result = watch_a_share_protection(
        portfolio_path=portfolio(tmp_path, symbol="600900"),
        state_path=state(tmp_path, symbol="600900", active_line="27.31"),
        events_path=tmp_path / "events.jsonl", quote_client=quote,
        notifier=notifier, poll_seconds=5, reconnect_seconds=60,
        now_fn=SequenceClock([
            "2026-07-15T09:30:00+08:00", "2026-07-15T09:30:05+08:00",
            "2026-07-15T15:00:01+08:00",
        ]),
        sleep_fn=lambda seconds: None,
    )

    assert result.trigger_count == 1
    assert sum("全部卖出" in message for _, message in notifier.messages) == 1
```

Add tests for: holiday silent exit; wait from 09:25 to 09:30; lunch pause; stop after 15:00; symbol absent from latest portfolio is not watched; line absent produces a visible manual exception and no comparison; existing same-day JSONL trigger suppresses repeats after process restart; OpenD failure sends one interruption alert, sleeps 60, recreates the client, sends one recovery alert, and resumes; one missing symbol is recorded as unknown rather than safe.

- [ ] **Step 2: Run tests and verify import failure**

Run: `.venv/bin/python -m pytest tests/test_a_share_trend_watch.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'open_trader.a_share_trend_watch'`.

- [ ] **Step 3: Implement session classification and append-only events**

Use these session boundaries:

```python
def cn_session(now: datetime) -> str:
    local = now.astimezone(SHANGHAI).time()
    if local < time(9, 30): return "before"
    if local <= time(11, 30): return "morning"
    if local < time(13, 0): return "lunch"
    if local <= time(15, 0): return "afternoon"
    return "closed"
```

Append JSON lines to `data/trend_a_share/watch_events.jsonl` with `event_id`, `symbol`, `trading_date`, `event_type`, `occurred_at`, `last_price`, and `active_line`. On startup, replay `protection_triggered` events for the current date into an alerted-symbol set.

- [ ] **Step 4: Implement one-symbol/day trigger and reconnect loop**

Core comparison:

```python
if symbol not in alerted and snapshot.last_price <= active_line:
    append_watch_event(..., event_type="protection_triggered")
    send_notification_with_results(
        notifier,
        f"A股保护线触发 · {symbol}",
        f"最新价 {snapshot.last_price} <= 活动保护线 {active_line}\n建议动作：全部卖出（人工执行）",
    )
    alerted.add(symbol)
```

Catch `FutuQuoteError` around both calendar and snapshot calls. On the first error in an outage, notify interruption and append `monitor_interrupted`; close the old client, sleep 60 seconds, and call the injected factory again. On first successful call after interruption, notify recovery and append `monitor_recovered`. Never emit a normal/safe status during an outage.

- [ ] **Step 5: Add CLI with fixed safe defaults**

```python
watch = subparsers.add_parser(
    "watch-trend-a-share", help="Watch Eastmoney A-share protection lines"
)
watch.add_argument("--config", type=Path, default=Path("config/daily_premarket.env"))
watch.add_argument("--poll-seconds", type=float, default=5.0)
watch.add_argument("--reconnect-seconds", type=float, default=60.0)
watch.add_argument("--once", action="store_true")
```

The command reuses the report lock only for loading/validating paths, but uses its own `data/runs/.trend_a_share_watch.lock` while running. `--once` performs one eligible-session poll for direct verification.

- [ ] **Step 6: Run watcher and CLI tests**

Run: `.venv/bin/python -m pytest tests/test_a_share_trend_watch.py tests/test_premarket_cli.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the watcher**

```bash
git add src/open_trader/a_share_trend_watch.py src/open_trader/cli.py \
  tests/test_a_share_trend_watch.py tests/test_premarket_cli.py
git commit -m "feat: watch A-share protection lines"
```

---

### Task 6: Add CN Report and Watcher to the Existing launchd Installer

**Files:**
- Create: `ops/launchd/com.open-trader.trend-a-share-report.plist.template`
- Create: `ops/launchd/com.open-trader.trend-a-share-watch.plist.template`
- Modify: `scripts/install_daily_premarket_launchd.sh`
- Modify: `scripts/uninstall_daily_premarket_launchd.sh`
- Modify: `tests/test_daily_premarket.py:3957-4535`

**Interfaces:**
- Consumes: current installer `--market` contract and `OPEN_TRADER_REPO`/`OPEN_TRADER_PYTHON` config.
- Produces: labels `com.open-trader.trend-a-share-report` and `com.open-trader.trend-a-share-watch` when `--market CN` or `all` is requested.

- [ ] **Step 1: Extend installer tests before shell/template edits**

Add assertions that `--market CN --dry-run` emits two valid plists:

```python
def test_launchd_installer_renders_cn_report_and_watcher(tmp_path: Path) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    _write_launchd_env(repo)
    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"),
         "--dry-run", "--market", "CN"],
        text=True, capture_output=True, check=True,
    )
    plists = _launchd_plists(result.stdout)
    assert {item["Label"] for item in plists} == {
        "com.open-trader.trend-a-share-report",
        "com.open-trader.trend-a-share-watch",
    }
    by_label = {item["Label"]: item for item in plists}
    assert "trend-a-share-report" in by_label[
        "com.open-trader.trend-a-share-report"
    ]["ProgramArguments"]
    assert by_label["com.open-trader.trend-a-share-report"][
        "StartCalendarInterval"
    ][0]["Hour"] == 17
    assert "watch-trend-a-share" in by_label[
        "com.open-trader.trend-a-share-watch"
    ]["ProgramArguments"]
```

Update the asset-copy helper to copy both new templates. Add uninstaller tests for CN-only and `all`; keep existing HK/US default expectations but update `all` to include CN.

- [ ] **Step 2: Run launchd tests and verify `CN` rejection**

Run: `.venv/bin/python -m pytest tests/test_daily_premarket.py -k launchd -q`

Expected: FAIL because the installer usage currently accepts only `HK|US|all`.

- [ ] **Step 3: Create two literal templates to avoid destabilizing HK/US**

The report template arguments are:

```xml
<array>
  <string>OPEN_TRADER_PYTHON</string>
  <string>-m</string><string>open_trader</string>
  <string>trend-a-share-report</string>
  <string>--date</string><string>today</string>
  <string>--config</string>
  <string>OPEN_TRADER_REPO/config/daily_premarket.env</string>
</array>
```

Use five weekday `StartCalendarInterval` entries at hour `17`, minute `0`. The watcher template uses `watch-trend-a-share`, hour `9`, minute `25`. Both set `WorkingDirectory` to `OPEN_TRADER_REPO`. Use exact log paths `logs/daily_premarket/launchd-CN-report.out.log`, `launchd-CN-report.err.log`, `launchd-CN-watch.out.log`, and `launchd-CN-watch.err.log`.

- [ ] **Step 4: Extend installer/uninstaller with one CN branch**

Change usage/validation to `HK|US|CN|all`; keep `markets=("HK" "US")` for the existing loop and invoke a `render_cn_jobs` function only for CN/all. Install/reload each CN plist with the same `plutil -lint`, `launchctl unload`, and `launchctl load` pattern already used. Uninstall the two exact CN labels for CN/all.

- [ ] **Step 5: Run launchd tests and shell syntax checks**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k launchd -q
bash -n scripts/install_daily_premarket_launchd.sh
bash -n scripts/uninstall_daily_premarket_launchd.sh
scripts/install_daily_premarket_launchd.sh --dry-run --market CN >/dev/null
```

Expected: pytest PASS, both `bash -n` commands exit 0, and the dry-run exits 0 after its built-in `plutil -lint` validates both rendered documents.

- [ ] **Step 6: Commit scheduling**

```bash
git add ops/launchd/com.open-trader.trend-a-share-report.plist.template \
  ops/launchd/com.open-trader.trend-a-share-watch.plist.template \
  scripts/install_daily_premarket_launchd.sh \
  scripts/uninstall_daily_premarket_launchd.sh tests/test_daily_premarket.py
git commit -m "feat: schedule CN trend report and watcher"
```

---

### Task 7: Real API Smoke, Full Gate, Exact-SHA Deployment, and Live Evidence

**Files:**
- Modify only if verification exposes a defect; repeat the failing task's TDD cycle and commit that fix before returning here.
- Inspect: `config/daily_premarket.env`, `reports/trend_a_share/`, `data/trend_animals/cache/`, `data/trend_a_share/`, `logs/daily_premarket/`, launchd plists, running processes.

**Interfaces:**
- Consumes: real Trend Animals key from the ignored 0600 env file, Futu OpenD, Feishu credentials, the current Eastmoney portfolio, launchd, and Dashboard acceptance environment.
- Produces: evidence that the committed code, live jobs, report artifacts, notifications, logs, and review URL all correspond to the same accepted SHA.

- [ ] **Step 1: Verify repository and secret hygiene before live calls**

Run:

```bash
git status --short
git check-ignore -v config/daily_premarket.env
stat -f '%Lp %N' config/daily_premarket.env
git grep -n 'sk-' -- . ':!docs/superpowers/plans/*'
```

Expected: unrelated user files remain untouched; the real env is ignored, mode is `600`, and `git grep` finds no live Trend Animals key.

- [ ] **Step 2: Run all automated tests**

Run: `.venv/bin/python -m pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Exercise calendar and report commands directly**

Run:

```bash
.venv/bin/python -m open_trader trend-a-share-report \
  --date today --config config/daily_premarket.env
.venv/bin/python -m open_trader watch-trend-a-share \
  --config config/daily_premarket.env --once
```

Expected: on a CN trading day with data ready, the report command prints `generated` or `existing` and both dated files exist; watcher prints a structured one-poll result. On holiday it prints `holiday` without a paid cache entry. Do not fabricate a success if real data/OpenD/Feishu is unavailable.

- [ ] **Step 4: Inspect artifacts, costs, cache reuse, and key absence**

Run the report command a second time, then inspect:

```bash
find reports/trend_a_share -maxdepth 1 -type f -print | sort | tail -10
find data/trend_animals/cache -type f -print | sort | tail -20
rg -n 'delivery_status|as_of_date|execution_date|estimated_cost|actual_cost' \
  reports/trend_a_share data/trend_a_share
rg -n 'TREND_ANIMALS_API_KEY|sk-' \
  reports/trend_a_share data/trend_animals data/trend_a_share logs/daily_premarket
```

Expected: the second normal run returns the frozen pair without new paid response files; cost/source fields exist; the final `rg` has no secret match.

- [ ] **Step 5: Run the mandatory final acceptance gate**

Run: `make acceptance`

Expected: exactly `PASS`. On `FAIL`, diagnose, add a failing regression test, implement the smallest fix, commit, and rerun. On `BLOCKED`, report the external blocker and stop; do not substitute curl, mocks, fixtures, screenshots, or unit tests.

- [ ] **Step 6: Record accepted SHA and redeploy that exact commit**

Run:

```bash
ACCEPTED_SHA="$(git rev-parse HEAD)"
git status --short
scripts/install_daily_premarket_launchd.sh --market CN
launchctl print gui/"$(id -u)"/com.open-trader.trend-a-share-report
launchctl print gui/"$(id -u)"/com.open-trader.trend-a-share-watch
```

Expected: task files are committed at `ACCEPTED_SHA`; only unrelated pre-existing worktree changes remain; both jobs show the repository working directory and expected program arguments.

- [ ] **Step 7: Restart stale processes and verify fresh live evidence**

Kick the report job only when doing so will not create an unwanted revision or duplicate notification:

```bash
launchctl kickstart -k gui/"$(id -u)"/com.open-trader.trend-a-share-report
sleep 2
ps -axo pid,lstart,command | rg 'trend-a-share-(report|watch)'
tail -100 logs/daily_premarket/launchd-CN-report.out.log
tail -100 logs/daily_premarket/launchd-CN-report.err.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: no pre-change A-share process remains; fresh log timestamps follow the kickstart; logged repository/SHA metadata equals `ACCEPTED_SHA`; the review URL returns HTTP 200. If the watcher is outside its run window, its loaded launchd definition plus the direct `--once` check is the honest evidence; do not claim an out-of-session long-running PID.

- [ ] **Step 8: Hand off only after all gates pass**

Report the accepted SHA, dated Markdown/JSON paths, launchd labels, live log paths, Feishu delivery status, and `http://127.0.0.1:8766/`. State clearly that orders remain manual and that Trend Animals analysis is reference information, not investment advice.
