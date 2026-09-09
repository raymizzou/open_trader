from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Iterator
import urllib.error
import urllib.request

import pytest

import open_trader.account_api as account_api
from open_trader.account_http import fetch_account_snapshot
from open_trader.account_sync_worker import AccountSyncWorker, AccountSyncWorkerConfig
from open_trader.holding_snapshot_import import (
    HoldingSnapshotImportService,
    load_staged_holding_snapshot,
)
from open_trader.holding_snapshot_workflow import (
    check_workflow_status,
    main,
    submit_confirmed_snapshot,
)
from open_trader.a_share_trend import load_real_holding_input
from open_trader.account_sync_state import write_json_atomic
from open_trader.futu_symbols import to_futu_symbol


def _generation(letter: str) -> str:
    return "sha256:" + letter * 64


def _snapshot(
    *,
    phillips_generation: str = "",
    eastmoney_generation: str = "",
    phillips_date: str = "2026-09-08",
    eastmoney_date: str = "2026-09-08",
    phillips_status: str = "healthy",
    eastmoney_status: str = "healthy",
    snapshot_generation: str = "f",
    quote_as_of: str = "2026-09-08T09:00:00+08:00",
) -> dict[str, object]:
    broker_sources = {
        "futu": {
            "source_kind": "live", "data_as_of": "2026-09-08T08:59:00+08:00",
            "last_success_at": "2026-09-08T08:59:00+08:00", "status": "healthy", "reason": None,
        },
        "tiger": {
            "source_kind": "live", "data_as_of": "2026-09-08T08:59:00+08:00",
            "last_success_at": "2026-09-08T08:59:00+08:00", "status": "healthy", "reason": None,
        },
        "phillips": {
            "source_kind": "manual", "data_as_of": phillips_date,
            "last_success_at": "2026-09-08T09:00:00+08:00", "status": phillips_status, "reason": None,
        },
        "eastmoney": {
            "source_kind": "manual", "data_as_of": eastmoney_date,
            "last_success_at": "2026-09-08T09:00:00+08:00", "status": eastmoney_status, "reason": None,
        },
    }
    status = "stale" if any(
        source["status"] != "healthy" for source in broker_sources.values()
    ) else "healthy"
    account = {
        "status": status,
        "as_of": "2026-09-08T09:00:00+08:00",
        "reason": None,
        "brokers": broker_sources,
    }
    payload = {
        "schema_version": 1,
        "snapshot_generation": _generation(snapshot_generation),
        "account_generation": _generation("e"),
        "generated_at": "2026-09-08T09:00:00+08:00",
        "quote_as_of": quote_as_of,
        "status": status,
        "stale": status == "stale",
        "sources": {
            "account": account,
            "quotes": {"status": "healthy", "as_of": quote_as_of, "reason": None},
        },
        "release": {"api_git_sha": "a" * 40, "worker_git_sha": "a" * 40},
        "summary": {
            "holding_value_hkd": "0", "cash_like_value_hkd": "0", "portfolio_value_hkd": "0",
            "holding_weight_hkd": "0%", "cash_like_weight_hkd": "0%", "holding_count": 0, "broker_count": 4,
        },
        "broker_summaries": [],
        "positions": [],
        "cash_balances": [],
        "errors": [],
        "accepted_statement_generation": {"phillips": "", "eastmoney": ""},
        "accepted_holding_generation": {
            "phillips": phillips_generation, "eastmoney": eastmoney_generation,
        },
    }
    return payload


def _payload(broker: str = "phillips") -> dict[str, object]:
    is_hk = broker == "phillips"
    return {
        "data_as_of": "2026-09-08",
        "confirmed": True,
        "complete": True,
        "positions": [{
            "symbol": "700" if is_hk else "600519",
            "name": "腾讯控股" if is_hk else "贵州茅台",
            "quantity": "10",
            "cost_price": "400" if is_hk else "1500",
        }],
        "cash": {
            "policy": "replace",
            "currency": "HKD" if is_hk else "CNY",
            "balance": "1000",
            "available_balance": "900",
        },
    }


