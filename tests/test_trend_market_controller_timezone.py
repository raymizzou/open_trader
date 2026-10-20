from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from open_trader.daily_premarket import DailyPremarketConfig
from open_trader.trend_market_controller import run_trend_market_controller


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


def readonly_config(tmp_path: Path) -> DailyPremarketConfig:
    return DailyPremarketConfig(
        repo=tmp_path,
        python=Path(sys.executable),
        timezone="Asia/Shanghai",
        deadline="09:00",
        futu_host="127.0.0.1",
        futu_port=11111,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        portfolio=tmp_path / "data/latest/portfolio.csv",
        trend_executor_host=f"{socket.gethostname()}-tzfix-other-host",
    )


@pytest.mark.parametrize("market", ["CN", "HK"])
@pytest.mark.parametrize(
    ("host_local_time", "expected_time"),
    [
        ("2026-07-19T18:31:00", "2026-07-20T09:31:00+08:00"),
        ("2026-01-19T17:31:00", "2026-01-20T09:31:00+08:00"),
    ],
)
def test_naive_host_local_controller_heartbeat(
    tmp_path: Path,
    los_angeles_host_timezone: None,
    market: str,
    host_local_time: str,
    expected_time: str,
) -> None:
    config = readonly_config(tmp_path)
    now = datetime.fromisoformat(host_local_time)

    result = run_trend_market_controller(config, market, once=True, now_fn=lambda: now)

    assert result["phase"] == "readonly"
    assert not config.data_dir.exists()
    for field in ("heartbeat_at", "next_check_at"):
        assert result[field] == expected_time
        observed = datetime.fromisoformat(result[field])
        assert observed.utcoffset() is not None
        assert observed == datetime.fromisoformat(expected_time)


@pytest.mark.parametrize("market", ["CN", "HK"])
@pytest.mark.parametrize(
    "aware_time",
    ["2026-07-20T01:31:00+00:00", "2026-07-19T18:31:00-07:00"],
)
def test_aware_controller_clock_preserves_instant(
    tmp_path: Path,
    los_angeles_host_timezone: None,
    market: str,
    aware_time: str,
) -> None:
    config = readonly_config(tmp_path)
    now = datetime.fromisoformat(aware_time)
    expected_time = "2026-07-20T09:31:00+08:00"

    result = run_trend_market_controller(config, market, once=True, now_fn=lambda: now)

    assert result["phase"] == "readonly"
    assert not config.data_dir.exists()
    for field in ("heartbeat_at", "next_check_at"):
        assert result[field] == expected_time
        observed = datetime.fromisoformat(result[field])
        assert observed.utcoffset() is not None
        assert observed == now
