from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
from http import HTTPStatus
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

import open_trader.account_api as account_api
import open_trader.account_snapshot as account_snapshot
from open_trader.account_snapshot import (
    build_instrument_id,
    build_position_id,
    load_account_snapshot,
)
from open_trader.account_sync_state import (
    LIVE_BROKERS,
    REQUIRED_BROKERS,
    BrokerAccountCandidate,
    accept_candidate,
    empty_account_sync_state,
    record_source_failure,
    with_dashboard_projection,
    write_json_atomic,
)
from open_trader.models import AssetClass, CashBalance, Market, Position


SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime.fromisoformat("2026-08-03T12:00:05+08:00")


@contextmanager
def _running(server: ThreadingHTTPServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url) as response:
        return json.load(response)


def _contract_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _write_publication(
    data_dir: Path,
    *,
    worker_sha: str = SHA,
    quote_price_time: str | None = None,
) -> None:
    account_as_of = "2026-08-03T12:00:00+08:00"
    quote_as_of = "2026-08-03T12:00:04+08:00"
    quote_price_time = quote_price_time or quote_as_of
    state = empty_account_sync_state()
    for index, broker in enumerate(REQUIRED_BROKERS):
        live = broker in LIVE_BROKERS
        alias = f"{broker}_main"
        state = accept_candidate(
            state,
            BrokerAccountCandidate(
                broker=broker,
                source_kind="live" if live else "statement",
                data_as_of=account_as_of if live else "2026-07-31",
                period="2026-08" if live else "2026-07",
                positions=(
                    Position(
                        statement_id="" if live else f"2026-07-31-{broker}",
                        broker=broker,
                        account_alias=alias,
                        market=Market.US,
                        asset_class=AssetClass.STOCK,
                        symbol=f"TEST{index}",
                        name=f"Test {index}",
                        currency="USD",
                        quantity=Decimal("1"),
                        cost_price=Decimal("10"),
                        last_price=Decimal("11"),
                        market_value=Decimal("11"),
                        cost_value=Decimal("10"),
                        unrealized_pnl=Decimal("1"),
                        confidence="high",
                        notes="",
                    ),
                ),
                cash=(
                    CashBalance(
                        statement_id="" if live else f"2026-07-31-{broker}",
                        broker=broker,
                        account_alias=alias,
                        currency="USD",
                        cash_balance=Decimal("5"),
                        available_balance=Decimal("4"),
                        confidence="high",
                        notes="",
                    ),
                ),
                fx_rates=(
                    {"account_alias": alias, "currency": "USD", "rate_to_hkd": "7.8"},
                ) if live else (),
                summary={"position_count": 1, "cash_count": 1},
            ),
            attempted_at=account_as_of,
        )
    quotes = {
        "status": "ok",
        "requested_count": 2,
        "quote_count": 2,
        "missing_count": 0,
        "fetched_at": quote_as_of,
        "last_success_at": quote_as_of,
        "stale": False,
        "quotes": {
            f"US.TEST{index}": {
                "market": "US",
                "symbol": f"TEST{index}",
                "status": "ok",
                "last_price": "11",
                "price_session": "regular",
                "price_time": quote_price_time,
                "fetched_at": quote_as_of,
                "stale": False,
            }
            for index in (0, 1)
        },
        "diagnostic": {},
    }
    state = with_dashboard_projection(state, quotes, generated_at=quote_as_of)
    write_json_atomic(data_dir / "latest/account_sync_state.json", state)
    write_json_atomic(data_dir / "latest/quotes.json", quotes)
    write_json_atomic(
        data_dir / "account_sync/controller_status.json",
        {
            "schema_version": "open_trader.account_sync.controller.v1",
            "pid": 123,
            "started_at": account_as_of,
            "working_directory": "/tmp/open-trader",
            "git_sha": worker_sha,
            "heartbeat_at": quote_as_of,
            "phase": "idle",
            "account_loop": {"status": "ok"},
            "quote_loop": {"status": "ok"},
            "blocker": None,
        },
    )


