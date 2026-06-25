from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .dashboard import DashboardConfig
from .futu_account import (
    FutuAccountClient,
    FutuPortfolioSyncResult,
    sync_futu_portfolio,
)
from .tiger_account import (
    TigerAccountClient,
    TigerAccountConfig,
    TigerPortfolioSyncResult,
    load_tiger_account_config,
    sync_tiger_portfolio,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_ACCOUNT_SYNC_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class BrokerSyncStatus:
    status: str
    updated_latest: bool
    position_count: int
    cash_count: int
    portfolio_path: str
    report_path: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "updated_latest": self.updated_latest,
            "position_count": self.position_count,
            "cash_count": self.cash_count,
            "portfolio_path": self.portfolio_path,
            "report_path": self.report_path,
            "message": self.message,
        }


@dataclass(frozen=True)
class AccountSyncResult:
    status: str
    interval_seconds: int
    attempted_at: str
    last_success_at: str
    next_sync_after_seconds: int
    brokers: dict[str, BrokerSyncStatus]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "interval_seconds": self.interval_seconds,
            "attempted_at": self.attempted_at,
            "last_success_at": self.last_success_at,
            "next_sync_after_seconds": self.next_sync_after_seconds,
            "brokers": {
                broker: status.to_dict()
                for broker, status in self.brokers.items()
            },
        }


class DashboardAccountSyncService:
    def __init__(
        self,
        *,
        config: DashboardConfig,
        interval_seconds: int = DEFAULT_ACCOUNT_SYNC_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now_text: Callable[[], str] | None = None,
        run_date: Callable[[], str] | None = None,
        futu_client_factory: Callable[[], Any] | None = None,
        tiger_config_loader: Callable[[], TigerAccountConfig] | None = None,
        tiger_client_factory: Callable[[TigerAccountConfig], Any] | None = None,
        futu_sync: Callable[..., FutuPortfolioSyncResult] = sync_futu_portfolio,
        tiger_sync: Callable[..., TigerPortfolioSyncResult] = sync_tiger_portfolio,
    ) -> None:
        self.config = config
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.now_text = now_text or _now_text
        self.run_date = run_date or _run_date
        self.futu_client_factory = futu_client_factory or self._default_futu_client
        self.tiger_config_loader = tiger_config_loader or self._default_tiger_config
        self.tiger_client_factory = tiger_client_factory or self._default_tiger_client
        self.futu_sync = futu_sync
        self.tiger_sync = tiger_sync
        self._lock = threading.Lock()
        self._last_attempt_monotonic: float | None = None
        self._last_success_at = ""

    def refresh_if_due(self, *, force: bool = False) -> AccountSyncResult:
        with self._lock:
            now_monotonic = self.clock()
            if not force and self._last_attempt_monotonic is not None:
                elapsed = now_monotonic - self._last_attempt_monotonic
                if elapsed < self.interval_seconds:
                    return self._skipped_result(elapsed)

            self._last_attempt_monotonic = now_monotonic
            attempted_at = self.now_text()
            run_date = self.run_date()
            brokers = {
                "futu": self._sync_futu(run_date),
                "tiger": self._sync_tiger(run_date),
            }
            ok_count = sum(1 for status in brokers.values() if status.status == "ok")
            if ok_count == len(brokers):
                status = "ok"
            elif ok_count:
                status = "partial"
            else:
                status = "failed"
            if ok_count:
                self._last_success_at = attempted_at
            return AccountSyncResult(
                status=status,
                interval_seconds=self.interval_seconds,
                attempted_at=attempted_at,
                last_success_at=self._last_success_at,
                next_sync_after_seconds=self.interval_seconds,
                brokers=brokers,
            )

    def _sync_futu(self, run_date: str) -> BrokerSyncStatus:
        client = None
        try:
            client = self.futu_client_factory()
            snapshot = client.fetch_snapshot()
            result = self.futu_sync(
                snapshot=snapshot,
                portfolio_path=self.config.portfolio_path,
                data_dir=self.config.data_dir,
                reports_dir=self.config.reports_dir,
                run_date=run_date,
                update_latest=True,
            )
            return _broker_result(result)
        except Exception as exc:
            return _broker_failure(exc)
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

    def _sync_tiger(self, run_date: str) -> BrokerSyncStatus:
        client = None
        try:
            config = self.tiger_config_loader()
            client = self.tiger_client_factory(config)
            snapshot = client.fetch_snapshot()
            result = self.tiger_sync(
                snapshot=snapshot,
                portfolio_path=self.config.portfolio_path,
                data_dir=self.config.data_dir,
                reports_dir=self.config.reports_dir,
                run_date=run_date,
                update_latest=True,
            )
            return _broker_result(result)
        except Exception as exc:
            return _broker_failure(exc)
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

    def _skipped_result(self, elapsed: float) -> AccountSyncResult:
        remaining = max(0, math.ceil(self.interval_seconds - elapsed))
        return AccountSyncResult(
            status="skipped",
            interval_seconds=self.interval_seconds,
            attempted_at="",
            last_success_at=self._last_success_at,
            next_sync_after_seconds=remaining,
            brokers={},
        )

    def _default_futu_client(self) -> FutuAccountClient:
        return FutuAccountClient(
            host=self.config.futu_host,
            port=self.config.futu_port,
        )

    def _default_tiger_config(self) -> TigerAccountConfig:
        return load_tiger_account_config(
            config_dir=Path("~/.tigeropen/"),
            account=None,
            sandbox=False,
        )

    def _default_tiger_client(self, config: TigerAccountConfig) -> TigerAccountClient:
        return TigerAccountClient(config=config)


def _broker_result(
    result: FutuPortfolioSyncResult | TigerPortfolioSyncResult,
) -> BrokerSyncStatus:
    return BrokerSyncStatus(
        status="ok",
        updated_latest=result.updated_latest,
        position_count=result.position_count,
        cash_count=result.cash_count,
        portfolio_path=str(result.portfolio_path),
        report_path=str(result.report_path),
    )


def _broker_failure(error: Exception) -> BrokerSyncStatus:
    return BrokerSyncStatus(
        status="failed",
        updated_latest=False,
        position_count=0,
        cash_count=0,
        portfolio_path="",
        report_path="",
        message=str(error),
    )


def _now_text() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def _run_date() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()
