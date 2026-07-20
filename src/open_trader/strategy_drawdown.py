from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from tempfile import NamedTemporaryFile


STATE_SCHEMA_VERSION = "open_trader.strategy_drawdown_state.v1"
SNAPSHOT_SCHEMA_VERSION = "open_trader.strategy_drawdown_snapshot.v1"
DECISION_SCHEMA_VERSION = "open_trader.strategy_drawdown.v1"
DRAWDOWN_LIMIT = Decimal("0.05")
DECISION_FIELDS = {
    "schema_version",
    "market",
    "strategy_id",
    "strategy_version",
    "kelly_sample_key",
    "state_status",
    "status",
    "status_label",
    "entry_allowed",
    "current_equity",
    "high_water_mark",
    "drawdown_pct",
    "drawdown_limit_pct",
    "pause_reason",
    "paused_at",
    "observed_at",
    "bootstrap_event",
    "recovery_event",
}


def valid_drawdown_decision(
    value: object,
    *,
    expected_market: str,
    expected_strategy_id: str,
    expected_strategy_version: str,
    expected_equity: object,
    expected_entry_date: str | None = None,
) -> bool:
    if not isinstance(value, Mapping) or set(value) != DECISION_FIELDS:
        return False
    try:
        key = _strategy_key(
            expected_market, expected_strategy_id, expected_strategy_version
        )
        current_equity = _positive_decimal(value["current_equity"], "current_equity")
        expected = _positive_decimal(expected_equity, "expected_equity")
    except (KeyError, ValueError):
        return False
    if (
        value.get("schema_version") != DECISION_SCHEMA_VERSION
        or _record_key(value) != key
        or value.get("kelly_sample_key") != "|".join(key)
        or value.get("drawdown_limit_pct") != str(DRAWDOWN_LIMIT)
        or current_equity != expected
        or not _is_canonical_timestamp(value.get("observed_at"))
    ):
        return False
    state_status = value.get("state_status")
    if state_status in {"missing", "corrupt"}:
        expected_reason = {
            "missing": "策略累计回撤状态缺失，暂停新开仓",
            "corrupt": "策略累计回撤状态损坏，暂停新开仓",
        }[state_status]
        return (
            value.get("status") == "paused"
            and value.get("status_label") == "暂停新开仓"
            and value.get("entry_allowed") is False
            and value.get("high_water_mark") is None
            and value.get("drawdown_pct") is None
            and value.get("pause_reason") == expected_reason
            and value.get("paused_at") is None
            and value.get("bootstrap_event") is None
            and value.get("recovery_event") is None
        )
    if state_status != "ok":
        return False
    bootstrap_event = value.get("bootstrap_event")
    if bootstrap_event is not None and (
        not _valid_automatic_bootstrap_event(bootstrap_event)
        or _record_key(bootstrap_event) != key
    ):
        return False
    recovery_event = value.get("recovery_event")
    if recovery_event is not None and (
        not _valid_recovery_event(recovery_event)
        or _record_key(recovery_event) != key
    ):
        return False
    pending_until = (
        str(bootstrap_event.get("entry_eligible_from"))
        if isinstance(bootstrap_event, dict)
        and expected_entry_date is not None
        and expected_entry_date < str(bootstrap_event.get("entry_eligible_from"))
        else None
    )
    try:
        high_water_mark = _positive_decimal(
            value["high_water_mark"], "high_water_mark"
        )
        drawdown = Decimal(str(value["drawdown_pct"]))
    except (InvalidOperation, KeyError, ValueError):
        return False
    if not drawdown.is_finite() or drawdown != _drawdown(
        high_water_mark, current_equity
    ):
        return False
    if value.get("entry_allowed") is True:
        return (
            value.get("status") == "active"
            and value.get("status_label") == "纪律内"
            and value.get("pause_reason") == ""
            and value.get("paused_at") is None
            and drawdown < DRAWDOWN_LIMIT
            and current_equity <= high_water_mark
        )
    if pending_until is not None:
        return (
            value.get("status") == "pending"
            and value.get("status_label") == "等待下一交易日"
            and value.get("pause_reason")
            == f"回撤基准将在 {pending_until} 起允许新开仓"
            and value.get("paused_at") is None
            and drawdown < DRAWDOWN_LIMIT
        )
    return (
        value.get("entry_allowed") is False
        and value.get("status") == "paused"
        and value.get("status_label") == "暂停新开仓"
        and value.get("pause_reason") == "策略累计回撤已达到 5%，需人工解锁"
        and _is_canonical_timestamp(value.get("paused_at"))
    )


