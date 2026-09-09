from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import date
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any

from .account_http import AccountHttpError, fetch_account_snapshot
from .account_sync_state import STATEMENT_BROKERS, write_json_atomic


DEFAULT_ACCOUNT_URL = "http://127.0.0.1:8768"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8766"
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_POST_ATTEMPTS = 3
_GENERATION_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BROKERS = tuple(STATEMENT_BROKERS)


class _UncertainRequest(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _loopback_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL must be loopback HTTP")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL must be loopback HTTP")
    host = parsed.hostname
    if host is None:
        raise ValueError("URL must be loopback HTTP")
    if host.lower() != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("URL must be loopback HTTP")
        except ValueError as error:
            raise ValueError("URL must be loopback HTTP") from error
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("URL must be loopback HTTP") from error
    return value.rstrip("/")


def _generation(value: object) -> str | None:
    return value if isinstance(value, str) and _GENERATION_RE.fullmatch(value) else None


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        body = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValueError("input is not JSON serializable") from error
    if len(body) > _MAX_INPUT_BYTES:
        raise ValueError("input JSON cannot exceed 1 MiB")
    return body


def _request_json(
    url: str,
    *,
    method: str,
    body: bytes | None = None,
    timeout_seconds: float,
    max_response_bytes: int = _MAX_INPUT_BYTES,
) -> tuple[int, object]:
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            **({
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            } if body is not None else {}),
        },
    )
    try:
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise _UncertainRequest("server response exceeded its size limit")
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(max_response_bytes + 1)
        except OSError:
            raw = b""
        if len(raw) > max_response_bytes:
            raise _UncertainRequest("server response exceeded its size limit")
        status = int(error.code)
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise _UncertainRequest("network request did not complete") from error
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _UncertainRequest("server returned invalid JSON") from error
    return status, payload


def _write_receipt(receipt_path: Path, result: Mapping[str, object]) -> None:
    write_json_atomic(receipt_path, result)


def _base_receipt(
    *, broker: str, request_digest: str, data_as_of: str | None,
) -> dict[str, object]:
    return {
        "schema_version": "open_trader.holding_snapshot_workflow.receipt.v1",
        "broker": broker,
        "data_as_of": data_as_of or "",
        "request_digest": request_digest,
        "holding_generation": "",
        "status": "needs_input",
        "reason": "",
    }


def _finish(
    receipt_path: Path,
    base: Mapping[str, object],
    *,
    status: str,
    reason: str,
    **fields: object,
) -> dict[str, object]:
    result = dict(base)
    result.update(status=status, reason=reason, **fields)
    _write_receipt(receipt_path, result)
    return result


