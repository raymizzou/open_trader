from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from os import close
from pathlib import Path
from shutil import copyfile, rmtree
from tempfile import mkdtemp, mkstemp
from typing import Iterable, Mapping

from .csv_io import write_rows
from .fx import StaticMonthEndFxProvider
from .models import CashBalance, ManifestRecord, Position, WarningRecord
from .parsers.base import ParseResult, StatementParser, sha256_file
from .portfolio import PORTFOLIO_FIELDNAMES, build_portfolio_rows


MANIFEST_FIELDNAMES = [
    "month",
    "broker",
    "source_file",
    "source_sha256",
    "parsed_at",
    "page_count",
    "parser_version",
    "status",
]

POSITION_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "quantity",
    "cost_price",
    "last_price",
    "market_value",
    "cost_value",
    "unrealized_pnl",
    "confidence",
    "notes",
]

CASH_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "currency",
    "cash_balance",
    "available_balance",
    "confidence",
    "notes",
]

WARNING_FIELDNAMES = [
    "statement_id",
    "broker",
    "page",
    "severity",
    "code",
    "message",
]


@dataclass(frozen=True)
class ImportResult:
    run_dir: Path
    portfolio_path: Path
    latest_path: Path
    positions_count: int
    cash_count: int
    warnings_count: int


def run_import(
    month: str,
    statement_paths: Mapping[str, Path],
    parsers: Iterable[StatementParser],
    data_dir: Path,
    fx_provider: StaticMonthEndFxProvider,
) -> ImportResult:
    parser_list = list(parsers)
    _validate_statement_paths(statement_paths, parser_list)

    run_dir = data_dir / "runs" / month
    latest_dir = data_dir / "latest"

    positions: list[Position] = []
    cash_balances: list[CashBalance] = []
    warnings: list[WarningRecord] = []
    manifest: list[ManifestRecord] = []

    for parser in parser_list:
        source_path = statement_paths[parser.broker]
        parsed_at = datetime.now(UTC).isoformat()
        parse_result = parser.parse(source_path, month)
        _validate_parse_result_brokers(parser.broker, parse_result)

        positions.extend(parse_result.positions)
        cash_balances.extend(parse_result.cash_balances)
        warnings.extend(parse_result.warnings)
        manifest.append(
            ManifestRecord(
                month=month,
                broker=parse_result.broker,
                source_file=str(source_path),
                source_sha256=sha256_file(source_path),
                parsed_at=parsed_at,
                page_count=parse_result.page_count,
                parser_version=parser.parser_version,
                status="parsed",
            )
        )

    portfolio_rows = build_portfolio_rows(month, positions, cash_balances, fx_provider)

    temp_run_dir = _make_temp_run_dir(run_dir)
    latest_dir.mkdir(parents=True, exist_ok=True)
    latest_path = latest_dir / "portfolio.csv"
    temp_latest_path = _make_temp_latest_path(latest_path)
    backup_run_dir: Path | None = None
    temp_run_promoted = False
    try:
        write_rows(
            temp_run_dir / "manifest.csv",
            MANIFEST_FIELDNAMES,
            (_manifest_to_row(record) for record in manifest),
        )
        write_rows(
            temp_run_dir / "extracted_positions.csv",
            POSITION_FIELDNAMES,
            (_position_to_row(position) for position in positions),
        )
        write_rows(
            temp_run_dir / "extracted_cash.csv",
            CASH_FIELDNAMES,
            (_cash_to_row(cash) for cash in cash_balances),
        )
        write_rows(
            temp_run_dir / "parse_warnings.csv",
            WARNING_FIELDNAMES,
            (warning.to_row() for warning in warnings),
        )
        write_rows(temp_run_dir / "portfolio.csv", PORTFOLIO_FIELDNAMES, portfolio_rows)

        copyfile(temp_run_dir / "portfolio.csv", temp_latest_path)
        if run_dir.exists():
            backup_run_dir = _make_backup_run_dir(run_dir)
            run_dir.rename(backup_run_dir)
        temp_run_dir.rename(run_dir)
        temp_run_promoted = True
        temp_latest_path.replace(latest_path)
        if backup_run_dir is not None and backup_run_dir.exists():
            rmtree(backup_run_dir)
    except Exception:
        if temp_run_promoted and run_dir.exists():
            rmtree(run_dir)
        if backup_run_dir is not None and backup_run_dir.exists():
            backup_run_dir.rename(run_dir)
        if temp_run_dir.exists():
            rmtree(temp_run_dir)
        if temp_latest_path.exists():
            temp_latest_path.unlink()
        raise

    portfolio_path = run_dir / "portfolio.csv"

    return ImportResult(
        run_dir=run_dir,
        portfolio_path=portfolio_path,
        latest_path=latest_path,
        positions_count=len(positions),
        cash_count=len(cash_balances),
        warnings_count=len(warnings),
    )


