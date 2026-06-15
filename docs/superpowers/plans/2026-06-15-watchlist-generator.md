# Watchlist Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `build-watchlist`, converting `premarket_actions.csv` into dated and latest `watchlist.csv` files.

**Architecture:** Add a small watchlist module that owns trigger parsing, row construction, CSV writes, and latest-file promotion. Reuse the existing CLI style in `src/open_trader/cli.py`; keep live price polling and notifications out of scope.

**Tech Stack:** Python dataclasses, standard-library `csv`, `re`, `pathlib`, `tempfile`, `shutil`, pytest.

---

## File Structure

- Modify `src/open_trader/advice/models.py`
  - Add stable watchlist CSV field names.
  - Add `WatchlistRow`.
- Create `src/open_trader/watchlist.py`
  - Parse `watch_trigger`.
  - Convert action rows into watchlist rows.
  - Write `data/runs/<date>/watchlist.csv`.
  - Atomically promote `data/latest/watchlist.csv` unless dry-run.
- Modify `src/open_trader/cli.py`
  - Add `build-watchlist`.
  - Reuse existing `canonical_date`.
- Create `tests/test_watchlist_models.py`
  - Verify field order and row serialization.
- Create `tests/test_watchlist.py`
  - Verify trigger parsing, pipeline output, dry-run behavior, and malformed input handling.
- Modify `tests/test_premarket_cli.py` or create `tests/test_watchlist_cli.py`
  - Verify CLI help and wiring.
- Modify `docs/monthly_portfolio_import.md`
  - Add the watchlist command after daily premarket advice.

---

### Task 1: Watchlist Row Model

**Files:**
- Modify: `src/open_trader/advice/models.py`
- Create: `tests/test_watchlist_models.py`

- [ ] **Step 1: Write the failing model test**

Create `tests/test_watchlist_models.py`:

```python
from open_trader.advice.models import WATCHLIST_FIELDNAMES, WatchlistRow


def test_watchlist_row_to_row_has_stable_csv_fields() -> None:
    row = WatchlistRow(
        run_date="2026-06-16",
        symbol="VIXY",
        market="US",
        suggested_action="reduce",
        severity="high",
        portfolio_weight_hkd="3.05%",
        trigger_type="price",
        operator="<=",
        trigger_price="95",
        trigger_text="below 95",
        status="active",
        error="",
    )

    serialized = row.to_row()

    assert list(serialized) == WATCHLIST_FIELDNAMES
    assert serialized == {
        "run_date": "2026-06-16",
        "symbol": "VIXY",
        "market": "US",
        "suggested_action": "reduce",
        "severity": "high",
        "portfolio_weight_hkd": "3.05%",
        "trigger_type": "price",
        "operator": "<=",
        "trigger_price": "95",
        "trigger_text": "below 95",
        "status": "active",
        "error": "",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist_models.py -v
```

Expected: FAIL because `WATCHLIST_FIELDNAMES` and `WatchlistRow` do not exist.

- [ ] **Step 3: Add the model**

In `src/open_trader/advice/models.py`, add:

```python
WatchlistStatus = Literal["active", "manual_review", "no_trigger", "error"]
TriggerType = Literal["price", "open_price", "manual_review", "none"]

WATCHLIST_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "suggested_action",
    "severity",
    "portfolio_weight_hkd",
    "trigger_type",
    "operator",
    "trigger_price",
    "trigger_text",
    "status",
    "error",
]


@dataclass(frozen=True)
class WatchlistRow:
    run_date: str
    symbol: str
    market: str
    suggested_action: str
    severity: Severity
    portfolio_weight_hkd: str
    trigger_type: TriggerType
    operator: str
    trigger_price: str
    trigger_text: str
    status: WatchlistStatus
    error: str

    def to_row(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in WATCHLIST_FIELDNAMES}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist_models.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/advice/models.py tests/test_watchlist_models.py
git commit -m "feat: add watchlist row model"
```

---

### Task 2: Trigger Parser

**Files:**
- Create: `src/open_trader/watchlist.py`
- Create or modify: `tests/test_watchlist.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_watchlist.py`:

```python
import pytest

from open_trader.watchlist import ParsedTrigger, parse_watch_trigger


@pytest.mark.parametrize(
    ("text", "trigger_type", "operator", "price"),
    [
        ("below 95", "price", "<=", "95"),
        ("under 95.50", "price", "<=", "95.50"),
        ("breaks below 95", "price", "<=", "95"),
        ("<= 95", "price", "<=", "95"),
        ("above 110", "price", ">=", "110"),
        ("over 110.25", "price", ">=", "110.25"),
        ("breaks above 110", "price", ">=", "110"),
        (">= 110", "price", ">=", "110"),
        ("open below 95", "open_price", "<=", "95"),
        ("open above 110", "open_price", ">=", "110"),
    ],
)
def test_parse_watch_trigger_returns_monitorable_price_trigger(
    text: str,
    trigger_type: str,
    operator: str,
    price: str,
) -> None:
    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type=trigger_type,
        operator=operator,
        trigger_price=price,
        trigger_text=text,
        status="active",
        error="",
    )


def test_parse_watch_trigger_marks_empty_trigger_as_no_trigger() -> None:
    assert parse_watch_trigger("") == ParsedTrigger(
        trigger_type="none",
        operator="",
        trigger_price="",
        trigger_text="",
        status="no_trigger",
        error="",
    )


def test_parse_watch_trigger_marks_unclear_text_as_manual_review() -> None:
    text = "watch if support fails"

    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=text,
        status="manual_review",
        error="",
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist.py -v
```

Expected: FAIL because `open_trader.watchlist` does not exist.

- [ ] **Step 3: Implement parser**

Create `src/open_trader/watchlist.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedTrigger:
    trigger_type: str
    operator: str
    trigger_price: str
    trigger_text: str
    status: str
    error: str


PRICE_RE = r"(?P<price>\d+(?:\.\d+)?)"
DOWNSIDE_RE = re.compile(
    rf"(?P<open>open\s+)?(?:(?:breaks\s+)?(?:below|under)|<=|<)\s*\$?{PRICE_RE}",
    re.IGNORECASE,
)
UPSIDE_RE = re.compile(
    rf"(?P<open>open\s+)?(?:(?:breaks\s+)?(?:above|over)|>=|>)\s*\$?{PRICE_RE}",
    re.IGNORECASE,
)


def parse_watch_trigger(text: str) -> ParsedTrigger:
    original = text.strip()
    if not original:
        return ParsedTrigger(
            trigger_type="none",
            operator="",
            trigger_price="",
            trigger_text="",
            status="no_trigger",
            error="",
        )

    downside = DOWNSIDE_RE.search(original)
    if downside:
        return ParsedTrigger(
            trigger_type="open_price" if downside.group("open") else "price",
            operator="<=",
            trigger_price=downside.group("price"),
            trigger_text=original,
            status="active",
            error="",
        )

    upside = UPSIDE_RE.search(original)
    if upside:
        return ParsedTrigger(
            trigger_type="open_price" if upside.group("open") else "price",
            operator=">=",
            trigger_price=upside.group("price"),
            trigger_text=original,
            status="active",
            error="",
        )

    return ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=original,
        status="manual_review",
        error="",
    )
```

- [ ] **Step 4: Run parser tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/watchlist.py tests/test_watchlist.py
git commit -m "feat: parse watchlist triggers"
```

---

### Task 3: Watchlist Pipeline and CSV Writes

**Files:**
- Modify: `src/open_trader/watchlist.py`
- Modify: `tests/test_watchlist.py`

- [ ] **Step 1: Add failing pipeline tests**

Append to `tests/test_watchlist.py`:

```python
import csv
from pathlib import Path

from open_trader.watchlist import build_watchlist


ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "change_type",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
]


def write_actions(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def test_build_watchlist_writes_run_and_latest_outputs(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
            {
                "run_date": "2026-06-16",
                "symbol": "QQQ",
                "market": "US",
                "portfolio_weight_hkd": "1.40%",
                "severity": "medium",
                "change_type": "new_signal",
                "suggested_action": "watch",
                "summary": "QQQ changed",
                "rationale": "Fake rationale",
                "watch_trigger": "support fails",
            },
        ],
    )

    result = build_watchlist(
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date=None,
        update_latest=True,
    )

    assert result.run_date == "2026-06-16"
    assert result.watchlist_count == 2
    assert result.watchlist_path == tmp_path / "data/runs/2026-06-16/watchlist.csv"
    assert result.latest_path == tmp_path / "data/latest/watchlist.csv"
    assert result.watchlist_path.exists()
    assert result.latest_path.exists()

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["trigger_type"] == "price"
    assert rows[0]["operator"] == "<="
    assert rows[0]["trigger_price"] == "95"
    assert rows[0]["status"] == "active"
    assert rows[1]["symbol"] == "QQQ"
    assert rows[1]["trigger_type"] == "manual_review"
    assert rows[1]["status"] == "manual_review"