def observe_strategy_equity(
    data_dir: Path,
    *,
    market: str,
    strategy_id: str,
    strategy_version: str,
    current_equity: Decimal,
    observed_at: str,
    entry_date: str | None = None,
) -> dict[str, object]:
    key = _strategy_key(market, strategy_id, strategy_version)
    equity = _positive_decimal(current_equity, "current_equity")
    _canonical_timestamp(observed_at, "observed_at")
    if entry_date is not None:
        _canonical_date(entry_date, "entry_date")
    with _state_lock(data_dir):
        return _observe_strategy_equity_locked(
            data_dir,
            key=key,
            equity=equity,
            observed_at=observed_at,
            entry_date=entry_date,
        )


def _observe_strategy_equity_locked(
    data_dir: Path,
    *,
    key: tuple[str, str, str],
    equity: Decimal,
    observed_at: str,
    entry_date: str | None,
) -> dict[str, object]:
    path = _state_path(data_dir)
    if not path.exists():
        return _decision(
            key=key,
            current_equity=equity,
            observed_at=observed_at,
            state_status="missing",
            pause_reason="策略累计回撤状态缺失，暂停新开仓",
        )
    try:
        payload = _load_state(path)
    except ValueError:
        return _decision(
            key=key,
            current_equity=equity,
            observed_at=observed_at,
            state_status="corrupt",
            pause_reason="策略累计回撤状态损坏，暂停新开仓",
        )
    records = payload["records"]
    assert isinstance(records, list)
    record = next(
        (_record for _record in records if _record_key(_record) == key), None
    )
    if record is None:
        return _decision(
            key=key,
            current_equity=equity,
            observed_at=observed_at,
            state_status="missing",
            pause_reason="策略累计回撤状态缺失，暂停新开仓",
        )
    else:
        assert isinstance(record, dict)
        high_water_mark = Decimal(str(record["high_water_mark"]))
        was_paused = record["paused"] is True
        if not was_paused:
            high_water_mark = max(high_water_mark, equity)
        drawdown = _drawdown(high_water_mark, equity)
        paused = was_paused or drawdown >= DRAWDOWN_LIMIT
        record.update(
            {
                "high_water_mark": _decimal_text(high_water_mark),
                "current_equity": _decimal_text(equity),
                "drawdown_pct": _decimal_text(drawdown),
                "paused": paused,
                "paused_at": (
                    record["paused_at"]
                    if was_paused
                    else observed_at
                    if paused
                    else None
                ),
                "updated_at": observed_at,
            }
        )
    _write_state(path, payload)
    assert isinstance(record, dict)
    return _decision_from_record(
        record,
        state_status="ok",
        events=payload["audit_events"],
        entry_date=entry_date,
    )


