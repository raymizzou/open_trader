from __future__ import annotations

import csv
from pathlib import Path

from open_trader.advice.models import PremarketAction
from open_trader.advice.report import write_premarket_outputs


def action(
    symbol: str,
    severity: str = "medium",
    weight: str = "3.05%",
) -> PremarketAction:
    return PremarketAction(
        run_date="2026-06-16",
        symbol=symbol,
        market="US",
        portfolio_weight_hkd=weight,
        severity=severity,  # type: ignore[arg-type]
        change_type="action_changed",
        suggested_action="reduce",
        summary=f"{symbol} needs action.",
        rationale=f"{symbol} latest advice changed.",
        watch_trigger="Watch the open.",
    )


def test_write_premarket_outputs_writes_actions_csv_and_markdown(
    tmp_path: Path,
) -> None:
    actions_csv, latest_csv, report_path = write_premarket_outputs(
        run_date="2026-06-16",
        actions=[
            action("QQQ", "low", "1.40%"),
            action("VIXY", "high", "3.05%"),
            action("SPY", "high", "5.10%"),
            action("AAPL", "high", "5.10%"),
            action("MSFT", "medium", "7.00%"),
        ],
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )

    assert actions_csv == tmp_path / "data/runs/2026-06-16/premarket_actions.csv"
    assert latest_csv == tmp_path / "data/latest/premarket_actions.csv"
    assert report_path == tmp_path / "reports/premarket/2026-06-16.md"

    rows = list(csv.DictReader(actions_csv.open(encoding="utf-8")))
    assert [row["symbol"] for row in rows] == ["AAPL", "SPY", "VIXY", "MSFT", "QQQ"]
    assert latest_csv.read_text(encoding="utf-8") == actions_csv.read_text(
        encoding="utf-8"
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "# Premarket Trading Brief - 2026-06-16" in markdown
    assert "## Action Items" in markdown
    assert "VIXY" in markdown
    assert "QQQ" in markdown


def test_write_premarket_outputs_handles_no_actions(tmp_path: Path) -> None:
    _, _, report_path = write_premarket_outputs(
        run_date="2026-06-16",
        actions=[],
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "No material trading advice changes" in markdown
