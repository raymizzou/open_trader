from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .account_snapshot import load_account_snapshot, load_worker_git_sha
from .account_sync_state import STATEMENT_BROKERS
from .daily_premarket import RunLock
from .statement_import import load_statement_trade_facts
from .trend_api_stats import (
    build_statement_actual_stats_payload,
    write_trend_api_stats,
)


CONSUMPTION_SCHEMA = "open_trader.trend.statement_consumption.v1"


def consume_accepted_statement_facts(
    *,
    data_dir: Path,
    reports_dir: Path,
    broker: str,
    generated_at: str | None = None,
) -> dict[str, object]:
    if broker not in STATEMENT_BROKERS:
        raise ValueError(f"unsupported statement broker: {broker}")
    with RunLock(
        data_dir / "trend_statement_consumption/.stats.lock", wait=True
    ):
        return _consume_accepted_statement_facts(
            data_dir=data_dir,
            reports_dir=reports_dir,
            broker=broker,
            generated_at=generated_at,
        )


def _consume_accepted_statement_facts(
    *,
    data_dir: Path,
    reports_dir: Path,
    broker: str,
    generated_at: str | None,
) -> dict[str, object]:
    snapshot = load_account_snapshot(
        data_dir,
        api_git_sha=load_worker_git_sha(data_dir),
        now=(
            datetime.fromisoformat(generated_at)
            if generated_at is not None
            else datetime.now().astimezone()
        ),
    )
    generations = snapshot.payload.get("accepted_statement_generation")
    account_generation = str(snapshot.payload.get("account_generation") or "")
    if snapshot.status_code != 200 or not isinstance(generations, dict):
        return {
            "schema_version": CONSUMPTION_SCHEMA,
            "status": "waiting_for_account_publication",
            "broker": broker,
            "statement_generation": "",
            "account_generation": account_generation,
        }
    statement_generation = str(generations[broker])
    if not statement_generation:
        return {
            "schema_version": CONSUMPTION_SCHEMA,
            "status": "waiting_for_promotion",
            "broker": broker,
            "statement_generation": "",
            "account_generation": account_generation,
        }
    status_path = data_dir / "trend_statement_consumption" / f"{broker}.json"
    previous = _read_json(status_path)
    if (
        previous.get("status") == "consumed"
        and previous.get("statement_generation") == statement_generation
        and previous.get("account_generation") == account_generation
    ):
        return {**previous, "status": "already_consumed"}

    consumed_at = generated_at or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    try:
        manifest, facts = load_statement_trade_facts(
            data_dir, broker, statement_generation
        )
        statement_period = manifest.get("statement_period")
        cutoff = manifest.get("trade_facts_cutoff_at")
        if not isinstance(statement_period, str) or not isinstance(cutoff, str):
            raise ValueError("statement trade facts contract is invalid")
        if datetime.fromisoformat(consumed_at) < datetime.fromisoformat(cutoff):
            waiting = {
                "schema_version": CONSUMPTION_SCHEMA,
                "status": "waiting_for_statement_cutoff",
                "broker": broker,
                "statement_generation": statement_generation,
                "account_generation": account_generation,
                "attempted_at": consumed_at,
                "retry_after": cutoff,
            }
            _write_json_atomic(status_path, waiting)
            return waiting
        payload = build_statement_actual_stats_payload(
            data_dir=data_dir,
            reports_dir=reports_dir,
            broker=broker,
            statement_period=statement_period,
            fills=facts,
            generated_at=consumed_at,
            statistics_cutoff_at=cutoff,
        )
        write_trend_api_stats(data_dir, payload)
    except Exception as error:
        failed = {
            "schema_version": CONSUMPTION_SCHEMA,
            "status": "failed",
            "broker": broker,
            "statement_generation": statement_generation,
            "account_generation": account_generation,
            "attempted_at": consumed_at,
            "error_type": type(error).__name__,
        }
        _write_json_atomic(status_path, failed)
        return failed
    status = {
        "schema_version": CONSUMPTION_SCHEMA,
        "status": "consumed",
        "broker": broker,
        "statement_generation": statement_generation,
        "account_generation": account_generation,
        "consumed_at": consumed_at,
        "statistics_cutoff_at": cutoff,
    }
    _write_json_atomic(status_path, status)
    return status


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
