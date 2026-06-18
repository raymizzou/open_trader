from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.cli import build_parser
from open_trader.futu_account import (
    FutuAccount,
    FutuAccountSnapshot,
    FutuPortfolioSyncResult,
)


def test_check_futu_account_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["check-futu-account", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output


def test_sync_futu_portfolio_help_includes_expected_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["sync-futu-portfolio", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "--portfolio" in output
    assert "--data-dir" in output
    assert "--reports-dir" in output
    assert "--date" in output
    assert "--host" in output
    assert "--port" in output
    assert "--update-latest" in output


def test_check_futu_account_main_prints_diagnostic_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeFutuAccountClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port
            self.closed = False

        def fetch_snapshot(self) -> FutuAccountSnapshot:
            return FutuAccountSnapshot(
                accounts=[
                    FutuAccount(
                        acc_id=111,
                        acc_index=0,
                        trd_env="REAL",
                        acc_type="CASH",
                        account_alias="futu_111",
                    )
                ],
                cash_records=[{"currency": "USD", "cash": "100"}],
                position_records=[{"code": "US.MSFT"}],
            )

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(cli, "FutuAccountClient", FakeFutuAccountClient)

    result = cli.main(["check-futu-account", "--host", "127.0.0.1", "--port", "11111"])

    assert result == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 11111
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "connected to Futu OpenD at 127.0.0.1:11111" in output
    assert "real_accounts: 1" in output
    assert "positions: 1" in output
    assert "cash_records: 1" in output


def test_sync_futu_portfolio_main_wires_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    snapshot = FutuAccountSnapshot(
        accounts=[
            FutuAccount(
                acc_id=111,
                acc_index=0,
                trd_env="REAL",
                acc_type="CASH",
                account_alias="futu_111",
            )
        ],
        cash_records=[],
        position_records=[],
    )

    class FakeFutuAccountClient:
        def __init__(self, *, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

        def fetch_snapshot(self) -> FutuAccountSnapshot:
            return snapshot

        def close(self) -> None:
            captured["closed"] = True

    def fake_sync_futu_portfolio(**kwargs: object) -> FutuPortfolioSyncResult:
        captured.update(kwargs)
        return FutuPortfolioSyncResult(
            run_date="2026-06-18",
            account_count=1,
            position_count=2,
            cash_count=1,
            merged_row_count=3,
            snapshot_path=tmp_path / "data/runs/2026-06-18/futu_account_snapshot.json",
            portfolio_path=tmp_path / "data/runs/2026-06-18/portfolio.csv",
            report_path=tmp_path / "reports/futu_account/2026-06-18.md",
            latest_path=tmp_path / "data/latest/portfolio.csv",
            updated_latest=True,
        )

    monkeypatch.setattr(cli, "FutuAccountClient", FakeFutuAccountClient)
    monkeypatch.setattr(cli, "sync_futu_portfolio", fake_sync_futu_portfolio)

    result = cli.main(
        [
            "sync-futu-portfolio",
            "--portfolio",
            str(tmp_path / "data/latest/portfolio.csv"),
            "--data-dir",
            str(tmp_path / "data"),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--date",
            "2026-06-18",
            "--host",
            "127.0.0.1",
            "--port",
            "11111",
            "--update-latest",
        ]
    )

    assert result == 0
    assert captured["snapshot"] is snapshot
    assert captured["portfolio_path"] == tmp_path / "data/latest/portfolio.csv"
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["reports_dir"] == tmp_path / "reports"
    assert captured["run_date"] == "2026-06-18"
    assert captured["update_latest"] is True
    assert captured["closed"] is True
    output = capsys.readouterr().out
    assert "run_date: 2026-06-18" in output
    assert "real_accounts: 1" in output
    assert "positions: 2" in output
    assert "cash: 1" in output
    assert "merged_rows: 3" in output
    assert f"report: {tmp_path / 'reports/futu_account/2026-06-18.md'}" in output
    assert "updated_latest: true" in output
