from __future__ import annotations

import csv
import fcntl
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .advice.change_classifier import ChangeClassifier, OpenAIClassifierClient
from .advice.premarket import run_premarket
from .advice.tradingagents_adapter import TradingAgentsSubprocessRunner
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .notifications import (
    CompositeNotifier,
    FeishuAppNotifier,
    FeishuWebhookNotifier,
    MacOSNotifier,
    Notifier,
    NullNotifier,
    render_feishu_order_review,
)
from .futu_watch import QuoteSnapshot
from .trade_actions import TradeActionsResult, generate_trade_actions
from .trading_plan import (
    TradingPlanBuildResult,
    build_trading_plan,
    evaluate_plan_quote,
    load_trading_plan_rows,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyPremarketConfig:
    repo: Path
    python: Path
    timezone: str
    deadline: str
    futu_host: str
    futu_port: int
    data_dir: Path
    reports_dir: Path
    logs_dir: Path
    portfolio: Path
    dry_run: bool = False
    max_workers: int = 4
    ta_timeout_seconds: float = 600.0
    ta_max_retries: int = 2
    tradingagents_path: Path = Path("/Users/ray/projects/TradingAgents")
    classifier_model: str = "deepseek-v4-flash"
    notifiers: tuple[str, ...] = ()
    feishu_webhook_url: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_receive_id_type: str = ""
    feishu_receive_id: str = ""
    feishu_message_format: str = "text"
    notify_daily_report: bool = False
    notify_action_triggers: bool = False


@dataclass(frozen=True)
class DailyRunResult:
    run_date: str
    status: str
    status_path: Path
    report_path: Path
    log_path: Path


@dataclass
class _LatestPromotion:
    source_path: Path
    latest_path: Path
    temp_path: Path | None = None
    backup_path: Path | None = None
    latest_replaced: bool = False


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: object | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("daily premarket run already active") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_env_config(path: Path, *, dry_run: bool = False) -> DailyPremarketConfig:
    values = _read_env_file(path)
    required = [
        "OPEN_TRADER_REPO",
        "OPEN_TRADER_PYTHON",
        "OPEN_TRADER_TIMEZONE",
        "OPEN_TRADER_DEADLINE",
        "OPEN_TRADER_FUTU_HOST",
        "OPEN_TRADER_FUTU_PORT",
        "DEEPSEEK_API_KEY",
    ]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError(f"missing config value(s): {', '.join(missing)}")

    for key, value in values.items():
        os.environ[key] = value

    repo = Path(values["OPEN_TRADER_REPO"]).expanduser()
    return DailyPremarketConfig(
        repo=repo,
        python=_config_path(values["OPEN_TRADER_PYTHON"], repo),
        timezone=values["OPEN_TRADER_TIMEZONE"],
        deadline=values["OPEN_TRADER_DEADLINE"],
        futu_host=values["OPEN_TRADER_FUTU_HOST"],
        futu_port=int(values["OPEN_TRADER_FUTU_PORT"]),
        data_dir=_config_path(values.get("OPEN_TRADER_DATA_DIR", "data"), repo),
        reports_dir=_config_path(values.get("OPEN_TRADER_REPORTS_DIR", "reports"), repo),
        logs_dir=_config_path(values.get("OPEN_TRADER_LOGS_DIR", "logs"), repo),
        portfolio=_config_path(
            values.get("OPEN_TRADER_PORTFOLIO", "data/latest/portfolio.csv"),
            repo,
        ),
        dry_run=dry_run,
        max_workers=int(values.get("OPEN_TRADER_MAX_WORKERS", "4")),
        ta_timeout_seconds=float(values.get("OPEN_TRADER_TA_TIMEOUT_SECONDS", "600")),
        ta_max_retries=int(values.get("OPEN_TRADER_TA_MAX_RETRIES", "2")),
        tradingagents_path=_config_path(
            values.get("OPEN_TRADER_TRADINGAGENTS_PATH", "/Users/ray/projects/TradingAgents"),
            repo,
        ),
        classifier_model=values.get("OPEN_TRADER_CLASSIFIER_MODEL", "deepseek-v4-flash"),
        notifiers=_csv_config(values.get("OPEN_TRADER_NOTIFIERS", "")),
        feishu_webhook_url=values.get("OPEN_TRADER_FEISHU_WEBHOOK_URL", ""),
        feishu_app_id=values.get("OPEN_TRADER_FEISHU_APP_ID", ""),
        feishu_app_secret=values.get("OPEN_TRADER_FEISHU_APP_SECRET", ""),
        feishu_receive_id_type=values.get("OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE", ""),
        feishu_receive_id=values.get("OPEN_TRADER_FEISHU_RECEIVE_ID", ""),
        feishu_message_format=_feishu_message_format_config(
            values.get("OPEN_TRADER_FEISHU_MESSAGE_FORMAT", "text"),
        ),
        notify_daily_report=_bool_config(
            values.get("OPEN_TRADER_NOTIFY_DAILY_REPORT", ""),
        ),
        notify_action_triggers=_bool_config(
            values.get("OPEN_TRADER_NOTIFY_ACTION_TRIGGERS", ""),
        ),
    )


def build_notifier(config: DailyPremarketConfig) -> Notifier:
    notifiers: list[Notifier] = []
    for name in config.notifiers:
        if name == "macos":
            notifiers.append(MacOSNotifier())
            continue
        if name == "feishu":
            if not config.feishu_webhook_url:
                raise ValueError("OPEN_TRADER_FEISHU_WEBHOOK_URL is required")
            notifiers.append(
                FeishuWebhookNotifier(webhook_url=config.feishu_webhook_url)
            )
            continue
        if name == "feishu_app":
            for field_name, value in [
                ("OPEN_TRADER_FEISHU_APP_ID", config.feishu_app_id),
                ("OPEN_TRADER_FEISHU_APP_SECRET", config.feishu_app_secret),
                ("OPEN_TRADER_FEISHU_RECEIVE_ID_TYPE", config.feishu_receive_id_type),
                ("OPEN_TRADER_FEISHU_RECEIVE_ID", config.feishu_receive_id),
            ]:
                if not value:
                    raise ValueError(f"{field_name} is required")
            notifiers.append(
                FeishuAppNotifier(
                    app_id=config.feishu_app_id,
                    app_secret=config.feishu_app_secret,
                    receive_id_type=config.feishu_receive_id_type,
                    receive_id=config.feishu_receive_id,
                )
            )
            continue
        raise ValueError(f"unknown notifier: {name}")

    if not notifiers:
        return NullNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return CompositeNotifier(notifiers)


class DailyPremarketRunner:
    def __init__(
        self,
        *,
        config: DailyPremarketConfig,
        premarket_runner: Callable[..., object] = run_premarket,
        plan_builder: Callable[..., TradingPlanBuildResult] = build_trading_plan,
        quote_client_factory: Callable[..., object] = FutuQuoteClient,
        trade_action_generator: Callable[
            ...,
            TradeActionsResult,
        ] = generate_trade_actions,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.premarket_runner = premarket_runner
        self.plan_builder = plan_builder
        self.quote_client_factory = quote_client_factory
        self.trade_action_generator = trade_action_generator
        self.notifier = notifier or NullNotifier()

    def run(self, run_date: str, *, dry_run: bool | None = None) -> DailyRunResult:
        _validate_run_date(run_date)
        effective_dry_run = self.config.dry_run if dry_run is None else dry_run
        zone = ZoneInfo(self.config.timezone)
        started_at = datetime.now(zone)
        status_path = self.config.data_dir / "runs" / run_date / "daily_run_status.json"
        report_path = self.config.reports_dir / "daily_runs" / f"{run_date}.md"
        log_path = self.config.logs_dir / "daily_premarket" / f"{run_date}.log"
        lock_log_path = self.config.logs_dir / "daily_premarket" / f"{run_date}.lock.log"
        lock_path = self.config.data_dir / "runs" / ".daily_premarket.lock"
        try:
            with RunLock(lock_path):
                try:
                    return self._run_locked(
                        run_date=run_date,
                        started_at=started_at,
                        status_path=status_path,
                        report_path=report_path,
                        log_path=log_path,
                        dry_run=effective_dry_run,
                    )
                except Exception as exc:
                    return self._write_failure(
                        run_date=run_date,
                        started_at=started_at,
                        status_path=status_path,
                        report_path=report_path,
                        log_path=log_path,
                        error=str(exc),
                        dry_run=effective_dry_run,
                    )
        except RuntimeError as exc:
            if str(exc) == "daily premarket run already active":
                return self._write_already_running(
                    run_date=run_date,
                    started_at=started_at,
                    status_path=status_path,
                    report_path=report_path,
                    log_path=lock_log_path,
                    error=str(exc),
                )
            return self._write_failure(
                run_date=run_date,
                started_at=started_at,
                status_path=status_path,
                report_path=report_path,
                log_path=log_path,
                error=str(exc),
                dry_run=effective_dry_run,
            )

    def _run_locked(
        self,
        *,
        run_date: str,
        started_at: datetime,
        status_path: Path,
        report_path: Path,
        log_path: Path,
        dry_run: bool,
    ) -> DailyRunResult:
        if not self.config.portfolio.exists():
            raise FileNotFoundError(f"portfolio not found: {self.config.portfolio}")

        premarket_result = self.premarket_runner(
            run_date=run_date,
            portfolio_path=self.config.portfolio,
            data_dir=self.config.data_dir,
            reports_dir=self.config.reports_dir,
            advice_runner=None,
            advice_runner_factory=self._advice_runner_factory(run_date),
            classifier=ChangeClassifier(
                client=OpenAIClassifierClient(model=self.config.classifier_model)
            ),
            symbols=None,
            excluded_symbols=None,
            update_latest=False,
            max_workers=self.config.max_workers,
            use_fallback=True,
            deadline_reached=_deadline_reached(self.config, run_date),
        )
        advice_path = Path(getattr(premarket_result, "advice_path"))
        actions_path = Path(getattr(premarket_result, "actions_path"))
        plan_result = self.plan_builder(
            advice_path=advice_path,
            data_dir=self.config.data_dir,
            run_date=run_date,
            update_latest=False,
        )
        futu_status = self._check_futu_plan(plan_result.plan_path)
        trade_actions_result = self.trade_action_generator(
            plan_path=plan_result.plan_path,
            portfolio_path=self.config.portfolio,
            data_dir=self.config.data_dir,
            reports_dir=self.config.reports_dir,
            snapshots=_snapshots_from_futu_status(futu_status),
            run_date=run_date,
            update_latest=False,
        )
        trade_action_counts = {
            "actions": trade_actions_result.action_count,
            "ready": trade_actions_result.ready_count,
            "review": trade_actions_result.review_count,
            "watch": trade_actions_result.watch_count,
        }
        advice_counts = _count_advice(advice_path)
        plan_counts = _count_plan(plan_result.plan_path)
        daily_state = _derive_daily_state(
            advice_counts=advice_counts,
            plan_counts=plan_counts,
            futu_status=futu_status,
            trade_actions=trade_action_counts,
        )
        status = str(daily_state["status"])

        latest_advice_path = self.config.data_dir / "latest" / "trading_advice.csv"
        latest_actions_path = self.config.data_dir / "latest" / "premarket_actions.csv"
        latest_plan_path = self.config.data_dir / "latest" / "trading_plan.csv"
        artifacts = {
            "advice": str(advice_path),
            "classifications": str(getattr(premarket_result, "classifications_path")),
            "actions": str(actions_path),
            "premarket_report": str(getattr(premarket_result, "report_path")),
            "trading_plan": str(plan_result.plan_path),
            "trade_actions": str(trade_actions_result.actions_path),
            "trade_actions_report": str(trade_actions_result.report_path),
            "latest_advice": str(latest_advice_path),
            "latest_actions": str(latest_actions_path),
            "latest_trading_plan": str(latest_plan_path),
            "latest_trade_actions": str(trade_actions_result.latest_path),
            "status": str(status_path),
            "report": str(report_path),
            "log": str(log_path),
        }
        result = self._write_status_and_report(
            run_date=run_date,
            started_at=started_at,
            status=status,
            readiness=str(daily_state["readiness"]),
            status_reasons=list(daily_state["status_reasons"]),
            premarket={
                **advice_counts,
                "eligible": int(getattr(premarket_result, "eligible_count")),
                "advice": int(getattr(premarket_result, "advice_count")),
                "actions": int(getattr(premarket_result, "action_count")),
            },
            plan_counts=plan_counts,
            futu_status=futu_status,
            trade_actions=trade_action_counts,
            artifacts=artifacts,
            status_path=status_path,
            report_path=report_path,
            log_path=log_path,
        )
        if not dry_run:
            _promote_latest_set(
                advice_path=advice_path,
                actions_path=actions_path,
                plan_path=plan_result.plan_path,
                trade_actions_path=trade_actions_result.actions_path,
                data_dir=self.config.data_dir,
            )
        if self.config.notify_daily_report and not dry_run:
            if _should_notify_blocker(
                status=status,
                futu_status=futu_status,
                trade_actions=trade_action_counts,
            ):
                try:
                    blocker_message = _blocker_notification_message(
                        run_date=run_date,
                        status=status,
                        futu_status=futu_status,
                        trade_actions=trade_action_counts,
                        artifacts=artifacts,
                        readiness=str(daily_state["readiness"]),
                        status_reasons=list(daily_state["status_reasons"]),
                    )
                except Exception:
                    pass
                else:
                    self._notify("Open Trader 阻塞通知", blocker_message)
            try:
                message = render_feishu_order_review(
                    run_date=run_date,
                    status=status,
                    actions_path=trade_actions_result.actions_path,
                    report_paths=[
                        trade_actions_result.report_path,
                        report_path,
                    ],
                )
            except Exception:
                pass
            else:
                self._notify("Open Trader 行动通知", message)
        return result

    def _advice_runner_factory(
        self, run_date: str
    ) -> Callable[[], TradingAgentsSubprocessRunner]:
        def factory() -> TradingAgentsSubprocessRunner:
            return TradingAgentsSubprocessRunner(
                project_path=self.config.tradingagents_path,
                config_overrides={
                    "llm_provider": "deepseek",
                    "deep_think_llm": "deepseek-v4-pro",
                    "quick_think_llm": "deepseek-v4-flash",
                    "llm_timeout": self.config.ta_timeout_seconds,
                    "llm_max_retries": self.config.ta_max_retries,
                },
                timeout_seconds=_seconds_until_deadline(self.config, run_date),
                python_executable=str(self.config.python),
            )

        return factory

    def _check_futu_plan(self, plan_path: Path) -> dict[str, object]:
        quote_client: object | None = None
        try:
            active_plans = [
                plan
                for plan in load_trading_plan_rows(plan_path)
                if plan.status == "active"
            ]
            if not active_plans:
                return {
                    "checked": 0,
                    "missing": 0,
                    "triggered": 0,
                    "items": [],
                    "error": "",
                    "diagnostic": _no_active_plans_diagnostic(
                        host=self.config.futu_host,
                        port=self.config.futu_port,
                    ),
                }

            quote_client = self.quote_client_factory(
                host=self.config.futu_host,
                port=self.config.futu_port,
            )
            snapshots = quote_client.get_snapshots(
                [plan.futu_symbol for plan in active_plans]
            )
            missing = 0
            triggered = 0
            items: list[dict[str, object]] = []
            for plan in active_plans:
                snapshot = snapshots.get(plan.futu_symbol)
                if snapshot is None:
                    missing += 1
                    items.append(
                        {
                            "symbol": plan.symbol,
                            "futu_symbol": plan.futu_symbol,
                            "status": "missing_quote",
                            "message": "No Futu snapshot was returned.",
                        }
                    )
                    continue
                quote_status = evaluate_plan_quote(plan, snapshot.last_price)
                if quote_status.status != "watch":
                    triggered += 1
                items.append(
                    {
                        "symbol": quote_status.symbol,
                        "futu_symbol": quote_status.futu_symbol,
                        "last_price": str(quote_status.last_price),
                        "status": quote_status.status,
                        "message": quote_status.message,
                    }
                )
            diagnostic = (
                _missing_quotes_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                    missing=missing,
                )
                if missing
                else _successful_futu_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                )
            )
            return {
                "checked": len(active_plans),
                "missing": missing,
                "triggered": triggered,
                "items": items,
                "error": "",
                "diagnostic": diagnostic,
            }
        except FutuQuoteError as exc:
            return {
                "checked": 0,
                "missing": 0,
                "triggered": 0,
                "items": [],
                "error": str(exc),
                "diagnostic": _error_futu_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                    error=exc,
                ),
            }
        finally:
            if quote_client is not None and hasattr(quote_client, "close"):
                try:
                    quote_client.close()
                except Exception:
                    pass

    def _write_status_and_report(
        self,
        *,
        run_date: str,
        started_at: datetime,
        status: str,
        readiness: str,
        status_reasons: list[str],
        premarket: dict[str, int],
        plan_counts: dict[str, int],
        futu_status: dict[str, object],
        trade_actions: dict[str, int],
        artifacts: dict[str, str],
        status_path: Path,
        report_path: Path,
        log_path: Path,
    ) -> DailyRunResult:
        finished_at = datetime.now(ZoneInfo(self.config.timezone))
        payload: dict[str, object] = {
            "run_date": run_date,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "deadline_at": _deadline_at(self.config, run_date).isoformat(),
            "status": status,
            "readiness": readiness,
            "status_reasons": status_reasons,
            "premarket": premarket,
            "trading_plan": plan_counts,
            "futu_plan_check": futu_status,
            "trade_actions": trade_actions,
            "artifacts": artifacts,
        }
        _write_json(status_path, payload)
        _write_text(report_path, _render_daily_report(payload))
        _write_text(log_path, json.dumps(payload, ensure_ascii=False) + "\n")
        return DailyRunResult(
            run_date=run_date,
            status=status,
            status_path=status_path,
            report_path=report_path,
            log_path=log_path,
        )

    def _write_failure(
        self,
        *,
        run_date: str,
        started_at: datetime,
        status_path: Path,
        report_path: Path,
        log_path: Path,
        error: str,
        dry_run: bool,
    ) -> DailyRunResult:
        finished_at = datetime.now(ZoneInfo(self.config.timezone))
        write_errors: list[dict[str, str]] = []
        daily_state = _derive_daily_state(
            advice_counts={"ok": 0, "fallback": 0, "error": 0},
            plan_counts={"active": 0, "fallback": 0, "error": 0},
            futu_status={
                "checked": 0,
                "missing": 0,
                "triggered": 0,
                "items": [],
                "error": "",
            },
            trade_actions={"actions": 0, "ready": 0, "review": 0, "watch": 0},
            run_failed=True,
        )
        payload: dict[str, object] = {
            "run_date": run_date,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "deadline_at": _failure_deadline_at(self.config, run_date),
            "status": "failed",
            "readiness": daily_state["readiness"],
            "status_reasons": daily_state["status_reasons"],
            "error": error,
            "premarket": {
                "eligible": 0,
                "advice": 0,
                "actions": 0,
                "ok": 0,
                "fallback": 0,
                "error": 0,
            },
            "trading_plan": {"active": 0, "fallback": 0, "error": 0},
            "futu_plan_check": {
                "checked": 0,
                "missing": 0,
                "triggered": 0,
                "items": [],
                "error": "",
                "diagnostic": _futu_diagnostic(
                    host=self.config.futu_host,
                    port=self.config.futu_port,
                    error_type="none",
                    message="",
                    next_step="",
                    opend_reachable=None,
                    context_ok=None,
                    snapshot_ok=None,
                ),
            },
            "trade_actions": {"actions": 0, "ready": 0, "review": 0, "watch": 0},
            "artifacts": {
                "advice": "",
                "classifications": "",
                "actions": "",
                "premarket_report": "",
                "trading_plan": "",
                "trade_actions": "",
                "trade_actions_report": "",
                "latest_advice": "",
                "latest_actions": "",
                "latest_trading_plan": "",
                "latest_trade_actions": "",
                "status": str(status_path),
                "report": str(report_path),
                "log": str(log_path),
            },
        }

        def attempt_write(label: str, write: Callable[[], None]) -> None:
            try:
                write()
            except Exception as exc:
                write_errors.append(
                    {
                        "artifact": label,
                        "error": str(exc),
                    }
                )
                payload["write_errors"] = write_errors

        attempt_write("status", lambda: _write_json(status_path, payload))
        attempt_write(
            "report",
            lambda: _write_text(report_path, _render_daily_report(payload)),
        )
        attempt_write(
            "log",
            lambda: _write_text(
                log_path,
                json.dumps(payload, ensure_ascii=False) + "\n",
            ),
        )
        if self.config.notify_daily_report and not dry_run:
            try:
                blocker_message = _blocker_notification_message(
                    run_date=run_date,
                    status="failed",
                    futu_status=_mapping(payload.get("futu_plan_check")),
                    trade_actions=_mapping(payload.get("trade_actions")),
                    artifacts=_mapping(payload.get("artifacts")),
                    error=error,
                    readiness=str(daily_state["readiness"]),
                    status_reasons=list(daily_state["status_reasons"]),
                )
            except Exception:
                pass
            else:
                self._notify("Open Trader 阻塞通知", blocker_message)
        return DailyRunResult(
            run_date=run_date,
            status="failed",
            status_path=status_path,
            report_path=report_path,
            log_path=log_path,
        )

    def _write_already_running(
        self,
        *,
        run_date: str,
        started_at: datetime,
        status_path: Path,
        report_path: Path,
        log_path: Path,
        error: str,
    ) -> DailyRunResult:
        daily_state = _derive_daily_state(
            advice_counts={"ok": 0, "fallback": 0, "error": 0},
            plan_counts={"active": 0, "fallback": 0, "error": 0},
            futu_status={
                "checked": 0,
                "missing": 0,
                "triggered": 0,
                "items": [],
                "error": "",
            },
            trade_actions={"actions": 0, "ready": 0, "review": 0, "watch": 0},
            already_running=True,
        )
        payload = {
            "run_date": run_date,
            "started_at": started_at.isoformat(),
            "status": "already_running",
            "readiness": daily_state["readiness"],
            "status_reasons": daily_state["status_reasons"],
            "error": error,
            "status_path": str(status_path),
            "report_path": str(report_path),
        }
        try:
            _write_text(log_path, json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return DailyRunResult(
            run_date=run_date,
            status="already_running",
            status_path=status_path,
            report_path=report_path,
            log_path=log_path,
        )

    def _notify(self, title: str, message: str) -> None:
        try:
            self.notifier.notify(title, message)
        except Exception as exc:
            LOGGER.warning(
                "通知发送失败：%s error_type=%s error=%s",
                title,
                exc.__class__.__name__,
                str(exc),
            )
            return
        LOGGER.info("通知已发送：%s", title)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _config_path(value: str, repo: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return repo / path


def _csv_config(value: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def _bool_config(value: str, default: bool = False) -> bool:
    if not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _feishu_message_format_config(value: str) -> str:
    message_format = value.strip().lower() or "text"
    if message_format != "text":
        raise ValueError("OPEN_TRADER_FEISHU_MESSAGE_FORMAT must be text")
    return message_format


def _validate_run_date(run_date: str) -> None:
    try:
        parsed = datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("run_date must be YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != run_date:
        raise ValueError("run_date must be YYYY-MM-DD")


def _promote_latest_set(
    *,
    advice_path: Path,
    actions_path: Path,
    plan_path: Path,
    trade_actions_path: Path,
    data_dir: Path,
) -> None:
    latest_dir = data_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    promotions = [
        _LatestPromotion(
            source_path=advice_path,
            latest_path=latest_dir / "trading_advice.csv",
        ),
        _LatestPromotion(
            source_path=actions_path,
            latest_path=latest_dir / "premarket_actions.csv",
        ),
        _LatestPromotion(
            source_path=plan_path,
            latest_path=latest_dir / "trading_plan.csv",
        ),
        _LatestPromotion(
            source_path=trade_actions_path,
            latest_path=latest_dir / "trade_actions.csv",
        ),
    ]

    try:
        for promotion in promotions:
            promotion.temp_path = _copy_latest_temp(
                source_path=promotion.source_path,
                latest_path=promotion.latest_path,
            )

        for promotion in promotions:
            if promotion.latest_path.exists():
                promotion.backup_path = _make_backup_latest_path(
                    promotion.latest_path
                )
                promotion.latest_path.rename(promotion.backup_path)
            if promotion.temp_path is None:
                raise RuntimeError("latest promotion temp path was not staged")
            _replace_latest_path(promotion.temp_path, promotion.latest_path)
            promotion.latest_replaced = True
            promotion.temp_path = None
    except Exception:
        _restore_latest_promotions(promotions)
        raise
    else:
        for promotion in promotions:
            if promotion.backup_path is not None and promotion.backup_path.exists():
                _best_effort_unlink(promotion.backup_path)
    finally:
        for promotion in promotions:
            if promotion.temp_path is not None and promotion.temp_path.exists():
                _best_effort_unlink(promotion.temp_path)


def _copy_latest_temp(*, source_path: Path, latest_path: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=latest_path.parent,
        prefix=f".{latest_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        with source_path.open("rb") as source:
            shutil.copyfileobj(source, handle)
    return temp_path


def _make_backup_latest_path(latest_path: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=latest_path.parent,
        prefix=f".{latest_path.name}.",
        suffix=".backup",
        delete=False,
    ) as handle:
        backup_path = Path(handle.name)
    backup_path.unlink()
    return backup_path


def _replace_latest_path(source_path: Path, latest_path: Path) -> None:
    source_path.replace(latest_path)


def _restore_latest_promotions(promotions: list[_LatestPromotion]) -> None:
    for promotion in reversed(promotions):
        if promotion.backup_path is not None and promotion.backup_path.exists():
            if promotion.latest_path.exists():
                _best_effort_unlink(promotion.latest_path)
            try:
                promotion.backup_path.rename(promotion.latest_path)
            except Exception:
                pass
        elif promotion.latest_replaced and promotion.latest_path.exists():
            _best_effort_unlink(promotion.latest_path)


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


def _deadline_reached(config: DailyPremarketConfig, run_date: str) -> Callable[[], bool]:
    def reached() -> bool:
        return datetime.now(ZoneInfo(config.timezone)) >= _deadline_at(config, run_date)

    return reached


def _seconds_until_deadline(config: DailyPremarketConfig, run_date: str) -> float:
    seconds = (
        _deadline_at(config, run_date) - datetime.now(ZoneInfo(config.timezone))
    ).total_seconds()
    return max(1.0, seconds)


def _failure_deadline_at(config: DailyPremarketConfig, run_date: str) -> str:
    try:
        return _deadline_at(config, run_date).isoformat()
    except Exception:
        return f"invalid:{config.deadline}"


def _deadline_at(config: DailyPremarketConfig, run_date: str) -> datetime:
    zone = ZoneInfo(config.timezone)
    hour, minute = _parse_deadline(config.deadline)
    return datetime.combine(
        datetime.strptime(run_date, "%Y-%m-%d").date(),
        time(hour, minute),
        tzinfo=zone,
    )


def _parse_deadline(deadline: str) -> tuple[int, int]:
    hour_text, minute_text = deadline.split(":", 1)
    return int(hour_text), int(minute_text)


def _count_advice(advice_path: Path) -> dict[str, int]:
    counts = {"ok": 0, "fallback": 0, "error": 0}
    csv.field_size_limit(sys.maxsize)
    with advice_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            status = (row.get("status") or "").strip()
            if status == "ok":
                counts["ok"] += 1
            elif status == "fallback":
                counts["fallback"] += 1
            else:
                counts["error"] += 1
    return counts


def _count_plan(plan_path: Path) -> dict[str, int]:
    counts = {"active": 0, "fallback": 0, "error": 0}
    with plan_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("status") or "").strip() == "active":
                counts["active"] += 1
            if (row.get("status") or "").strip() == "error":
                counts["error"] += 1
            if (row.get("source_status") or "").strip() == "fallback":
                counts["fallback"] += 1
    return counts


def _render_daily_report(payload: dict[str, object]) -> str:
    premarket = _mapping(payload.get("premarket"))
    trading_plan = _mapping(payload.get("trading_plan"))
    futu = _mapping(payload.get("futu_plan_check"))
    artifacts = _mapping(payload.get("artifacts"))
    lines = [
        f"# Daily Premarket Run {payload.get('run_date', '')}",
        "",
        f"- Status: {payload.get('status', '')}",
        f"- Started: {payload.get('started_at', '')}",
        f"- Finished: {payload.get('finished_at', '')}",
        f"- Deadline: {payload.get('deadline_at', '')}",
    ]
    if payload.get("error"):
        lines.append(f"- Error: {payload.get('error')}")
    readiness = str(payload.get("readiness", "")).strip()
    reason_labels = _reason_labels(payload.get("status_reasons"))
    lines.extend(
        [
            "",
            "## 可用性判断",
            "",
            f"- 可用性：{_readiness_label(readiness)}",
            f"- 原因：{', '.join(reason_labels) if reason_labels else '无'}",
            f"- 下一步：{_diagnostic_next_step(payload)}",
        ]
    )
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Premarket: {premarket.get('ok', 0)} ok, "
            f"{premarket.get('fallback', 0)} fallback, "
            f"{premarket.get('error', 0)} error",
            f"- Trading plan: {trading_plan.get('active', 0)} active, "
            f"{trading_plan.get('fallback', 0)} fallback, "
            f"{trading_plan.get('error', 0)} error",
            f"- Futu plan check: {futu.get('checked', 0)} checked, "
            f"{futu.get('missing', 0)} missing, "
            f"{futu.get('triggered', 0)} triggered",
        ]
    )
    if futu.get("error"):
        lines.append(f"- Futu error: {futu.get('error')}")

    lines.extend(["", "## Futu Plan Checks", ""])
    items = futu.get("items") if isinstance(futu.get("items"), list) else []
    if items:
        for item in items:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('futu_symbol', '')}: {item.get('status', '')} "
                f"{item.get('last_price', '')} {item.get('message', '')}".rstrip()
            )
    else:
        lines.append("- No Futu plan check items.")

    lines.extend(["", "## Artifacts", ""])
    for name in [
        "advice",
        "classifications",
        "actions",
        "premarket_report",
        "trading_plan",
        "trade_actions",
        "trade_actions_report",
        "latest_trading_plan",
        "latest_trade_actions",
        "status",
        "report",
        "log",
    ]:
        value = artifacts.get(name, "")
        lines.append(f"- {name}: {value}")
    return "\n".join(lines) + "\n"