def test_account_api_health_snapshot_etag_and_not_found(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    server = account_api.create_account_api(
        data_dir,
        host="127.0.0.1",
        port=0,
        runtime_metadata={
            "pid": 321,
            "started_at": "2026-08-03T12:00:00+08:00",
            "cwd": "/tmp/open-trader",
            "api_git_sha": SHA,
        },
    )
    with _running(server):
        base = f"http://127.0.0.1:{server.server_address[1]}"
        health = _get_json(base + "/healthz")
        assert health["module"] == "account_api"
        assert health["mode"] == "shadow"
        assert health["release_match"] is True
        with urllib.request.urlopen(base + "/api/v1/account/snapshot") as response:
            payload = json.load(response)
            etag = response.headers["ETag"]
            assert response.headers.get("Access-Control-Allow-Origin") is None
        request = urllib.request.Request(
            base + "/api/v1/account/snapshot",
            headers={"If-None-Match": etag},
        )
        with pytest.raises(urllib.error.HTTPError) as unchanged:
            urllib.request.urlopen(request)
        assert unchanged.value.code == HTTPStatus.NOT_MODIFIED
        assert unchanged.value.read() == b""
        assert payload["snapshot_generation"].removeprefix("sha256:") in etag
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/api/unknown")
        assert missing.value.code == HTTPStatus.NOT_FOUND
        assert json.load(missing.value)["code"] == "not_found"


def test_account_api_health_is_live_without_a_matching_worker_release(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir, worker_sha="f" * 40)
    server = account_api.create_account_api(
        data_dir,
        host="127.0.0.1",
        port=0,
        runtime_metadata={
            "pid": 321,
            "started_at": "2026-08-03T12:00:00+08:00",
            "cwd": "/tmp/open-trader",
            "api_git_sha": SHA,
        },
    )
    with _running(server):
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert _get_json(base + "/healthz")["release_match"] is False
        with pytest.raises(urllib.error.HTTPError) as snapshot:
            urllib.request.urlopen(base + "/api/v1/account/snapshot")
        assert snapshot.value.code == HTTPStatus.SERVICE_UNAVAILABLE


def test_account_api_rejects_non_loopback_listener(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        account_api.create_account_api(tmp_path, host="0.0.0.0", port=8768)


def test_account_api_entrypoint_does_not_import_domain_adapters() -> None:
    script = '''
import runpy, sys
sys.argv = ["open_trader", "account-api", "--help"]
try:
    runpy.run_module("open_trader", run_name="__main__")
except SystemExit as error:
    assert error.code == 0
print("LOADED_MODULES")
print("\\n".join(sorted(sys.modules)))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = completed.stdout.split("LOADED_MODULES\n", 1)[1].splitlines()
    for forbidden in (
        "open_trader.account_sync_worker",
        "open_trader.frontend_gateway",
        "open_trader.dashboard_web",
        "open_trader.futu_account",
        "open_trader.tiger_account",
        "open_trader.futu_quote",
        "open_trader.pipeline",
        "open_trader.trend_review",
        "open_trader.trend_kelly",
    ):
        assert forbidden not in loaded


def test_account_api_parity_entrypoint_does_not_import_domain_adapters() -> None:
    script = '''
import runpy, sys
sys.argv = ["open_trader", "account-api-parity", "--help"]
try:
    runpy.run_module("open_trader", run_name="__main__")
except SystemExit as error:
    assert error.code == 0
print("LOADED_MODULES")
print("\\n".join(sorted(sys.modules)))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = completed.stdout.split("LOADED_MODULES\n", 1)[1].splitlines()
    for forbidden in (
        "open_trader.account_sync_worker",
        "open_trader.frontend_gateway",
        "open_trader.dashboard_web",
        "open_trader.futu_account",
        "open_trader.tiger_account",
        "open_trader.futu_quote",
        "open_trader.pipeline",
        "open_trader.trend_review",
        "open_trader.trend_kelly",
    ):
        assert forbidden not in loaded


def test_account_api_cli_uses_only_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    observed: list[Path] = []
    monkeypatch.setattr(account_api, "serve_account_api", observed.append)

    assert account_api.main(["--data-dir", str(data_dir)]) == 0

    assert observed == [data_dir]


def test_live_parity_passes_against_raw_publication(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir, quote_price_time="2026-08-03 04:18:41.889")
    server = account_api.create_account_api(
        data_dir,
        host="127.0.0.1",
        port=0,
        runtime_metadata={
            "pid": 1,
            "started_at": NOW.isoformat(),
            "cwd": str(tmp_path),
            "api_git_sha": SHA,
        },
    )

    with _running(server):
        result = account_api.check_account_api_parity(
            data_dir,
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
        )

    assert result.status == "PASS"
    assert result.reason == "ok"
    assert result.account_generation.startswith("sha256:")
    assert result.quote_as_of == "2026-08-03T12:00:04+08:00"


def test_live_parity_fails_on_wrong_opaque_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    _make_live_sources_fresh(data_dir)
    snapshot = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)
    payload = dict(snapshot.payload)
    positions = list(payload["positions"])
    positions[0] = {**positions[0], "position_id": "pos_wrong"}
    payload["positions"] = positions
    monkeypatch.setattr(
        account_api, "_fetch_snapshot", lambda _url: (200, payload, snapshot.etag),
        raising=False,
    )

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "FAIL"
    assert result.reason == "position_id_mismatch"


def _resign_snapshot_payload(payload: dict[str, object]) -> str:
    visible = dict(payload)
    visible.pop("snapshot_generation", None)
    generation = _contract_sha(visible)
    payload["snapshot_generation"] = generation
    return f'"account-v1-{generation.removeprefix("sha256:")}"'


def _make_live_sources_fresh(data_dir: Path) -> None:
    path = data_dir / "latest/account_sync_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for broker in LIVE_BROKERS:
        state["brokers"][broker]["attempted_at"] = "2099-01-01T00:00:00+00:00"
        state["brokers"][broker]["last_success_at"] = "2099-01-01T00:00:00+00:00"
    write_json_atomic(path, state)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda payload: payload.update({"forged": True}), "envelope_fields_mismatch"),
        (lambda payload: payload.update({"schema_version": 2}), "schema_version_mismatch"),
        (lambda payload: payload.update({"status": "stale"}), "status_mismatch"),
        (lambda payload: payload.update({"stale": True}), "stale_mismatch"),
        (
            lambda payload: payload.update(
                {"release": {"api_git_sha": "f" * 40, "worker_git_sha": "f" * 40}}
            ),
            "release_mismatch",
        ),
        (
            lambda payload: payload["sources"]["account"].update({"status": "stale"}),
            "sources_mismatch",
        ),
        (
            lambda payload: payload["sources"]["account"].update({"forged": True}),
            "sources_mismatch",
        ),
        (
            lambda payload: payload["sources"].update({"forged": True}),
            "sources_mismatch",
        ),
        (
            lambda payload: payload["sources"]["quotes"].pop("reason"),
            "sources_mismatch",
        ),
        (
            lambda payload: payload["sources"]["account"]["brokers"]["futu"].update(
                {"forged": True}
            ),
            "sources_mismatch",
        ),
        (
            lambda payload: payload.update(
                {
                    "errors": [{
                        "code": "forged_error",
                        "source": "account",
                        "message": "forged",
                        "retryable": True,
                    }]
                }
            ),
            "errors_mismatch",
        ),
    ],
)
def test_live_parity_rejects_resigned_v1_envelope_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    reason: str,
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    _make_live_sources_fresh(data_dir)
    snapshot = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)
    payload = json.loads(json.dumps(snapshot.payload))
    mutation(payload)
    etag = _resign_snapshot_payload(payload)
    monkeypatch.setattr(
        account_api, "_fetch_snapshot", lambda _url: (200, payload, etag),
        raising=False,
    )

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "FAIL"
    assert result.reason == reason


