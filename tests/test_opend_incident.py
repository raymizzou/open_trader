from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from open_trader.futu_quote import FutuQuoteError
from open_trader.opend_incident import (
    OpenDIncidentStateError,
    classify_opend_error,
    record_opend_failure,
    record_opend_health,
)


def incident_path(data_dir: Path, category: str = "connectivity") -> Path:
    return (
        data_dir
        / "trend_controller/shared/incidents"
        / f"opend-{category.replace('_', '-')}.json"
    )


def read_incident(
    data_dir: Path, category: str = "connectivity"
) -> dict[str, object]:
    return json.loads(incident_path(data_dir, category).read_text(encoding="utf-8"))


def create_active_incident(data_dir: Path, category: str) -> None:
    path = incident_path(data_dir, category)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.opend_incident.v1",
                "category": category,
                "active": True,
                "first_detected_at": "2026-07-22T09:59:00+08:00",
                "updated_at": "2026-07-22T09:59:00+08:00",
                "affected_markets": ["CN"],
                "reasons": {"CN": "连接超时"},
                "healthy_markets": [],
                "feishu_attempts": 1,
                "feishu_delivered_at": "2026-07-22T09:59:00+08:00",
                "channels": ["feishu_app"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_controller_statuses(
    data_dir: Path, *, cn: str, hk: str, us: str
) -> None:
    for market, clock in {"CN": cn, "HK": hk, "US": us}.items():
        path = data_dir / "trend_controller" / market / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"heartbeat_at": f"2026-07-22T{clock}+08:00"}),
            encoding="utf-8",
        )


def test_classifies_known_opend_failures_without_guessing() -> None:
    assert (
        classify_opend_error(
            FutuQuoteError(
                "down", error_type="opend_unreachable", opend_reachable=False
            )
        )
        == "connectivity"
    )
    assert classify_opend_error(RuntimeError("获取历史K线频率太高")) == "rate_limit"
    assert classify_opend_error(RuntimeError("Connect timeout")) == "connectivity"
    assert classify_opend_error(RuntimeError("unknown broker response")) is None


def test_three_concurrent_markets_send_one_incident(tmp_path: Path) -> None:
    sent: list[tuple[str, str]] = []
    barrier = threading.Barrier(3)

    def report(market: str) -> None:
        barrier.wait()
        record_opend_failure(
            data_dir=tmp_path,
            market=market,
            category="connectivity",
            reason="Connect timeout",
            occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
            send_feishu=lambda title, message: sent.append((title, message))
            or "feishu_app",
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(report, ("CN", "HK", "US")))

    assert len(sent) == 1
    assert read_incident(tmp_path)["affected_markets"] == ["CN", "HK", "US"]


def test_recovery_requires_every_fresh_controller_but_ignores_stale_heartbeat(
    tmp_path: Path,
) -> None:
    write_controller_statuses(tmp_path, cn="10:00:00", hk="10:00:00", us="09:57:59")
    create_active_incident(tmp_path, category="connectivity")

    record_opend_health(
        tmp_path, "CN", datetime.fromisoformat("2026-07-22T10:00:30+08:00")
    )
    assert read_incident(tmp_path)["active"] is True
    record_opend_health(
        tmp_path, "HK", datetime.fromisoformat("2026-07-22T10:00:30+08:00")
    )

    assert read_incident(tmp_path)["active"] is False


def test_recovery_waits_for_all_three_when_all_heartbeats_are_fresh(
    tmp_path: Path,
) -> None:
    write_controller_statuses(tmp_path, cn="10:00:00", hk="10:00:00", us="10:00:00")
    create_active_incident(tmp_path, category="connectivity")
    observed = datetime.fromisoformat("2026-07-22T10:00:30+08:00")

    record_opend_health(tmp_path, "CN", observed)
    record_opend_health(tmp_path, "HK", observed)
    assert read_incident(tmp_path)["active"] is True
    record_opend_health(tmp_path, "US", observed)

    assert read_incident(tmp_path)["active"] is False


def test_recovery_ignores_health_observed_before_incident_creation(
    tmp_path: Path,
) -> None:
    write_controller_statuses(tmp_path, cn="09:59:50", hk="09:59:50", us="09:59:50")
    create_active_incident(tmp_path, category="connectivity")
    path = incident_path(tmp_path)
    state = read_incident(tmp_path)
    state["first_detected_at"] = "2026-07-22T10:00:00+08:00"
    state["updated_at"] = "2026-07-22T10:00:00+08:00"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    record_opend_health(
        tmp_path, "CN", datetime.fromisoformat("2026-07-22T09:59:59+08:00")
    )
    record_opend_health(
        tmp_path, "CN", datetime.fromisoformat("2026-07-22T10:00:00+08:00")
    )

    assert read_incident(tmp_path)["healthy_markets"] == []
    assert read_incident(tmp_path)["active"] is True
    for market in ("CN", "HK", "US"):
        record_opend_health(
            tmp_path,
            market,
            datetime.fromisoformat("2026-07-22T10:00:01+08:00"),
        )

    assert read_incident(tmp_path)["active"] is False


