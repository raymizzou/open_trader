from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
import re
from shutil import copyfile
from tempfile import TemporaryDirectory
from uuid import uuid4

from .fx import StaticMonthEndFxProvider
from .parsers.base import StatementParser
from .parsers.eastmoney import EastmoneyStatementParser
from .parsers.phillips import PhillipsStatementParser
from .pipeline import run_uploaded_statement


RATES_TO_HKD = {
    "phillips": {"USD": Decimal("7.8")},
    "eastmoney": {"CNY": Decimal("1.08")},
}
STATEMENT_PERIOD = re.compile(r"^(\d{4}-\d{2}(?:-\d{2})?)-")


class StatementImportService:
    def __init__(
        self,
        *,
        data_dir: Path,
        portfolio_path: Path,
        eastmoney_password: str,
    ) -> None:
        self.data_dir = data_dir
        self.portfolio_path = portfolio_path
        self.eastmoney_password = eastmoney_password

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
            archive = self._archive_path(broker, statement_date)
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
            except Exception:
                _restore_archive(archive, backup)
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
                            return match.group(1)
        return ""


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