def strategy_parameter_hash(parameters: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(parameters),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("strategy parameters must be canonical JSON") from None
    return hashlib.sha256(encoded).hexdigest()


def automatic_bootstrap_strategy_drawdown(
    data_dir: Path,
    *,
    market: str,
    strategy_id: str,
    strategy_version: str,
    parameters: Mapping[str, object],
    baseline_equity: Decimal,
    source_date: str,
    accepted_git_sha: str,
    actor: str,
    occurred_at: str,
    reason: str,
    entry_eligible_from: str,
    entry_date: str | None = None,
) -> dict[str, object]:
    key = _strategy_key(market, strategy_id, strategy_version)
    equity = _positive_decimal(baseline_equity, "baseline_equity")
    parameter_hash = strategy_parameter_hash(parameters)
    _canonical_date(source_date, "source_date")
    _canonical_date(entry_eligible_from, "entry_eligible_from")
    if entry_date is not None:
        _canonical_date(entry_date, "entry_date")
    _canonical_timestamp(occurred_at, "occurred_at")
    if not _is_sha1(accepted_git_sha):
        raise ValueError("accepted_git_sha must be a full lowercase Git SHA")
    if not actor.strip():
        raise ValueError("actor must be non-empty")
    if reason not in {"first_activation", "new_strategy_version"}:
        raise ValueError("invalid automatic bootstrap reason")
    event_id = "automatic-bootstrap-" + hashlib.sha256(
        "|".join((*key, parameter_hash)).encode("utf-8")
    ).hexdigest()
    with _state_lock(data_dir):
        path = _state_path(data_dir)
        payload = _load_state_for_unlock(path)
        records = payload["records"]
        events = payload["audit_events"]
        assert isinstance(records, list) and isinstance(events, list)
        record = next((item for item in records if _record_key(item) == key), None)
        event = next(
            (
                item
                for item in events
                if isinstance(item, dict)
                and item.get("event_type") == "automatic_bootstrap"
                and _record_key(item) == key
            ),
            None,
        )
        if record is not None:
            if event is None:
                raise ValueError("strategy parameter identity is unavailable")
            if event.get("parameter_hash") != parameter_hash:
                raise ValueError("strategy parameters changed without a version bump")
            assert isinstance(record, dict)
            return _decision_from_record(
                record,
                state_status="ok",
                events=events,
                entry_date=entry_date,
            )
        if event is not None:
            raise ValueError("automatic bootstrap event has no strategy record")
        record = _new_record(key, equity=equity, updated_at=occurred_at)
        event = {
            "event_id": event_id,
            "event_type": "automatic_bootstrap",
            "market": key[0],
            "strategy_id": key[1],
            "strategy_version": key[2],
            "actor": actor.strip(),
            "occurred_at": occurred_at,
            "baseline_equity": _decimal_text(equity),
            "source_date": source_date,
            "accepted_git_sha": accepted_git_sha,
            "parameter_hash": parameter_hash,
            "reason": reason,
            "entry_eligible_from": entry_eligible_from,
        }
        records.append(record)
        records.sort(key=lambda item: _record_key(item))
        events.append(event)
        _write_state(path, payload)
        return _decision_from_record(
            record,
            state_status="ok",
            events=events,
            entry_date=entry_date,
        )


def manual_unlock_strategy_drawdown(
    data_dir: Path,
    *,
    market: str,
    strategy_id: str,
    strategy_version: str,
    current_equity: Decimal,
    occurred_at: str,
    event_id: str,
    actor: str,
) -> dict[str, object]:
    key = _strategy_key(market, strategy_id, strategy_version)
    equity = _positive_decimal(current_equity, "current_equity")
    _canonical_timestamp(occurred_at, "occurred_at")
    for field, value in (("event_id", event_id), ("actor", actor)):
        if not value.strip():
            raise ValueError(f"{field} must be non-empty")
    with _state_lock(data_dir):
        return _manual_unlock_strategy_drawdown_locked(
            data_dir,
            key=key,
            equity=equity,
            occurred_at=occurred_at,
            event_id=event_id,
            actor=actor,
        )


def _manual_unlock_strategy_drawdown_locked(
    data_dir: Path,
    *,
    key: tuple[str, str, str],
    equity: Decimal,
    occurred_at: str,
    event_id: str,
    actor: str,
) -> dict[str, object]:
    path = _state_path(data_dir)
    payload = _load_state_for_unlock(path)
    records = payload["records"]
    assert isinstance(records, list)
    record = next(
        (_record for _record in records if _record_key(_record) == key), None
    )
    events = payload["audit_events"]
    assert isinstance(events, list)
    existing_event = next(
        (
            event
            for event in events
            if isinstance(event, dict) and event.get("event_id") == event_id
        ),
        None,
    )
    if existing_event is not None:
        request_matches = all(
            existing_event.get(field) == expected
            for field, expected in {
                "market": key[0],
                "strategy_id": key[1],
                "strategy_version": key[2],
                "actor": actor,
                "rebased_high_water_mark": _decimal_text(equity),
            }.items()
        )
        if not request_matches:
            raise ValueError("strategy drawdown unlock event_id was reused")
        assert isinstance(record, dict)
        return _decision_from_record(record, state_status="ok", events=events)
    if not isinstance(record, dict) or record.get("paused") is not True:
        raise ValueError("manual unlock requires an existing paused strategy record")
    previous_high_water_mark = record["high_water_mark"]
    previous_paused = True
    record.update(
        {
            "high_water_mark": _decimal_text(equity),
            "current_equity": _decimal_text(equity),
            "drawdown_pct": "0",
            "paused": False,
            "paused_at": None,
            "updated_at": occurred_at,
        }
    )
    events.append(
        {
            "event_id": event_id,
            "event_type": "manual_unlock",
            "market": key[0],
            "strategy_id": key[1],
            "strategy_version": key[2],
            "actor": actor,
            "occurred_at": occurred_at,
            "previous_high_water_mark": previous_high_water_mark,
            "previous_paused": previous_paused,
            "rebased_high_water_mark": _decimal_text(equity),
        }
    )
    records.sort(key=lambda item: _record_key(item))
    _write_state(path, payload)
    return _decision_from_record(record, state_status="ok", events=events)


def _state_path(data_dir: Path) -> Path:
    return data_dir / "trend_drawdown" / "state.json"


def strategy_drawdown_state_status(data_dir: Path) -> str:
    path = _state_path(data_dir)
    if not path.exists():
        return "missing"
    try:
        _load_state(path)
    except ValueError:
        return "corrupt"
    return "ok"


def strategy_drawdown_keys(data_dir: Path) -> set[tuple[str, str, str]]:
    path = _state_path(data_dir)
    if not path.exists():
        return set()
    payload = _load_state(path)
    records = payload["records"]
    assert isinstance(records, list)
    return {_record_key(record) for record in records}


def recover_strategy_drawdown_state(
    data_dir: Path, *, actor: str, occurred_at: str
) -> dict[str, object]:
    if not actor.strip():
        raise ValueError("actor must be non-empty")
    _canonical_timestamp(occurred_at, "occurred_at")
    with _state_lock(data_dir):
        path = _state_path(data_dir)
        if path.exists():
            try:
                _load_state(path)
            except ValueError:
                pass
            else:
                raise ValueError("strategy drawdown state is already valid")
        snapshots_dir = path.parent / "snapshots"
        candidates = sorted(
            snapshots_dir.glob("*.json"),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
        for snapshot_path in candidates:
            try:
                envelope = json.loads(snapshot_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(envelope, dict)
                    or set(envelope) != {
                        "schema_version",
                        "state",
                        "state_sha256",
                    }
                    or envelope.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
                    or snapshot_path.stem != envelope.get("state_sha256")
                ):
                    continue
                state = _validate_state(envelope.get("state"))
                digest = hashlib.sha256(_state_bytes(state)).hexdigest()
                if digest != envelope["state_sha256"]:
                    continue
                records = state["records"]
                events = state["audit_events"]
                assert isinstance(records, list) and isinstance(events, list)
                for record in records:
                    key = _record_key(record)
                    event_id = "snapshot-recovery-" + hashlib.sha256(
                        "|".join((*key, digest, occurred_at)).encode("utf-8")
                    ).hexdigest()
                    if any(
                        isinstance(event, dict)
                        and event.get("event_id") == event_id
                        for event in events
                    ):
                        continue
                    events.append({
                        "event_id": event_id,
                        "event_type": "snapshot_recovery",
                        "market": key[0],
                        "strategy_id": key[1],
                        "strategy_version": key[2],
                        "actor": actor.strip(),
                        "occurred_at": occurred_at,
                        "snapshot": snapshot_path.name,
                        "state_sha256": digest,
                    })
                _write_state(path, state)
                return {
                    "status": "recovered",
                    "snapshot": str(snapshot_path),
                    "state_sha256": digest,
                }
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
        raise ValueError("no valid strategy drawdown snapshot is available")


@contextmanager
def _state_lock(data_dir: Path) -> Iterator[None]:
    path = data_dir / "trend_drawdown" / ".state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_state_for_unlock(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "records": [],
            "audit_events": [],
        }
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("strategy drawdown state is unreadable or malformed") from None
    return _validate_state(payload)


def _load_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("strategy drawdown state is unreadable or malformed") from None
    return _validate_state(payload)


def _validate_state(payload: object) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "records", "audit_events"}
        or payload.get("schema_version") != STATE_SCHEMA_VERSION
        or not isinstance(payload.get("records"), list)
        or not isinstance(payload.get("audit_events"), list)
    ):
        raise ValueError("strategy drawdown state has an invalid schema")
    records = payload["records"]
    assert isinstance(records, list)
    keys: set[tuple[str, str, str]] = set()
    for record in records:
        if not _valid_record(record):
            raise ValueError("strategy drawdown state contains an invalid record")
        key = _record_key(record)
        if key in keys:
            raise ValueError("strategy drawdown state contains duplicate records")
        keys.add(key)
    event_ids: set[str] = set()
    events = payload["audit_events"]
    assert isinstance(events, list)
    for event in events:
        if not _valid_audit_event(event):
            raise ValueError("strategy drawdown state contains an invalid audit event")
        assert isinstance(event, dict)
        event_id = str(event["event_id"])
        if event_id in event_ids:
            raise ValueError("strategy drawdown state contains duplicate audit events")
        event_ids.add(event_id)
        if _record_key(event) not in keys:
            raise ValueError("strategy drawdown audit event has no strategy record")
    return payload