def test_live_parity_fails_closed_on_malformed_success_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)

    class MalformedResponse(io.BytesIO):
        status = HTTPStatus.OK
        headers: dict[str, str] = {}

        def __enter__(self) -> "MalformedResponse":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        account_api.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: MalformedResponse(b"{"),
    )

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "FAIL"
    assert result.reason == "api_payload_invalid"


def test_live_parity_blocks_on_complete_unstable_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    payload = {
        "schema_version": 1,
        "status": "unavailable",
        "release": {"api_git_sha": SHA, "worker_git_sha": SHA},
        "errors": [{
            "code": "account_publication_unstable",
            "source": "account",
            "message": "Account publication is unstable",
            "retryable": True,
        }],
    }
    monkeypatch.setattr(
        account_api, "_fetch_snapshot", lambda _url: (503, payload, None), raising=False
    )

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "BLOCKED"
    assert result.reason == "account_publication_unstable"


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"code": "account_publication_unstable"}]},
        {
            "schema_version": 1,
            "status": "unavailable",
            "release": {"api_git_sha": SHA, "worker_git_sha": SHA},
            "errors": [{"code": "account_publication_unstable"}],
        },
        {
            "schema_version": 1,
            "status": "unavailable",
            "release": {"api_git_sha": SHA, "worker_git_sha": SHA},
            "errors": [{
                "code": "account_publication_unstable",
                "source": "quotes",
                "message": "wrong",
                "retryable": True,
            }],
        },
    ],
)
def test_live_parity_fails_on_malformed_unstable_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    monkeypatch.setattr(
        account_api, "_fetch_snapshot", lambda _url: (503, payload, None), raising=False
    )

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "FAIL"
    assert result.reason == "http_status_503"


