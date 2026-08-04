from __future__ import annotations

import csv
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Lock
from zoneinfo import ZoneInfo

from .account_sync_state import (
    BrokerAccountCandidate,
    STATEMENT_BROKERS,
    load_latest_statement_candidate,
    statement_generation_digest,
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
        # ponytail: uploads are rare; split per broker only if this lock contends.
        self._stage_lock = Lock()

    def stage_pdf(self, broker: str, body: bytes) -> dict[str, object]:
        if not body.startswith(b"%PDF-"):
            raise ValueError("请求正文必须是有效的 PDF")
        if len(body) > MAX_STATEMENT_BYTES:
            raise ValueError("PDF 不能超过 20 MiB")
        with self._stage_lock:
            return self._stage_pdf(broker, body)

    def _stage_pdf(self, broker: str, body: bytes) -> dict[str, object]:
        parser = self._parser(broker)
        content_sha256 = _content_sha256(body)
        generations = self.data_dir / "account_statements/generations" / broker
        existing = _staged_for_content(generations, broker, content_sha256)
        if existing is not None:
            return existing

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
            facts_bytes = json.dumps(
                facts, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            (root / "trade_facts.json").write_bytes(facts_bytes)
            staged_at = datetime.now().astimezone()
            candidate_sha256 = _directory_sha256(root / "candidate")
            trade_facts_sha256 = _content_sha256(facts_bytes)
            manifest = {
                "schema_version": STATEMENT_GENERATION_SCHEMA,
                "status": "staged",
                "broker": broker,
                "statement_date": statement_date,
                "statement_period": statement_period,
                "content_sha256": content_sha256,
                "candidate_sha256": candidate_sha256,
                "trade_facts_sha256": trade_facts_sha256,
                "staged_at": staged_at.isoformat(timespec="seconds"),
                "trade_facts_cutoff_at": _statement_cutoff(
                    statement_date, broker, staged_at, facts
                ),
                "positions": len(parsed.positions),
                "cash": len(parsed.cash_balances),
                "warnings": len(parsed.warnings),
                "trades": len(parsed.trades),
                "candidate_run": str(imported.run_dir.relative_to(root)),
            }
            generation = _statement_generation(manifest)
            manifest["statement_generation"] = generation
            (root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            destination = generations / generation.removeprefix("sha256:")
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
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _content_sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _directory_sha256(root: Path) -> str:
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    return _content_sha256(
        json.dumps(files, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _statement_generation(manifest: dict[str, object]) -> str:
    return _content_sha256(
        json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _staged_for_content(
    generations: Path, broker: str, content_sha256: str
) -> dict[str, object] | None:
    if not generations.is_dir():
        return None
    # ponytail: generation counts are tiny; add an index only if this scan grows.
    for root in generations.iterdir():
        if not root.is_dir() or root.name.startswith(".stage-"):
            continue
        generation = f"sha256:{root.name}"
        manifest, _candidate, _facts = _load_statement_generation(
            root, broker, generation
        )
        if manifest.get("content_sha256") == content_sha256:
            return dict(manifest)
    return None


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
    if broker not in STATEMENT_BROKERS:
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
    if broker not in STATEMENT_BROKERS:
        raise ValueError(f"unsupported statement broker: {broker}")
    digest = statement_generation_digest(generation)
    if digest is None:
        raise ValueError("invalid statement generation")
    root = (
        data_dir
        / "account_statements/generations"
        / broker
        / digest
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
    content_sha256 = manifest.get("content_sha256") if manifest else None
    candidate_sha256 = manifest.get("candidate_sha256") if manifest else None
    trade_facts_sha256 = manifest.get("trade_facts_sha256") if manifest else None
    unsigned_manifest = dict(manifest or {})
    unsigned_manifest.pop("statement_generation", None)
    if (
        not root.is_dir()
        or manifest is None
        or manifest.get("schema_version") != STATEMENT_GENERATION_SCHEMA
        or manifest.get("broker") != broker
        or manifest.get("statement_generation") != generation
        or statement_generation_digest(content_sha256) is None
        or statement_generation_digest(candidate_sha256) is None
        or statement_generation_digest(trade_facts_sha256) is None
        or statement_generation_digest(generation) != root.name
        or _statement_generation(unsigned_manifest) != generation
    ):
        raise ValueError(f"invalid statement generation: {root.name}")
    try:
        pdf = (root / "statement.pdf").read_bytes()
        facts_bytes = (root / "trade_facts.json").read_bytes()
        raw_facts = json.loads(facts_bytes)
        observed_candidate_sha256 = _directory_sha256(root / "candidate")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid statement generation: {root.name}") from error
    if (
        _content_sha256(pdf) != content_sha256
        or _content_sha256(facts_bytes) != trade_facts_sha256
        or observed_candidate_sha256 != candidate_sha256
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


def _statement_cutoff(
    statement_date: str,
    broker: str,
    staged_at: datetime,
    facts: list[dict[str, object]],
) -> str:
    timezone = ZoneInfo(
        "Asia/Shanghai" if broker == "eastmoney" else "Asia/Hong_Kong"
    )
    end_of_day = datetime.combine(
        date.fromisoformat(statement_date), time(23, 59, 59), timezone
    )
    cutoff = min(end_of_day, staged_at.astimezone(timezone))
    if facts:
        fill_times = [
            datetime.fromisoformat(str(fact["filled_at"])) for fact in facts
        ]
        if any(value.tzinfo is None or value.utcoffset() is None for value in fill_times):
            raise ValueError("结单成交时间必须包含时区")
        latest_fill = max(value.astimezone(timezone) for value in fill_times)
        if latest_fill > end_of_day:
            raise ValueError("结单成交时间晚于结单日期")
        cutoff = max(cutoff, latest_fill)
    return cutoff.isoformat()
