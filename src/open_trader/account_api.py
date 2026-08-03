from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Literal, Mapping
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .account_snapshot import load_account_snapshot, load_worker_git_sha
from .account_sync_state import ACCOUNT_STALE_SECONDS, REQUIRED_BROKERS


_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_PARITY_ATTEMPTS = 3
_FUTU_NAIVE_PRICE_TIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\Z"
)
_US_MARKET_TIMEZONE = ZoneInfo("America/New_York")
_SUMMARY_FIELDS = (
    "holding_value_hkd", "cash_like_value_hkd", "portfolio_value_hkd",
    "holding_weight_hkd", "cash_like_weight_hkd", "holding_count", "broker_count",
)
_BROKER_SUMMARY_FIELDS = (
    "broker", "label", "source_kind", "detail_available", "holding_value_hkd",
    "cash_like_value_hkd", "portfolio_value_hkd", "holding_count",
)
_POSITION_FIELDS = (
    "broker", "account_alias", "market", "asset_class", "symbol", "name", "currency",
    "quantity", "cost_price", "cost_value", "last_price", "price_kind", "price_as_of",
    "market_value", "market_value_usd", "market_value_hkd", "cost_value_hkd",
    "unrealized_pnl", "unrealized_pnl_pct", "account_weight_hkd", "portfolio_weight_hkd",
    "statement_id", "confidence", "notes",
)
_CASH_FIELDS = (
    "broker", "account_alias", "currency", "cash_balance", "available_balance",
    "cash_balance_hkd", "available_balance_hkd", "statement_id", "confidence", "notes",
)
_SOURCE_BROKER_FIELDS = ("source_kind", "data_as_of", "last_success_at")
_SNAPSHOT_FIELDS = frozenset({
    "schema_version", "snapshot_generation", "account_generation", "generated_at",
    "quote_as_of", "status", "stale", "sources", "release", "summary",
    "broker_summaries", "positions", "cash_balances", "errors",
})


@dataclass(frozen=True)
class ParityResult:
    status: Literal["PASS", "FAIL", "BLOCKED"]
    reason: str
    account_generation: str
    quote_as_of: str


def create_account_api(
    data_dir: Path,
    *,
    host: str,
    port: int,
    runtime_metadata: Mapping[str, object] | None = None,
) -> ThreadingHTTPServer:
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("host must be a loopback address")
    except ValueError as error:
        raise ValueError("host must be a loopback address") from error
    runtime = dict(runtime_metadata) if runtime_metadata is not None else _runtime_metadata()

    class AccountApiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/healthz":
                worker_sha = load_worker_git_sha(data_dir)
                api_sha = str(runtime["api_git_sha"])
                self._send_json(
                    {
                        "schema_version": "open_trader.account_api.health.v1",
                        "module": "account_api",
                        "status": "ok",
                        "mode": "shadow",
                        "pid": runtime["pid"],
                        "started_at": runtime["started_at"],
                        "api_git_sha": api_sha,
                        "worker_git_sha": worker_sha,
                        "release_match": bool(worker_sha) and worker_sha == api_sha,
                        "source": "account_sync_worker_publication",
                    }
                )
                return
            if path == "/api/v1/account/snapshot":
                result = load_account_snapshot(
                    data_dir,
                    api_git_sha=str(runtime["api_git_sha"]),
                    now=datetime.now().astimezone(),
                )
                if result.etag and self.headers.get("If-None-Match") == result.etag:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", result.etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send_json(result.payload, result.status_code, etag=result.etag)
                return
            self._send_json(
                {
                    "schema_version": "open_trader.account_api.error.v1",
                    "code": "not_found",
                    "message": "Not found",
                },
                HTTPStatus.NOT_FOUND,
            )

        def _send_json(
            self,
            payload: dict[str, object],
            status: int | HTTPStatus = HTTPStatus.OK,
            *,
            etag: str | None = None,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _write(self, body: bytes) -> None:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), AccountApiHandler)
    server.daemon_threads = True
    server.runtime_metadata = runtime  # type: ignore[attr-defined]
    return server


