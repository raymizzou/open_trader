from pathlib import Path
import subprocess
import sys

import pytest


CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_production_log.py"
METADATA_LOG = "\n".join(
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


@pytest.mark.parametrize("line", METADATA_LOG.splitlines()[1:])
def test_metadata_network_warnings_are_rejected(tmp_path: Path, line: str) -> None:
    result = _run_checker(tmp_path, line + "\n")

    assert result.returncode == 1
    reason = "log_remote_protocol_error" if "RemoteProtocolError" in line else "log_error_signal"
    assert result.stderr == f"production_log_check_failed reason={reason}\n"
    assert result.stdout == ""


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
    result = _run_checker(tmp_path, "INFO clean startup\n" + bad_line + "\n")

    assert result.returncode == 1
    reason = "log_remote_protocol_error" if "RemoteProtocolError" in bad_line else "log_error_signal"
    assert result.stderr == f"production_log_check_failed reason={reason}\n"


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
def test_timestamp_and_transport_variants_are_rejected(
    tmp_path: Path, line: str, ending: str
) -> None:
    result = _run_checker(tmp_path, line + ending)

    assert result.returncode == 1
    reason = "log_remote_protocol_error" if "RemoteProtocolError" in line else "log_error_signal"
    assert result.stderr == f"production_log_check_failed reason={reason}\n"


def test_errors_before_last_200_lines_are_rejected(tmp_path: Path) -> None:
    log = "ERROR outside retained window\n" + "\n".join(["INFO clean"] * 200) + "\n"

    result = _run_checker(tmp_path, log)

    assert result.returncode == 1
    assert "reason=log_error_signal" in result.stderr
    assert "outside retained window" not in result.stderr


@pytest.mark.parametrize("line", [
    'RemoteProtocolError private-sentinel',
    '{"message":"RemoteProtocolError private-sentinel"}',
    r'{"message":"RemoteProtocol\u0045rror private-sentinel"}',
])
def test_remote_protocol_error_has_explicit_reason(tmp_path, line):
    result = _run_checker(tmp_path, line)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "production_log_check_failed reason=log_remote_protocol_error\n"


@pytest.mark.parametrize("case", [
    "legacy", "file", "required-present", "required-missing", "empty", "whitespace",
    "missing", "directory", "invalid-utf8", "no-source", "both-files", "both-sources",
    "journal-without-since", "journal-empty-since", "file-with-since", "unknown-option",
    "missing-option-value",
])
def test_file_arguments_and_required_text(tmp_path, case):
    log = tmp_path / "private-path-sentinel"
    log.write_text("INFO prediction_runtime_state ready\n")
    arguments = ["--file", str(log)]
    if case == "legacy": arguments = [str(log)]
    elif case == "required-present": arguments += ["--require-text", "prediction_runtime_state"]
    elif case == "required-missing": arguments += ["--require-text", "missing-marker-sentinel"]
    elif case == "empty": log.write_bytes(b"")
    elif case == "whitespace": log.write_text(" \r\n\t")
    elif case == "missing": log.unlink()
    elif case == "directory": log.unlink(); log.mkdir()
    elif case == "invalid-utf8": log.write_bytes(b"INFO clean\n\xff")
    elif case == "no-source": arguments = []
    elif case == "both-files": arguments += [str(log)]
    elif case == "both-sources": arguments += ["--journal-unit", "unit-sentinel", "--since", "start-sentinel"]
    elif case == "journal-without-since": arguments = ["--journal-unit", "unit-sentinel"]
    elif case == "journal-empty-since": arguments = ["--journal-unit", "unit-sentinel", "--since", " "]
    elif case == "file-with-since": arguments += ["--since", "start-sentinel"]
    elif case == "unknown-option": arguments += ["--private-option-sentinel"]
    elif case == "missing-option-value": arguments = ["--file"]
    result = subprocess.run([sys.executable, str(CHECKER), *arguments], capture_output=True, text=True, timeout=3)
    assert result.stdout == ""
    if case in ("legacy", "file", "required-present"):
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    else:
        assert result.returncode == 1
        assert result.stderr.startswith("production_log_check_failed reason=")
        assert len(result.stderr.splitlines()) == 1
        assert "sentinel" not in result.stderr


def _journal_fixture(tmp_path, monkeypatch, output, *, returncode=0):
    import os
    fake = tmp_path / "journalctl"
    arguments = tmp_path / "journal-argv.json"
    fake.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        f"pathlib.Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.buffer.write({output!r})\n"
        "sys.stderr.write('private-stderr-sentinel')\n"
        f"sys.exit({returncode})\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return arguments


def _run_journal():
    return subprocess.run([
        sys.executable, str(CHECKER), "--journal-unit", "open-trader-prediction.service",
        "--since", "2026-10-25 12:34:56 UTC", "--require-text", "prediction_runtime_state",
    ], capture_output=True, text=True, timeout=3)


def test_journal_source_uses_start_boundary(tmp_path, monkeypatch):
    import json
    recorded = _journal_fixture(tmp_path, monkeypatch, b"INFO prediction_runtime_state ready\n")
    result = _run_journal()
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert json.loads(recorded.read_text()) == [
        "-u", "open-trader-prediction.service", "--since", "2026-10-25 12:34:56 UTC",
        "--no-pager", "-o", "cat",
    ]


@pytest.mark.parametrize("case", ["nonzero", "empty", "missing-marker", "invalid-utf8"])
def test_journal_source_failures_are_rejected(tmp_path, monkeypatch, case):
    output = dict(nonzero=b"prediction_runtime_state private-stdout-sentinel", empty=b"",
                  **{"missing-marker":b"INFO private-stdout-sentinel", "invalid-utf8":b"prediction_runtime_state \xff"})[case]
    _journal_fixture(tmp_path, monkeypatch, output, returncode=1 if case == "nonzero" else 0)
    result = _run_journal()
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("production_log_check_failed reason=")
    assert len(result.stderr.splitlines()) == 1
    assert "sentinel" not in result.stderr


@pytest.mark.parametrize("line, reason", [
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null}', None),
    (r'{"level":"INFO","status":"healthy","degraded_reasons":[],"last_\u0065rror":null}', None),
    ('ERROR {"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null}', "log_error_signal"),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"other_error":null}', "log_error_signal"),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"last_error":null}', "log_record_invalid"),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"metric":NaN}', "log_record_invalid"),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null,"metric":Infinity}', "log_record_invalid"),
    ('{"level":"INFO","status":"healthy","degraded_reasons":[],"last_error":null', "log_record_invalid"),
])
def test_healthy_json_null_remains_allowed(tmp_path, line, reason):
    result = _run_checker(tmp_path, line)
    assert result.returncode == (1 if reason else 0)
    assert result.stdout == ""
    assert result.stderr == (f"production_log_check_failed reason={reason}\n" if reason else "")