def _valid_record(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "market",
        "strategy_id",
        "strategy_version",
        "kelly_sample_key",
        "high_water_mark",
        "current_equity",
        "drawdown_pct",
        "paused",
        "paused_at",
        "updated_at",
    }:
        return False
    try:
        key = _strategy_key(
            str(value["market"]),
            str(value["strategy_id"]),
            str(value["strategy_version"]),
        )
        high_water_mark = _positive_decimal(value["high_water_mark"], "high_water_mark")
        current_equity = _positive_decimal(value["current_equity"], "current_equity")
        drawdown = Decimal(str(value["drawdown_pct"]))
    except (ValueError, InvalidOperation):
        return False
    expected_drawdown = _drawdown(high_water_mark, current_equity)
    return (
        value["kelly_sample_key"] == "|".join(key)
        and drawdown.is_finite()
        and drawdown == expected_drawdown
        and isinstance(value["paused"], bool)
        and (value["paused"] is True or drawdown < DRAWDOWN_LIMIT)
        and (value["paused"] is True or current_equity <= high_water_mark)
        and (
            value["paused_at"] is None
            or isinstance(value["paused_at"], str)
            and _is_canonical_timestamp(value["paused_at"])
        )
        and (value["paused"] is (value["paused_at"] is not None))
        and isinstance(value["updated_at"], str)
        and _is_canonical_timestamp(value["updated_at"])
    )


