from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from .daily_premarket import RunLock


OpenDCategory = Literal["connectivity", "rate_limit"]
SendFeishu = Callable[[str, str], str | None]
SCHEMA = "open_trader.opend_incident.v1"
CATEGORIES: tuple[OpenDCategory, ...] = ("connectivity", "rate_limit")


class OpenDIncidentStateError(RuntimeError):
    pass


def classify_opend_error(error: BaseException) -> OpenDCategory | None:
    error_type = str(getattr(error, "error_type", "")).lower()
    message = str(error).lower()
    if (
        error_type == "opend_unreachable"
        or getattr(error, "opend_reachable", None) is False
    ):
        return "connectivity"
    if any(
        token in message for token in ("频率太高", "rate limit", "too many requests")
    ):
        return "rate_limit"
    if error_type == "quote_server_interrupted" or any(
        token in message
        for token in (
            "connect timeout",
            "connection refused",
            "network down",
            "protocol disconnected",
            "网络中断",
        )
    ):
        return "connectivity"
    return None


def _incident_path(data_dir: Path, category: OpenDCategory) -> Path:
    return (
        data_dir
        / "trend_controller/shared/incidents"
        / f"opend-{category.replace('_', '-')}.json"
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read(path: Path, category: OpenDCategory) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    reasons = payload.get("reasons") if isinstance(payload, dict) else None
    attempts = (
        payload.get("feishu_attempts") if isinstance(payload, dict) else None
    )
    delivered_at = (
        payload.get("feishu_delivered_at") if isinstance(payload, dict) else None
    )
    channels = payload.get("channels") if isinstance(payload, dict) else None
    first_detected_at = _parse_timestamp(
        payload.get("first_detected_at") if isinstance(payload, dict) else None
    )
    updated_at = _parse_timestamp(
        payload.get("updated_at") if isinstance(payload, dict) else None
    )
    delivered_at_timestamp = (
        _parse_timestamp(delivered_at) if delivered_at else None
    )
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA
        or payload.get("category") != category
        or not isinstance(payload.get("active"), bool)
        or first_detected_at is None
        or updated_at is None
        or not isinstance(payload.get("affected_markets"), list)
        or not all(isinstance(market, str) for market in payload["affected_markets"])
        or not isinstance(reasons, dict)
        or not all(
            isinstance(market, str) and isinstance(reason, str)
            for market, reason in reasons.items()
        )
        or not isinstance(payload.get("healthy_markets"), list)
        or not all(isinstance(market, str) for market in payload["healthy_markets"])
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or not 0 <= attempts <= 2
        or not isinstance(delivered_at, str)
        or bool(delivered_at) and delivered_at_timestamp is None
        or not isinstance(channels, list)
        or not all(isinstance(channel, str) for channel in channels)
        or bool(delivered_at) != bool(channels)
        or any(channel not in {"feishu", "feishu_app"} for channel in channels)
        or len(channels) > 1
        or bool(delivered_at) and attempts == 0
        or (
            delivered_at_timestamp is not None
            and (
                first_detected_at is None
                or (delivered_at_timestamp.tzinfo is None)
                != (first_detected_at.tzinfo is None)
                or delivered_at_timestamp < first_detected_at
            )
        )
    ):
        raise ValueError(f"invalid OpenD incident state: {path}")
    return payload


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=path.parent
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_opend_failure(
    *,
    data_dir: Path,
    market: str,
    category: OpenDCategory,
    reason: str,
    occurred_at: datetime,
    send_feishu: SendFeishu,
    title: str = "",
    message: str = "",
) -> bool:
    lock_path = data_dir / "trend_controller/shared/opend-incidents.lock"
    path = _incident_path(data_dir, category)
    now_text = occurred_at.isoformat()
    try:
        with RunLock(lock_path, wait=True):
            state = _read(path, category)
            if state is None or state.get("active") is not True:
                state = {
                    "schema_version": SCHEMA,
                    "category": category,
                    "active": True,
                    "first_detected_at": now_text,
                    "updated_at": now_text,
                    "affected_markets": [],
                    "reasons": {},
                    "healthy_markets": [],
                    "feishu_attempts": 0,
                    "feishu_delivered_at": "",
                    "channels": [],
                }
            affected = {str(item) for item in state["affected_markets"]}
            affected.add(market)
            reasons = dict(state["reasons"])
            reasons[market] = reason
            state["affected_markets"] = sorted(affected)
            state["reasons"] = reasons
            state["updated_at"] = now_text
            if (
                not state["feishu_delivered_at"]
                and int(state["feishu_attempts"]) < 2
            ):
                state["feishu_attempts"] = int(state["feishu_attempts"]) + 1
                try:
                    delivered_channel = send_feishu(title, message)
                except Exception:
                    delivered_channel = None
                if delivered_channel:
                    state["feishu_delivered_at"] = now_text
                    state["channels"] = [delivered_channel]
            _write(path, state)
            return bool(state["feishu_delivered_at"])
    except Exception as exc:
        if isinstance(exc, OpenDIncidentStateError):
            raise
        raise OpenDIncidentStateError(str(exc)) from exc


def _fresh_markets(data_dir: Path, observed_at: datetime) -> set[str]:
    fresh: set[str] = set()
    for market in ("CN", "HK", "US"):
        try:
            payload = json.loads(
                (
                    data_dir / "trend_controller" / market / "status.json"
                ).read_text(encoding="utf-8")
            )
            heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"]))
        except (
            FileNotFoundError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ):
            continue
        if abs(observed_at - heartbeat) <= timedelta(minutes=2):
            fresh.add(market)
    return fresh


def record_opend_health(
    data_dir: Path, market: str, observed_at: datetime
) -> None:
    lock_path = data_dir / "trend_controller/shared/opend-incidents.lock"
    try:
        with RunLock(lock_path, wait=True):
            for category in CATEGORIES:
                path = _incident_path(data_dir, category)
                state = _read(path, category)
                if state is None or state.get("active") is not True:
                    continue
                first_detected_at = datetime.fromisoformat(
                    str(state["first_detected_at"])
                )
                if observed_at <= first_detected_at:
                    continue
                healthy = {str(item) for item in state["healthy_markets"]}
                healthy.add(market)
                state["healthy_markets"] = sorted(healthy)
                state["updated_at"] = observed_at.isoformat()
                quorum = _fresh_markets(data_dir, observed_at) | {market}
                if quorum <= healthy:
                    state["active"] = False
                _write(path, state)
    except Exception as exc:
        if isinstance(exc, OpenDIncidentStateError):
            raise
        raise OpenDIncidentStateError(str(exc)) from exc
