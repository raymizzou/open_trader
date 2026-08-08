from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .account_http import (
    AccountHttpError,
    DEFAULT_ACCOUNT_API_URL,
    DEFAULT_ACCOUNT_TIMEOUT_SECONDS,
    fetch_account_snapshot,
    fetch_statement_trade_facts,
)
from .account_sync_state import STATEMENT_BROKERS
from .daily_premarket import RunLock
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
    account_url: str = DEFAULT_ACCOUNT_API_URL,
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
            account_url=account_url,
        )


def _consume_accepted_statement_facts(
    *,
    data_dir: Path,
    reports_dir: Path,
    broker: str,
    generated_at: str | None,
    account_url: str,
) -> dict[str, object]:
    for attempt_index in range(2):
        try:
            snapshot = fetch_account_snapshot(
                account_url, DEFAULT_ACCOUNT_TIMEOUT_SECONDS
            )
        except AccountHttpError as error:
            return _blocked_status({}, broker, "", error.code, generated_at)
        generations = snapshot["accepted_statement_generation"]
        statement_generation = str(generations.get(broker) or "")
        if not statement_generation:
            return _waiting_for_promotion_status(snapshot, broker)
        try:
            facts_payload = fetch_statement_trade_facts(
                account_url,
                broker,
                statement_generation,
                DEFAULT_ACCOUNT_TIMEOUT_SECONDS,
            )
        except AccountHttpError as error:
            if (
                error.code == "accepted_statement_generation_changed"
                and attempt_index == 0
            ):
                continue
            return _blocked_status(
                snapshot, broker, statement_generation, error.code, generated_at
            )
        return _consume_facts_payload(
            snapshot=snapshot,
            facts_payload=facts_payload,
            data_dir=data_dir,
            reports_dir=reports_dir,
            broker=broker,
            generated_at=generated_at,
        )
    raise AssertionError("statement consumption retry loop did not return")


def _consume_facts_payload(
    *,
    snapshot: dict[str, object],
    facts_payload: dict[str, object],
    data_dir: Path,
    reports_dir: Path,
    broker: str,
    generated_at: str | None,
) -> dict[str, object]:
    statement_generation = str(facts_payload.get("statement_generation") or "")
    account_generation = str(snapshot.get("account_generation") or "")
    snapshot_generation = str(snapshot.get("snapshot_generation") or "")
    status_path = data_dir / "trend_statement_consumption" / f"{broker}.json"
    previous = _read_json(status_path)
    if (
        previous.get("status") == "consumed"
        and previous.get("statement_generation") == statement_generation
        and previous.get("account_generation") == account_generation
    ):
        return {
            **previous,
            "status": "already_consumed",
            "snapshot_generation": snapshot_generation,
        }

    consumed_at = generated_at or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    try:
        statement_period = facts_payload.get("statement_period")
        cutoff = facts_payload.get("trade_facts_cutoff_at")
        facts = facts_payload.get("facts")
        if not isinstance(statement_period, str) or not isinstance(cutoff, str):
            raise ValueError("statement trade facts contract is invalid")
        if not isinstance(facts, list) or any(
            not isinstance(fact, dict) for fact in facts
        ):
            raise ValueError("statement trade facts contract is invalid")
        if datetime.fromisoformat(consumed_at) < datetime.fromisoformat(cutoff):
            waiting = {
                "schema_version": CONSUMPTION_SCHEMA,
                "status": "waiting_for_statement_cutoff",
                "broker": broker,
                "snapshot_generation": snapshot_generation,
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
    except Exception:
        failed = {
            "schema_version": CONSUMPTION_SCHEMA,
            "status": "failed",
            "broker": broker,
            "statement_generation": statement_generation,
            "account_generation": account_generation,
            "attempted_at": consumed_at,
            "reason": "statement_facts_processing_failed",
        }
        _write_json_atomic(status_path, failed)
        return failed
    status = {
        "schema_version": CONSUMPTION_SCHEMA,
        "status": "consumed",
        "broker": broker,
        "snapshot_generation": snapshot_generation,
        "statement_generation": statement_generation,
        "account_generation": account_generation,
        "consumed_at": consumed_at,
        "statistics_cutoff_at": cutoff,
    }
    _write_json_atomic(status_path, status)
    return status


def _waiting_for_promotion_status(
    snapshot: dict[str, object], broker: str
) -> dict[str, object]:
    return {
        "schema_version": CONSUMPTION_SCHEMA,
        "status": "waiting_for_promotion",
        "broker": broker,
        "snapshot_generation": str(snapshot.get("snapshot_generation") or ""),
        "statement_generation": "",
        "account_generation": str(snapshot.get("account_generation") or ""),
    }


def _blocked_status(
    snapshot: dict[str, object],
    broker: str,
    statement_generation: str,
    reason: str,
    generated_at: str | None,
) -> dict[str, object]:
    status = {
        "schema_version": CONSUMPTION_SCHEMA,
        "status": "blocked",
        "broker": broker,
        "snapshot_generation": str(snapshot.get("snapshot_generation") or ""),
        "statement_generation": statement_generation,
        "account_generation": str(snapshot.get("account_generation") or ""),
        "reason": reason,
    }
    if generated_at is not None:
        status["attempted_at"] = generated_at
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