def _valid_audit_event(value: object) -> bool:
    if isinstance(value, dict) and value.get("event_type") == "automatic_bootstrap":
        return _valid_automatic_bootstrap_event(value)
    if isinstance(value, dict) and value.get("event_type") == "snapshot_recovery":
        return _valid_recovery_event(value)
    if not isinstance(value, dict) or set(value) != {
        "event_id",
        "event_type",
        "market",
        "strategy_id",
        "strategy_version",
        "actor",
        "occurred_at",
        "previous_high_water_mark",
        "previous_paused",
        "rebased_high_water_mark",
    }:
        return False
    try:
        _strategy_key(
            str(value["market"]),
            str(value["strategy_id"]),
            str(value["strategy_version"]),
        )
        rebased = _positive_decimal(
            value["rebased_high_water_mark"], "rebased_high_water_mark"
        )
        previous = value["previous_high_water_mark"]
        if previous is not None:
            _positive_decimal(previous, "previous_high_water_mark")
    except ValueError:
        return False
    return (
        value["event_type"] == "manual_unlock"
        and isinstance(value["event_id"], str)
        and bool(value["event_id"].strip())
        and isinstance(value["actor"], str)
        and bool(value["actor"].strip())
        and isinstance(value["occurred_at"], str)
        and _is_canonical_timestamp(value["occurred_at"])
        and (
            value["previous_paused"] is None
            or isinstance(value["previous_paused"], bool)
        )
        and ((previous is None) is (value["previous_paused"] is None))
        and rebased > 0
    )