def _notification_message(
    status: str,
    plan_counts: dict[str, int],
    futu_status: dict[str, object],
    advice_counts: dict[str, int],
) -> str:
    if status == "success":
        return (
            f"finished: {plan_counts.get('active', 0)} plans, "
            f"{futu_status.get('triggered', 0)} triggered"
        )
    if status == "partial":
        return (
            f"partial: {advice_counts.get('ok', 0)} ok, "
            f"{advice_counts.get('fallback', 0)} fallback, "
            f"{advice_counts.get('error', 0)} error"
        )
    return "failed: see daily run logs"


def _futu_diagnostic(
    *,
    host: str,
    port: int,
    error_type: str,
    message: str = "",
    next_step: str = "",
    opend_reachable: bool | None = None,
    context_ok: bool | None = None,
    snapshot_ok: bool | None = None,
) -> dict[str, object]:
    return {
        "host": host,
        "port": port,
        "opend_reachable": opend_reachable,
        "context_ok": context_ok,
        "snapshot_ok": snapshot_ok,
        "error_type": error_type,
        "message": message,
        "next_step": next_step,
    }


def _successful_futu_diagnostic(*, host: str, port: int) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type="none",
        message="",
        next_step="",
        opend_reachable=True,
        context_ok=True,
        snapshot_ok=True,
    )