def serve_account_api(data_dir: Path) -> None:
    host = "127.0.0.1"
    port = 8768
    server = create_account_api(data_dir, host=host, port=port)
    runtime = {
        "schema_version": "open_trader.account_api.runtime.v1",
        "module": "account_api",
        "mode": "shadow",
        **server.runtime_metadata,  # type: ignore[attr-defined]
        "host": host,
        "port": server.server_address[1],
    }
    print(
        f"account_api_runtime: {json.dumps(runtime, separators=(',', ':'))}",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader account-api")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    serve_account_api(args.data_dir)
    return 0


def check_account_api_parity(
    data_dir: Path,
    *,
    base_url: str = "http://127.0.0.1:8768",
    attempts: int = _PARITY_ATTEMPTS,
) -> ParityResult:
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    snapshot_url = base_url.rstrip("/") + "/api/v1/account/snapshot"
    for _ in range(min(max(attempts, 1), _PARITY_ATTEMPTS)):
        try:
            account_first = _read_parity_bytes(account_path)
            quotes_first = _read_parity_bytes(quotes_path)
            status, payload, etag = _fetch_snapshot(snapshot_url)
            account_second = _read_parity_bytes(account_path)
            quotes_second = _read_parity_bytes(quotes_path)
        except OSError:
            return ParityResult("FAIL", "api_request_failed", "", "")
        if account_first != account_second or quotes_first != quotes_second:
            continue
        expected = _raw_parity_projection(
            account_first,
            quotes_first,
            now=datetime.now().astimezone(),
        )
        if expected is None:
            return ParityResult("FAIL", "raw_publication_invalid", "", "")
        account_generation = expected["account_generation"]
        quote_as_of = expected["quote_as_of"]
        if status == HTTPStatus.SERVICE_UNAVAILABLE and _is_api_unstable(payload):
            return ParityResult(
                "BLOCKED", "account_publication_unstable", account_generation, quote_as_of
            )
        if status != HTTPStatus.OK:
            return ParityResult("FAIL", f"http_status_{status}", account_generation, quote_as_of)
        reason = _compare_parity_payload(
            payload,
            etag,
            expected,
            worker_git_sha=load_worker_git_sha(data_dir),
        )
        return ParityResult(
            "PASS" if reason is None else "FAIL",
            "ok" if reason is None else reason,
            account_generation,
            quote_as_of,
        )
    return ParityResult("BLOCKED", "publication_changed_during_parity", "", "")


def parity_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader account-api-parity")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    result = check_account_api_parity(args.data_dir)
    print(
        f"{result.status} {result.reason} "
        f"account_generation={result.account_generation} quote_as_of={result.quote_as_of}"
    )
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result.status]


def _read_parity_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _fetch_snapshot(url: str) -> tuple[int, object, str | None]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            try:
                payload = json.load(response)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            return response.status, payload, response.headers.get("ETag")
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return error.code, payload, error.headers.get("ETag")