def _valid_automatic_bootstrap_event(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "event_id",
        "event_type",
        "market",
        "strategy_id",
        "strategy_version",
        "actor",
        "occurred_at",
        "baseline_equity",
        "source_date",
        "accepted_git_sha",
        "parameter_hash",
        "reason",
        "entry_eligible_from",
    }:
        return False
    try:
        _strategy_key(
            str(value["market"]),
            str(value["strategy_id"]),
            str(value["strategy_version"]),
        )
        _positive_decimal(value["baseline_equity"], "baseline_equity")
        _canonical_date(str(value["source_date"]), "source_date")
        _canonical_date(str(value["entry_eligible_from"]), "entry_eligible_from")
    except ValueError:
        return False
    return (
        value["event_type"] == "automatic_bootstrap"
        and isinstance(value["event_id"], str)
        and value["event_id"].startswith("automatic-bootstrap-")
        and isinstance(value["actor"], str)
        and bool(value["actor"].strip())
        and _is_canonical_timestamp(value["occurred_at"])
        and _is_sha1(value["accepted_git_sha"])
        and _is_sha256(value["parameter_hash"])
        and value["reason"] in {"first_activation", "new_strategy_version"}
    )


def _valid_recovery_event(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "event_id",
        "event_type",
        "market",
        "strategy_id",
        "strategy_version",
        "actor",
        "occurred_at",
        "snapshot",
        "state_sha256",
    }:
        return False
    try:
        _strategy_key(
            str(value["market"]),
            str(value["strategy_id"]),
            str(value["strategy_version"]),
        )
    except ValueError:
        return False
    return (
        value["event_type"] == "snapshot_recovery"
        and isinstance(value["event_id"], str)
        and value["event_id"].startswith("snapshot-recovery-")
        and isinstance(value["actor"], str)
        and bool(value["actor"].strip())
        and _is_canonical_timestamp(value["occurred_at"])
        and isinstance(value["snapshot"], str)
        and value["snapshot"] == f"{value['state_sha256']}.json"
        and _is_sha256(value["state_sha256"])
    )


def _write_state(path: Path, payload: dict[str, object]) -> None:
    _validate_state(payload)
    state_bytes = _state_bytes(payload)
    _replace_json(path, state_bytes)
    _write_snapshot(path.parent / "snapshots", payload, state_bytes)


def _state_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _replace_json(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb", delete=False, dir=path.parent
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _write_snapshot(
    snapshots_dir: Path,
    payload: dict[str, object],
    state_bytes: bytes,
) -> None:
    digest = hashlib.sha256(state_bytes).hexdigest()
    path = snapshots_dir / f"{digest}.json"
    envelope = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "state": payload,
        "state_sha256": digest,
    }
    content = _state_bytes(envelope)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("strategy drawdown snapshot digest collision")
        return
    _replace_json(path, content)


def _strategy_key(
    market: str, strategy_id: str, strategy_version: str
) -> tuple[str, str, str]:
    normalized_market = market.strip().upper()
    if normalized_market not in {"CN", "US", "HK"}:
        raise ValueError(f"unsupported market: {market}")
    if not strategy_id.strip() or not strategy_version.strip():
        raise ValueError("strategy_id and strategy_version must be non-empty")
    return normalized_market, strategy_id.strip(), strategy_version.strip()