def _no_active_plans_diagnostic(*, host: str, port: int) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type="no_active_plans",
        message="没有需要检查行情的 active trading plan。",
        next_step="",
        opend_reachable=None,
        context_ok=None,
        snapshot_ok=None,
    )


def _missing_quotes_diagnostic(
    *,
    host: str,
    port: int,
    missing: int,
) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type="missing_quotes",
        message=f"缺失 {missing} 个标的行情。",
        next_step=f"请人工复核缺失 {missing} 个标的行情，再决定是否执行相关交易动作。",
        opend_reachable=True,
        context_ok=True,
        snapshot_ok=True,
    )


def _error_futu_diagnostic(
    *,
    host: str,
    port: int,
    error: FutuQuoteError,
) -> dict[str, object]:
    return _futu_diagnostic(
        host=host,
        port=port,
        error_type=getattr(error, "error_type", "snapshot_failed"),
        message=str(error),
        next_step=getattr(
            error,
            "next_step",
            "请检查 OpenD 行情服务状态后重新运行每日盘前流程。",
        ),
        opend_reachable=getattr(error, "opend_reachable", None),
        context_ok=getattr(error, "context_ok", None),
        snapshot_ok=getattr(error, "snapshot_ok", None),
    )


def _derive_daily_state(
    *,
    advice_counts: dict[str, int],
    plan_counts: dict[str, int],
    futu_status: dict[str, object],
    trade_actions: dict[str, int],
    run_failed: bool = False,
    already_running: bool = False,
) -> dict[str, object]:
    reasons: list[str] = []
    if run_failed:
        reasons.append("run_failed")
    if already_running:
        reasons.append("already_running")
    if int(advice_counts.get("fallback", 0) or 0) > 0:
        reasons.append("advice_fallback")
    if int(advice_counts.get("error", 0) or 0) > 0:
        reasons.append("advice_error")
    if int(plan_counts.get("fallback", 0) or 0) > 0:
        reasons.append("plan_fallback")
    if int(plan_counts.get("error", 0) or 0) > 0:
        reasons.append("plan_error")
    if str(futu_status.get("error", "")).strip():
        reasons.append("futu_error")
    if int(futu_status.get("missing", 0) or 0) > 0:
        reasons.append("missing_quotes")
    if int(trade_actions.get("review", 0) or 0) > 0:
        reasons.append("trade_action_review")

    if run_failed:
        status = "failed"
    elif already_running:
        status = "already_running"
    elif any(reason != "trade_action_review" for reason in reasons):
        status = "partial"
    else:
        status = "success"

    if any(reason in {"run_failed", "already_running", "futu_error"} for reason in reasons):
        readiness = "blocked"
    elif reasons:
        readiness = "review_required"
    else:
        readiness = "ready"

    return {
        "status": status,
        "readiness": readiness,
        "status_reasons": reasons,
    }


