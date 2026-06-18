from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.tiger_account import (
    TigerAccount,
    TigerAccountConfig,
    TigerAccountSnapshot,
    TigerPortfolioSyncResult,
)


def tiger_config(*, account: str = "123456789") -> TigerAccountConfig:
    return TigerAccountConfig(
        tiger_id="tiger-123",
        account=account,
        private_key_path=None,
        private_key="private-key",
        secret_key=None,
        token=None,
        sandbox=False,
        config_dir=Path("unused"),
    )


def tiger_snapshot() -> TigerAccountSnapshot:
    return TigerAccountSnapshot(
        accounts=[
            TigerAccount(
                account="123456789",
                account_alias="tiger_6789",
                account_type="STANDARD",
                capability="REGTMARGIN",
                status="FUNDED",
                asset_method="get_prime_assets",
            )
        ],
        cash_records=[{"currency": "USD", "cash_balance": "100"}],
        position_records=[{"symbol": "MSFT"}],
    )


def test_check_tiger_account_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["check-tiger-account", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--config-dir" in output
    assert "--account" in output
    assert "--sandbox" in output


def test_sync_tiger_portfolio_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["sync-tiger-portfolio", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--reports-dir" in output
    assert "--date" in output
    assert "--config-dir" in output
    assert "--account" in output
    assert "--sandbox" in output
    assert "--update-latest" in output


def test_check_tiger_account_main_prints_diagnostic_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    snapshot = tiger_snapshot()
    config = tiger_config()

    def fake_load_tiger_account_config(**kwargs: object) -> TigerAccountConfig:
        captured.update(kwargs)
        return config

    class FakeTigerAccountClient:
        def __init__(self, *, config: TigerAccountConfig) -> None:
            captured["client_config"] = config

        def fetch_snapshot(self) -> TigerAccountSnapshot:
            return snapshot

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(cli, "load_tiger_account_config", fake_load_tiger_account_config)
    monkeypatch.setattr(cli, "TigerAccountClient", FakeTigerAccountClient)

    result = cli.main(
        [
            "check-tiger-account",
            "--config-dir",
            str(tmp_path / ".tigeropen"),
            "--account",
            "123456789",
            "--sandbox",
        ]
    )

    assert result == 0
    assert captured["config_dir"] == tmp_path / ".tigeropen"
    assert captured["account"] == "123456789"
    assert captured["sandbox"] is True
    assert captured["client_config"] is config
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "connected to Tiger OpenAPI account *****6789" in output
    assert "accounts: 1" in output
    assert "positions: 1" in output
    assert "cash_records: 1" in output


def test_sync_tiger_portfolio_main_wires_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    snapshot = tiger_snapshot()
    config = tiger_config(account="DU575569")

    def fake_load_tiger_account_config(**kwargs: object) -> TigerAccountConfig:
        captured.update(kwargs)
        return config

    class FakeTigerAccountClient:
        def __init__(self, *, config: TigerAccountConfig) -> None:
            captured["client_config"] = config

        def fetch_snapshot(self) -> TigerAccountSnapshot:
            return snapshot

        def close(self) -> None:
            captured["closed"] = True

    def fake_sync_tiger_portfolio(**kwargs: object) -> TigerPortfolioSyncResult:
        captured.update(kwargs)
        return TigerPortfolioSyncResult(
            run_date="2026-06-19",
            account_count=1,
            position_count=2,
            cash_count=1,
            merged_row_count=4,
            snapshot_path=tmp_path / "data/runs/2026-06-19/tiger_account_snapshot.json",
            portfolio_path=tmp_path / "data/runs/2026-06-19/portfolio.csv",
            report_path=tmp_path / "reports/tiger_account/2026-06-19.md",
            latest_path=tmp_path / "data/latest/portfolio.csv",
            updated_latest=True,
        )

    monkeypatch.setattr(cli, "load_tiger_account_config", fake_load_tiger_account_config)
    monkeypatch.setattr(cli, "TigerAccountClient", FakeTigerAccountClient)
    monkeypatch.setattr(cli, "sync_tiger_portfolio", fake_sync_tiger_portfolio)

    result = cli.main(
        [
            "sync-tiger-portfolio",
            "--portfolio",
            str(tmp_path / "data/latest/portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--date",
            "2026-06-19",
            "--config-dir",
            str(tmp_path / ".tigeropen"),
            "--account",
            "DU575569",
            "--sandbox",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["config_dir"] == tmp_path / ".tigeropen"
    assert captured["account"] == "DU575569"
    assert captured["sandbox"] is True
    assert captured["client_config"] is config
    assert captured["snapshot"] is snapshot
    assert captured["portfolio_path"] == tmp_path / "data/latest/portfolio.csv"
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["reports_dir"] == tmp_path / "reports"
    assert captured["run_date"] == "2026-06-19"
    assert captured["update_latest"] is True
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "connected to Tiger OpenAPI account ***5569" in output
    assert "run_date: 2026-06-19" in output
    assert "accounts: 1" in output
    assert "positions: 2" in output
    assert "cash: 1" in output
    assert "merged_rows: 4" in output
    assert (
        f"snapshot: {tmp_path / 'data/runs/2026-06-19/tiger_account_snapshot.json'}"
        in output
    )
    assert f"portfolio: {tmp_path / 'data/runs/2026-06-19/portfolio.csv'}" in output
    assert f"report: {tmp_path / 'reports/tiger_account/2026-06-19.md'}" in output
    assert f"latest: {tmp_path / 'data/latest/portfolio.csv'}" in output
    assert "updated_latest: true" in output
