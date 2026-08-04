from __future__ import annotations

import importlib
import io
import json
from pathlib import Path

import pytest

import open_trader.account_sync_worker as worker_module
import open_trader.account_api as account_api
import open_trader.cli as cli
import open_trader.dashboard_quotes as quotes_module
from open_trader.account_http import (
    AccountHttpError,
    DEFAULT_ACCOUNT_API_URL,
    DEFAULT_ACCOUNT_TIMEOUT_SECONDS,
)
from open_trader.account_sync_worker import AccountSyncWorkerConfig
from open_trader.cli import build_parser


GENERATION = "sha256:" + "a" * 64


def test_parser_exposes_only_account_sync_commands() -> None:
    parser = build_parser()

    worker = parser.parse_args(["account-sync-worker", "--once"])
    status = parser.parse_args(["account-sync-status", "--json"])

    assert worker.config == Path("config/daily_premarket.env")
    assert worker.data_dir == Path("data")
    assert worker.reports_dir == Path("reports")
    assert worker.portfolio == Path("data/latest/portfolio.csv")
    assert worker.tiger_config_dir == Path("~/.tigeropen/")
    assert worker.account_interval_seconds == 60.0
    assert worker.quote_interval_seconds == 5.0
    assert worker.once is True
    assert status.account_url == DEFAULT_ACCOUNT_API_URL
    assert status.json is True

    overridden = parser.parse_args(
        ["account-sync-status", "--account-url", "http://127.0.0.1:9876"]
    )
    assert overridden.account_url == "http://127.0.0.1:9876"

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["account-sync-status", "--data-dir", "data"])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["account-sync-controller"])
    assert exc_info.value.code == 2

    for command in (
        "check-futu-quotes",
        "check-futu-account",
        "sync-futu-portfolio",
        "check-tiger-account",
        "sync-tiger-portfolio",
    ):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([command])
        assert exc_info.value.code == 2


def test_old_account_sync_controller_module_is_not_available() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("open_trader.account_sync_controller")


def test_worker_uses_only_futu_connection_env_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "daily_premarket.env"
    config_path.write_text(
        "OPEN_TRADER_FUTU_HOST=10.0.0.7\n"
        "OPEN_TRADER_FUTU_PORT=12345\n"
        "OPENAI_API_KEY=must-not-be-loaded\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run(config: AccountSyncWorkerConfig, *, once: bool) -> int:
        captured.update(config=config, once=once)
        return 0

    monkeypatch.setattr(cli, "run_account_sync_worker", fake_run)
    monkeypatch.setattr(
        cli,
        "load_env_config",
        lambda *_args, **_kwargs: pytest.fail("worker must not load notifier/LLM config"),
    )

    assert (
        cli.main(
            [
                "account-sync-worker",
                "--config",
                str(config_path),
                "--data-dir",
                str(tmp_path / "data"),
                "--reports-dir",
                str(tmp_path / "reports"),
                "--portfolio",
                str(tmp_path / "data/latest/portfolio.csv"),
                "--tiger-config-dir",
                str(tmp_path / ".tigeropen"),
                "--account-interval-seconds",
                "61",
                "--quote-interval-seconds",
                "6",
                "--once",
            ]
        )
        == 0
    )

    config = captured["config"]
    assert isinstance(config, AccountSyncWorkerConfig)
    assert config.futu_host == "10.0.0.7"
    assert config.futu_port == 12345
    assert config.account_interval_seconds == 61.0
    assert config.quote_interval_seconds == 6.0
    assert config.tiger_account is None
    assert captured["once"] is True


@pytest.mark.parametrize(
    ("status", "account_status", "quote_status", "reason"),
    [
        ("healthy", "healthy", "healthy", None),
        ("stale", "stale", "healthy", "broker_refresh_failed"),
    ],
)
def test_status_projects_one_account_snapshot_as_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    account_status: str,
    quote_status: str,
    reason: str | None,
) -> None:
    calls: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> dict[str, object]:
        calls.append((url, timeout))
        return _snapshot(
            status=status,
            account_status=account_status,
            quote_status=quote_status,
            reason=reason,
        )

    _forbid_non_account_reads(monkeypatch)
    monkeypatch.setattr(cli, "fetch_account_snapshot", fetch)

    assert cli.main(["account-sync-status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": status,
        "reason": reason,
        "snapshot_generation": GENERATION,
        "account_generation": GENERATION,
        "quotes": {"status": quote_status},
        "brokers": {
            "futu": {"status": account_status},
            "tiger": {"status": "healthy"},
            "phillips": {"status": "healthy"},
            "eastmoney": {"status": "healthy"},
        },
    }
    assert calls == [(DEFAULT_ACCOUNT_API_URL, DEFAULT_ACCOUNT_TIMEOUT_SECONDS)]


def test_status_human_output_labels_stale_truthfully(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_non_account_reads(monkeypatch)
    monkeypatch.setattr(
        cli,
        "fetch_account_snapshot",
        lambda *_args: _snapshot(
            status="stale",
            account_status="healthy",
            quote_status="stale",
            reason=None,
        ),
    )

    assert cli.main(["account-sync-status", "--account-url", "http://account"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "status: stale",
        "reason: ",
        f"snapshot_generation: {GENERATION}",
        f"account_generation: {GENERATION}",
        "quotes: stale",
        "futu: healthy",
        "tiger: healthy",
        "phillips: healthy",
        "eastmoney: healthy",
    ]


def test_status_returns_only_sanitized_account_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_non_account_reads(monkeypatch)
    monkeypatch.setattr(
        cli,
        "fetch_account_snapshot",
        lambda *_args: (_ for _ in ()).throw(AccountHttpError("account_unavailable")),
    )

    assert cli.main(["account-sync-status", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "account_unavailable\n"


def test_status_sanitizes_malformed_nested_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return io.BytesIO(
                json.dumps({
                    "schema_version": 1,
                    "snapshot_generation": GENERATION,
                    "account_generation": GENERATION,
                    "generated_at": "2026-08-04T12:00:00+08:00",
                    "quote_as_of": "2026-08-04T12:00:00+08:00",
                    "status": "healthy",
                    "stale": False,
                    "sources": {},
                    "release": {},
                    "summary": {},
                    "broker_summaries": [],
                    "positions": [],
                    "cash_balances": [],
                    "errors": [],
                    "accepted_statement_generation": {"phillips": "", "eastmoney": ""},
                }).encode()
            ).read(size)

    monkeypatch.setattr(
        "open_trader.account_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    assert cli.main(["account-sync-status", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "account_contract_invalid\n"


def _snapshot(
    *, status: str, account_status: str, quote_status: str, reason: str | None
) -> dict[str, object]:
    return {
        "status": status,
        "snapshot_generation": GENERATION,
        "account_generation": GENERATION,
        "sources": {
            "account": {
                "status": account_status,
                "reason": reason,
                "brokers": {
                    "futu": {"status": account_status},
                    "tiger": {"status": "healthy"},
                    "phillips": {"status": "healthy"},
                    "eastmoney": {"status": "healthy"},
                },
            },
            "quotes": {"status": quote_status},
        },
    }


def _forbid_non_account_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        pytest.fail("account-sync-status must only read the Account snapshot")

    monkeypatch.setattr(worker_module, "FutuAccountClient", fail)
    monkeypatch.setattr(worker_module, "TigerAccountClient", fail)
    monkeypatch.setattr(quotes_module, "FutuQuoteClient", fail)
    monkeypatch.setattr(account_api, "load_account_snapshot", fail)
    monkeypatch.setattr(cli, "_load_optional_json", fail)
