from __future__ import annotations

import csv
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from .account_sync_state import (
    BrokerAccountCandidate,
    load_latest_statement_candidate,
)
from .fx import StaticMonthEndFxProvider
from .models import StatementTrade
from .parsers.base import StatementParser
from .parsers.eastmoney import EastmoneyStatementParser
from .parsers.phillips import PhillipsStatementParser
from .pipeline import run_uploaded_statement


RATES_TO_HKD = {
    "phillips": {"USD": Decimal("7.8"), "CNY": Decimal("1.08")},
    "eastmoney": {"USD": Decimal("7.8"), "CNY": Decimal("1.08")},
}
STATEMENT_PERIOD = re.compile(r"^(\d{4}-\d{2}(?:-\d{2})?)-")
MAX_STATEMENT_BYTES = 20 * 1024 * 1024
STATEMENT_GENERATION_SCHEMA = "open_trader.account.statement_generation.v1"


class StatementImportService:
    def __init__(
        self,
        *,
        data_dir: Path,
        eastmoney_password: str,
    ) -> None:
        self.data_dir = data_dir
        self.eastmoney_password = eastmoney_password

    def stage_pdf(self, broker: str, body: bytes) -> dict[str, object]:
        if not body.startswith(b"%PDF-"):
            raise ValueError("请求正文必须是有效的 PDF")
        if len(body) > MAX_STATEMENT_BYTES:
            raise ValueError("PDF 不能超过 20 MiB")
        parser = self._parser(broker)
        digest = hashlib.sha256(body).hexdigest()
        generation = f"sha256:{digest}"
        generations = self.data_dir / "account_statements/generations" / broker
        destination = generations / digest
        if destination.is_dir():
            return _staged_response(destination, broker, generation)

        generations.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".stage-", dir=generations) as name:
            root = Path(name)
            uploaded = root / "statement.pdf"
            uploaded.write_bytes(body)
            try:
                statement_date = parser.statement_date(uploaded)  # type: ignore[attr-defined]
                parsed = parser.parse(uploaded, statement_date)
            except ValueError:
                raise
            except Exception as error:
                label = {"phillips": "辉立", "eastmoney": "东方财富"}[broker]
                raise ValueError(f"{label}结单无法解析") from error
            if not parsed.positions and not parsed.cash_balances:
                raise ValueError(f"{broker} 结单没有可导入的持仓或现金")
            statement_period = (
                statement_date if broker == "phillips" else statement_date[:7]
            )
            current_period = max(
                self._latest_statement_period(broker),
                self._latest_staged_period(broker),
            )
            if current_period and statement_period < current_period:
                raise ValueError(
                    f"{statement_period} 早于当前结单 {current_period}，拒绝导入"
                )
            imported = run_uploaded_statement(
                statement_date=statement_date,
                statement_path=uploaded,
                parser=parser,
                data_dir=root / "candidate",
                fx_provider=StaticMonthEndFxProvider(
                    statement_date[:7],
                    RATES_TO_HKD[broker],
                    fx_date=statement_date,
                ),
            )
            facts = [_trade_fill(trade, statement_period) for trade in parsed.trades]
            (root / "trade_facts.json").write_text(
                json.dumps(facts, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            manifest = {
                "schema_version": STATEMENT_GENERATION_SCHEMA,
                "status": "staged",
                "broker": broker,
                "statement_date": statement_date,
                "statement_period": statement_period,
                "statement_generation": generation,
                "content_sha256": generation,
                "staged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "trade_facts_cutoff_at": _statement_cutoff(
                    statement_date, broker
                ),
                "positions": len(parsed.positions),
                "cash": len(parsed.cash_balances),
                "warnings": len(parsed.warnings),
                "trades": len(parsed.trades),
                "candidate_run": str(imported.run_dir.relative_to(root)),
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                root.rename(destination)
            except FileExistsError:
                pass
        return _staged_response(destination, broker, generation)

    def _parser(self, broker: str) -> StatementParser:
        if broker == "phillips":
            return PhillipsStatementParser()
        if broker == "eastmoney":
            if not self.eastmoney_password:
                raise ValueError("未配置东方财富对账单密码")
            return EastmoneyStatementParser(self.eastmoney_password)
        raise ValueError(f"不支持的券商：{broker}")

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
                        value = row.get("statement_id", "")
                        match = STATEMENT_PERIOD.match(f"{value}-")
                        if match is not None:
                            periods.append(match.group(1))
        return max(periods) if periods else ""

    def _latest_staged_period(self, broker: str) -> str:
        staged = load_staged_statement_candidate(self.data_dir, broker)
        return staged[0].period if staged is not None else ""


def _read_manifest(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _staged_response(
    root: Path, broker: str, generation: str
) -> dict[str, object]:
    try:
        manifest, _candidate, _facts = _load_statement_generation(
            root, broker, generation
        )
    except ValueError as error:
        raise ValueError("invalid statement generation") from error
    return dict(manifest)


def load_staged_statement_candidate(
    data_dir: Path, broker: str
) -> tuple[BrokerAccountCandidate, str] | None:
    if broker not in {"phillips", "eastmoney"}:
        raise ValueError(f"unsupported statement broker: {broker}")
    generations = data_dir / "account_statements/generations" / broker
    if not generations.is_dir():
        return None
    candidates: list[tuple[str, str, int, BrokerAccountCandidate, str]] = []
    for root in generations.iterdir():
        if not root.is_dir() or root.name.startswith(".stage-"):
            continue
        generation = f"sha256:{root.name}"
        manifest, candidate, _facts = _load_statement_generation(
            root, broker, generation
        )
        period = manifest.get("statement_period")
        staged_at = manifest.get("staged_at")
        if not isinstance(period, str) or not isinstance(staged_at, str):
            raise ValueError(f"invalid statement generation: {root.name}")
        candidates.append(
            (period, staged_at, root.stat().st_mtime_ns, candidate, generation)
        )
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: item[:3])
    return selected[3], selected[4]


def load_statement_trade_facts(
    data_dir: Path, broker: str, generation: str
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if broker not in {"phillips", "eastmoney"}:
        raise ValueError(f"unsupported statement broker: {broker}")
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", generation)
    if match is None:
        raise ValueError("invalid statement generation")
    root = (
        data_dir
        / "account_statements/generations"
        / broker
        / match.group(1)
    )
    manifest, _candidate, facts = _load_statement_generation(
        root, broker, generation
    )
    return manifest, facts


def _load_statement_generation(
    root: Path, broker: str, generation: str
) -> tuple[
    dict[str, object],
    BrokerAccountCandidate,
    list[dict[str, object]],
]:
    manifest = _read_manifest(root / "manifest.json")
    if (
        not root.is_dir()
        or manifest is None
        or manifest.get("schema_version") != STATEMENT_GENERATION_SCHEMA
        or manifest.get("broker") != broker
        or manifest.get("statement_generation") != generation
        or manifest.get("content_sha256") != generation
        or generation != f"sha256:{root.name}"
    ):
        raise ValueError(f"invalid statement generation: {root.name}")
    try:
        pdf = (root / "statement.pdf").read_bytes()
        raw_facts = json.loads(
            (root / "trade_facts.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid statement generation: {root.name}") from error
    if (
        hashlib.sha256(pdf).hexdigest() != root.name
        or not isinstance(raw_facts, list)
        or any(not isinstance(fact, dict) for fact in raw_facts)
    ):
        raise ValueError(f"invalid statement generation: {root.name}")
    candidate = load_latest_statement_candidate(root / "candidate", broker)
    if candidate is None or candidate.period != manifest.get("statement_period"):
        raise ValueError(f"invalid statement generation: {root.name}")
    return manifest, candidate, [dict(fact) for fact in raw_facts]


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
