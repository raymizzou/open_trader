from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from open_trader.dashboard import DashboardConfig
from open_trader.dashboard_account_sync import DashboardAccountSyncService
from open_trader.futu_account import FutuPortfolioSyncResult
from open_trader.tiger_account import TigerPortfolioSyncResult


@dataclass
class FakeFutuClient:
    calls: list[str]
    closed: bool = False

    def fetch_snapshot(self) -> object:
        self.calls.append("futu_fetch")
        return object()

    def close(self) -> None:
        self.closed = True
        self.calls.append("futu_close")


@dataclass
class FakeTigerClient:
    calls: list[str]
    config: object
    closed: bool = False

    def fetch_snapshot(self) -> object:
        self.calls.append("tiger_fetch")
        return object()

    def close(self) -> None:
        self.closed = True
        self.calls.append("tiger_close")


def config(tmp_path: Path) -> DashboardConfig:
    return DashboardConfig(
        portfolio_path=tmp_path / "data/latest/portfolio.csv",
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        poll_seconds=5.0,
        futu_host="127.0.0.1",
        futu_port=11111,
    )


def futu_result(tmp_path: Path) -> FutuPortfolioSyncResult:
    return FutuPortfolioSyncResult(
        run_date="2026-06-25",
        account_count=1,
        position_count=2,
        cash_count=1,
        merged_row_count=3,
        snapshot_path=tmp_path / "data/runs/2026-06-25/futu_account_snapshot.json",
        portfolio_path=tmp_path / "data/runs/2026-06-25/portfolio.csv",
        report_path=tmp_path / "reports/futu_account/2026-06-25.md",
        latest_path=tmp_path / "data/latest/portfolio.csv",
        updated_latest=True,
    )


def tiger_result(tmp_path: Path) -> TigerPortfolioSyncResult:
    return TigerPortfolioSyncResult(
        run_date="2026-06-25",
        account_count=1,
        position_count=3,
        cash_count=1,
        merged_row_count=4,
        snapshot_path=tmp_path / "data/runs/2026-06-25/tiger_account_snapshot.json",
        portfolio_path=tmp_path / "data/runs/2026-06-25/portfolio.csv",
        report_path=tmp_path / "reports/tiger_account/2026-06-25.md",
        latest_path=tmp_path / "data/latest/portfolio.csv",
        updated_latest=True,
    )


def test_dashboard_account_sync_runs_brokers_in_order_and_throttles(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    times = iter([100.0, 120.0, 161.0])

    def futu_client_factory() -> FakeFutuClient:
        calls.append("futu_client")
        return FakeFutuClient(calls)

    def tiger_config_loader() -> object:
        calls.append("tiger_config")
        return object()

    def tiger_client_factory(tiger_config: object) -> FakeTigerClient:
        calls.append("tiger_client")
        return FakeTigerClient(calls, tiger_config)

    def sync_futu(**kwargs: object) -> FutuPortfolioSyncResult:
        calls.append("futu_sync")
        assert kwargs["portfolio_path"] == tmp_path / "data/latest/portfolio.csv"
        assert kwargs["run_date"] == "2026-06-25"
        assert kwargs["update_latest"] is True
        return futu_result(tmp_path)

    def sync_tiger(**kwargs: object) -> TigerPortfolioSyncResult:
        calls.append("tiger_sync")
        assert kwargs["portfolio_path"] == tmp_path / "data/latest/portfolio.csv"
        assert kwargs["run_date"] == "2026-06-25"
        assert kwargs["update_latest"] is True
        return tiger_result(tmp_path)

    service = DashboardAccountSyncService(
        config=config(tmp_path),
        interval_seconds=60,
        clock=lambda: next(times),
        now_text=lambda: "2026-06-25T10:00:00+08:00",
        run_date=lambda: "2026-06-25",
        futu_client_factory=futu_client_factory,
        tiger_config_loader=tiger_config_loader,
        tiger_client_factory=tiger_client_factory,
        futu_sync=sync_futu,
        tiger_sync=sync_tiger,
    )

    first = service.refresh_if_due().to_dict()
    second = service.refresh_if_due().to_dict()
    third = service.refresh_if_due().to_dict()

    assert first["status"] == "ok"
    assert first["brokers"]["futu"]["status"] == "ok"
    assert first["brokers"]["tiger"]["position_count"] == 3
    assert second["status"] == "skipped"
    assert second["next_sync_after_seconds"] == 40
    assert third["status"] == "ok"
    assert calls == [
        "futu_client",
        "futu_fetch",
        "futu_sync",
        "futu_close",
        "tiger_config",
        "tiger_client",
        "tiger_fetch",
        "tiger_sync",
        "tiger_close",
        "futu_client",
        "futu_fetch",
        "futu_sync",
        "futu_close",
        "tiger_config",
        "tiger_client",
        "tiger_fetch",
        "tiger_sync",
        "tiger_close",
    ]


def test_dashboard_account_sync_reports_partial_failure(tmp_path: Path) -> None:
    service = DashboardAccountSyncService(
        config=config(tmp_path),
        clock=lambda: 100.0,
        now_text=lambda: "2026-06-25T10:00:00+08:00",
        run_date=lambda: "2026-06-25",
        futu_client_factory=lambda: FakeFutuClient([]),
        tiger_config_loader=lambda: object(),
        tiger_client_factory=lambda _config: FakeTigerClient([], _config),
        futu_sync=lambda **_kwargs: futu_result(tmp_path),
        tiger_sync=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("tiger down")),
    )

    result = service.refresh_if_due().to_dict()

    assert result["status"] == "partial"
    assert result["brokers"]["futu"]["status"] == "ok"
    assert result["brokers"]["tiger"]["status"] == "failed"
    assert result["brokers"]["tiger"]["message"] == "tiger down"