def submit_confirmed_snapshot(
    broker: str,
    payload: Mapping[str, object] | object,
    *,
    account_url: str = DEFAULT_ACCOUNT_URL,
    receipt_path: Path,
    timeout_seconds: float = 120,
    poll_seconds: float = 5,
) -> dict[str, object]:
    """Stage a confirmed snapshot, then wait for the existing worker to publish it."""
    normalized_broker = str(broker).strip().lower()
    request_digest = ""
    data_as_of: str | None = None
    if isinstance(payload, Mapping):
        raw_date = payload.get("data_as_of")
        data_as_of = raw_date if isinstance(raw_date, str) else None
        try:
            body = _json_bytes(payload)
            request_digest = "sha256:" + hashlib.sha256(body).hexdigest()
        except ValueError:
            body = None
    else:
        body = None
    base = _base_receipt(
        broker=normalized_broker, request_digest=request_digest, data_as_of=data_as_of
    )
    receipt_path = Path(receipt_path)
    if normalized_broker not in _BROKERS:
        return _finish(receipt_path, base, status="needs_input", reason="unsupported_broker")
    if not isinstance(payload, Mapping) or payload.get("confirmed") is not True:
        return _finish(receipt_path, base, status="needs_input", reason="confirmation_required")
    if payload.get("complete") is not True:
        return _finish(receipt_path, base, status="needs_input", reason="complete_position_set_required")
    if body is None:
        return _finish(receipt_path, base, status="needs_input", reason="invalid_input_json")
    try:
        account_url = _loopback_url(account_url)
    except ValueError as error:
        raise
    try:
        timeout = float(timeout_seconds)
        poll = float(poll_seconds)
    except (TypeError, ValueError) as error:
        raise ValueError("timeouts must be finite numbers") from error
    if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(poll) or poll < 0:
        raise ValueError("timeouts must be finite numbers")
    deadline = time.monotonic() + timeout
    expected_date = data_as_of
    staged_generation: str | None = None
    last_reason = "submission_unknown"
    for attempt in range(_MAX_POST_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status, response = _request_json(
                f"{account_url}/api/v1/account/holding-snapshots/{normalized_broker}",
                method="POST", body=body, timeout_seconds=max(0.01, remaining),
                max_response_bytes=_MAX_RESPONSE_BYTES,
            )
        except _UncertainRequest:
            last_reason = "submission_unknown"
            if attempt + 1 < _MAX_POST_ATTEMPTS:
                continue
            break
        if status == 202:
            if not isinstance(response, Mapping):
                return _finish(receipt_path, base, status="rejected", reason="invalid_staging_response")
            returned_broker = response.get("broker")
            returned_date = response.get("data_as_of")
            returned_generation = _generation(response.get("holding_generation"))
            if returned_broker != normalized_broker:
                return _finish(receipt_path, base, status="rejected", reason="staging_broker_mismatch")
            if returned_date != expected_date:
                return _finish(receipt_path, base, status="rejected", reason="staging_date_mismatch")
            if returned_generation is None:
                return _finish(receipt_path, base, status="rejected", reason="staging_generation_invalid")
            staged_generation = returned_generation
            break
        if 400 <= status < 500:
            return _finish(receipt_path, base, status="rejected", reason="server_validation_rejected")
        if status >= 500:
            last_reason = "submission_unknown"
            continue
        return _finish(receipt_path, base, status="submission_unknown", reason="unexpected_submission_status")
    if staged_generation is None:
        return _finish(receipt_path, base, status="submission_unknown", reason=last_reason)

    pending_base = dict(base)
    pending_base.update(
        holding_generation=staged_generation,
        expected_generation=staged_generation,
        current_generation="",
    )
    _write_receipt(receipt_path, {**pending_base, "status": "pending", "reason": "awaiting_account_publication"})
    current_generation = ""
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            snapshot = fetch_account_snapshot(
                account_url, timeout_seconds=max(0.01, remaining), opener=_OPENER
            )
        except Exception:
            snapshot = None
        if isinstance(snapshot, Mapping):
            accepted = snapshot.get("accepted_holding_generation")
            if isinstance(accepted, Mapping):
                value = accepted.get(normalized_broker)
                current_generation = value if isinstance(value, str) else ""
            sources = snapshot.get("sources")
            account_source = sources.get("account") if isinstance(sources, Mapping) else None
            brokers = account_source.get("brokers") if isinstance(account_source, Mapping) else None
            broker_source = brokers.get(normalized_broker) if isinstance(brokers, Mapping) else None
            source_status = str(broker_source.get("status") or "").lower() if isinstance(broker_source, Mapping) else ""
            if current_generation == staged_generation and source_status in {"healthy", "ok"}:
                return _finish(
                    receipt_path, pending_base, status="published", reason="accepted_generation_confirmed",
                    current_generation=current_generation,
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll, remaining))
    return _finish(
        receipt_path, pending_base, status="pending", reason="accepted_generation_not_observed",
        current_generation=current_generation,
    )


