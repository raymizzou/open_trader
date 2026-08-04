from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest


SHA = "sha256:" + "a" * 64
STATEMENT_GENERATION = "sha256:" + "b" * 64


class _Response:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def _snapshot(*, status: str = "healthy") -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot_generation": SHA,
        "account_generation": SHA,
        "generated_at": "2026-08-04T12:00:00+08:00",
        "quote_as_of": "2026-08-04T12:00:00+08:00",
        "status": status,
        "stale": status == "stale",
        "sources": {},
        "release": {},
        "summary": {},
        "broker_summaries": [],
        "positions": [],
        "cash_balances": [],
        "errors": [],
        "accepted_statement_generation": {"phillips": "", "eastmoney": ""},
    }


def _facts() -> dict[str, object]:
    return {
        "schema_version": "open_trader.account.statement_trade_facts.v1",
        "broker": "phillips",
        "statement_generation": STATEMENT_GENERATION,
        "statement_period": "2026-08-04",
        "trade_facts_cutoff_at": "2026-08-04T16:00:00+08:00",
        "trade_facts_sha256": SHA,
        "facts": [],
    }


@pytest.mark.parametrize("status", ["healthy", "stale"])
def test_fetch_snapshot_sends_production_marker_and_validates_v1_envelope(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from open_trader.account_http import fetch_account_snapshot

    calls: list[tuple[object, float]] = []

    def urlopen(request: object, *, timeout: float) -> _Response:
        calls.append((request, timeout))
        return _Response(_snapshot(status=status))

    monkeypatch.setattr("open_trader.account_http.urllib.request.urlopen", urlopen)

    assert fetch_account_snapshot("http://account", 1.25)["status"] == status
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.get_header("X-open-trader-account-route") == "production"
    assert timeout == 1.25


@pytest.mark.parametrize("status", [201, 202])
def test_fetch_snapshot_rejects_non_200_even_with_valid_envelope(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    from open_trader.account_http import AccountHttpError, fetch_account_snapshot

    monkeypatch.setattr(
        "open_trader.account_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(_snapshot(), status=status),
    )

    with pytest.raises(AccountHttpError, match="^account_unavailable$"):
        fetch_account_snapshot("http://account", 1)


def test_fetch_statement_facts_sends_marker_and_validates_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_trader.account_http import fetch_statement_trade_facts

    calls: list[tuple[object, float]] = []

    def urlopen(request: object, *, timeout: float) -> _Response:
        calls.append((request, timeout))
        return _Response(_facts())

    monkeypatch.setattr("open_trader.account_http.urllib.request.urlopen", urlopen)

    assert fetch_statement_trade_facts(
        "http://account", "phillips", STATEMENT_GENERATION, 2.5
    )["facts"] == []
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.get_header("X-open-trader-account-route") == "production"
    assert timeout == 2.5
    assert request.full_url.endswith(f"/phillips/{STATEMENT_GENERATION}/trade-facts")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**_snapshot(), "status": "unavailable"},
        {key: value for key, value in _snapshot().items() if key != "positions"},
        {**_snapshot(), "snapshot_generation": "not-a-generation"},
        {**_snapshot(), "accepted_statement_generation": {"phillips": "bad", "eastmoney": ""}},
    ],
)
def test_fetch_snapshot_rejects_invalid_contract(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    from open_trader.account_http import AccountHttpError, fetch_account_snapshot

    monkeypatch.setattr(
        "open_trader.account_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(AccountHttpError, match="^account_contract_invalid$"):
        fetch_account_snapshot("http://account", 1)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {key: value for key, value in _facts().items() if key != "facts"},
        {**_facts(), "broker": "eastmoney"},
        {**_facts(), "trade_facts_sha256": "bad"},
        {**_facts(), "trade_facts_cutoff_at": "2026-08-04T16:00:00"},
        {**_facts(), "facts": {}},
    ],
)
def test_fetch_statement_facts_rejects_invalid_contract(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    from open_trader.account_http import AccountHttpError, fetch_statement_trade_facts

    monkeypatch.setattr(
        "open_trader.account_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(AccountHttpError, match="^account_contract_invalid$"):
        fetch_statement_trade_facts("http://account", "phillips", STATEMENT_GENERATION, 1)


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("http://private/secret", 503, "secret", {}, io.BytesIO(b'{"code":"secret"}')),
        HTTPError("http://private/secret", 404, "secret", {}, io.BytesIO(b"{}")),
        TimeoutError("secret"),
        URLError("secret"),
    ],
)
def test_fetch_snapshot_sanitizes_transport_and_http_failures(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    from open_trader.account_http import AccountHttpError, fetch_account_snapshot

    def urlopen(*_args: object, **_kwargs: object) -> _Response:
        raise failure

    monkeypatch.setattr("open_trader.account_http.urllib.request.urlopen", urlopen)

    with pytest.raises(AccountHttpError) as raised:
        fetch_account_snapshot("http://account", 1)
    assert raised.value.code == "account_unavailable"
    assert str(raised.value) == "account_unavailable"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_fetch_statement_facts_preserves_only_generation_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_trader.account_http import AccountHttpError, fetch_statement_trade_facts

    error = HTTPError(
        "http://private/secret", 409, "secret", {},
        io.BytesIO(b'{"code":"accepted_statement_generation_changed","secret":"no"}'),
    )
    monkeypatch.setattr(
        "open_trader.account_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(AccountHttpError) as raised:
        fetch_statement_trade_facts("http://account", "phillips", STATEMENT_GENERATION, 1)
    assert raised.value.code == "accepted_statement_generation_changed"
    assert str(raised.value) == "accepted_statement_generation_changed"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("timeout", [0, -1])
def test_helpers_reject_nonpositive_timeouts(timeout: float) -> None:
    from open_trader.account_http import fetch_account_snapshot

    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        fetch_account_snapshot("http://account", timeout)


def test_statement_helper_rejects_invalid_generation_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_trader.account_http import AccountHttpError, fetch_statement_trade_facts

    monkeypatch.setattr(
        "open_trader.account_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("must not request invalid generation"),
    )

    with pytest.raises(AccountHttpError, match="^account_contract_invalid$"):
        fetch_statement_trade_facts("http://account", "phillips", "bad", 1)
