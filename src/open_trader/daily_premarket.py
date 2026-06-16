from __future__ import annotations

import csv
import fcntl
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .advice.change_classifier import ChangeClassifier, OpenAIClassifierClient
from .advice.premarket import run_premarket
from .advice.tradingagents_adapter import TradingAgentsSubprocessRunner
from .futu_quote import FutuQuoteClient, FutuQuoteError
from .trading_plan import (
    TradingPlanBuildResult,
    build_trading_plan,
    evaluate_plan_quote,
    load_trading_plan_rows,
)


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
    classifier_model: str = "gpt-5.4-mini"


@dataclass(frozen=True)
class DailyRunResult:
    run_date: str
    status: str
    status_path: Path
    report_path: Path
    log_path: Path


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: object | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("daily premarket run already active") from exc
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


class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        pass


class NullNotifier:
    def notify(self, title: str, message: str) -> None:
        pass


class MacOSNotifier:
    def notify(self, title: str, message: str) -> None:
        script = (
            f'display notification "{_escape_osascript(message)}" '
            f'with title "{_escape_osascript(title)}"'
        )
        subprocess.run(["osascript", "-e", script], check=False)


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
        "OPENAI_API_KEY",
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
        classifier_model=values.get("OPEN_TRADER_CLASSIFIER_MODEL", "gpt-5.4-mini"),
    )