def test_recovery_ignores_health_before_subsecond_failure(
    tmp_path: Path,
) -> None:
    fault_at = datetime.fromisoformat("2026-07-22T10:00:00.900000+08:00")
    record_opend_failure(
        data_dir=tmp_path,
        market="CN",
        category="connectivity",
        reason="连接超时",
        occurred_at=fault_at,
        send_feishu=lambda _title, _message: "feishu_app",
    )

    assert read_incident(tmp_path)["first_detected_at"] == fault_at.isoformat()
    record_opend_health(
        tmp_path,
        "CN",
        datetime.fromisoformat("2026-07-22T10:00:00.100000+08:00"),
    )

    assert read_incident(tmp_path)["healthy_markets"] == []
    assert read_incident(tmp_path)["active"] is True


def test_categories_are_separate_and_each_stops_after_one_retry(
    tmp_path: Path,
) -> None:
    attempts = {"connectivity": 0, "rate_limit": 0}
    for category in ("connectivity", "rate_limit"):
        for _ in range(3):
            record_opend_failure(
                data_dir=tmp_path,
                market="US",
                category=category,
                reason="连接异常" if category == "connectivity" else "请求限频",
                occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
                send_feishu=lambda _title, _message, category=category: attempts.__setitem__(
                    category, attempts[category] + 1
                ),
            )

    assert attempts == {"connectivity": 2, "rate_limit": 2}
    assert read_incident(tmp_path, "connectivity")["feishu_attempts"] == 2
    assert read_incident(tmp_path, "rate_limit")["feishu_attempts"] == 2


def test_recovered_incident_can_send_again(tmp_path: Path) -> None:
    delivered: list[str] = []
    record_opend_failure(
        data_dir=tmp_path,
        market="CN",
        category="connectivity",
        reason="连接超时",
        occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
        send_feishu=lambda _title, _message: delivered.append("first")
        or "feishu_app",
    )
    write_controller_statuses(tmp_path, cn="10:00:05", hk="09:57:00", us="09:57:00")
    record_opend_health(
        tmp_path, "CN", datetime.fromisoformat("2026-07-22T10:00:05+08:00")
    )
    record_opend_failure(
        data_dir=tmp_path,
        market="CN",
        category="connectivity",
        reason="连接再次超时",
        occurred_at=datetime.fromisoformat("2026-07-22T10:01:00+08:00"),
        send_feishu=lambda _title, _message: delivered.append("second")
        or "feishu_app",
    )

    assert delivered == ["first", "second"]


def test_malformed_incident_state_raises_for_controller_fallback(
    tmp_path: Path,
) -> None:
    path = incident_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(OpenDIncidentStateError):
        record_opend_failure(
            data_dir=tmp_path,
            market="CN",
            category="connectivity",
            reason="连接超时",
            occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
            send_feishu=lambda _title, _message: "feishu_app",
        )


def test_invalid_delivered_timestamp_in_valid_header_raises_for_fallback(
    tmp_path: Path,
) -> None:
    create_active_incident(tmp_path, category="connectivity")
    path = incident_path(tmp_path)
    state = read_incident(tmp_path)
    state["feishu_delivered_at"] = "untrusted timestamp"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(OpenDIncidentStateError):
        record_opend_failure(
            data_dir=tmp_path,
            market="HK",
            category="connectivity",
            reason="连接超时",
            occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
            send_feishu=lambda _title, _message: pytest.fail(
                "invalid state must not suppress fallback"
            ),
        )


def test_inconsistent_delivered_state_with_zero_attempts_raises_for_fallback(
    tmp_path: Path,
) -> None:
    create_active_incident(tmp_path, category="connectivity")
    path = incident_path(tmp_path)
    state = read_incident(tmp_path)
    state["feishu_attempts"] = 0
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(OpenDIncidentStateError):
        record_opend_failure(
            data_dir=tmp_path,
            market="HK",
            category="connectivity",
            reason="连接超时",
            occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
            send_feishu=lambda _title, _message: pytest.fail(
                "inconsistent delivery state must not suppress fallback"
            ),
        )


def test_delivered_channel_without_timestamp_raises_for_fallback(
    tmp_path: Path,
) -> None:
    create_active_incident(tmp_path, category="connectivity")
    path = incident_path(tmp_path)
    state = read_incident(tmp_path)
    state["feishu_delivered_at"] = ""
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(OpenDIncidentStateError):
        record_opend_failure(
            data_dir=tmp_path,
            market="HK",
            category="connectivity",
            reason="连接超时",
            occurred_at=datetime.fromisoformat("2026-07-22T10:00:00+08:00"),
            send_feishu=lambda _title, _message: pytest.fail(
                "delivered channel without timestamp must not suppress fallback"
            ),
        )
