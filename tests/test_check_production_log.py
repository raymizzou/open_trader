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


@pytest.mark.parametrize("event, reason", [("connected", None), ("disconnected", "closed")])
@pytest.mark.parametrize("exception_class", [None, ""], ids=["null", "empty-string"])
@pytest.mark.parametrize("prefix", [
    "",
    "2026-10-29T13:44:10+0000 INFO open_trader.polymarket_lp_orderbook ",
    "2026-10-29 13:44:10,123 INFO open_trader.polymarket_lp_orderbook ",
    "INFO:open_trader.polymarket_lp_orderbook:",
    "INFO open_trader.polymarket_lp_orderbook ",
], ids=["bare", "timestamp", "space-timestamp", "python-info", "info"])
def test_normal_orderbook_connection_null_is_not_an_error(
    tmp_path, event, reason, exception_class, prefix
):
    import json
    row = {"at": "2026-10-29T13:44:10+00:00", "event": event, "reason": reason,
           "code": None, "exception_class": exception_class}
    result = _run_checker(
        tmp_path, prefix + "prediction_lp_orderbook_connection " + json.dumps(row) + "\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("case", [
    "class-runtime-error", "class-nonempty", "class-remote", "class-escaped-remote",
    "class-false", "class-zero", "class-list", "class-dict", "class-whitespace",
    "event-unknown", "event-disconnected-null", "reason-unknown", "reason-connected-closed",
    "code-zero", "code-false", "code-close", "missing-at", "missing-event", "missing-reason",
    "missing-code", "at-empty", "at-whitespace", "at-null", "at-false", "at-zero", "at-list",
    "at-dict", "at-error", "at-escaped-error", "extra-error", "extra-neutral", "body-error",
    "unrelated-logger", "unrelated-python-logger", "lookalike-marker-prefix",
    "lookalike-marker-suffix", "marker-in-json", "no-marker", "message-before-marker",
    "severity-error", "severity-fatal", "severity-warning", "severity-critical",
    "severity-debug", "severity-python-warning", "prefix-error", "prefix-error-body-error",
    "duplicate-key", "duplicate-escaped-key", "malformed", "nan", "infinity", "negative-infinity",
    "trailing-error", "earlier-error-beyond-200", "later-error", "metadata-network-error",
])
def test_orderbook_connection_exception_or_invalid_record_still_fails(tmp_path, case):
    import json
    row = {"at": "2026-10-29T13:44:10+00:00", "event": "connected", "reason": None,
           "code": None, "exception_class": None}
    prefix = "prediction_lp_orderbook_connection "
    reason = "log_error_signal"
    class_values = {
        "class-runtime-error": "RuntimeError private-sentinel",
        "class-nonempty": "ConnectionClosed", "class-remote": "RemoteProtocolError private-sentinel",
        "class-escaped-remote": "RemoteProtocolError private-sentinel", "class-false": False,
        "class-zero": 0, "class-list": [], "class-dict": {}, "class-whitespace": " \t",
    }
    if case in class_values:
        row["exception_class"] = class_values[case]
    elif case == "event-unknown": row["event"] = "reconnecting"
    elif case == "event-disconnected-null": row["event"] = "disconnected"
    elif case == "reason-unknown": row["reason"] = "server_close"
    elif case == "reason-connected-closed": row["reason"] = "closed"
    elif case == "code-zero": row["code"] = 0
    elif case == "code-false": row["code"] = False
    elif case == "code-close": row["code"] = 1000
    elif case.startswith("missing-"): del row[case.removeprefix("missing-")]
    elif case.startswith("at-"):
        row["at"] = {"at-empty": "", "at-whitespace": " \t", "at-null": None,
                     "at-false": False, "at-zero": 0, "at-list": [], "at-dict": {},
                     "at-error": "Error private-sentinel", "at-escaped-error": "Error private-sentinel"}[case]
    elif case == "extra-error": row["other_error"] = None
    elif case == "extra-neutral": row["extra"] = "private-sentinel"
    elif case == "body-error": row["message"] = "Error private-sentinel"
    elif case == "unrelated-logger": prefix = "2026-10-29T13:44:10+0000 INFO other.logger " + prefix
    elif case == "unrelated-python-logger": prefix = "INFO:other.logger:" + prefix
    elif case == "lookalike-marker-prefix": prefix = "other_" + prefix
    elif case == "lookalike-marker-suffix": prefix = "prediction_lp_orderbook_connection_extra "
    elif case == "marker-in-json": prefix = ""; row["message"] = "prediction_lp_orderbook_connection"
    elif case == "no-marker": prefix = ""
    elif case == "message-before-marker": prefix = "INFO open_trader.polymarket_lp_orderbook unrelated " + prefix
    elif case.startswith("severity-"):
        severity = case.removeprefix("severity-").upper()
        prefix = ("WARNING:open_trader.polymarket_lp_orderbook:" if case == "severity-python-warning"
                  else f"2026-10-29T13:44:10+0000 {severity} open_trader.polymarket_lp_orderbook ") + prefix
    elif case.startswith("prefix-error"):
        prefix = "ERROR private-sentinel " + prefix
        if case == "prefix-error-body-error": row["at"] = "Error private-sentinel"
    body = json.dumps(row)
    if case in ("class-remote", "class-escaped-remote", "metadata-network-error"):
        reason = "log_remote_protocol_error"
    if case == "class-escaped-remote": body = body.replace("RemoteProtocolError", r"RemoteProtocol\u0045rror")
    elif case == "at-escaped-error": body = body.replace("Error", r"\u0045rror")
    elif case in ("duplicate-key", "duplicate-escaped-key"):
        key = '"exception_class"' if case == "duplicate-key" else r'"exception_\u0063lass"'
        body = body[:-1] + ', ' + key + ': null}'
        reason = "log_record_invalid"
    elif case == "malformed": body = body[:-1]; reason = "log_record_invalid"
    elif case in ("nan", "infinity", "negative-infinity"):
        body = body.replace('"code": null', '"code": ' + {"nan": "NaN", "infinity": "Infinity", "negative-infinity": "-Infinity"}[case])
        reason = "log_record_invalid"
    line = prefix + body + "\n"
    if case == "trailing-error": line = line.rstrip() + " Error private-sentinel\n"; reason = "log_record_invalid"
    elif case == "earlier-error-beyond-200": line = "Error private-sentinel\n" + "INFO clean\n" * 201 + line
    elif case == "later-error": line += "Error private-sentinel\n"
    elif case == "metadata-network-error": line += METADATA_LOG
    result = _run_checker(tmp_path, line)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"production_log_check_failed reason={reason}\n"
    assert "private-sentinel" not in result.stdout + result.stderr