def _make_backup_run_dir(run_dir: Path) -> Path:
    backup_dir = Path(
        mkdtemp(
            prefix=f".{run_dir.name}.",
            suffix=".backup",
            dir=run_dir.parent,
        )
    )
    rmtree(backup_dir)
    return backup_dir


def _make_temp_latest_path(latest_path: Path) -> Path:
    file_descriptor, name = mkstemp(
        prefix=".portfolio.",
        suffix=".tmp",
        dir=latest_path.parent,
    )
    close(file_descriptor)
    return Path(name)


def _make_temp_run_dir(run_dir: Path) -> Path:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        mkdtemp(
            prefix=f".{run_dir.name}.",
            suffix=".tmp",
            dir=run_dir.parent,
        )
    )


def _validate_statement_paths(
    statement_paths: Mapping[str, Path],
    parsers: list[StatementParser],
) -> None:
    parser_brokers = {parser.broker for parser in parsers}
    path_brokers = set(statement_paths)
    missing = sorted(parser_brokers - path_brokers)
    if missing:
        raise ValueError(f"missing statement path for broker(s): {', '.join(missing)}")

    unknown = sorted(path_brokers - parser_brokers)
    if unknown:
        raise ValueError(f"unknown statement path broker(s): {', '.join(unknown)}")


def _validate_parse_result_brokers(
    expected_broker: str,
    parse_result: ParseResult,
) -> None:
    result_broker = parse_result.broker
    if result_broker != expected_broker:
        raise ValueError(
            f"parser broker {expected_broker} returned result broker {result_broker}"
        )

    for collection_name in ("positions", "cash_balances", "warnings"):
        for record in getattr(parse_result, collection_name):
            if record.broker != expected_broker:
                raise ValueError(
                    f"parser broker {expected_broker} emitted {collection_name} "
                    f"record for broker {record.broker}"
                )


def _manifest_to_row(record: ManifestRecord) -> dict[str, str]:
    return {
        "month": record.month,
        "broker": record.broker,
        "source_file": record.source_file,
        "source_sha256": record.source_sha256,
        "parsed_at": record.parsed_at,
        "page_count": str(record.page_count),
        "parser_version": record.parser_version,
        "status": record.status,
    }


def _position_to_row(position: Position) -> dict[str, str]:
    return {
        "statement_id": position.statement_id,
        "broker": position.broker,
        "account_alias": position.account_alias,
        "market": position.market.value,
        "asset_class": position.asset_class.value,
        "symbol": position.symbol,
        "name": position.name,
        "currency": position.currency,
        "quantity": _decimal_to_str(position.quantity),
        "cost_price": _decimal_to_str(position.cost_price),
        "last_price": _decimal_to_str(position.last_price),
        "market_value": _decimal_to_str(position.market_value),
        "cost_value": _decimal_to_str(position.cost_value),
        "unrealized_pnl": _decimal_to_str(position.unrealized_pnl),
        "confidence": position.confidence,
        "notes": position.notes,
    }


def _cash_to_row(cash: CashBalance) -> dict[str, str]:
    return {
        "statement_id": cash.statement_id,
        "broker": cash.broker,
        "account_alias": cash.account_alias,
        "currency": cash.currency,
        "cash_balance": _decimal_to_str(cash.cash_balance),
        "available_balance": _decimal_to_str(cash.available_balance),
        "confidence": cash.confidence,
        "notes": cash.notes,
    }


def _decimal_to_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")