def _should_notify_blocker(
    *,
    status: str,
    futu_status: dict[str, object],
    trade_actions: dict[str, int],
) -> bool:
    if status == "failed":
        return True
    if str(futu_status.get("error", "")).strip():
        return True
    if int(futu_status.get("missing", 0) or 0) > 0:
        return True
    if int(trade_actions.get("review", 0) or 0) > 0:
        return True
    return False


def _blocker_notification_message(
    *,
    run_date: str,
    status: str,
    futu_status: dict[str, object],
    trade_actions: dict[str, object],
    artifacts: dict[str, object],
    error: str = "",
    readiness: str = "",
    status_reasons: list[str] | None = None,
) -> str:
    reason_labels = [_status_reason_label(reason) for reason in (status_reasons or [])]
    diagnostic = _mapping(futu_status.get("diagnostic"))
    diagnostic_next_step = str(diagnostic.get("next_step", "")).strip()
    lines = [
        "Open Trader｜阻塞通知",
        f"日期：{run_date}｜状态：{_daily_status_label(status)}",
        f"可用性：{_readiness_label(readiness)}",
        f"原因：{', '.join(reason_labels) if reason_labels else '未分类'}",
        "",
    ]
    if error:
        lines.append("运行失败：每日流程未完成。")

    futu_error = str(futu_status.get("error", "")).strip()
    if futu_error:
        lines.append("Futu 行情异常：行情检查未完成。")

    missing = int(futu_status.get("missing", 0) or 0)
    if missing > 0:
        lines.append(f"缺失行情：{missing}")

    review = int(trade_actions.get("review", 0) or 0)
    if review > 0:
        lines.append(f"需人工处理：{review}")

    if not any((error, futu_error, missing > 0, review > 0)):
        lines.append("阻塞：系统未能完成自动盘前流程。")

    lines.extend(
        [
            "",
            "影响：自动流程不能给出完整可靠的可执行结论。",
            f"下一步：{diagnostic_next_step or '请先处理阻塞项，再重新运行每日盘前流程。'}",
        ]
    )

    status_path = str(artifacts.get("status", "")).strip()
    report_path = str(artifacts.get("report", "")).strip()
    artifact_lines: list[str] = []
    if status_path:
        artifact_lines.append(f"状态文件：{status_path}")
    if report_path:
        artifact_lines.append(f"报告：{report_path}")
    if artifact_lines:
        lines.extend(["", *artifact_lines])
    return "\n".join(lines).strip() + "\n"