def test_live_parity_blocks_when_raw_publication_never_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    real_read = Path.read_bytes
    counter = 0

    def changing(path: Path) -> bytes:
        nonlocal counter
        body = real_read(path)
        if path.name == "account_sync_state.json":
            counter += 1
            return body + str(counter).encode()
        return body

    monkeypatch.setattr(account_api, "_read_parity_bytes", changing, raising=False)

    result = account_api.check_account_api_parity(data_dir)

    assert result.status == "BLOCKED"
    assert result.reason == "publication_changed_during_parity"


@pytest.mark.parametrize(
    ("result", "expected_exit"),
    [
        ("PASS", 0),
        ("FAIL", 1),
        ("BLOCKED", 2),
    ],
)
def test_account_api_parity_cli_reports_status_and_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: str,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(
        account_api,
        "check_account_api_parity",
        lambda data_dir: account_api.ParityResult(result, "test_reason", "sha256:test", "quote"),
        raising=False,
    )

    assert account_api.parity_main(["--data-dir", str(tmp_path / "data")]) == expected_exit
    assert capsys.readouterr().out.split()[0] == result


def _rewrite_json(path: Path, updates: dict[str, object]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload.update(updates)
    write_json_atomic(path, payload)


def test_snapshot_maps_current_publication_to_frozen_v1_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["schema_version"] == 1
    assert result.payload["status"] == "healthy"
    assert result.payload["stale"] is False
    assert result.payload["generated_at"] == "2026-08-03T12:00:04+08:00"
    assert result.payload["quote_as_of"] == "2026-08-03T12:00:04+08:00"
    assert result.payload["release"] == {"api_git_sha": SHA, "worker_git_sha": SHA}
    assert result.payload["sources"]["account"]["as_of"] == "2026-08-03T12:00:00+08:00"
    assert [row["broker"] for row in result.payload["broker_summaries"]] == sorted(REQUIRED_BROKERS)
    assert result.payload["positions"] == sorted(
        result.payload["positions"],
        key=lambda row: (
            row["broker"], row["account_alias"], row["market"],
            row["asset_class"], row["symbol"], row["position_id"],
        ),
    )
    assert result.payload["cash_balances"] == sorted(
        result.payload["cash_balances"],
        key=lambda row: (row["broker"], row["account_alias"], row["currency"]),
    )
    position = next(row for row in result.payload["positions"] if row["broker"] == "futu")
    canonical = json.dumps(["US", "stock", "TEST0"], ensure_ascii=False, separators=(",", ":"))
    instrument_id = "ins_" + hashlib.sha256(canonical.encode()).hexdigest()
    assert position["instrument_id"] == instrument_id
    assert position["position_id"] == "pos_" + hashlib.sha256(
        json.dumps(["futu", "futu_main", instrument_id], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert result.payload["account_generation"].startswith("sha256:")
    assert result.payload["snapshot_generation"].startswith("sha256:")
    assert result.etag == f'"account-v1-{result.payload["snapshot_generation"].removeprefix("sha256:")}"'
    account_input = {
        "summary": result.payload["summary"],
        "broker_summaries": result.payload["broker_summaries"],
        "positions": result.payload["positions"],
        "cash_balances": result.payload["cash_balances"],
        "accepted_account_as_of": result.payload["sources"]["account"]["as_of"],
        "accepted_broker_data_as_of": {
            broker: result.payload["sources"]["account"]["brokers"][broker]["data_as_of"]
            for broker in sorted(REQUIRED_BROKERS)
        },
    }
    assert result.payload["account_generation"] == _contract_sha(account_input)
    visible = dict(result.payload)
    visible.pop("snapshot_generation")
    assert result.payload["snapshot_generation"] == _contract_sha(visible)
    assert not ({"risk_flag", "actionable", "decision_plan"} & result.payload.keys())


def test_public_stable_id_helpers_match_position_rows() -> None:
    position = account_snapshot._position_row({
        "broker": "FUTU",
        "account_alias": "futu_main",
        "market": "US",
        "asset_class": "OPTION",
        "symbol": "VIXY260821C22000",
        "name": "VIXY option",
        "currency": "USD",
        "quantity": "1",
        "cost_price": "1",
        "cost_value": "1",
        "last_price": "1",
        "price_kind": "live",
        "price_as_of": "2026-08-03T12:00:04+08:00",
        "market_value": "1",
        "market_value_usd": "1",
        "market_value_hkd": "7.8",
        "cost_value_hkd": "7.8",
        "unrealized_pnl": "0",
        "unrealized_pnl_pct": "0",
        "account_weight_hkd": "1",
        "portfolio_weight_hkd": "1",
        "statement_id": "",
        "confidence": "high",
        "notes": "",
    })

    instrument_id = build_instrument_id("us", "OPTION", " vixy260821c22000 ")

    assert instrument_id == position["instrument_id"]
    assert build_position_id("FUTU", "futu_main", instrument_id) == position["position_id"]
    assert instrument_id == build_instrument_id(" US ", " option ", " VIXY260821C22000 ")
    assert instrument_id != build_instrument_id("us", "STOCK", " vixy260821c22000 ")


def test_snapshot_whitelists_public_fields_and_requires_quote_publication_time(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    state_path = data_dir / "latest/account_sync_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    projection = state["dashboard_projection"]
    projection["summary"]["risk_flag"] = "overweight"
    projection["broker_summaries"][0]["actionable"] = True
    projection["broker_positions"][0]["decision_plan"] = {"action": "buy"}
    projection["cash_details"][0]["research"] = "private"
    write_json_atomic(state_path, state)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert "risk_flag" not in result.payload["summary"]
    assert "actionable" not in result.payload["broker_summaries"][0]
    assert "decision_plan" not in result.payload["positions"][0]
    assert "research" not in result.payload["cash_balances"][0]

    quotes_path = data_dir / "latest/quotes.json"
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    quotes["last_success_at"] = "2026-08-03T12:00:03+08:00"
    write_json_atomic(quotes_path, quotes)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.payload["errors"][0]["code"] == "account_publication_unstable"


def test_snapshot_does_not_require_quotes_for_cash_like_live_positions(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    state_path = data_dir / "latest/account_sync_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    quotes = json.loads((data_dir / "latest/quotes.json").read_text(encoding="utf-8"))
    for broker, market, symbol, asset_class in (
        ("futu", "CASH", "FUTU_UNMAPPED_ASSETS", "cash"),
        ("tiger", "HK", "HK0000951506.HKD", "money_market_fund"),
    ):
        source = state["brokers"][broker]
        position = dict(source["positions"][0])
        position.update({
            "market": market,
            "symbol": symbol,
            "asset_class": asset_class,
        })
        source["positions"].append(position)
    state = with_dashboard_projection(
        state,
        quotes,
        generated_at=quotes["last_success_at"],
    )
    write_json_atomic(state_path, state)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda data: (data / "latest/account_sync_state.json").unlink(), "account_publication_missing"),
        (lambda data: (data / "latest/account_sync_state.json").write_text("{bad", encoding="utf-8"), "account_publication_invalid"),
        (lambda data: _rewrite_json(data / "latest/account_sync_state.json", {"version": 2}), "account_schema_unsupported"),
        (lambda data: (data / "latest/quotes.json").unlink(), "quotes_publication_missing"),
        (lambda data: (data / "latest/quotes.json").write_text("[]", encoding="utf-8"), "quotes_publication_invalid"),
    ],
)
def test_snapshot_returns_contract_503_for_invalid_publication(
    tmp_path: Path, mutate, code: str
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    mutate(data_dir)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload == {
        "schema_version": 1,
        "status": "unavailable",
        "release": {
            "api_git_sha": SHA,
            "worker_git_sha": SHA
            if code in {"account_publication_invalid", "account_schema_unsupported", "quotes_publication_invalid"}
            else "",
        },
        "errors": [{
            "code": code,
            "source": "account" if code.startswith("account_") else "quotes",
            "message": "Account publication is unavailable" if code.startswith("account_") else "Quotes publication is unavailable",
            "retryable": True,
        }],
    }


def test_snapshot_returns_release_mismatch_without_worker_details(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    worker_sha = "f" * 40
    _write_publication(data_dir, worker_sha=worker_sha)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload == {
        "schema_version": 1,
        "status": "unavailable",
        "release": {"api_git_sha": SHA, "worker_git_sha": worker_sha},
        "errors": [{
            "code": "account_release_mismatch",
            "source": "release",
            "message": "Account API and Account Sync Worker releases differ",
            "retryable": True,
        }],
    }


def test_snapshot_rejects_incomplete_worker_heartbeat(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    write_json_atomic(
        data_dir / "account_sync/controller_status.json",
        {
            "schema_version": "open_trader.account_sync.controller.v1",
            "git_sha": SHA,
        },
    )

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "account_release_mismatch"


def test_snapshot_returns_stale_with_retained_broker_facts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    before = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)
    path = data_dir / "latest/account_sync_state.json"
    account = json.loads(path.read_text(encoding="utf-8"))
    account = record_source_failure(
        account,
        "futu",
        attempted_at="2026-08-03T12:00:03+08:00",
        message="secret upstream response",
    )
    account["dashboard_projection"] = json.loads(path.read_text(encoding="utf-8"))["dashboard_projection"]
    write_json_atomic(path, account)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["status"] == "stale"
    assert result.payload["stale"] is True
    assert result.payload["account_generation"] == before.payload["account_generation"]
    assert result.payload["errors"] == [{
        "code": "broker_refresh_failed",
        "source": "futu",
        "message": "Latest broker refresh failed; serving last accepted account facts",
        "retryable": True,
    }]
    assert "secret" not in json.dumps(result.payload)


def test_snapshot_returns_stale_with_complete_retained_quotes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    quotes_path = data_dir / "latest/quotes.json"
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    quotes["status"] = "failed"
    quotes["requested_count"] = 0
    quotes["quote_count"] = 0
    quotes["missing_count"] = 0
    quotes["stale"] = True
    write_json_atomic(quotes_path, quotes)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["status"] == "stale"
    assert result.payload["sources"]["quotes"] == {
        "status": "stale",
        "as_of": "2026-08-03T12:00:04+08:00",
        "reason": "quotes_refresh_failed",
    }
    assert result.payload["errors"] == [{
        "code": "quotes_refresh_failed",
        "source": "quotes",
        "message": "Latest quote refresh failed; serving last accepted quotes",
        "retryable": True,
    }]


def test_snapshot_keeps_complete_partial_quotes_healthy(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    _rewrite_json(data_dir / "latest/quotes.json", {"status": "partial"})

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["status"] == "healthy"
    assert result.payload["sources"]["quotes"]["status"] == "healthy"
    assert result.payload["errors"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "partial", "quote_count": 1, "missing_count": 1},
        {"status": "failed", "quotes": {}},
    ],
)
def test_snapshot_rejects_incomplete_retained_quotes(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    quotes_path = data_dir / "latest/quotes.json"
    if updates["status"] == "partial":
        quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
        quotes.update(updates)
        quotes["quotes"]["US.TEST0"].update(
            {"status": "missing_quote", "last_price": "", "price_time": ""}
        )
        write_json_atomic(quotes_path, quotes)
    else:
        _rewrite_json(quotes_path, updates)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "quotes_publication_missing"


def test_snapshot_rejects_quotes_without_an_accepted_publication(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    account = json.loads(account_path.read_text(encoding="utf-8"))
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    account["dashboard_projection"]["quote_as_of"] = ""
    quotes["status"] = "partial"
    quotes["last_success_at"] = ""
    write_json_atomic(account_path, account)
    write_json_atomic(quotes_path, quotes)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "quotes_publication_missing"


def test_snapshot_classifies_malformed_quote_rows_as_invalid(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    _rewrite_json(data_dir / "latest/quotes.json", {"quotes": {"US.TEST0": []}})

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "quotes_publication_invalid"
    assert result.payload["release"]["worker_git_sha"] == SHA


def test_snapshot_rejects_nonfinite_quote_price(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    quotes_path = data_dir / "latest/quotes.json"
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    quotes["quotes"]["US.TEST0"]["last_price"] = "NaN"
    write_json_atomic(quotes_path, quotes)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "quotes_publication_invalid"


def test_snapshot_rejects_non_aware_quote_fetched_at(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    _rewrite_json(
        data_dir / "latest/quotes.json",
        {"fetched_at": "not-an-iso-timestamp"},
    )

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "quotes_publication_invalid"


def test_snapshot_accepts_futu_active_us_price_time(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    price_time = "2026-08-03 04:18:41.889"
    _write_publication(data_dir, quote_price_time=price_time)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["status"] == "healthy"
    futu_position = next(
        row for row in result.payload["positions"] if row["broker"] == "futu"
    )
    assert futu_position["price_as_of"] == "2026-08-03T04:18:41.889-04:00"
    parsed_price_as_of = datetime.fromisoformat(futu_position["price_as_of"])
    assert parsed_price_as_of.utcoffset() is not None
    statement_position = next(
        row for row in result.payload["positions"] if row["broker"] == "phillips"
    )
    assert statement_position["price_as_of"] == "2026-07-31"


def test_snapshot_rejects_negative_quote_count(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    _rewrite_json(data_dir / "latest/quotes.json", {"missing_count": -1})

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "quotes_publication_invalid"


def test_snapshot_rejects_quote_count_row_mismatch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    _rewrite_json(
        data_dir / "latest/quotes.json",
        {"requested_count": 3, "quote_count": 3, "missing_count": 0},
    )

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "quotes_publication_invalid"


def test_snapshot_rejects_a_stable_phased_account_projection_pair(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    account = json.loads(account_path.read_text(encoding="utf-8"))
    account["brokers"]["futu"]["positions"][0]["symbol"] = "TEST2"
    account["generation"] = "2026-08-03T12:00:05+08:00"
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    quotes["quotes"]["US.TEST2"] = dict(quotes["quotes"].pop("US.TEST0"))
    quotes["quotes"]["US.TEST2"]["symbol"] = "TEST2"
    write_json_atomic(account_path, account)
    write_json_atomic(quotes_path, quotes)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "account_publication_unstable"
    assert result.payload["release"]["worker_git_sha"] == SHA


def test_snapshot_rejects_account_source_without_accepted_facts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    path = data_dir / "latest/account_sync_state.json"
    account = json.loads(path.read_text(encoding="utf-8"))
    account["brokers"]["futu"]["last_success_at"] = ""
    write_json_atomic(path, account)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.etag is None
    assert result.payload["errors"][0]["code"] == "account_publication_missing"


def test_quote_age_alone_does_not_stale_a_successful_publication(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    account_path = data_dir / "latest/account_sync_state.json"
    quotes_path = data_dir / "latest/quotes.json"
    account = json.loads(account_path.read_text(encoding="utf-8"))
    quotes = json.loads(quotes_path.read_text(encoding="utf-8"))
    old_quote = "2026-08-02T16:00:00+08:00"
    account["dashboard_projection"]["quote_as_of"] = old_quote
    quotes["last_success_at"] = old_quote
    quotes["fetched_at"] = old_quote
    for row in account["dashboard_projection"]["broker_positions"]:
        if row["broker"] in {"futu", "tiger"}:
            row["price_as_of"] = old_quote
    for row in quotes["quotes"].values():
        row["price_time"] = old_quote
        row["fetched_at"] = old_quote
    write_json_atomic(account_path, account)
    write_json_atomic(quotes_path, quotes)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert result.payload["sources"]["quotes"]["status"] == "healthy"


def test_live_account_age_stales_account_but_not_statement_sources(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)

    result = load_account_snapshot(
        data_dir,
        api_git_sha=SHA,
        now=datetime.fromisoformat("2026-08-03T12:03:01+08:00"),
    )

    assert result.status_code == 200
    assert result.payload["status"] == "stale"
    assert result.payload["sources"]["account"]["brokers"]["futu"]["status"] == "stale"
    assert result.payload["sources"]["account"]["brokers"]["phillips"]["status"] == "healthy"


def test_snapshot_retries_one_account_quote_read_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    real_read = Path.read_bytes
    account_path = data_dir / "latest/account_sync_state.json"
    calls = 0

    def racing_read(path: Path) -> bytes:
        nonlocal calls
        body = real_read(path)
        if path == account_path:
            calls += 1
            if calls == 2:
                changed = json.loads(body)
                changed["generation"] = "2026-08-03T12:00:01+08:00"
                return json.dumps(changed, sort_keys=True).encode()
        return body

    monkeypatch.setattr(account_snapshot, "_read_bytes", racing_read, raising=False)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 200
    assert calls >= 4


def test_snapshot_returns_unstable_after_three_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _write_publication(data_dir)
    real_read = Path.read_bytes
    counter = 0

    def always_changing(path: Path) -> bytes:
        nonlocal counter
        body = real_read(path)
        if path.name == "account_sync_state.json":
            counter += 1
            return body + str(counter).encode()
        return body

    monkeypatch.setattr(account_snapshot, "_read_bytes", always_changing, raising=False)

    result = load_account_snapshot(data_dir, api_git_sha=SHA, now=NOW)

    assert result.status_code == 503
    assert result.payload["errors"][0]["code"] == "account_publication_unstable"
    assert counter == 6
