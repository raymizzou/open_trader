from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from open_trader.daily_premarket import DailyPremarketConfig
from open_trader.trend_allocation import run_trend_allocation_controller


@pytest.fixture
def los_angeles_host_timezone() -> Iterator[None]:
    original_timezone = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        yield
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()


def run_allocation_cycle(tmp_path: Path, now: datetime) -> dict[str, object]:
    config = DailyPremarketConfig(
        repo=tmp_path,
        python=Path(sys.executable),
        timezone="Asia/Shanghai",
        deadline="21:10",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        trend_executor_host=socket.gethostname(),
    )

    class Quote:
        def get_cn_trading_days(self, **_kwargs: object) -> list[str]:
            return []

        def close(self) -> None:
            pass

    class StopLoop(Exception):
        pass

    def stop_after_status(_seconds: float) -> None:
        raise StopLoop

    def unexpected_api(**_kwargs: object) -> object:
        pytest.fail("a waiting or holiday allocation must not fetch Trend Animals")

    with pytest.raises(StopLoop):
        run_trend_allocation_controller(
            config,
            now_fn=lambda: now,
            sleep_fn=stop_after_status,
            quote_factory=lambda **_kwargs: Quote(),
            api_factory=unexpected_api,
        )

    return json.loads(
        (config.data_dir / "trend_allocation/controller_status.json").read_text()
    )


@pytest.mark.parametrize(
    ("host_local_time", "expected_time", "phase", "attempted_for", "next_check"),
    [
        (
            "2026-07-19T18:31:00", "2026-07-20T09:31:00+08:00",
            "waiting", None, "2026-07-20T16:20:00+08:00",
        ),
        (
            "2026-01-19T17:31:00", "2026-01-20T09:31:00+08:00",
            "waiting", None, "2026-01-20T16:20:00+08:00",
        ),
        (
            "2026-07-20T01:21:00", "2026-07-20T16:21:00+08:00",
            "holiday", "2026-07-20", "2026-07-21T16:21:00+08:00",
        ),
        (
            "2026-01-20T00:21:00", "2026-01-20T16:21:00+08:00",
            "holiday", "2026-01-20", "2026-01-21T16:21:00+08:00",
        ),
    ],
    ids=["summer-waiting", "winter-waiting", "summer-holiday", "winter-holiday"],
)
def test_naive_host_local_allocation_schedule(
    tmp_path: Path,
    los_angeles_host_timezone: None,
    host_local_time: str,
    expected_time: str,
    phase: str,
    attempted_for: str | None,
    next_check: str,
) -> None:
    status = run_allocation_cycle(tmp_path, datetime.fromisoformat(host_local_time))

    assert status["heartbeat_at"] == expected_time
    assert status["phase"] == phase
    assert status["attempted_for"] == attempted_for
    assert status["next_check_at"] == next_check
    assert status["blocker"] is None
    assert status["latest_daily_path"] is None
    assert status["latest_sha256"] is None


@pytest.mark.parametrize(
    "aware_time",
    ["2026-07-20T08:21:00+00:00", "2026-07-20T01:21:00-07:00"],
    ids=["utc", "los-angeles"],
)
def test_aware_allocation_clock_preserves_instant(
    tmp_path: Path,
    los_angeles_host_timezone: None,
    aware_time: str,
) -> None:
    now = datetime.fromisoformat(aware_time)

    status = run_allocation_cycle(tmp_path, now)

    assert status["heartbeat_at"] == "2026-07-20T16:21:00+08:00"
    assert datetime.fromisoformat(status["heartbeat_at"]) == now
    assert status["phase"] == "holiday"
    assert status["attempted_for"] == "2026-07-20"
    assert status["next_check_at"] == "2026-07-21T16:21:00+08:00"
    assert status["blocker"] is None
    assert status["latest_daily_path"] is None
    assert status["latest_sha256"] is None