def _daily_status_label(status: str) -> str:
    return {
        "success": "成功",
        "partial": "部分完成",
        "failed": "失败",
        "already_running": "已有任务运行中",
    }.get(status.strip().lower(), "未知状态")


def _readiness_label(readiness: str) -> str:
    return {
        "ready": "可复核",
        "review_required": "需要人工复核",
        "blocked": "阻塞",
    }.get(readiness.strip().lower(), "未分类")


def _status_reason_label(reason: str) -> str:
    return {
        "advice_fallback": "使用历史建议",
        "advice_error": "建议生成异常",
        "plan_fallback": "交易计划使用历史建议",
        "plan_error": "交易计划异常",
        "futu_error": "Futu 行情异常",
        "missing_quotes": "缺失行情",
        "trade_action_review": "交易动作需要人工复核",
        "run_failed": "运行失败",
        "already_running": "已有任务运行中",
    }.get(reason.strip().lower(), "其他原因")


def _reason_labels(reasons: object) -> list[str]:
    if not isinstance(reasons, list):
        return []
    return [_status_reason_label(str(reason)) for reason in reasons]


def _diagnostic_next_step(payload: dict[str, object]) -> str:
    futu = _mapping(payload.get("futu_plan_check"))
    diagnostic = _mapping(futu.get("diagnostic"))
    next_step = str(diagnostic.get("next_step", "")).strip()
    if next_step:
        return next_step
    readiness = str(payload.get("readiness", "")).strip()
    if readiness == "blocked":
        return "请先处理阻塞原因，再重新运行每日盘前流程。"
    if readiness == "review_required":
        return "请先人工复核标记项，再决定是否执行交易动作。"
    if str(payload.get("status", "")).strip() == "failed" or str(
        payload.get("error", "")
    ).strip():
        return "请先查看运行失败原因，修复后重新运行每日盘前流程。"
    if str(futu.get("error", "")).strip():
        return "请启动或重启 Futu OpenD，确认行情连接恢复后重新运行每日盘前流程。"
    if int(futu.get("missing", 0) or 0) > 0:
        return "请人工复核缺失行情标的，补齐行情后重新运行每日盘前流程。"
    trade_actions = _mapping(payload.get("trade_actions"))
    if int(trade_actions.get("review", 0) or 0) > 0:
        return "请人工复核交易动作，再决定是否执行。"
    return "无需处理。"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _snapshots_from_futu_status(
    futu_status: dict[str, object],
) -> dict[str, QuoteSnapshot]:
    items = futu_status.get("items")
    if not isinstance(items, list):
        return {}
    snapshots: dict[str, QuoteSnapshot] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        futu_symbol = str(item.get("futu_symbol", "")).strip()
        last_price_text = str(item.get("last_price", "")).strip()
        if not futu_symbol or not last_price_text:
            continue
        try:
            last_price = Decimal(last_price_text)
        except Exception:
            continue
        if not last_price.is_finite():
            continue
        snapshots[futu_symbol] = QuoteSnapshot(
            futu_symbol=futu_symbol,
            last_price=last_price,
        )
    return snapshots