def _report_source(report: Mapping[str, object]) -> Mapping[str, object]:
    source = report.get("real_position_source") or report.get("real_holdings_source")
    return source if isinstance(source, Mapping) else {}


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _lineage_status(
    *, latest_generation: str | None, report_generation: str | None,
    latest_date: str | None, report_date: str | None,
) -> str:
    if latest_generation and report_generation:
        return "matched" if latest_generation == report_generation else "pending"
    if latest_date and report_date and latest_date > report_date:
        return "pending"
    return "unknown"


def check_workflow_status(
    account_snapshot: Mapping[str, object] | object,
    dashboard: Mapping[str, object] | object,
    *,
    expected_date: str | None = None,
) -> dict[str, object]:
    """Return stable, non-volatile lineage and controller health facts."""
    expected = _iso_date(expected_date) if expected_date is not None else None
    if expected_date is not None and expected is None:
        raise ValueError("expected_date must be an ISO date")
    account = account_snapshot if isinstance(account_snapshot, Mapping) else {}
    board = dashboard if isinstance(dashboard, Mapping) else {}
    accepted = account.get("accepted_holding_generation")
    sources = account.get("sources")
    account_source = sources.get("account") if isinstance(sources, Mapping) else None
    broker_sources = account_source.get("brokers") if isinstance(account_source, Mapping) else None
    reports = board.get("trend_reports")
    controllers = board.get("trend_controllers")
    results: dict[str, dict[str, object]] = {}
    issues: set[str] = set()
    for broker in _BROKERS:
        source = broker_sources.get(broker) if isinstance(broker_sources, Mapping) else None
        broker_source = source if isinstance(source, Mapping) else {}
        report_value = reports.get(broker) if isinstance(reports, Mapping) else None
        report = report_value if isinstance(report_value, Mapping) else {}
        latest_date = _iso_date(broker_source.get("data_as_of") or broker_source.get("as_of"))
        latest_generation = _generation(accepted.get(broker)) if isinstance(accepted, Mapping) else None
        report_source = _report_source(report)
        report_date = _iso_date(report_source.get("snapshot_period") or report_source.get("data_as_of"))
        report_generation = _generation(report_source.get("holding_generation"))
        report_data_date = _iso_date(report.get("data_date"))
        broker_issues: set[str] = set()
        source_status = str(broker_source.get("status") or "").lower()
        account_available = source_status in {"healthy", "ok"}
        if not account_available:
            broker_issues.add("account_unavailable")
            issues.add("account_unavailable")
        report_available = report.get("available") is True
        if not report_available:
            broker_issues.add("report_unavailable")
            issues.add("report_unavailable")
            lineage = "report_unavailable"
        elif not account_available:
            lineage = "unavailable"
        else:
            lineage = _lineage_status(
                latest_generation=latest_generation,
                report_generation=report_generation,
                latest_date=latest_date,
                report_date=report_date,
            )
            if lineage == "pending":
                issues.add("holdings_pending")
        controller_value = controllers.get(broker) if isinstance(controllers, Mapping) else None
        controller = controller_value if isinstance(controller_value, Mapping) else {}
        health = str(controller.get("health") or "").lower()
        readonly_nonblocking = health == "readonly" and controller.get("blocking") is not True
        if health not in {"healthy", "ok", "readonly"}:
            broker_issues.add("controller_unavailable")
            issues.add("controller_unavailable")
        if controller.get("blocking") is True:
            broker_issues.add("controller_blocking")
            issues.add("controller_blocking")
        controller_reason = ""
        if health not in {"healthy", "ok"} and not readonly_nonblocking:
            raw_reason = controller.get("reason")
            if isinstance(raw_reason, str):
                controller_reason = raw_reason.strip()
        holiday = health in {"healthy", "ok"} and str(controller.get("phase") or "").lower() in {"holiday", "market_holiday", "holiday_skip"}
        if expected is not None and not holiday:
            if latest_date is None or latest_date < expected:
                broker_issues.add("holding_input_overdue")
                issues.add("holding_input_overdue")
            if report_data_date is None or report_data_date < expected:
                broker_issues.add("report_overdue")
                issues.add("report_overdue")
        results[broker] = {
            "holding_date": latest_date or "",
            "holding_generation": latest_generation or "",
            "report_holding_date": report_date or "",
            "report_holding_generation": report_generation or "",
            "report_data_date": report_data_date or "",
            "lineage_status": lineage,
            "controller_health": health or "unavailable",
            "controller_reason": controller_reason,
            "issues": sorted(broker_issues),
        }
    if any(item["lineage_status"] in {"pending", "unavailable", "report_unavailable"} for item in results.values()) or issues:
        overall = "pending" if "account_unavailable" not in issues or "controller_unavailable" in issues else "unavailable"
    elif all(item["lineage_status"] == "matched" for item in results.values()):
        overall = "matched"
    else:
        overall = "unknown"
    return {"status": overall, "brokers": results, "issues": sorted(issues)}


