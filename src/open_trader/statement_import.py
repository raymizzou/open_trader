from __future__ import annotations

import csv
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
import re
from shutil import copyfile, copytree, rmtree
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

from .fx import StaticMonthEndFxProvider
from .models import StatementTrade
from .parsers.base import StatementParser
from .parsers.eastmoney import EastmoneyStatementParser
from .parsers.phillips import PhillipsStatementParser
from .pipeline import run_uploaded_statement
from .trend_api_stats import (
    build_statement_actual_stats_payload,
    write_trend_api_stats,
)


RATES_TO_HKD = {
    "phillips": {"USD": Decimal("7.8"), "CNY": Decimal("1.08")},
    "eastmoney": {"USD": Decimal("7.8"), "CNY": Decimal("1.08")},
}
STATEMENT_PERIOD = re.compile(r"^(\d{4}-\d{2}(?:-\d{2})?)-")


class StatementImportService:
    def __init__(
        self,
        *,
        data_dir: Path,
        portfolio_path: Path,
        eastmoney_password: str,
        reports_dir: Path | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.portfolio_path = portfolio_path
        self.eastmoney_password = eastmoney_password
        self.reports_dir = reports_dir or data_dir

    def import_pdf(self, broker: str, body: bytes) -> dict[str, object]:
        parser = self._parser(broker)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".statement-upload-", dir=self.data_dir) as name:
            uploaded = Path(name) / "statement.pdf"
            uploaded.write_bytes(body)
            statement_date = parser.statement_date(uploaded)  # type: ignore[attr-defined]
            parsed = parser.parse(uploaded, statement_date)
            if not parsed.positions and not parsed.cash_balances:
                raise ValueError(f"{broker} 结单没有可导入的持仓或现金")
            current_period = self._latest_statement_period(broker)
            if current_period and statement_date[: len(current_period)] < current_period:
                raise ValueError(
                    f"{statement_date} 早于当前结单 {current_period}，拒绝导入"
                )
            statement_period = (
                statement_date if broker == "phillips" else statement_date[:7]
            )
            statistics_cutoff_at = _statement_cutoff(statement_date, broker)
            generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
            stats = build_statement_actual_stats_payload(
                data_dir=self.data_dir,
                reports_dir=self.reports_dir,
                broker=broker,
                statement_period=statement_period,
                fills=[_trade_fill(trade, statement_period) for trade in parsed.trades],
                generated_at=generated_at,
                statistics_cutoff_at=statistics_cutoff_at,
            )
            archive = self._archive_path(broker, statement_date)
            snapshots = [
                _snapshot_path(self.portfolio_path, Path(name) / "rollback", "portfolio"),
                _snapshot_path(
                    self.data_dir / "runs" / statement_date[:7],
                    Path(name) / "rollback",
                    "run",
                ),
                _snapshot_path(
                    self.data_dir / "latest" / "trend_api_stats.json",
                    Path(name) / "rollback",
                    "stats",
                ),
            ]
            backup = _promote_archive(uploaded, archive)
            try:
                run_uploaded_statement(
                    statement_date=statement_date,
                    statement_path=archive,
                    parser=parser,
                    data_dir=self.data_dir,
                    portfolio_path=self.portfolio_path,
                    fx_provider=StaticMonthEndFxProvider(
                        statement_date[:7],
                        RATES_TO_HKD[broker],
                        fx_date=statement_date,
                    ),
                )
                write_trend_api_stats(self.data_dir, stats)
            except Exception:
                try:
                    _restore_archive(archive, backup)
                finally:
                    for snapshot in snapshots:
                        _restore_snapshot(snapshot)
                raise
            if backup is not None:
                backup.unlink(missing_ok=True)
        return {
            "status": "ok",
            "broker": broker,
            "statement_date": statement_date,
            "positions": len(parsed.positions),
            "cash": len(parsed.cash_balances),
            "warnings": len(parsed.warnings),
            "trades": len(parsed.trades),
            "actual_rounds": sum(
                round_["broker"] == broker
                and round_["attribution_status"] == "attributed"
                for round_ in stats["rounds"]
            ),
            "statistics_cutoff_at": statistics_cutoff_at,
        }

    def _parser(self, broker: str) -> StatementParser:
        if broker == "phillips":
            return PhillipsStatementParser()
        if broker == "eastmoney":
            if not self.eastmoney_password:
                raise ValueError("未配置东方财富对账单密码")
            return EastmoneyStatementParser(self.eastmoney_password)
        raise ValueError(f"不支持的券商：{broker}")

    def _archive_path(self, broker: str, statement_date: str) -> Path:
        period = statement_date if broker == "phillips" else statement_date[:7]
        return self.data_dir / "statements" / broker / period / "statement.pdf"

    def _latest_statement_period(self, broker: str) -> str:
        runs_dir = self.data_dir / "runs"
        if not runs_dir.exists():
            return ""
        periods: list[str] = []
        for run_dir in sorted(runs_dir.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            for filename in ("extracted_positions.csv", "extracted_cash.csv"):
                path = run_dir / filename
                if not path.exists():
                    continue
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        if row.get("broker", "").strip().lower() != broker:
                            continue
                        match = STATEMENT_PERIOD.match(row.get("statement_id", ""))
                        if match is not None:
                            periods.append(match.group(1))
        return max(periods) if periods else ""


def _promote_archive(source: Path, destination: Path) -> Path | None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".statement.{uuid4().hex}.tmp"
    backup = destination.parent / f".statement.{uuid4().hex}.backup"
    copyfile(source, temporary)
    had_previous = destination.exists()
    try:
        if had_previous:
            destination.rename(backup)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        if had_previous and backup.exists():
            backup.rename(destination)
        raise
    return backup if had_previous else None


def _restore_archive(destination: Path, backup: Path | None) -> None:
    destination.unlink(missing_ok=True)
    if backup is not None and backup.exists():
        backup.rename(destination)


def _trade_fill(trade: StatementTrade, statement_period: str) -> dict[str, object]:
    broker = trade.broker
    account_id = trade.account_alias
    reference = trade.reference
    return {
        "fill_id": f"statement:{broker}:{reference}",
        "order_id": f"statement:{broker}:{reference}",
        "source": "actual",
        "source_id": f"actual:{broker}:{account_id}",
        "broker": broker,
        "account_id": account_id,
        "market": str(trade.market),
        "symbol": trade.symbol,
        "currency": trade.currency,
        "side": trade.side,
        "quantity": str(trade.quantity),
        "price": str(trade.price),
        "fee": str(trade.fee),
        "costs_complete": trade.costs_complete,
        "filled_at": trade.traded_at,
        "execution_granularity": trade.execution_granularity,
        "timestamp_semantics": "market_close_ordering_sentinel",
        "statement_sequence": trade.statement_sequence,
        "statement_period": statement_period,
        "strategy_id": "",
        "strategy_version": "",
        "report_sha256": "",
        "attribution_status": "outside_strategy",
        "exclusion_reason": "no_matching_opening_strategy_action",
    }


def _statement_cutoff(statement_date: str, broker: str) -> str:
    timezone = ZoneInfo(
        "Asia/Shanghai" if broker == "eastmoney" else "Asia/Hong_Kong"
    )
    return datetime.combine(
        date.fromisoformat(statement_date), time(23, 59, 59), timezone
    ).isoformat()


def _snapshot_path(
    path: Path, backup_root: Path, label: str
) -> tuple[Path, Path | None, bool]:
    if not path.exists():
        return path, None, False
    backup = backup_root / label
    backup.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        copytree(path, backup)
        return path, backup, True
    copyfile(path, backup)
    return path, backup, False


def _restore_snapshot(snapshot: tuple[Path, Path | None, bool]) -> None:
    path, backup, is_directory = snapshot
    if path.is_dir():
        rmtree(path)
    else:
        path.unlink(missing_ok=True)
    if backup is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_directory:
        copytree(backup, path)
    else:
        copyfile(backup, path)