def _staged_response(broker: str, generation: str) -> dict[str, object]:
    return {
        "schema_version": "open_trader.account.holding_generation.v1",
        "status": "staged",
        "broker": broker,
        "data_as_of": "2026-09-08",
        "holding_generation": generation,
    }


class _ScriptedHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = self.server
        server.post_paths.append(self.path)  # type: ignore[attr-defined]
        server.post_headers.append(dict(self.headers.items()))  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        server.post_bodies.append(self.rfile.read(length))  # type: ignore[attr-defined]
        response = server.post_responses.pop(0)  # type: ignore[attr-defined]
        if response is None:
            self.close_connection = True
            self.connection.close()
            return
        status, payload = response
        self._send_json(status, payload)

    def do_GET(self) -> None:
        server = self.server
        server.get_paths.append(self.path)  # type: ignore[attr-defined]
        server.get_headers.append(dict(self.headers.items()))  # type: ignore[attr-defined]
        response = server.get_responses.pop(0)  # type: ignore[attr-defined]
        if callable(response):
            response = response()
        status, payload = response
        self._send_json(status, payload)

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _scripted_server(
    *,
    post_responses: list[tuple[int, object] | None],
    get_responses: list[tuple[int, object] | callable],
) -> Iterator[tuple[str, _ScriptedHandler]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    server.post_responses = list(post_responses)  # type: ignore[attr-defined]
    server.get_responses = list(get_responses)  # type: ignore[attr-defined]
    server.post_bodies = []  # type: ignore[attr-defined]
    server.post_paths = []  # type: ignore[attr-defined]
    server.post_headers = []  # type: ignore[attr-defined]
    server.get_paths = []  # type: ignore[attr-defined]
    server.get_headers = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server  # type: ignore[misc]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _RejectingProxyHandler(BaseHTTPRequestHandler):
    def _reject(self) -> None:
        server = self.server
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        server.proxy_requests.append((self.command, self.path, body))  # type: ignore[attr-defined]
        self.send_error(HTTPStatus.BAD_GATEWAY, "proxy must not be used")

    do_GET = _reject
    do_POST = _reject
    do_CONNECT = _reject

    def log_message(self, format: str, *args: object) -> None:
        return


class _RedirectingAccountHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        if self.path == "/api/v1/account/snapshot":
            self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
            self.send_header("Location", "/redirect-target")
            self.end_headers()
            return
        if self.path == "/redirect-target":
            server.redirect_target_hits += 1  # type: ignore[attr-defined]
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _rejecting_proxy() -> Iterator[tuple[str, _RejectingProxyHandler]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RejectingProxyHandler)
    server.proxy_requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server  # type: ignore[misc]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def _redirecting_account_server() -> Iterator[tuple[str, _RedirectingAccountHandler]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingAccountHandler)
    server.redirect_target_hits = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server  # type: ignore[misc]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_submit_waits_for_matching_published_generation(tmp_path: Path) -> None:
    payload = _payload()
    generation_a = _generation("a")
    generation_b = _generation("b")
    responses = [
        (HTTPStatus.OK, _snapshot(phillips_generation=generation_b)),
        (HTTPStatus.OK, _snapshot(phillips_generation=generation_a)),
    ]
    with _scripted_server(
        post_responses=[(HTTPStatus.ACCEPTED, _staged_response("phillips", generation_a))],
        get_responses=responses,
    ) as (url, server):
        original = json.loads(json.dumps(payload))
        result = submit_confirmed_snapshot(
            "phillips", payload, account_url=url, receipt_path=tmp_path / "receipt.json",
            timeout_seconds=1, poll_seconds=0.01,
        )
    assert result["status"] == "published"
    assert result["holding_generation"] == generation_a
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "published"
    assert receipt["holding_generation"] == generation_a
    assert receipt["broker"] == "phillips"
    assert payload == original
    assert server.post_paths == ["/api/v1/account/holding-snapshots/phillips"]
    assert server.get_paths == ["/api/v1/account/snapshot"] * 2


def test_submit_pending_timeout_never_claims_published(tmp_path: Path) -> None:
    payload = _payload()
    generation_a = _generation("a")
    generation_b = _generation("b")
    with _scripted_server(
        post_responses=[(HTTPStatus.ACCEPTED, _staged_response("phillips", generation_a))],
        get_responses=[(HTTPStatus.OK, _snapshot(phillips_generation=generation_b))],
    ) as (url, _server):
        result = submit_confirmed_snapshot(
            "phillips", payload, account_url=url, receipt_path=tmp_path / "pending.json",
            timeout_seconds=0.08, poll_seconds=0.01,
        )
    assert result["status"] == "pending"
    assert result["expected_generation"] == generation_a
    assert result["current_generation"] == generation_b
    assert result["status"] != "published"
    receipt = json.loads((tmp_path / "pending.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "pending"

    rejected_payload = _payload()
    with _scripted_server(
        post_responses=[(HTTPStatus.BAD_REQUEST, {"code": "holding_snapshot_rejected"})],
        get_responses=[],
    ) as (url, server):
        rejected = submit_confirmed_snapshot(
            "phillips", rejected_payload, account_url=url, receipt_path=tmp_path / "rejected.json",
            timeout_seconds=1, poll_seconds=0,
        )
    assert rejected["status"] == "rejected"
    assert server.post_bodies == [json.dumps(rejected_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()]

    with _scripted_server(
        post_responses=[None, (HTTPStatus.ACCEPTED, _staged_response("phillips", generation_a))],
        get_responses=[(HTTPStatus.OK, _snapshot(phillips_generation=generation_a))],
    ) as (url, server):
        retried = submit_confirmed_snapshot(
            "phillips", payload, account_url=url, receipt_path=tmp_path / "retry.json",
            timeout_seconds=1, poll_seconds=0,
        )
    assert retried["status"] == "published"
    assert len(server.post_bodies) == 2
    assert server.post_bodies[0] == server.post_bodies[1]


def test_submit_requires_explicit_confirmation_before_post(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = _payload()
    payload["confirmed"] = False
    with _scripted_server(post_responses=[], get_responses=[]) as (url, server):
        result = submit_confirmed_snapshot(
            "phillips", payload, account_url=url, receipt_path=tmp_path / "needs.json",
            timeout_seconds=1, poll_seconds=0,
        )
    assert result["status"] == "needs_input"
    assert server.post_paths == []

    generation = _generation("a")
    for bad_response in (
        _staged_response("eastmoney", generation),
        {**_staged_response("phillips", "not-a-generation")},
    ):
        with _scripted_server(
            post_responses=[(HTTPStatus.ACCEPTED, bad_response)], get_responses=[]
        ) as (url, _server):
            result = submit_confirmed_snapshot(
                "phillips", _payload(), account_url=url,
                receipt_path=tmp_path / f"bad-{len(bad_response)}.json",
                timeout_seconds=1, poll_seconds=0,
            )
        assert result["status"] == "rejected"

    with pytest.raises(ValueError, match="loopback"):
        submit_confirmed_snapshot(
            "phillips", _payload(), account_url="http://example.com",
            receipt_path=tmp_path / "external.json", timeout_seconds=1, poll_seconds=0,
        )

    input_path = tmp_path / "confirmed.json"
    input_path.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    assert main([
        "submit", "--broker", "phillips", "--input", str(input_path),
        "--receipt", str(input_path), "--account-url", "http://127.0.0.1:1",
    ]) != 0
    assert "receipt" in capsys.readouterr().out

    oversized_path = tmp_path / "oversized.json"
    oversized_path.write_text(
        json.dumps({**_payload(), "padding": "x" * (1024 * 1024)}, ensure_ascii=False),
        encoding="utf-8",
    )
    with _scripted_server(post_responses=[], get_responses=[]) as (url, server):
        assert main([
            "submit", "--broker", "phillips", "--input", str(oversized_path),
            "--receipt", str(tmp_path / "oversized-receipt.json"), "--account-url", url,
        ]) != 0
        assert server.post_paths == []
    assert "needs_input" in capsys.readouterr().out


def test_workflow_ignores_proxy_environment_for_all_requests(tmp_path: Path) -> None:
    payload = _payload()
    generation = _generation("a")
    account = _snapshot(phillips_generation=generation, eastmoney_generation=generation)
    dashboard = {
        "trend_reports": {
            broker: {
                "available": True,
                "data_date": "2026-09-08",
                "real_position_source": {
                    "snapshot_period": "2026-09-08",
                    "holding_generation": generation,
                },
            }
            for broker in ("phillips", "eastmoney")
        },
        "trend_controllers": {
            broker: {"health": "healthy", "blocking": False, "phase": "monitoring"}
            for broker in ("phillips", "eastmoney")
        },
    }
    submit_script = (
        "import sys\n"
        "from open_trader.holding_snapshot_workflow import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )
    with _rejecting_proxy() as (proxy_url, proxy):
        with _scripted_server(
            post_responses=[(HTTPStatus.ACCEPTED, _staged_response("phillips", generation))],
            get_responses=[
                (HTTPStatus.OK, account),
                (HTTPStatus.OK, account),
                (HTTPStatus.OK, dashboard),
            ],
        ) as (url, server):
            environment = os.environ.copy()
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                environment[key] = proxy_url
            for key in ("NO_PROXY", "no_proxy"):
                environment[key] = ""
            input_path = tmp_path / "confirmed.json"
            input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            submit = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    submit_script,
                    "submit",
                    "--broker",
                    "phillips",
                    "--input",
                    str(input_path),
                    "--receipt",
                    str(tmp_path / "receipt.json"),
                    "--account-url",
                    url,
                    "--timeout-seconds",
                    "1",
                    "--poll-seconds",
                    "0",
                ],
                cwd=Path.cwd(),
                env={**environment, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
                capture_output=True,
                text=True,
                check=False,
            )
            assert submit.returncode == 0, submit.stderr

            check = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    submit_script,
                    "check",
                    "--account-url",
                    url,
                    "--dashboard-url",
                    url,
                ],
                cwd=Path.cwd(),
                env={**environment, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
                capture_output=True,
                text=True,
                check=False,
            )
            assert check.returncode == 0, check.stderr
            assert json.loads(check.stdout)["status"] == "matched"

            assert server.post_paths == ["/api/v1/account/holding-snapshots/phillips"]
            assert server.get_paths == [
                "/api/v1/account/snapshot",
                "/api/v1/account/snapshot",
                "/api/dashboard",
            ]
            assert server.get_headers
            assert all(
                headers.get("X-Open-Trader-Account-Route") == "production"
                for headers in server.get_headers[:2]
            )
            assert "X-Open-Trader-Account-Route" not in server.get_headers[2]
            assert server.post_headers[0]["Content-Type"] == "application/json"
            receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
            assert receipt["status"] == "published"
            assert receipt["holding_generation"] == generation
        assert proxy.proxy_requests == []


def test_check_does_not_follow_account_redirect(capsys: pytest.CaptureFixture[str]) -> None:
    with _redirecting_account_server() as (url, server):
        exit_code = main(["check", "--account-url", url, "--dashboard-url", url])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "issues": ["account_or_dashboard_unavailable"],
        "status": "unavailable",
    }
    assert server.redirect_target_hits == 0


def test_check_reports_stale_controller_and_new_holdings_without_volatile_output() -> None:
    generation_a = _generation("a")
    generation_b = _generation("b")
    account = _snapshot(
        phillips_generation=generation_b, eastmoney_generation=generation_a,
        phillips_date="2026-09-08", eastmoney_date="2026-09-08",
    )
    dashboard = {
        "trend_reports": {
            "phillips": {
                "available": True, "data_date": "2026-09-07",
                "real_position_source": {"snapshot_period": "2026-09-07", "holding_generation": generation_a},
            },
            "eastmoney": {
                "available": True, "data_date": "2026-09-08",
                "real_position_source": {"snapshot_period": "2026-09-08", "holding_generation": generation_a},
            },
        },
        "trend_controllers": {
            "phillips": {"health": "unavailable", "blocking": True, "heartbeat_at": "volatile"},
            "eastmoney": {"health": "healthy", "blocking": False, "phase": "monitoring"},
        },
    }
    first = check_workflow_status(account, dashboard)
    account["quote_as_of"] = "2026-09-09T12:30:00+08:00"
    account["snapshot_generation"] = _generation("z")
    dashboard["trend_controllers"]["phillips"]["heartbeat_at"] = "changed"
    second = check_workflow_status(account, dashboard)
    assert first == second
    assert first["brokers"]["phillips"]["lineage_status"] == "pending"
    assert first["brokers"]["eastmoney"]["lineage_status"] == "matched"
    assert "controller_unavailable" in first["issues"]
    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
    for volatile in ("volatile", "changed", "snapshot_generation", "quote_as_of", "heartbeat_at"):
        assert volatile not in serialized


def test_check_supports_nonblocking_readonly_controllers() -> None:
    generation = _generation("a")
    account = _snapshot(
        phillips_generation=generation,
        eastmoney_generation=generation,
        phillips_date="2026-09-08",
        eastmoney_date="2026-09-08",
    )
    dashboard = {
        "trend_reports": {
            broker: {
                "available": True,
                "data_date": "2026-09-08",
                "real_position_source": {
                    "snapshot_period": "2026-09-08",
                    "holding_generation": generation,
                },
            }
            for broker in ("phillips", "eastmoney")
        },
        "trend_controllers": {
            broker: {
                "health": "readonly",
                "blocking": False,
                "reason": "market is closed",
                "phase": "monitoring",
            }
            for broker in ("phillips", "eastmoney")
        },
    }
    matched = check_workflow_status(account, dashboard)
    assert matched["status"] == "matched"
    assert "controller_unavailable" not in matched["issues"]
    assert "controller_blocking" not in matched["issues"]
    for broker in ("phillips", "eastmoney"):
        assert matched["brokers"][broker]["controller_health"] == "readonly"  # type: ignore[index]
        assert matched["brokers"][broker]["controller_reason"] == ""  # type: ignore[index]

    dashboard["trend_controllers"]["phillips"]["blocking"] = True  # type: ignore[index]
    blocked = check_workflow_status(account, dashboard)
    assert blocked["status"] == "pending"
    assert "controller_blocking" in blocked["issues"]
    assert blocked["brokers"]["phillips"]["controller_health"] == "readonly"  # type: ignore[index]


def test_check_flags_due_input_and_report_but_preserves_holiday(capsys: pytest.CaptureFixture[str]) -> None:
    generation_a = _generation("a")
    account = _snapshot(phillips_generation=generation_a, eastmoney_generation=generation_a,
                        phillips_date="2026-09-08", eastmoney_date="2026-09-08")
    dashboard = {
        "trend_reports": {
            "phillips": {"available": True, "data_date": "2026-09-08",
                         "real_position_source": {"snapshot_period": "2026-09-08", "holding_generation": generation_a}},
            "eastmoney": {"available": True, "data_date": "2026-09-08",
                           "real_position_source": {}},
        },
        "trend_controllers": {
            "phillips": {"health": "healthy", "blocking": False, "phase": "holiday"},
            "eastmoney": {"health": "healthy", "blocking": False, "phase": "monitoring"},
        },
    }
    due = check_workflow_status(account, dashboard, expected_date="2026-09-09")
    assert "holding_input_overdue" in due["issues"]
    assert "report_overdue" in due["issues"]
    assert due["brokers"]["phillips"]["lineage_status"] == "matched"
    assert "holding_input_overdue" not in due["brokers"]["phillips"]["issues"]
    assert "report_overdue" not in due["brokers"]["phillips"]["issues"]
    legacy = check_workflow_status(
        account,
        {**dashboard, "trend_reports": {"phillips": {
            "available": True, "data_date": "2026-09-08",
            "real_position_source": {"snapshot_period": "2026-09-08"},
        }}},
    )
    assert legacy["brokers"]["phillips"]["lineage_status"] == "unknown"

    with pytest.raises(ValueError):
        check_workflow_status(account, dashboard, expected_date="09-09-2026")

    exit_code = main([
        "check", "--account-url", "http://127.0.0.1:1", "--dashboard-url", "http://127.0.0.1:1",
    ])
    assert exit_code == 0
    cli_output = capsys.readouterr().out
    assert "unavailable" in cli_output
    assert "traceback" not in cli_output.lower()

    invalid_exit_code = main(["check", "--expected-date", "09-09-2026"])
    assert invalid_exit_code != 0
    assert "invalid_arguments" in capsys.readouterr().out


def test_check_accepts_normal_large_dashboard_response(
    capsys: pytest.CaptureFixture[str],
) -> None:
    generation = _generation("a")
    account = _snapshot(
        phillips_generation=generation,
        eastmoney_generation=generation,
        phillips_date="2026-09-08",
        eastmoney_date="2026-09-08",
    )
    dashboard = {
        "trend_reports": {
            broker: {
                "available": True,
                "data_date": "2026-09-08",
                "real_position_source": {
                    "snapshot_period": "2026-09-08",
                    "holding_generation": generation,
                },
            }
            for broker in ("phillips", "eastmoney")
        },
        "trend_controllers": {
            broker: {"health": "healthy", "blocking": False, "phase": "monitoring"}
            for broker in ("phillips", "eastmoney")
        },
        "padding": "x" * 2_100_000,
    }
    with _scripted_server(
        post_responses=[], get_responses=[(HTTPStatus.OK, account), (HTTPStatus.OK, dashboard)]
    ) as (url, _server):
        exit_code = main(["check", "--account-url", url, "--dashboard-url", url])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "matched"
    assert result["brokers"]["phillips"]["lineage_status"] == "matched"
    assert result["brokers"]["eastmoney"]["lineage_status"] == "matched"
    assert "account_or_dashboard_unavailable" not in result["issues"]

    oversized_dashboard = {
        **dashboard,
        "padding": "x" * (16 * 1024 * 1024 + 1),
    }
    with _scripted_server(
        post_responses=[], get_responses=[(HTTPStatus.OK, account), (HTTPStatus.OK, oversized_dashboard)]
    ) as (url, _server):
        exit_code = main(["check", "--account-url", url, "--dashboard-url", url])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "issues": ["account_or_dashboard_unavailable"],
        "status": "unavailable",
    }


def test_check_retains_actionable_controller_reason() -> None:
    generation = _generation("a")
    account = _snapshot(
        phillips_generation=generation,
        eastmoney_generation=generation,
        phillips_date="2026-09-08",
        eastmoney_date="2026-09-08",
    )

    def dashboard(*, phillips_reason: str, phillips_heartbeat: str, phillips_pid: int) -> dict[str, object]:
        return {
            "trend_reports": {
                broker: {
                    "available": True,
                    "data_date": "2026-09-08",
                    "real_position_source": {
                        "snapshot_period": "2026-09-08",
                        "holding_generation": generation,
                    },
                }
                for broker in ("phillips", "eastmoney")
            },
            "trend_controllers": {
                "phillips": {
                    "health": "unavailable",
                    "blocking": True,
                    "reason": phillips_reason,
                    "heartbeat_at": phillips_heartbeat,
                    "pid": phillips_pid,
                },
                "eastmoney": {
                    "health": "healthy",
                    "blocking": False,
                    "reason": "ignored healthy reason",
                    "phase": "monitoring",
                    "heartbeat_at": "volatile healthy heartbeat",
                    "pid": 99,
                },
            },
        }

    stale = check_workflow_status(
        account,
        dashboard(
            phillips_reason="controller heartbeat is stale",
            phillips_heartbeat="2026-09-08T08:00:00+08:00",
            phillips_pid=123,
        ),
    )
    assert stale["brokers"]["phillips"]["controller_health"] == "unavailable"  # type: ignore[index]
    assert stale["brokers"]["phillips"]["controller_reason"] == "controller heartbeat is stale"  # type: ignore[index]
    assert stale["brokers"]["eastmoney"]["controller_health"] == "healthy"  # type: ignore[index]
    assert stale["brokers"]["eastmoney"]["controller_reason"] == ""  # type: ignore[index]

    changed_reason = check_workflow_status(
        account,
        dashboard(
            phillips_reason="controller status file is missing",
            phillips_heartbeat="2026-09-08T08:00:00+08:00",
            phillips_pid=123,
        ),
    )
    assert changed_reason["brokers"]["phillips"]["controller_reason"] == "controller status file is missing"  # type: ignore[index]
    assert changed_reason != stale

    changed_volatile_fields = check_workflow_status(
        account,
        dashboard(
            phillips_reason="controller heartbeat is stale",
            phillips_heartbeat="2026-09-09T12:00:00+08:00",
            phillips_pid=456,
        ),
    )
    assert json.dumps(changed_volatile_fields, sort_keys=True) == json.dumps(stale, sort_keys=True)


def test_submit_observes_generation_from_large_real_staging_response(tmp_path: Path) -> None:
    from tests.test_account_api import _write_publication

    data_dir = tmp_path / "data"
    _write_publication(data_dir, worker_sha="a" * 40)
    payload = _payload()
    payload["positions"][0]["name"] = "长" * 180_000  # type: ignore[index]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    assert len(body) < 1024 * 1024
    service = HoldingSnapshotImportService(data_dir=data_dir)
    server = account_api.create_account_api(
        data_dir,
        host="127.0.0.1",
        port=0,
        mode="production",
        holding_snapshot_service=service,
        runtime_metadata={
            "api_git_sha": "a" * 40,
            "pid": 1,
            "started_at": "2026-09-08T09:00:00+08:00",
        },
    )
    api_thread = threading.Thread(target=server.serve_forever, daemon=True)
    api_thread.start()
    account_url = f"http://127.0.0.1:{server.server_address[1]}"
    receipt_path = tmp_path / "large.json"
    try:
        request = urllib.request.Request(
            f"{account_url}/api/v1/account/holding-snapshots/phillips",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            raw_response = response.read()
            assert response.status == HTTPStatus.ACCEPTED
        assert len(raw_response) > 1024 * 1024
        staged = json.loads(raw_response)
        expected_generation = staged["holding_generation"]

        result = submit_confirmed_snapshot(
            "phillips",
            payload,
            account_url=account_url,
            receipt_path=receipt_path,
            timeout_seconds=0.25,
            poll_seconds=0,
        )
    finally:
        server.shutdown()
        server.server_close()
        api_thread.join(timeout=2)

    assert result["status"] == "pending"
    assert result["holding_generation"] == expected_generation
    assert result["current_generation"] == ""


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize(
    ("broker", "market", "symbol", "name", "currency"),
    [("phillips", "HK", "700", "腾讯控股", "HKD"), ("eastmoney", "CN", "600519", "贵州茅台", "CNY")],
)
def test_submit_real_api_worker_and_report_lineage(
    tmp_path: Path, broker: str, market: str, symbol: str, name: str, currency: str,
) -> None:
    from tests.test_account_api import _write_publication

    data_dir = tmp_path / "data"
    _write_publication(data_dir, worker_sha="a" * 40)
    quotes_path = data_dir / "latest" / "quotes.json"
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    quote_symbol = symbol.zfill(5) if market == "HK" else symbol
    quotes["quotes"][to_futu_symbol(market, quote_symbol)] = {
        "market": market,
        "symbol": quote_symbol,
        "status": "ok",
        "last_price": "10",
        "price_session": "statement",
        "price_time": quotes["last_success_at"],
        "fetched_at": quotes["last_success_at"],
        "stale": False,
    }
    quotes["requested_count"] += 1
    quotes["quote_count"] += 1
    write_json_atomic(quotes_path, quotes)
    report_sentinel = tmp_path / "reports" / "sentinel.json"
    report_sentinel.parent.mkdir(parents=True)
    report_sentinel.write_text("frozen-report", encoding="utf-8")
    payload = _payload(broker)
    payload["positions"] = [{"symbol": symbol, "name": name, "quantity": "10", "cost_price": "10"}]
    payload["cash"] = {"policy": "replace", "currency": currency, "balance": "1000", "available_balance": "900"}
    receipt_path = tmp_path / "receipts" / f"{broker}.json"
    server = account_api.create_account_api(
        data_dir, host="127.0.0.1", port=0, mode="production",
        runtime_metadata={"api_git_sha": "a" * 40, "pid": 1, "started_at": "2026-09-08T09:00:00+08:00"},
    )
    api_thread = threading.Thread(target=server.serve_forever, daemon=True)
    api_thread.start()
    account_url = f"http://127.0.0.1:{server.server_address[1]}"
    result_holder: dict[str, object] = {}
    try:
        def submit() -> None:
            result_holder["result"] = submit_confirmed_snapshot(
                broker, payload, account_url=account_url, receipt_path=receipt_path,
                timeout_seconds=5, poll_seconds=0.02,
            )

        submit_thread = threading.Thread(target=submit, daemon=True)
        submit_thread.start()
        staged: dict[str, object] | None = None
        for _ in range(100):
            staged = load_staged_holding_snapshot(data_dir, broker)
            if staged is not None:
                break
            time.sleep(0.01)
        assert staged is not None
        worker = AccountSyncWorker(AccountSyncWorkerConfig(
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            portfolio_path=data_dir / "latest" / "portfolio.csv",
            futu_host="127.0.0.1", futu_port=_unused_port(),
            tiger_config_dir=tmp_path / "tiger", tiger_account=None,
            account_interval_seconds=0, quote_interval_seconds=0,
        ))
        worker.sync_accounts_once()
        submit_thread.join(timeout=5)
        assert not submit_thread.is_alive()
        result = result_holder["result"]
        assert isinstance(result, dict)
        assert result["status"] == "published", result
        generation = str(result["holding_generation"])
        snapshot = fetch_account_snapshot(account_url, timeout_seconds=1)
        assert snapshot["accepted_holding_generation"][broker] == generation  # type: ignore[index]
        loaded = load_real_holding_input(snapshot, market, state_path=tmp_path / f"{broker}.json")
        assert loaded.source["holding_generation"] == generation
        assert loaded.positions[0].quantity == 10
        assert loaded.available_cash == 900
        assert report_sentinel.read_text(encoding="utf-8") == "frozen-report"
        rerun = submit_confirmed_snapshot(
            broker, payload, account_url=account_url, receipt_path=tmp_path / "receipts" / f"{broker}-rerun.json",
            timeout_seconds=1, poll_seconds=0,
        )
        assert rerun["status"] == "published"
        assert rerun["holding_generation"] == generation
    finally:
        server.shutdown()
        server.server_close()
        api_thread.join(timeout=2)
