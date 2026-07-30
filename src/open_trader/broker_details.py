from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path


_RUN_DATE = re.compile(r"(\d{4}-\d{2}(?:-\d{2})?)")
_SUPPORTED_BROKERS = {"futu", "tiger", "phillips", "eastmoney"}


@dataclass(frozen=True)
class BrokerDetailSnapshot:
    broker: str
    positions: tuple[dict[str, str], ...]
    cash: tuple[dict[str, str], ...]
    snapshot_period: str
    source_kind: str
    available: bool
    reason: str


def load_broker_detail_snapshot(
    data_dir: Path,
    broker: str,
) -> BrokerDetailSnapshot:
    normalized_broker = broker.strip().lower()
    if normalized_broker not in _SUPPORTED_BROKERS:
        raise ValueError(f"unsupported broker: {broker}")

    runs_dir = data_dir / "runs"
    candidates: list[
        tuple[
            tuple[str, str],
            str,
            list[dict[str, str]],
            list[dict[str, str]],
        ]
    ] = []
    if runs_dir.exists():
        for run_dir in sorted(
            (path for path in runs_dir.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        ):
            positions = [
                row
                for row in _read_rows(run_dir / "extracted_positions.csv")
                if row.get("broker", "").strip().lower() == normalized_broker
            ]
            cash = [
                row
                for row in _read_rows(run_dir / "extracted_cash.csv")
                if row.get("broker", "").strip().lower() == normalized_broker
            ]
            if not positions and not cash:
                continue
            period = _snapshot_period([*positions, *cash]) or run_dir.name
            live = _is_live_snapshot([*positions, *cash], normalized_broker)
            candidates.append(
                ((period, run_dir.name), "live_account" if live else "statement", positions, cash)
            )

    if not candidates:
        return BrokerDetailSnapshot(
            broker=normalized_broker,
            positions=(),
            cash=(),
            snapshot_period="",
            source_kind="live_account" if normalized_broker in {"futu", "tiger"} else "statement",
            available=False,
            reason=f"未找到可用的{normalized_broker}账户明细",
        )

    live_candidates = [candidate for candidate in candidates if candidate[1] == "live_account"]
    selected = max(live_candidates or candidates, key=lambda candidate: candidate[0])
    key, source_kind, positions, cash = selected
    return BrokerDetailSnapshot(
        broker=normalized_broker,
        positions=tuple(positions),
        cash=tuple(cash),
        snapshot_period=key[0],
        source_kind=source_kind,
        available=True,
        reason="",
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _snapshot_period(rows: list[dict[str, str]]) -> str:
    periods = [
        match.group(1)
        for row in rows
        if (match := _RUN_DATE.search(row.get("statement_id", ""))) is not None
    ]
    return max(periods) if periods else ""


def _is_live_snapshot(rows: list[dict[str, str]], broker: str) -> bool:
    suffixes = (f"-{broker}-live", "-tiger-live", "-futu-live")
    return any(
        row.get("statement_id", "").strip().lower().endswith(suffix)
        for row in rows
        for suffix in suffixes
    )
