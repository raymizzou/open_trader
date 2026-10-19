from pathlib import Path
import subprocess
import sys

import pytest


CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_production_log.py"
ALLOWED_LOG = "\n".join(
    [
        "2026-10-19T13:33:00+0800 INFO open_trader.polymarket_trading metadata refresh",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError>RemoteProtocolError",
        "2026-10-19T13:33:23+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=event error_types=TransportError>RemoteProtocolError>RemoteProtocolError",
        "2026-10-19T13:33:41+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=RequestRejectedError status=400",
        "2026-10-19T13:45:02+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>ConnectTimeout>ConnectTimeout",
        "2026-10-19T13:46:10+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=event error_types=TransportError>LocalProtocolError>LocalProtocolError",
    ]
) + "\n"


def _run_checker(tmp_path: Path, content: str) -> subprocess.CompletedProcess[str]:
    log_path = tmp_path / "launchd.err.log"
    log_path.write_bytes(content.encode("utf-8"))
    return subprocess.run(
        [sys.executable, str(CHECKER), str(log_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_metadata_network_warnings_are_allowed(tmp_path: Path) -> None:
    result = _run_checker(tmp_path, ALLOWED_LOG)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "bad_line",
    [
        "Traceback (most recent call last):\n  sqlite3.OperationalError: database is locked",
        "2026-10-19T13:33:09+0800 ERROR open_trader.prediction_runtime prediction_lp_reward_refresh_failed",
        "2026-10-19T13:33:09+0800 ERROR open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError",
        "2026-10-19T13:33:09+0800 CRITICAL open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=KeyError",
        "2026-10-19T13:33:09+0800 WARNING open_trader.lp_business_writer lp_business_save_outcome_unknown error_type=OperationalError",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_snapshot_stage stage=account_trades elapsed_seconds=0.126 error_types=TransportError>RemoteProtocolError thread=6267400192 task_id=ae8c364182874e95a42a861726d",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError Traceback",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=RequestRejectedError status=500",
        "2026-10-19T13:33:09+0800 WARNING other.logger lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=account error_types=TransportError>RemoteProtocolError",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError>LocalProtocolError",
        "2026-10-19T13:33:09+0800 warning open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError exception",
        "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError ",
        "WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError",
    ],
)
def test_other_error_signals_are_rejected(tmp_path: Path, bad_line: str) -> None:
    result = _run_checker(tmp_path, ALLOWED_LOG + bad_line + "\n")

    assert result.returncode == 1
    assert "reason=log_error_signal" in result.stderr


@pytest.mark.parametrize(
    ("line", "ending"),
    [
        (
            "2026-10-19T13:33:09Z WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>RemoteProtocolError",
            "\r\n",
        ),
        (
            "2026-10-19T13:33:09+08:00 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=event error_types=TransportError>LocalProtocolError",
            "",
        ),
        (
            "2026-10-19T13:33:09+0800 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=market error_types=TransportError>ConnectTimeout",
            "",
        ),
        (
            "2026-10-19T13:33:09+08:00 WARNING open_trader.polymarket_trading lp_metadata_read_failed stage=event error_types=RequestRejectedError status=400",
            "\r\n",
        ),
    ],
)
def test_allowed_timestamp_and_transport_variants(
    tmp_path: Path, line: str, ending: str
) -> None:
    result = _run_checker(tmp_path, line + ending)

    assert result.returncode == 0, result.stderr


def test_only_last_200_lines_are_checked(tmp_path: Path) -> None:
    log = ALLOWED_LOG + "ERROR outside retained window\n" + "\n".join(["INFO clean"] * 200) + "\n"

    result = _run_checker(tmp_path, log)

    assert result.returncode == 0, result.stderr