class DailyPremarketRunner:
    def __init__(
        self,
        *,
        config: DailyPremarketConfig,
        premarket_runner: Callable[..., object] = run_premarket,
        plan_builder: Callable[..., TradingPlanBuildResult] = build_trading_plan,
        quote_client_factory: Callable[..., object] = FutuQuoteClient,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.premarket_runner = premarket_runner
        self.plan_builder = plan_builder
        self.quote_client_factory = quote_client_factory
        self.notifier = notifier or NullNotifier()

    def run(self, run_date: str) -> DailyRunResult:
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
                    )
                except Exception as exc:
                    return self._write_failure(
                        run_date=run_date,
                        started_at=started_at,
                        status_path=status_path,
                        report_path=report_path,
                        log_path=log_path,
                        error=str(exc),
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
            )

    def _run_locked(
        self,
        *,
        run_date: str,
        started_at: datetime,
        status_path: Path,
        report_path: Path,
        log_path: Path,
    ) -> DailyRunResult:
        if not self.config.portfolio.exists():
            raise FileNotFoundError(f"portfolio not found: {self.config.portfolio}")

        premarket_result = self.premarket_runner(
            run_date=run_date,
            portfolio_path=self.config.portfolio,
            data_dir=self.config.data_dir,
            reports_dir=self.config.reports_dir,
            advice_runner=None,
            advice_runner_factory=self._advice_runner_factory(),
            classifier=ChangeClassifier(
                client=OpenAIClassifierClient(model=self.config.classifier_model)
            ),
            symbols=None,
            excluded_symbols=None,
            update_latest=not self.config.dry_run,
            max_workers=self.config.max_workers,
            use_fallback=True,
            deadline_reached=_deadline_reached(self.config),
        )
        advice_path = Path(getattr(premarket_result, "advice_path"))
        plan_result = self.plan_builder(
            advice_path=advice_path,
            data_dir=self.config.data_dir,
            run_date=run_date,
            update_latest=not self.config.dry_run,
        )
        futu_status = self._check_futu_plan(plan_result.plan_path)
        advice_counts = _count_advice(advice_path)
        plan_counts = _count_plan(plan_result.plan_path)
        status = (
            "partial"
            if advice_counts["fallback"]
            or advice_counts["error"]
            or plan_counts["fallback"]
            or plan_counts["error"]
            or int(futu_status.get("missing", 0)) > 0
            or futu_status["error"]
            else "success"
        )

        artifacts = {
            "advice": str(advice_path),
            "classifications": str(getattr(premarket_result, "classifications_path")),
            "actions": str(getattr(premarket_result, "actions_path")),
            "premarket_report": str(getattr(premarket_result, "report_path")),
            "trading_plan": str(plan_result.plan_path),
            "latest_trading_plan": str(plan_result.latest_path),
            "status": str(status_path),
            "report": str(report_path),
            "log": str(log_path),
        }
        result = self._write_status_and_report(
            run_date=run_date,
            started_at=started_at,
            status=status,
            premarket={
                **advice_counts,
                "eligible": int(getattr(premarket_result, "eligible_count")),
                "advice": int(getattr(premarket_result, "advice_count")),
                "actions": int(getattr(premarket_result, "action_count")),
            },
            plan_counts=plan_counts,
            futu_status=futu_status,
            artifacts=artifacts,
            status_path=status_path,
            report_path=report_path,
            log_path=log_path,
        )
        self._notify(
            "Open Trader daily premarket",
            _notification_message(status, plan_counts, futu_status, advice_counts),
        )
        return result

    def _advice_runner_factory(self) -> Callable[[], TradingAgentsSubprocessRunner]:
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
                timeout_seconds=_seconds_until_deadline(self.config),
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
            return {
                "checked": len(active_plans),
                "missing": missing,
                "triggered": triggered,
                "items": items,
                "error": "",
            }
        except FutuQuoteError as exc:
            return {
                "checked": 0,
                "missing": 0,
                "triggered": 0,
                "items": [],
                "error": str(exc),
            }
        finally:
            if quote_client is not None and hasattr(quote_client, "close"):
                quote_client.close()

    def _write_status_and_report(
        self,
        *,
        run_date: str,
        started_at: datetime,
        status: str,
        premarket: dict[str, int],
        plan_counts: dict[str, int],
        futu_status: dict[str, object],
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
            "deadline_at": _deadline_at(self.config).isoformat(),
            "status": status,
            "premarket": premarket,
            "trading_plan": plan_counts,
            "futu_plan_check": futu_status,
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
    ) -> DailyRunResult:
        finished_at = datetime.now(ZoneInfo(self.config.timezone))
        payload: dict[str, object] = {
            "run_date": run_date,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "deadline_at": _deadline_at(self.config).isoformat(),
            "status": "failed",
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
            },
            "artifacts": {
                "advice": "",
                "classifications": "",
                "actions": "",
                "premarket_report": "",
                "trading_plan": "",
                "latest_trading_plan": "",
                "status": str(status_path),
                "report": str(report_path),
                "log": str(log_path),
            },
        }
        _write_json(status_path, payload)
        _write_text(report_path, _render_daily_report(payload))
        _write_text(log_path, json.dumps(payload, ensure_ascii=False) + "\n")
        self._notify(
            "Open Trader daily premarket",
            _notification_message("failed", {}, {}, {}),
        )
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
        payload = {
            "run_date": run_date,
            "started_at": started_at.isoformat(),
            "status": "already_running",
            "error": error,
            "status_path": str(status_path),
            "report_path": str(report_path),
        }
        _write_text(log_path, json.dumps(payload, ensure_ascii=False) + "\n")
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
        except Exception:
            pass


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


def _deadline_reached(config: DailyPremarketConfig) -> Callable[[], bool]:
    def reached() -> bool:
        return datetime.now(ZoneInfo(config.timezone)) >= _deadline_at(config)

    return reached


def _seconds_until_deadline(config: DailyPremarketConfig) -> float:
    seconds = (_deadline_at(config) - datetime.now(ZoneInfo(config.timezone))).total_seconds()
    return max(1.0, seconds)


def _deadline_at(config: DailyPremarketConfig) -> datetime:
    zone = ZoneInfo(config.timezone)
    hour, minute = _parse_deadline(config.deadline)
    return datetime.combine(datetime.now(zone).date(), time(hour, minute), tzinfo=zone)


def _parse_deadline(deadline: str) -> tuple[int, int]:
    hour_text, minute_text = deadline.split(":", 1)
    return int(hour_text), int(minute_text)


def _count_advice(advice_path: Path) -> dict[str, int]:
    counts = {"ok": 0, "fallback": 0, "error": 0}
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
        "latest_trading_plan",
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


def _write_json(path: Path, payload: dict[str, object]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _escape_osascript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