def _raw_parity_projection(
    account_raw: bytes,
    quotes_raw: bytes,
    *,
    now: datetime,
) -> dict[str, object] | None:
    try:
        account = json.loads(account_raw)
        quotes = json.loads(quotes_raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(account, dict) or not isinstance(quotes, dict):
        return None
    projection = account.get("dashboard_projection")
    brokers = account.get("brokers")
    if not isinstance(projection, dict) or not isinstance(brokers, dict):
        return None
    try:
        summary = _public_fields(projection["summary"], _SUMMARY_FIELDS)
        broker_summaries = sorted(
            (_public_fields(row, _BROKER_SUMMARY_FIELDS) for row in projection["broker_summaries"]),
            key=lambda row: str(row["broker"]),
        )
        positions = sorted(
            (_parity_position(row) for row in projection["broker_positions"]),
            key=lambda row: (
                str(row["broker"]), str(row["account_alias"]), str(row["market"]),
                str(row["asset_class"]), str(row["symbol"]), str(row["position_id"]),
            ),
        )
        cash_balances = sorted(
            (_public_fields(row, _CASH_FIELDS) for row in projection["cash_details"]),
            key=lambda row: (str(row["broker"]), str(row["account_alias"]), str(row["currency"])),
        )
        quote_as_of = quotes["last_success_at"]
        generated_at = projection["generated_at"]
        projection_quote_as_of = projection["quote_as_of"]
        if not isinstance(quote_as_of, str) or not isinstance(generated_at, str):
            return None
        if projection_quote_as_of != quote_as_of:
            return None
        source_brokers = {
            name: _public_fields(source, _SOURCE_BROKER_FIELDS)
            for name, source in brokers.items()
            if isinstance(name, str)
        }
        if set(source_brokers) != set(REQUIRED_BROKERS):
            return None
        accepted_account_as_of = max(
            (str(source["last_success_at"]) for source in source_brokers.values()),
            key=datetime.fromisoformat,
        )
    except (KeyError, TypeError, ValueError):
        return None
    account_generation = _parity_sha({
        "summary": summary,
        "broker_summaries": broker_summaries,
        "positions": positions,
        "cash_balances": cash_balances,
        "accepted_account_as_of": accepted_account_as_of,
        "accepted_broker_data_as_of": {
            broker: source_brokers[broker]["data_as_of"]
            for broker in sorted(source_brokers)
        },
    })
    broker_stale: dict[str, bool] = {}
    for broker in REQUIRED_BROKERS:
        stale = _parity_broker_stale(brokers[broker], now=now)
        if stale is None:
            return None
        broker_stale[broker] = stale
    quotes_status = quotes.get("status")
    if quotes_status not in {"ok", "partial", "failed"}:
        return None
    quotes_stale = quotes_status == "failed"
    account_stale = any(broker_stale.values())
    stale = account_stale or quotes_stale
    sources = {
        "account": {
            "status": "stale" if account_stale else "healthy",
            "as_of": accepted_account_as_of,
            "reason": "broker_refresh_failed" if account_stale else None,
            "brokers": {
                broker: {
                    **source_brokers[broker],
                    "status": "stale" if broker_stale[broker] else "healthy",
                    "reason": "broker_refresh_failed" if broker_stale[broker] else None,
                }
                for broker in sorted(REQUIRED_BROKERS)
            },
        },
        "quotes": {
            "status": "stale" if quotes_stale else "healthy",
            "as_of": quote_as_of,
            "reason": "quotes_refresh_failed" if quotes_stale else None,
        },
    }
    errors = [
        {
            "code": "broker_refresh_failed",
            "source": broker,
            "message": "Latest broker refresh failed; serving last accepted account facts",
            "retryable": True,
        }
        for broker in sorted(REQUIRED_BROKERS)
        if broker_stale[broker]
    ]
    if quotes_stale:
        errors.append({
            "code": "quotes_refresh_failed",
            "source": "quotes",
            "message": "Latest quote refresh failed; serving last accepted quotes",
            "retryable": True,
        })
    return {
        "summary": summary,
        "broker_summaries": broker_summaries,
        "positions": positions,
        "cash_balances": cash_balances,
        "generated_at": generated_at,
        "quote_as_of": quote_as_of,
        "account_as_of": accepted_account_as_of,
        "source_brokers": source_brokers,
        "account_generation": account_generation,
        "status": "stale" if stale else "healthy",
        "stale": stale,
        "sources": sources,
        "errors": errors,
    }


def _public_fields(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("not a mapping")
    return {field: value[field] for field in fields}


def _parity_broker_stale(source: object, *, now: datetime) -> bool | None:
    if not isinstance(source, Mapping):
        return None
    status = source.get("status")
    if status == "failed":
        return True
    if status != "ok":
        return None
    source_kind = source.get("source_kind")
    if source_kind == "statement":
        return False
    if source_kind != "live":
        return None
    last_success_at = source.get("last_success_at")
    if not isinstance(last_success_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(last_success_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return (now - parsed).total_seconds() > ACCOUNT_STALE_SECONDS


def _parity_position(value: object) -> dict[str, object]:
    position = _public_fields(value, _POSITION_FIELDS)
    try:
        position["price_as_of"] = _normalize_parity_price_as_of(
            position["market"], position["price_as_of"]
        )
        instrument_id = "ins_" + hashlib.sha256(json.dumps(
            [
                str(position["market"]).strip().upper(),
                str(position["asset_class"]).strip().lower(),
                str(position["symbol"]).strip().upper(),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        position["instrument_id"] = instrument_id
        position["position_id"] = "pos_" + hashlib.sha256(json.dumps(
            [
                str(position["broker"]).strip().lower(),
                str(position["account_alias"]).strip(),
                instrument_id,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        raise TypeError("invalid position") from None
    return position


def _normalize_parity_price_as_of(market: object, value: object) -> object:
    if (
        market != "US"
        or not isinstance(value, str)
        or _FUTU_NAIVE_PRICE_TIME_RE.fullmatch(value) is None
    ):
        return value
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return value
    return parsed.replace(tzinfo=_US_MARKET_TIMEZONE).isoformat(timespec="milliseconds")


def _parity_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")).hexdigest()


def _compare_parity_payload(
    payload: object,
    etag: str | None,
    expected: Mapping[str, object],
    *,
    worker_git_sha: str,
) -> str | None:
    if not isinstance(payload, dict):
        return "api_payload_invalid"
    if set(payload) != _SNAPSHOT_FIELDS:
        return "envelope_fields_mismatch"
    if payload.get("schema_version") != 1:
        return "schema_version_mismatch"
    if payload.get("status") != expected["status"]:
        return "status_mismatch"
    if payload.get("stale") != expected["stale"]:
        return "stale_mismatch"
    release = payload.get("release")
    if (
        not isinstance(release, Mapping)
        or set(release) != {"api_git_sha", "worker_git_sha"}
        or not _GIT_SHA_RE.fullmatch(str(release.get("api_git_sha", "")))
        or release.get("worker_git_sha") != release.get("api_git_sha")
        or release.get("worker_git_sha") != worker_git_sha
    ):
        return "release_mismatch"
    if not _compare_sources(payload.get("sources"), expected["sources"]):
        return "sources_mismatch"
    if payload.get("errors") != expected["errors"]:
        return "errors_mismatch"
    for field in ("generated_at", "quote_as_of", "summary", "broker_summaries", "cash_balances"):
        expected_value = expected[field]
        observed = payload.get(field)
        if observed != expected_value:
            return f"{field}_mismatch"
    positions = payload.get("positions")
    expected_positions = expected["positions"]
    if not isinstance(positions, list) or positions != expected_positions:
        if isinstance(positions, list) and len(positions) == len(expected_positions):
            for observed, raw in zip(positions, expected_positions):
                if not isinstance(observed, Mapping) or not isinstance(raw, Mapping):
                    break
                for field in ("instrument_id", "position_id", *_POSITION_FIELDS):
                    if observed.get(field) != raw.get(field):
                        return f"{field}_mismatch"
        return "positions_mismatch"
    account_generation = expected["account_generation"]
    if payload.get("account_generation") != account_generation:
        return "account_generation_mismatch"
    snapshot_generation = payload.get("snapshot_generation")
    if not isinstance(snapshot_generation, str):
        return "snapshot_generation_mismatch"
    visible = dict(payload)
    visible.pop("snapshot_generation", None)
    if snapshot_generation != _parity_sha(visible):
        return "snapshot_generation_mismatch"
    expected_etag = f'"account-v1-{snapshot_generation.removeprefix("sha256:")}"'
    if etag is None:
        return "etag_missing"
    if etag != expected_etag:
        return "etag_mismatch"
    return None


def _compare_sources(observed: object, expected: object) -> bool:
    if not isinstance(observed, Mapping) or not isinstance(expected, Mapping):
        return False
    observed_account = observed.get("account")
    expected_account = expected.get("account")
    observed_quotes = observed.get("quotes")
    expected_quotes = expected.get("quotes")
    if (
        not isinstance(observed_account, Mapping)
        or not isinstance(expected_account, Mapping)
        or not isinstance(observed_quotes, Mapping)
        or not isinstance(expected_quotes, Mapping)
    ):
        return False
    for field in ("status", "as_of", "reason"):
        if observed_account.get(field) != expected_account.get(field):
            return False
        if observed_quotes.get(field) != expected_quotes.get(field):
            return False
    observed_brokers = observed_account.get("brokers")
    expected_brokers = expected_account.get("brokers")
    if (
        not isinstance(observed_brokers, Mapping)
        or not isinstance(expected_brokers, Mapping)
        or set(observed_brokers) != set(expected_brokers)
    ):
        return False
    for broker, expected_source in expected_brokers.items():
        observed_source = observed_brokers.get(broker)
        if not isinstance(observed_source, Mapping) or not isinstance(expected_source, Mapping):
            return False
        for field in (*_SOURCE_BROKER_FIELDS, "status", "reason"):
            if observed_source.get(field) != expected_source.get(field):
                return False
    return True


def _is_api_unstable(payload: object) -> bool:
    return (
        isinstance(payload, Mapping)
        and isinstance(payload.get("errors"), list)
        and bool(payload["errors"])
        and isinstance(payload["errors"][0], Mapping)
        and payload["errors"][0].get("code") == "account_publication_unstable"
    )


def _runtime_metadata() -> dict[str, object]:
    cwd = Path.cwd().resolve()
    try:
        api_git_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        api_git_sha = ""
    if _GIT_SHA_RE.fullmatch(api_git_sha) is None:
        api_git_sha = ""
    return {
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
        "cwd": str(cwd),
        "api_git_sha": api_git_sha,
    }