def test_build_watchlist_dry_run_does_not_update_latest(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    latest_path = tmp_path / "data/latest/watchlist.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("existing\n", encoding="utf-8")
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
        ],
    )

    result = build_watchlist(
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date=None,
        update_latest=False,
    )

    assert result.watchlist_path.exists()
    assert latest_path.read_text(encoding="utf-8") == "existing\n"


def test_build_watchlist_writes_empty_headers_when_no_actions(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(actions_path, [])

    result = build_watchlist(
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-16",
        update_latest=True,
    )

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert rows == []
    assert result.watchlist_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist.py -v
```

Expected: FAIL because `build_watchlist` does not exist.

- [ ] **Step 3: Implement pipeline**

Extend `src/open_trader/watchlist.py`:

```python
import csv
import shutil
from tempfile import NamedTemporaryFile
from pathlib import Path

from .advice.models import WATCHLIST_FIELDNAMES, WatchlistRow


ACTION_REQUIRED_FIELDS = {
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "suggested_action",
    "watch_trigger",
}


@dataclass(frozen=True)
class WatchlistResult:
    run_date: str
    watchlist_count: int
    watchlist_path: Path
    latest_path: Path


def build_watchlist(
    *,
    actions_path: Path,
    data_dir: Path,
    run_date: str | None,
    update_latest: bool,
) -> WatchlistResult:
    rows = _read_action_rows(actions_path)
    effective_run_date = run_date or _latest_run_date(rows)
    watchlist_rows = [_row_from_action(row, effective_run_date) for row in rows]
    watchlist_path = _write_watchlist_rows(
        data_dir / "runs" / effective_run_date / "watchlist.csv",
        watchlist_rows,
    )
    latest_path = data_dir / "latest" / "watchlist.csv"
    if update_latest:
        _promote_latest(source_path=watchlist_path, latest_path=latest_path)
    return WatchlistResult(
        run_date=effective_run_date,
        watchlist_count=len(watchlist_rows),
        watchlist_path=watchlist_path,
        latest_path=latest_path,
    )


def _read_action_rows(actions_path: Path) -> list[dict[str, str]]:
    with actions_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing = sorted(ACTION_REQUIRED_FIELDS - fieldnames)
        if missing:
            raise ValueError(f"missing action column(s): {', '.join(missing)}")
        return list(reader)


def _latest_run_date(rows: list[dict[str, str]]) -> str:
    dates = sorted({row.get("run_date", "").strip() for row in rows if row.get("run_date", "").strip()})
    if not dates:
        raise ValueError("--date is required when actions file has no run_date rows")
    return dates[-1]


def _row_from_action(row: dict[str, str], fallback_run_date: str) -> WatchlistRow:
    parsed = parse_watch_trigger(row.get("watch_trigger", ""))
    return WatchlistRow(
        run_date=row.get("run_date", "").strip() or fallback_run_date,
        symbol=row.get("symbol", "").strip(),
        market=row.get("market", "").strip(),
        suggested_action=row.get("suggested_action", "").strip(),
        severity=row.get("severity", "low").strip() or "low",
        portfolio_weight_hkd=row.get("portfolio_weight_hkd", "").strip(),
        trigger_type=parsed.trigger_type,
        operator=parsed.operator,
        trigger_price=parsed.trigger_price,
        trigger_text=parsed.trigger_text,
        status=parsed.status,
        error=parsed.error,
    )


def _write_watchlist_rows(path: Path, rows: list[WatchlistRow]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WATCHLIST_FIELDNAMES)
        writer.writeheader()
        writer.writerows(row.to_row() for row in rows)
    return path


def _promote_latest(*, source_path: Path, latest_path: Path) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "wb",
        dir=latest_path.parent,
        prefix=f".{latest_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        with source_path.open("rb") as source:
            shutil.copyfileobj(source, handle)
    temp_path.replace(latest_path)
```

- [ ] **Step 4: Run pipeline tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist.py tests/test_watchlist_models.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/watchlist.py tests/test_watchlist.py
git commit -m "feat: build watchlist csv"
```

---

### Task 4: CLI Command

**Files:**
- Modify: `src/open_trader/cli.py`
- Create: `tests/test_watchlist_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_watchlist_cli.py`:

```python
from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.watchlist import WatchlistResult


def test_build_watchlist_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["build-watchlist", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--actions" in output
    assert "--data-dir" in output
    assert "--date" in output
    assert "--dry-run" in output


def test_build_watchlist_main_wires_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_build_watchlist(**kwargs: object) -> WatchlistResult:
        captured.update(kwargs)
        data_dir = kwargs["data_dir"]
        assert isinstance(data_dir, Path)
        return WatchlistResult(
            run_date="2026-06-16",
            watchlist_count=2,
            watchlist_path=data_dir / "runs/2026-06-16/watchlist.csv",
            latest_path=data_dir / "latest/watchlist.csv",
        )

    monkeypatch.setattr(cli, "build_watchlist", fake_build_watchlist)

    result = cli.main(
        [
            "build-watchlist",
            "--actions",
            "premarket_actions.csv",
            "--data-dir",
            str(tmp_path / "data"),
            "--date",
            "2026-06-16",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured["actions_path"] == Path("premarket_actions.csv")
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["run_date"] == "2026-06-16"
    assert captured["update_latest"] is False

    output = capsys.readouterr().out
    assert "run_date: 2026-06-16" in output
    assert "watchlist: 2" in output
    assert "watchlist_csv:" in output
    assert "latest:" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist_cli.py -v
```

Expected: FAIL because `build-watchlist` is not registered.

- [ ] **Step 3: Implement CLI**

Modify `src/open_trader/cli.py`:

```python
from .watchlist import build_watchlist
```

Inside `build_parser()` after the `run-premarket` parser:

```python
    watchlist_parser = subparsers.add_parser(
        "build-watchlist",
        help="Convert premarket action rows into watchlist.csv",
    )
    watchlist_parser.add_argument(
        "--actions",
        type=Path,
        default=Path("data/latest/premarket_actions.csv"),
    )
    watchlist_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    watchlist_parser.add_argument(
        "--date",
        type=canonical_date,
        help="Run date, YYYY-MM-DD. Required only when actions rows do not contain run_date.",
    )
    watchlist_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write run output but do not update latest watchlist",
    )
```

Inside `main()` before the unknown-command error:

```python
    if args.command == "build-watchlist":
        result = build_watchlist(
            actions_path=args.actions,
            data_dir=args.data_dir,
            run_date=args.date,
            update_latest=not args.dry_run,
        )
        print(f"run_date: {result.run_date}")
        print(f"watchlist: {result.watchlist_count}")
        print(f"watchlist_csv: {result.watchlist_path}")
        print(f"latest: {result.latest_path}")
        return 0
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_watchlist_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/cli.py tests/test_watchlist_cli.py
git commit -m "feat: add watchlist cli command"
```

---

### Task 5: Documentation and End-to-End Verification

**Files:**
- Modify: `docs/monthly_portfolio_import.md`

- [ ] **Step 1: Add documentation**

Append this section after the daily premarket advice section in
`docs/monthly_portfolio_import.md`:

````markdown
## Build Watchlist

After the premarket run creates `data/latest/premarket_actions.csv`, convert it
into monitorable watchlist rows:

```bash
.venv/bin/python -m open_trader build-watchlist \
  --actions data/latest/premarket_actions.csv \
  --data-dir data
```

Optional dry run:

```bash
.venv/bin/python -m open_trader build-watchlist \
  --actions data/latest/premarket_actions.csv \
  --data-dir data \
  --dry-run
```

Main output:

```text
data/latest/watchlist.csv
```

Rows with clear price conditions become `active`. Rows with unclear trigger text
become `manual_review` and should be reviewed before automated alerting.
````

- [ ] **Step 2: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Verify CLI help**

Run:

```bash
.venv/bin/python -m open_trader build-watchlist --help
```

Expected: help includes `--actions`, `--data-dir`, `--date`, and `--dry-run`.

- [ ] **Step 4: Verify with current local data**

Run:

```bash
.venv/bin/python -m open_trader build-watchlist \
  --actions data/latest/premarket_actions.csv \
  --data-dir data \
  --dry-run
```

Expected: command exits 0 and prints:

```text
run_date: <date>
watchlist: <row count>
watchlist_csv: data/runs/<date>/watchlist.csv
latest: data/latest/watchlist.csv
```

- [ ] **Step 5: Commit**

```bash
git add docs/monthly_portfolio_import.md
git commit -m "docs: record watchlist command"
```

---

## Final Verification

- [ ] Run full tests:

```bash
.venv/bin/python -m pytest -q
```

- [ ] Check git status:

```bash
git status --short --branch
```

- [ ] Review recent commits:

```bash
git log --oneline -5
```
