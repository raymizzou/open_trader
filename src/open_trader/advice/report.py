from __future__ import annotations

import csv
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Mapping

from open_trader.market_scope import (
    MarketScope,
    market_report_path,
    market_run_dir,
    market_scoped_latest_path,
    parse_market_scope,
)

from .models import PREMARKET_ACTION_FIELDNAMES, PremarketAction


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def write_premarket_outputs(
    *,
    run_date: str,
    actions: Iterable[PremarketAction],
    data_dir: Path,
    reports_dir: Path,
    update_latest: bool = True,
    no_eligible: bool = False,
    market: str | MarketScope | None = None,
) -> tuple[Path, Path, Path]:
    sorted_actions = sorted(
        actions,
        key=lambda action: (
            SEVERITY_ORDER[action.severity],
            _negative_weight(action.portfolio_weight_hkd),
            action.symbol,
        ),
    )
    rows = [action.to_row() for action in sorted_actions]

    market_scope = parse_market_scope(market) if market is not None else None
    if market_scope is not None:
        run_actions_path = market_run_dir(
            data_dir,
            run_date,
            market_scope,
        ) / "premarket_actions.csv"
        latest_actions_path = market_scoped_latest_path(
            data_dir,
            market_scope,
            "premarket_actions.csv",
        )
        report_path = market_report_path(
            reports_dir,
            "premarket",
            run_date,
            market_scope,
        )
    else:
        run_actions_path = data_dir / "runs" / run_date / "premarket_actions.csv"
        latest_actions_path = data_dir / "latest" / "premarket_actions.csv"
        report_path = reports_dir / "premarket" / f"{run_date}.md"

    _atomic_write_csv(run_actions_path, PREMARKET_ACTION_FIELDNAMES, rows)
    if update_latest:
        _atomic_write_csv(latest_actions_path, PREMARKET_ACTION_FIELDNAMES, rows)
    _atomic_write_text(
        report_path,
        _render_markdown(
            run_date,
            sorted_actions,
            no_eligible=no_eligible,
            market=market_scope,
        ),
    )

    return run_actions_path, latest_actions_path, report_path


def _render_markdown(
    run_date: str,
    actions: list[PremarketAction],
    *,
    no_eligible: bool = False,
    market: MarketScope | None = None,
) -> str:
    lines = [f"# Premarket Trading Brief - {run_date}", ""]
    if no_eligible:
        lines.extend([_no_eligible_message(market), ""])
        return "\n".join(lines)

    if not actions:
        lines.extend(["No material trading advice changes were generated.", ""])
        return "\n".join(lines)

    lines.extend(["## Action Items", ""])
    for index, action in enumerate(actions, start=1):
        lines.extend(
            [
                f"### {index}. {action.symbol}",
                "",
                f"- Severity: {action.severity}",
                f"- Current weight: {action.portfolio_weight_hkd}",
                f"- Change type: {action.change_type}",
                f"- Suggested action: {action.suggested_action}",
                f"- Summary: {action.summary}",
                f"- Rationale: {action.rationale}",
            ]
        )
        if action.watch_trigger:
            lines.append(f"- Watch trigger: {action.watch_trigger}")
        lines.append("")

    return "\n".join(lines)


def _no_eligible_message(market: MarketScope | None) -> str:
    if market == MarketScope.HK:
        return "No eligible HK stocks or ETFs were found."
    if market == MarketScope.US:
        return "No eligible US stocks or ETFs were found."
    return "No eligible stocks or ETFs were found."


def _negative_weight(value: str) -> float:
    try:
        return -float(value.strip().rstrip("%").replace(",", ""))
    except ValueError:
        return 0.0


def _atomic_write_csv(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        fieldname: (
                            "" if row.get(fieldname) is None else row.get(fieldname)
                        )
                        for fieldname in fieldnames
                    }
                )
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            _best_effort_unlink(temp_path)
        raise


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            _best_effort_unlink(temp_path)
        raise


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass
