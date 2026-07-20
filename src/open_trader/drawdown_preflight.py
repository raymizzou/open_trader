from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from .notifications import Notifier
from .strategy_drawdown import (
    automatic_bootstrap_strategy_drawdown,
    recover_strategy_drawdown_state,
    strategy_drawdown_keys,
    strategy_drawdown_state_status,
)


REPORT_DIRECTORIES = {
    "CN": "trend_a_share",
    "HK": "trend_hk_phillips",
    "US": "trend_us_tiger",
}
MARKET_TIMEZONES = {
    "CN": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}


@dataclass(frozen=True)
class DrawdownMarketInput:
    market: str
    strategy_snapshot: Mapping[str, object]
    baseline_equity: Decimal | None
    source_date: str | None
    entry_eligible_from: str | None
    error: str = ""


def market_preflight_dates(
    market: str, *, now: datetime, trading_days: list[str]
) -> tuple[str, str]:
    settings = {
        "CN": (MARKET_TIMEZONES["CN"], time(9, 30), time(15)),
        "HK": (MARKET_TIMEZONES["HK"], time(9, 30), time(16)),
        "US": (MARKET_TIMEZONES["US"], time(9, 30), time(16)),
    }
    try:
        timezone, opened_at, closed_at = settings[market.strip().upper()]
    except KeyError:
        raise ValueError(f"unsupported drawdown preflight market: {market}") from None
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("drawdown preflight clock must be timezone-aware")
    local_now = now.astimezone(timezone)
    days = sorted({date.fromisoformat(value) for value in trading_days})
    completed = [
        day
        for day in days
        if datetime.combine(day, closed_at, timezone) <= local_now
    ]
    eligible = [
        day
        for day in days
        if datetime.combine(day, opened_at, timezone) > local_now
    ]
    if not completed or not eligible:
        raise ValueError(f"{market} trading calendar has no preflight boundary")
    return completed[-1].isoformat(), eligible[0].isoformat()


def frozen_missing_baseline(
    reports_dir: Path,
    *,
    market: str,
    strategy_id: str,
    strategy_version: str,
    source_date: str,
) -> Decimal | None:
    directory = REPORT_DIRECTORIES[market]
    for path in sorted((reports_dir / directory).glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata")
        strategy = payload.get("strategy_snapshot")
        account = payload.get("account")
        drawdown = payload.get("drawdown_summary")
        if not (
            isinstance(metadata, dict)
            and str(metadata.get("market") or "").upper() == market
            and isinstance(strategy, dict)
            and strategy.get("strategy_id") == strategy_id
            and strategy.get("strategy_version") == strategy_version
            and isinstance(account, dict)
            and account.get("source_date") == source_date
            and isinstance(drawdown, dict)
            and drawdown.get("state_status") == "missing"
        ):
            continue
        try:
            equity = Decimal(str(account.get("net_value")))
        except Exception:
            continue
        if equity.is_finite() and equity > 0:
            return equity
    return None


def run_drawdown_preflight(
    *,
    data_dir: Path,
    reports_dir: Path,
    market_inputs: Mapping[str, DrawdownMarketInput],
    accepted_git_sha: str,
    actor: str,
    occurred_at: str,
    notifier: Notifier,
) -> dict[str, object]:
    initial_status = strategy_drawdown_state_status(data_dir)
    historical_ok = _any_frozen_report_has_healthy_drawdown(reports_dir)
    recovered = False
    recovery_details: dict[str, object] | None = None
    if initial_status == "corrupt" or initial_status == "missing" and historical_ok:
        try:
            recovery_details = recover_strategy_drawdown_state(
                data_dir, actor=actor, occurred_at=occurred_at
            )
            recovered = True
        except ValueError as exc:
            error = str(exc)
            failure_status = f"state_{initial_status}_recovery_failed"
            results = [
                {
                    "market": market,
                    "status": "failed",
                    "failure_status": failure_status,
                    "error": error,
                }
                for market in _ordered_markets(market_inputs)
            ]
            result = {"status": "failed", "markets": results}
            _sync_failure_alerts(data_dir, market_inputs, results, notifier)
            return result

    first_activation = initial_status == "missing" and not historical_ok
    existing_keys = strategy_drawdown_keys(data_dir)
    results: list[dict[str, object]] = []
    for market in _ordered_markets(market_inputs):
        item = market_inputs[market]
        if item.error:
            results.append(
                {"market": market, "status": "unavailable", "error": item.error}
            )
            continue
        if (
            item.baseline_equity is None
            or item.source_date is None
            or item.entry_eligible_from is None
        ):
            results.append({
                "market": market,
                "status": "failed",
                "failure_status": "baseline_unavailable",
                "error": "completed-date frozen Futu baseline is unavailable",
            })
            continue
        strategy_id = str(item.strategy_snapshot.get("strategy_id") or "")
        strategy_version = str(
            item.strategy_snapshot.get("strategy_version") or ""
        )
        parameters = item.strategy_snapshot.get("parameters")
        if not isinstance(parameters, Mapping):
            results.append(
                {
                    "market": market,
                    "status": "failed",
                    "error": "strategy parameters are unavailable",
                }
            )
            continue
        key = (market, strategy_id, strategy_version)
        was_present = key in existing_keys
        reason = (
            "first_activation"
            if first_activation or not any(row[0] == market for row in existing_keys)
            else "new_strategy_version"
        )
        try:
            decision = automatic_bootstrap_strategy_drawdown(
                data_dir,
                market=market,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                parameters=parameters,
                baseline_equity=item.baseline_equity,
                source_date=item.source_date,
                accepted_git_sha=accepted_git_sha,
                actor=actor,
                occurred_at=occurred_at,
                reason=reason,
                entry_eligible_from=item.entry_eligible_from,
                entry_date=_market_date(market, occurred_at),
            )
        except (OSError, ValueError) as exc:
            error = str(exc)
            results.append(
                {
                    "market": market,
                    "status": "failed",
                    "failure_status": (
                        "parameter_mismatch"
                        if "parameters changed" in error
                        else "parameter_identity_missing"
                        if "parameter identity" in error
                        else "preflight_failed"
                    ),
                    "error": error,
                }
            )
            continue
        existing_keys.add(key)
        result_status = (
            "recovered" if recovered and was_present
            else "ready" if was_present
            else "bootstrapped"
        )
        result = {
            "market": market,
            "status": result_status,
            "state_status": decision["state_status"],
            "entry_allowed": decision["entry_allowed"],
            "high_water_mark": decision["high_water_mark"],
            "bootstrap_event": decision["bootstrap_event"],
            "recovery_event": decision["recovery_event"],
        }
        if result_status == "recovered" and recovery_details is not None:
            result["recovery"] = recovery_details
        results.append(result)
    statuses = {str(item["status"]) for item in results}
    overall = "failed" if "failed" in statuses else "unavailable" if "unavailable" in statuses else "ready"
    _sync_failure_alerts(data_dir, market_inputs, results, notifier)
    return {"status": overall, "markets": results}


def _sync_failure_alerts(
    data_dir: Path,
    market_inputs: Mapping[str, DrawdownMarketInput],
    results: list[dict[str, object]],
    notifier: Notifier,
) -> None:
    path = data_dir / "trend_drawdown/alerts.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        active = set(payload["active"])
        if not all(isinstance(item, str) for item in active):
            raise ValueError
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        active = set()
    original = set(active)
    for result in results:
        market = str(result["market"])
        strategy = market_inputs[market].strategy_snapshot
        version = str(strategy.get("strategy_version") or "")
        prefix = f"{market}|{version}|"
        if result["status"] in {"ready", "bootstrapped", "recovered"}:
            active = {key for key in active if not key.startswith(prefix)}
            continue
        failure_status = result.get("failure_status")
        if result["status"] != "failed" or not failure_status:
            continue
        key = prefix + str(failure_status)
        if key in active:
            continue
        try:
            notifier.notify(
                "高优先级：策略累计回撤状态阻断",
                f"{market} {version}：{result.get('error', failure_status)}",
            )
        except Exception:
            continue
        active.add(key)
    if active == original:
        return
    content = (
        json.dumps(
            {"active": sorted(active)},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=path.parent
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _ordered_markets(
    market_inputs: Mapping[str, DrawdownMarketInput],
) -> list[str]:
    return [market for market in ("CN", "HK", "US") if market in market_inputs]


def _market_date(market: str, occurred_at: str) -> str:
    return datetime.fromisoformat(occurred_at).astimezone(
        MARKET_TIMEZONES[market]
    ).date().isoformat()


def _any_frozen_report_has_healthy_drawdown(reports_dir: Path) -> bool:
    for market, directory in REPORT_DIRECTORIES.items():
        for path in (reports_dir / directory).glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            metadata = payload.get("metadata")
            drawdown = payload.get("drawdown_summary")
            if (
                isinstance(metadata, dict)
                and str(metadata.get("market") or "").upper() == market
                and isinstance(drawdown, dict)
                and drawdown.get("state_status") == "ok"
            ):
                return True
    return False