@pytest.mark.parametrize("case", ["normal-null", "normal-empty", "real-exception", "missing-marker"])
def test_orderbook_connection_journal_preserves_start_boundary_and_errors(tmp_path, monkeypatch, case):
    import json
    rows = [
        {"at": "2026-10-29T13:44:10+00:00", "event": "connected", "reason": None,
         "code": None, "exception_class": "" if case == "normal-empty" else None},
        {"at": "2026-10-29T13:44:11+00:00", "event": "disconnected", "reason": "closed",
         "code": None, "exception_class": "" if case == "normal-empty" else None},
    ]
    if case == "real-exception":
        rows[-1]["exception_class"] = "RemoteProtocolError private-payload-sentinel"
    content = "" if case == "missing-marker" else "INFO prediction_runtime_state ready\n"
    content += "\n".join(
        "INFO:open_trader.polymarket_lp_orderbook:prediction_lp_orderbook_connection " + json.dumps(row)
        for row in rows
    ) + "\n"
    recorded = _journal_fixture(tmp_path, monkeypatch, content.encode("utf-8"))
    result = _run_journal()
    assert json.loads(recorded.read_text()) == [
        "-u", "open-trader-prediction.service", "--since", "2026-10-25 12:34:56 UTC",
        "--no-pager", "-o", "cat",
    ]
    assert result.stdout == ""
    if case in ("normal-null", "normal-empty"):
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    else:
        assert result.returncode == 1
        reason = "log_remote_protocol_error" if case == "real-exception" else "log_required_text_missing"
        assert result.stderr == f"production_log_check_failed reason={reason}\n"
    assert "sentinel" not in result.stdout + result.stderr