def _read_dashboard(url: str, timeout_seconds: float) -> Mapping[str, object]:
    status, payload = _request_json(
        f"{_loopback_url(url)}/api/dashboard",
        method="GET",
        timeout_seconds=timeout_seconds,
        max_response_bytes=_MAX_RESPONSE_BYTES,
    )
    if status != 200 or not isinstance(payload, Mapping):
        raise _UncertainRequest("dashboard unavailable")
    return payload


def _cli_submit(args: argparse.Namespace) -> int:
    input_path = Path(args.input).resolve()
    receipt_path = Path(args.receipt).resolve()
    if input_path == receipt_path:
        result = {"status": "needs_input", "reason": "input_and_receipt_paths_must_differ"}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    try:
        raw = input_path.read_bytes()
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValueError("input JSON cannot exceed 1 MiB")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        result = {"status": "needs_input", "reason": "invalid_input_json"}
        _write_receipt(receipt_path, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    result = submit_confirmed_snapshot(
        args.broker, payload, account_url=args.account_url, receipt_path=receipt_path,
        timeout_seconds=args.timeout_seconds, poll_seconds=args.poll_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "published" else 1


def _cli_check(args: argparse.Namespace) -> int:
    try:
        account_url = _loopback_url(args.account_url)
        dashboard_url = _loopback_url(args.dashboard_url)
        if args.expected_date is not None and _iso_date(args.expected_date) is None:
            raise ValueError("expected_date must be an ISO date")
    except ValueError:
        print(json.dumps({"status": "invalid_input", "issues": ["invalid_arguments"]}, sort_keys=True))
        return 2
    try:
        account = fetch_account_snapshot(account_url, timeout_seconds=10, opener=_OPENER)
        dashboard = _read_dashboard(dashboard_url, 10)
        result = check_workflow_status(account, dashboard, expected_date=args.expected_date)
    except (AccountHttpError, _UncertainRequest, OSError, TimeoutError, urllib.error.URLError):
        result = {"status": "unavailable", "issues": ["account_or_dashboard_unavailable"]}
    except ValueError:
        print(json.dumps({"status": "invalid_input", "issues": ["invalid_arguments"]}, sort_keys=True))
        return 2
    except Exception:
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m open_trader.holding_snapshot_workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit")
    submit.add_argument("--broker", choices=_BROKERS, required=True)
    submit.add_argument("--input", required=True)
    submit.add_argument("--receipt", required=True)
    submit.add_argument("--account-url", default=DEFAULT_ACCOUNT_URL)
    submit.add_argument("--timeout-seconds", type=float, default=120)
    submit.add_argument("--poll-seconds", type=float, default=5)
    check = subparsers.add_parser("check")
    check.add_argument("--account-url", default=DEFAULT_ACCOUNT_URL)
    check.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    check.add_argument("--expected-date")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "submit":
        return _cli_submit(args)
    return _cli_check(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