def _record_key(record: object) -> tuple[str, str, str]:
    if not isinstance(record, dict):
        return "", "", ""
    return (
        str(record.get("market") or ""),
        str(record.get("strategy_id") or ""),
        str(record.get("strategy_version") or ""),
    )


def _new_record(
    key: tuple[str, str, str], *, equity: Decimal, updated_at: str
) -> dict[str, object]:
    return {
        "market": key[0],
        "strategy_id": key[1],
        "strategy_version": key[2],
        "kelly_sample_key": "|".join(key),
        "high_water_mark": _decimal_text(equity),
        "current_equity": _decimal_text(equity),
        "drawdown_pct": "0",
        "paused": False,
        "paused_at": None,
        "updated_at": updated_at,
    }


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a positive finite decimal") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a positive finite decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _drawdown(high_water_mark: Decimal, current_equity: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return max(
            Decimal("0"),
            (high_water_mark - current_equity) / high_water_mark,
        )


def _canonical_timestamp(value: str, field: str) -> None:
    if not _is_canonical_timestamp(value):
        raise ValueError(f"{field} must be a canonical timezone-aware ISO timestamp")


def _canonical_date(value: str, field: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a canonical ISO date") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical ISO date")


def _is_sha1(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_canonical_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.isoformat() == value
    )


def _decision(
    *,
    key: tuple[str, str, str],
    current_equity: Decimal,
    observed_at: str,
    state_status: str,
    pause_reason: str,
) -> dict[str, object]:
    market, strategy_id, strategy_version = key
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "market": market,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "kelly_sample_key": "|".join(key),
        "state_status": state_status,
        "status": "paused",
        "status_label": "暂停新开仓",
        "entry_allowed": False,
        "current_equity": _decimal_text(current_equity),
        "high_water_mark": None,
        "drawdown_pct": None,
        "drawdown_limit_pct": str(DRAWDOWN_LIMIT),
        "pause_reason": pause_reason,
        "paused_at": None,
        "observed_at": observed_at,
        "bootstrap_event": None,
        "recovery_event": None,
    }


def _decision_from_record(
    record: dict[str, object],
    *,
    state_status: str,
    events: object = (),
    entry_date: str | None = None,
) -> dict[str, object]:
    paused = record.get("paused") is True
    bootstrap_event = next(
        (
            dict(event)
            for event in events
            if isinstance(event, dict)
            and event.get("event_type") == "automatic_bootstrap"
            and _record_key(event) == _record_key(record)
        ),
        None,
    )
    recovery_event = next(
        (
            dict(event)
            for event in reversed(events if isinstance(events, list) else [])
            if isinstance(event, dict)
            and event.get("event_type") == "snapshot_recovery"
            and _record_key(event) == _record_key(record)
        ),
        None,
    )
    pending_until = (
        str(bootstrap_event["entry_eligible_from"])
        if not paused
        and bootstrap_event is not None
        and entry_date is not None
        and entry_date < str(bootstrap_event["entry_eligible_from"])
        else None
    )
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "market": record["market"],
        "strategy_id": record["strategy_id"],
        "strategy_version": record["strategy_version"],
        "kelly_sample_key": record["kelly_sample_key"],
        "state_status": state_status,
        "status": "paused" if paused else "pending" if pending_until else "active",
        "status_label": (
            "暂停新开仓" if paused else "等待下一交易日" if pending_until else "纪律内"
        ),
        "entry_allowed": not paused and pending_until is None,
        "current_equity": record["current_equity"],
        "high_water_mark": record["high_water_mark"],
        "drawdown_pct": record["drawdown_pct"],
        "drawdown_limit_pct": str(DRAWDOWN_LIMIT),
        "pause_reason": (
            "策略累计回撤已达到 5%，需人工解锁"
            if paused
            else f"回撤基准将在 {pending_until} 起允许新开仓"
            if pending_until
            else ""
        ),
        "paused_at": record["paused_at"],
        "observed_at": record["updated_at"],
        "bootstrap_event": bootstrap_event,
        "recovery_event": recovery_event,
    }
