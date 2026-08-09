from __future__ import annotations

import copy
import csv
import fcntl
import hashlib
import json
import os
import uuid
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo

from .models import TradeFill
from .strategy_drawdown import ALLOCATION_DYNAMIC_PARAMETER_NAMES
from .trend_kelly import trend_kelly_identity_matches

EVIDENCE_SCHEMA_VERSION = "open_trader.trend_review.evidence.v1"
REPLAY_SCHEMA_VERSION = "open_trader.trend_review.replay.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_TIMEZONES = {
    "CN": SHANGHAI,
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}
BENCHMARK_IDENTITIES = {
    "CN": {
        "name": "中证 500",
        "source_id": "CSI_500_PRICE",
        "futu_symbol": "SH.000905",
    },
    "HK": {
        "name": "恒生指数",
        "source_id": "HSI_PRICE",
        "futu_symbol": "HK.800000",
    },
    "US": {
        "name": "S&P 500 ETF",
        "source_id": "SPY_QFQ",
        "futu_symbol": "US.SPY",
    },
}
LEGACY_BENCHMARK_IDENTITIES = {
    "CN": {"source_id": "CSI_ALL_SHARE_PRICE", "futu_symbol": "SH.000985"},
    "HK": {"source_id": "HSCI_PRICE", "futu_symbol": "HK.800701"},
}
BENCHMARK_SOURCE_IDS = {
    market: str(identity["source_id"])
    for market, identity in BENCHMARK_IDENTITIES.items()
}
BENCHMARK_FUTU_SYMBOLS = {
    market: str(identity["futu_symbol"])
    for market, identity in BENCHMARK_IDENTITIES.items()
}
TREND_V1_EFFECTIVE_FROM = {
    "CN": "2026-07-16",
    "US": "2026-07-17",
    "HK": "2026-07-17",
}
ACTUAL_FILL_MARKETS_BY_BROKER = {
    "eastmoney": "CN",
    "phillips": "HK",
    "tiger": "US",
}
REJECTED_ORDER_STATUSES = {
    "FAILED",
    "SUBMIT_FAILED",
    "TIMEOUT",
    "DISABLED",
    "DELETED",
    "REJECTED",
}
ACTIVE_ORDER_STATUSES = {
    "SUBMITTING",
    "SUBMITTED",
    "WAITING_SUBMIT",
    "FILLED_PART",
}
TERMINAL_ORDER_STATUSES = REJECTED_ORDER_STATUSES | {
    "CANCELLED",
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "FILLED",
    "FILLED_ALL",
}
RESOLUTION_STATUSES = {
    "confirm-submitted": "resolved_submitted",
    "authorize-retry": "retry_authorized",
    "abandon": "abandoned",
}
PROTECTION_STATE_ROOTS = {
    "CN": "trend_a_share",
    "HK": "trend_hk_phillips",
    "US": "trend_us_tiger",
}
TREND_STRATEGY_VERSIONS = frozenset(
    {
        "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10",
        "v11", "v12", "v13",
    }
)


class TrendReplayIncompleteError(ValueError):
    pass


class TrendReviewAccountStateError(ValueError):
    pass


def benchmark_fact(
    quote: object, market: str, trading_date: str
) -> dict[str, str]:
    market = _market(market)
    symbol = BENCHMARK_FUTU_SYMBOLS[market]
    bars = quote.get_daily_kline(symbol, start=trading_date, end=trading_date)
    bar = next((item for item in bars if item.date == trading_date), None)
    if bar is None:
        raise ValueError(f"benchmark is missing {trading_date}")
    close = _required_decimal(bar.close, "benchmark close")
    if close <= 0:
        raise ValueError("benchmark close must be positive")
    return {
        "date": trading_date,
        "close": format(close.normalize(), "f"),
        "source_id": BENCHMARK_SOURCE_IDS[market],
        "futu_symbol": symbol,
    }


def long_term_benchmark_snapshot_path(data_dir: Path, market: str) -> Path:
    return data_dir / "trend_review" / "long_term_benchmarks" / _market(market) / "latest.json"


def long_term_benchmark_cycle_path(data_dir: Path, market: str, month: str) -> Path:
    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise ValueError("benchmark month must be YYYY-MM") from exc
    if parsed_month.strftime("%Y-%m") != month:
        raise ValueError("benchmark month must be YYYY-MM")
    return (
        data_dir
        / "trend_review"
        / "long_term_benchmarks"
        / _market(market)
        / "cycles"
        / f"{month}.json"
    )


def _long_term_benchmark_attempt_path(data_dir: Path, market: str, month: str) -> Path:
    return (
        long_term_benchmark_cycle_path(data_dir, market, month)
        .parent.parent
        / "attempts"
        / f"{month}.json"
    )


def _record_long_term_benchmark_failure(
    data_dir: Path,
    market: str,
    month: str,
    *,
    now: datetime,
    process_git_sha: str,
    force: bool,
    actor: str,
    reason: str,
    error: BaseException,
    preserve_existing_cycle: bool = False,
) -> None:
    cycle_path = long_term_benchmark_cycle_path(data_dir, market, month)
    try:
        existing = json.loads(cycle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        existing = None
    target = (
        _long_term_benchmark_attempt_path(data_dir, market, month)
        if preserve_existing_cycle
        and isinstance(existing, Mapping)
        and existing.get("schema_version")
        == "open_trader.trend_review.long_term_benchmark.v1"
        and existing.get("status") is None
        else cycle_path
    )
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        previous = {}
    try:
        attempt_count = int(
            previous.get("attempt_count", 0)
            if isinstance(previous, Mapping)
            else 0
        )
    except (TypeError, ValueError):
        attempt_count = 0
    _write_json_atomic(
        target,
        {
            "schema_version": "open_trader.trend_review.long_term_benchmark.attempt.v1",
            "status": "failed",
            "market": market,
            "month": month,
            "attempt_count": attempt_count + 1,
            "attempted_at": now.isoformat(timespec="seconds"),
            "process_git_sha": process_git_sha,
            "reason": str(error),
            "refresh": {
                "force": force,
                "actor": actor.strip() if force else None,
                "reason": reason.strip() if force else None,
            },
        },
    )


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _validated_benchmark_closes(bars: Sequence[object]) -> list[tuple[date, Decimal]]:
    closes: list[tuple[date, Decimal]] = []
    previous: date | None = None
    for bar in bars:
        raw_date = str(
            bar.get("date", "") if isinstance(bar, Mapping) else getattr(bar, "date", "")
        )
        try:
            trading_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError("benchmark date must be ISO-8601") from exc
        if raw_date != trading_date.isoformat():
            raise ValueError("benchmark date must be ISO-8601")
        if previous is not None and trading_date <= previous:
            raise ValueError("benchmark dates must be strictly increasing")
        close = _required_decimal(
            bar.get("close") if isinstance(bar, Mapping) else getattr(bar, "close", None),
            "benchmark close",
        )
        if close <= 0:
            raise ValueError("benchmark close must be positive")
        closes.append((trading_date, close))
        previous = trading_date
    if not closes:
        raise ValueError("benchmark series is empty")
    return closes


def _completed_cycle_matches_latest_snapshot(
    cycle_path: Path,
    snapshot_path: Path,
    *,
    market: str,
    month: str,
) -> bool:
    try:
        cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(cycle, Mapping) or not isinstance(snapshot, Mapping):
            return False
        if (
            cycle.get("schema_version")
            != "open_trader.trend_review.long_term_benchmark.v1"
            or cycle.get("market") != market
            or cycle.get("month") != month
        ):
            return False
        return _canonical_json_bytes(cycle) == _canonical_json_bytes(snapshot)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return False


def _benchmark_window(
    closes: Sequence[tuple[date, Decimal]],
    rates: Mapping[date, Decimal],
    *,
    years: int,
    cutoff: date,
) -> dict[str, object]:
    start = _years_before(cutoff, years)
    before_start = [
        (trading_date, close) for trading_date, close in closes if trading_date < start
    ]
    window = (
        ([before_start[-1]] if before_start else [])
        + [(trading_date, close) for trading_date, close in closes if trading_date >= start]
    )
    if (
        len(window) < 2
        or window[0][0] > start
        or not any(trading_date >= start for trading_date, _ in window)
    ):
        raise ValueError(f"benchmark {years}Y window must cover full period")
    curve = [
        {"date": trading_date.isoformat(), "equity": format(close, "f")}
        for trading_date, close in window
    ]
    metrics = _portfolio_metrics(curve, rates, window[0][1])
    elapsed_days = max(1, (window[-1][0] - window[0][0]).days)
    annualized_return = (
        (window[-1][1] / window[0][1])
        ** (Decimal("365") / Decimal(elapsed_days))
        - Decimal("1")
    ) * Decimal("100") if window[-1][1] > 0 else Decimal("-100")
    daily_returns = []
    for (previous_date, previous_close), (trading_date, close) in zip(window, window[1:]):
        risk_free_return = (
            Decimal("1") + _rate_on_or_before(rates, previous_date) / Decimal("100")
        ) ** (Decimal((trading_date - previous_date).days) / Decimal("365")) - Decimal("1")
        daily_return = close / previous_close - Decimal("1")
        daily_returns.append(
            {
                "date": trading_date.isoformat(),
                "return": format(daily_return, "f"),
                "risk_free_return": format(risk_free_return, "f"),
                "excess_return": format(daily_return - risk_free_return, "f"),
            }
        )
    return {
        "start": start.isoformat(),
        "cutoff": cutoff.isoformat(),
        "observation_count": len(window),
        "daily_returns": daily_returns,
        "metrics": {
            **metrics,
            "annualized_return_pct": format(annualized_return, "f"),
        },
    }


def _validate_long_term_benchmark_snapshot(
    payload: object,
    market: str,
    *,
    rates: Mapping[date, Decimal],
    expected_month: str | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("long-term benchmark snapshot must be an object")
    market = _market(market)
    identity = BENCHMARK_IDENTITIES[market]
    if (
        payload.get("schema_version") != "open_trader.trend_review.long_term_benchmark.v1"
        or payload.get("market") != market
        or payload.get("benchmark")
        != {
            "name": identity["name"],
            "source_id": identity["source_id"],
            "futu_symbol": identity["futu_symbol"],
        }
    ):
        raise ValueError("invalid long-term benchmark snapshot identity")
    closes = payload.get("daily_closes")
    if not isinstance(closes, list):
        raise ValueError("long-term benchmark snapshot closes must be a list")
    parsed = _validated_benchmark_closes(closes)
    if payload.get("cutoff") != parsed[-1][0].isoformat():
        raise ValueError("long-term benchmark snapshot cutoff is invalid")
    month = payload.get("month")
    try:
        parsed_month = datetime.strptime(str(month), "%Y-%m")
    except ValueError as exc:
        raise ValueError("long-term benchmark snapshot month is invalid") from exc
    if parsed_month.strftime("%Y-%m") != month:
        raise ValueError("long-term benchmark snapshot month is invalid")
    if expected_month is not None and month != expected_month:
        raise ValueError("long-term benchmark snapshot month does not match cycle")
    completed_at = payload.get("completed_at")
    try:
        completed_at_value = datetime.fromisoformat(str(completed_at))
    except ValueError as exc:
        raise ValueError("long-term benchmark snapshot timestamp is invalid") from exc
    if completed_at_value.tzinfo is None or completed_at_value.utcoffset() is None:
        raise ValueError("long-term benchmark snapshot timestamp must be timezone-aware")
    refresh = payload.get("refresh")
    if not isinstance(refresh, Mapping) or set(refresh) != {"force", "actor", "reason"}:
        raise ValueError("long-term benchmark snapshot refresh is invalid")
    if refresh.get("force") is True:
        if not isinstance(refresh.get("actor"), str) or not refresh["actor"].strip():
            raise ValueError("long-term benchmark force actor is invalid")
        if not isinstance(refresh.get("reason"), str) or not refresh["reason"].strip():
            raise ValueError("long-term benchmark force reason is invalid")
    elif (
        refresh.get("force") is not False
        or refresh.get("actor") is not None
        or refresh.get("reason") is not None
    ):
        raise ValueError("long-term benchmark snapshot refresh is invalid")
    windows = payload.get("windows")
    if not isinstance(windows, Mapping) or set(windows) != {"1Y", "5Y"}:
        raise ValueError("long-term benchmark snapshot windows are invalid")
    for label, years in (("1Y", 1), ("5Y", 5)):
        window = windows[label]
        if not isinstance(window, Mapping):
            raise ValueError("long-term benchmark snapshot window is invalid")
        try:
            expected_window = _benchmark_window(
                parsed, rates, years=years, cutoff=parsed[-1][0]
            )
        except ValueError as exc:
            raise ValueError(f"long-term benchmark {label} window is invalid") from exc
        if dict(window) != expected_window:
            raise ValueError("long-term benchmark metrics are invalid")
    return dict(payload)


def read_long_term_benchmark_snapshot(data_dir: Path, market: str) -> dict[str, object]:
    path = long_term_benchmark_snapshot_path(data_dir, market)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read long-term benchmark snapshot: {path}") from exc
    rates = _load_dgs3mo_csv(data_dir / "rates" / "DGS3MO.csv")
    return _validate_long_term_benchmark_snapshot(payload, market, rates=rates)


def _refresh_long_term_benchmark_locked(
    data_dir: Path,
    market: str,
    quote: object,
    *,
    now: datetime,
    process_git_sha: str,
    force: bool = False,
    actor: str = "",
    reason: str = "",
) -> dict[str, object]:
    market = _market(market)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("benchmark refresh now must be timezone-aware")
    if force and (not actor.strip() or not reason.strip()):
        raise ValueError("forced benchmark refresh requires actor and reason")
    market_now = now.astimezone(MARKET_TIMEZONES[market])
    month = market_now.strftime("%Y-%m")
    cycle_path = long_term_benchmark_cycle_path(data_dir, market, month)
    snapshot_path = long_term_benchmark_snapshot_path(data_dir, market)
    existing_cycle_is_valid = False
    if force and cycle_path.exists():
        existing_cycle_is_valid = _completed_cycle_matches_latest_snapshot(
            cycle_path, snapshot_path, market=market, month=month
        )
    if cycle_path.exists() and not force:
        try:
            rates = _load_dgs3mo_csv(data_dir / "rates" / "DGS3MO.csv")
            payload = _validate_long_term_benchmark_snapshot(
                json.loads(cycle_path.read_text(encoding="utf-8")),
                market,
                rates=rates,
                expected_month=month,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            try:
                previous = json.loads(cycle_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                previous = None
            retryable_failure = (
                isinstance(previous, Mapping)
                and previous.get("schema_version")
                == "open_trader.trend_review.long_term_benchmark.attempt.v1"
                and previous.get("status") == "failed"
                and previous.get("market") == market
                and previous.get("month") == month
            )
            if not retryable_failure:
                try:
                    _record_long_term_benchmark_failure(
                        data_dir,
                        market,
                        month,
                        now=now,
                        process_git_sha=process_git_sha,
                        force=False,
                        actor="",
                        reason="",
                        error=exc,
                    )
                except Exception:
                    pass
                return {
                    "status": "failed",
                    "market": market,
                    "month": month,
                    "error": str(exc),
                }
        else:
            existing_cycle_is_valid = True
            try:
                current_snapshot = read_long_term_benchmark_snapshot(data_dir, market)
            except ValueError:
                _write_json_atomic(snapshot_path, payload)
            else:
                if _canonical_json_bytes(current_snapshot) != _canonical_json_bytes(payload):
                    _write_json_atomic(snapshot_path, payload)
            return {
                "status": "already_completed",
                "market": market,
                "month": month,
                "cutoff": payload["cutoff"],
            }
    try:
        as_of = market_now.date()
        bars = quote.get_daily_kline(
            BENCHMARK_FUTU_SYMBOLS[market],
            start=(_years_before(as_of, 5) - timedelta(days=7)).isoformat(),
            end=as_of.isoformat(),
        )
        closes = _validated_benchmark_closes(bars)
        cutoff = closes[-1][0]
        if closes[0][0] > _years_before(cutoff, 5):
            raise ValueError("benchmark series must cover five years")
        rates = _load_dgs3mo_csv(data_dir / "rates" / "DGS3MO.csv")
        identity = BENCHMARK_IDENTITIES[market]
        payload = {
            "schema_version": "open_trader.trend_review.long_term_benchmark.v1",
            "market": market,
            "month": month,
            "completed_at": now.isoformat(),
            "process_git_sha": process_git_sha,
            "refresh": {
                "force": force,
                "actor": actor.strip() if force else None,
                "reason": reason.strip() if force else None,
            },
            "benchmark": {
                "name": identity["name"],
                "source_id": identity["source_id"],
                "futu_symbol": identity["futu_symbol"],
            },
            "cutoff": cutoff.isoformat(),
            "daily_closes": [
                {"date": trading_date.isoformat(), "close": format(close, "f")}
                for trading_date, close in closes
            ],
            "windows": {
                "1Y": _benchmark_window(closes, rates, years=1, cutoff=cutoff),
                "5Y": _benchmark_window(closes, rates, years=5, cutoff=cutoff),
            },
        }
        _validate_long_term_benchmark_snapshot(payload, market, rates=rates)
    except Exception as exc:
        _record_long_term_benchmark_failure(
            data_dir,
            market,
            month,
            now=now,
            process_git_sha=process_git_sha,
            force=force,
            actor=actor,
            reason=reason,
            error=exc,
            preserve_existing_cycle=existing_cycle_is_valid,
        )
        return {"status": "failed", "market": market, "month": month, "error": str(exc)}
    _write_json_atomic(snapshot_path, payload)
    _write_json_atomic(cycle_path, payload)
    return {"status": "completed", "market": market, "month": month, "cutoff": payload["cutoff"]}


def refresh_long_term_benchmark(
    data_dir: Path,
    market: str,
    quote: object,
    *,
    now: datetime,
    process_git_sha: str,
    force: bool = False,
    actor: str = "",
    reason: str = "",
) -> dict[str, object]:
    market = _market(market)
    lock_root = data_dir / "trend_review" / "long_term_benchmarks" / market
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_root / ".refresh.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _refresh_long_term_benchmark_locked(
            data_dir,
            market,
            quote,
            now=now,
            process_git_sha=process_git_sha,
            force=force,
            actor=actor,
            reason=reason,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            _json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _create_immutable(path: Path, body: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != body:
            raise FileExistsError(f"immutable artifact collision: {path}") from None
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def _write_immutable(path: Path, body: bytes) -> Path:
    _create_immutable(path, body)
    return path


def _write_immutable_batch(artifacts: Sequence[tuple[Path, bytes]]) -> None:
    prepared: dict[Path, bytes] = {}
    for path, body in artifacts:
        previous = prepared.get(path)
        if previous is not None and previous != body:
            raise FileExistsError(f"immutable artifact collision: {path}")
        prepared[path] = body
    for path, body in prepared.items():
        if path.exists() and path.read_bytes() != body:
            raise FileExistsError(f"immutable artifact collision: {path}")

    created: list[Path] = []
    try:
        for path, body in prepared.items():
            if _create_immutable(path, body):
                created.append(path)
    except Exception:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _market(value: object) -> str:
    market = str(value).upper()
    if market not in {"CN", "US", "HK"}:
        raise ValueError(f"unsupported trend review market: {value}")
    return market


def trend_action_futu_symbol(
    report: Mapping[str, object],
    action: Mapping[str, object],
    market: str,
) -> str:
    from .futu_symbols import to_futu_symbol
    from .trend_animals import TREND_SYMBOL_MAPPING_SCHEMA

    metadata = report.get("metadata")
    marker = (
        metadata.get("symbol_mapping_schema")
        if isinstance(metadata, Mapping)
        else None
    )
    if marker is None:
        return to_futu_symbol(market, str(action.get("symbol") or ""))
    if marker != TREND_SYMBOL_MAPPING_SCHEMA:
        raise ValueError("trend report symbol mapping schema is unsupported")
    frozen = action.get("futu_symbol")
    if not isinstance(frozen, str) or not frozen.strip():
        raise ValueError("trend action frozen Futu symbol is unavailable")
    canonical = to_futu_symbol(market, frozen)
    if frozen != canonical:
        raise ValueError("trend action frozen Futu symbol is invalid")
    return frozen


def _fact_path(
    data_dir: Path, stream: str, market: str, trading_date: str
) -> Path:
    return (
        data_dir
        / "trend_review"
        / "facts"
        / stream
        / market
        / f"{trading_date}.json"
    )


def freeze_discipline_fact(
    data_dir: Path,
    market: str,
    trading_date: str,
    equity: object,
    orders: Sequence[Mapping[str, object]],
    strategy_snapshot: Mapping[str, object],
) -> Path:
    market = _market(market)
    payload = {
        "schema_version": "open_trader.trend_review.discipline.v1",
        "market": market,
        "date": trading_date,
        "equity_after_fees": str(_required_decimal(equity, "discipline equity")),
        "orders": [dict(order) for order in orders],
        "strategy_snapshot": dict(strategy_snapshot),
    }
    return _write_immutable(
        _fact_path(data_dir, "discipline", market, trading_date),
        _canonical_json_bytes(payload),
    )


def freeze_actual_equity_fact(
    data_dir: Path,
    market: str,
    trading_date: str,
    equity: object,
    opening_positions: Sequence[Mapping[str, object]],
    strategy_snapshot: Mapping[str, object],
) -> Path:
    market = _market(market)
    payload = {
        "schema_version": "open_trader.trend_review.actual_equity.v1",
        "market": market,
        "date": trading_date,
        "equity": str(_required_decimal(equity, "actual equity")),
        "opening_positions": [dict(position) for position in opening_positions],
        "strategy_snapshot": dict(strategy_snapshot),
    }
    return _write_immutable(
        _fact_path(data_dir, "actual_equity", market, trading_date),
        _canonical_json_bytes(payload),
    )


def freeze_benchmark_fact(
    data_dir: Path,
    market: str,
    trading_date: str,
    benchmark: Mapping[str, object],
) -> Path:
    market = _market(market)
    validated = _validate_benchmark(
        benchmark, market=market, trading_date=trading_date
    )
    payload = {
        "schema_version": "open_trader.trend_review.benchmark.v1",
        "market": market,
        "date": trading_date,
        "benchmark": dict(validated),
    }
    return _write_immutable(
        _fact_path(data_dir, "benchmark", market, trading_date),
        _canonical_json_bytes(payload),
    )


def _actual_fill_identity(fill: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(fill["broker"]),
        str(fill["account_alias"]),
        str(fill["source_id"]),
    )


def _compatible_actual_fill_body(
    path: Path, payload: Mapping[str, object]
) -> bytes:
    body = _canonical_json_bytes(payload)
    if not path.exists():
        return body
    existing_body = path.read_bytes()
    try:
        existing = json.loads(existing_body)
    except (UnicodeError, json.JSONDecodeError):
        return body
    if not isinstance(existing, dict) or "source_sequence" not in existing:
        return body
    source_sequence = existing.pop("source_sequence")
    if source_sequence is not None and (
        not isinstance(source_sequence, int)
        or isinstance(source_sequence, bool)
        or source_sequence < 0
    ):
        return body
    return existing_body if _canonical_json_bytes(existing) == body else body


def freeze_actual_fill_batch(
    data_dir: Path,
    source_metadata: Mapping[str, object],
    fills: Sequence[TradeFill],
    complete_through: str,
    *,
    coverage_start: str | None = None,
) -> list[Path]:
    try:
        complete_date = date.fromisoformat(complete_through)
    except ValueError:
        raise ValueError("actual fill complete_through must be an ISO date") from None
    if complete_date.isoformat() != complete_through:
        raise ValueError("actual fill complete_through must be an ISO date")
    coverage_start = coverage_start or complete_through
    try:
        coverage_start_date = date.fromisoformat(coverage_start)
    except ValueError:
        raise ValueError("actual fill coverage_start must be an ISO date") from None
    if (
        coverage_start_date.isoformat() != coverage_start
        or coverage_start_date > complete_date
    ):
        raise ValueError("actual fill coverage_start must not follow complete_through")
    broker = str(source_metadata.get("broker") or "").lower()
    broker_market = ACTUAL_FILL_MARKETS_BY_BROKER.get(broker)
    explicit_market = source_metadata.get("market")
    raw_market = explicit_market if explicit_market is not None else broker_market
    if raw_market is None and fills:
        raw_market = fills[0].market
    market = _market(raw_market)
    if broker_market is not None and market != broker_market:
        raise ValueError("actual fill source metadata market does not match broker")
    paths: list[Path] = []
    identities: list[tuple[str, str, str]] = []
    artifacts: list[tuple[Path, bytes]] = []
    fill_order: list[dict[str, object]] = []
    for fill in fills:
        if not isinstance(fill, TradeFill):
            raise ValueError("actual fill must be a TradeFill")
        if _market(fill.market) != market:
            raise ValueError("actual fill market does not match source metadata")
        payload = {
            "schema_version": "open_trader.trend_review.fill.v1",
            **asdict(fill),
        }
        if any(
            not str(payload.get(field) or "").strip()
            for field in (
                "source_id",
                "broker",
                "account_alias",
                "symbol",
                "currency",
                "executed_at",
            )
        ):
            raise ValueError("actual fill has missing required fields")
        if payload["side"] not in {"BUY", "SELL"}:
            raise ValueError("actual fill side must be BUY or SELL")
        if _required_decimal(payload["quantity"], "fill quantity") <= 0:
            raise ValueError("fill quantity must be positive")
        if _required_decimal(payload["price"], "fill price") <= 0:
            raise ValueError("fill price must be positive")
        if payload["fees"] is not None:
            _required_decimal(payload["fees"], "fill fees")
        source_sequence = payload.get("source_sequence")
        if source_sequence is not None and (
            not isinstance(source_sequence, int)
            or isinstance(source_sequence, bool)
            or source_sequence < 0
        ):
            raise ValueError("actual fill source_sequence must be a non-negative integer")
        payload.pop("source_sequence")
        executed_at = str(payload["executed_at"])
        try:
            execution_date = (
                date.fromisoformat(executed_at)
                if len(executed_at) == 10
                else datetime.fromisoformat(executed_at.replace("Z", "+00:00")).date()
            )
        except ValueError:
            raise ValueError(
                "actual fill executed_at must be an ISO date or timestamp"
            ) from None
        if execution_date > complete_date:
            raise ValueError("actual fill is later than complete_through")
        identity = _actual_fill_identity(payload)
        digest = hashlib.sha256(
            _canonical_json_bytes({"identity": identity})
        ).hexdigest()
        path = (
            data_dir
            / "trend_review"
            / "facts"
            / "actual_fills"
            / market
            / f"{digest}.json"
        )
        paths.append(path)
        artifacts.append((path, _compatible_actual_fill_body(path, payload)))
        identities.append(identity)
        fill_order.append(
            {
                "identity": list(identity),
                "source_sequence": source_sequence,
            }
        )
    completeness = {
        "schema_version": "open_trader.trend_review.fill_completeness.v1",
        "market": market,
        "complete_through": complete_through,
        "coverage_start": coverage_start,
        "coverage_end": complete_through,
        "source_metadata": dict(source_metadata),
        "fill_identities": sorted(identities),
        "fill_order": fill_order,
    }
    digest = hashlib.sha256(_canonical_json_bytes(completeness)).hexdigest()
    artifacts.append(
        (
            data_dir
            / "trend_review"
            / "facts"
            / "actual_fill_completeness"
            / market
            / f"{digest}.json",
            _canonical_json_bytes(completeness),
        )
    )
    _write_immutable_batch(artifacts)
    return paths


def freeze_trend_evidence(
    data_dir: Path, evidence: Mapping[str, object]
) -> dict[str, str]:
    payload = {"schema_version": EVIDENCE_SCHEMA_VERSION, **dict(evidence)}
    body = _canonical_json_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    path = (
        data_dir
        / "trend_review"
        / "evidence"
        / _market(payload.get("market"))
        / f"{digest}.json"
    )
    _write_immutable(path, body)
    return {"path": str(path), "sha256": digest}


def freeze_report_evidence(
    *,
    data_dir: Path,
    report: object,
    candidates: object,
    holding_snapshots: object,
    bars_by_symbol: object,
    prior_state: object,
    watch_events: object,
    query: Mapping[str, object],
    responses: Mapping[str, object],
    candidate_pool_ids: object,
    lot_sizes: Mapping[str, int],
    price_fx_to_account_currency: Decimal,
    previous_attention_rows: object,
    option_attention_broker_label: str | None,
    kelly_rounds: object = (),
    kelly_data_reason: str = "",
    real_holdings_input: object | None = None,
) -> dict[str, str]:
    metadata = getattr(report, "metadata")
    strategy_snapshot = getattr(report, "strategy_snapshot")
    risk_summary = getattr(report, "risk_summary")
    account_input = getattr(report, "account_input", {})
    frozen_allocation = getattr(report, "allocation", None)
    allocation_evidence: dict[str, object] | None = None
    if frozen_allocation is not None:
        from .a_share_trend import valid_frozen_allocation

        if not valid_frozen_allocation(frozen_allocation):
            raise ValueError("frozen allocation is invalid")
        daily_path = PurePosixPath(str(frozen_allocation["daily_path"]))
        try:
            relative_path = daily_path.relative_to("data")
            body = (data_dir / relative_path).read_bytes()
        except (ValueError, OSError):
            raise ValueError("frozen allocation evidence is unavailable") from None
        if hashlib.sha256(body).hexdigest() != frozen_allocation["sha256"]:
            raise ValueError("frozen allocation evidence hash mismatch")
        allocation_evidence = {
            "reference": dict(frozen_allocation),
            "daily_json": body.decode("utf-8"),
        }
    evidence = {
        "market": str(metadata.get("market") or "CN"),
        "report_id": getattr(report, "as_of_date"),
        "query": dict(query),
        "responses": dict(responses),
        "market_data": bars_by_symbol,
        "account": getattr(report, "account"),
        "strategy_snapshot": strategy_snapshot,
        "industry_contexts": getattr(report, "industry_contexts", ()),
        "industry_context_status": getattr(report, "industry_context_status", {}),
        "estimated_api_cost_complete": getattr(
            report, "estimated_api_cost_complete", True
        ),
        "process_version": str(strategy_snapshot.get("process_version") or ""),
        "rebuild_inputs": {
            "as_of_date": getattr(report, "as_of_date"),
            "execution_date": getattr(report, "execution_date"),
            "account": getattr(report, "account"),
            "candidates": candidates,
            "holding_snapshots": holding_snapshots,
            "bars_by_symbol": bars_by_symbol,
            "prior_state": prior_state,
            "watch_events": watch_events,
            "api_facts": getattr(report, "api_facts"),
            "data_sources": getattr(report, "data_sources"),
            "estimated_api_cost": getattr(report, "estimated_api_cost"),
            "actual_api_cost": getattr(report, "actual_api_cost"),
            "industry_contexts": getattr(report, "industry_contexts", ()),
            "industry_context_status": getattr(
                report, "industry_context_status", {}
            ),
            "estimated_api_cost_complete": getattr(
                report, "estimated_api_cost_complete", True
            ),
            "market": str(metadata.get("market") or "CN"),
            "lot_sizes": dict(lot_sizes),
            "position_weight": metadata.get("position_weight"),
            "position_weight_source": metadata.get("position_weight_source"),
            "price_fx_to_account_currency": price_fx_to_account_currency,
            "normal_cost_rate": risk_summary.get("normal_cost_rate"),
            "drawdown_summary": getattr(report, "drawdown_summary", None),
            "option_attention": {
                "previous_rows": previous_attention_rows,
                "broker_label": option_attention_broker_label,
            },
            "candidate_pool_ids": candidate_pool_ids,
            "generated_at": getattr(report, "generated_at"),
            "metadata": metadata,
            "managed_symbols": list(
                getattr(report, "protection_state").get("managed_symbols", [])
            ),
            "kelly_rounds": kelly_rounds,
            "kelly_data_reason": kelly_data_reason,
            **(
                {"account_input": dict(account_input)}
                if isinstance(account_input, Mapping) and account_input
                else {}
            ),
            **(
                {"real_holdings": real_holdings_input}
                if real_holdings_input is not None
                else {}
            ),
            "simulate_rotation_pairs": getattr(report, "simulate_rotation_pairs", ()),
            "real_rotation_pairs": getattr(report, "real_rotation_pairs", ()),
            "simulate_rotation_comparisons": getattr(
                report, "simulate_rotation_comparisons", ()
            ),
            "real_rotation_comparisons": getattr(
                report, "real_rotation_comparisons", ()
            ),
            **(
                {"allocation": allocation_evidence}
                if allocation_evidence is not None
                else {}
            ),
        },
    }
    return freeze_trend_evidence(data_dir, evidence)


def _load_valid_evidence(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid trend review evidence: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError(f"invalid trend review evidence: {path}")
    _market(payload.get("market"))
    return payload


def replay_trend_evidence(
    evidence_path: Path,
    data_dir: Path,
    *,
    fixed_process_version: str,
    rebuild: Callable[[dict[str, object]], dict[str, object]],
    replayed_at: str | None = None,
) -> Path:
    original = _load_valid_evidence(evidence_path)
    replay_input = copy.deepcopy(original)
    replay_input["process_version"] = fixed_process_version
    corrected = rebuild(replay_input)
    payload = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "market": original["market"],
        "original_evidence_path": str(evidence_path),
        "original_evidence_sha256": hashlib.sha256(
            evidence_path.read_bytes()
        ).hexdigest(),
        "fixed_process_version": fixed_process_version,
        "replayed_at": replayed_at
        or datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "corrected_report": corrected,
    }
    body = _canonical_json_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    return _write_immutable(
        data_dir
        / "trend_review"
        / "replays"
        / _market(original["market"])
        / f"{digest}.json",
        body,
    )


def _required_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _report_hash(report: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(report)).hexdigest()


def _protection_report(symbol: str, event_id: str) -> dict[str, object]:
    return {
        "strategy_snapshot": {"strategy_version": "protection-v1"},
        "strategy_judgments": {
            "formal_actions": [
                {
                    "action": "SELL_ALL",
                    "symbol": symbol,
                    "event_id": event_id,
                    "reason": "protection_event",
                }
            ]
        },
    }


def _protection_event_identity(
    event: Mapping[str, object],
    *,
    market: str,
    execution_date: str,
    action_key: str,
) -> tuple[str, int, str] | None:
    if (
        event.get("status") != "reason_added"
        or event.get("strategy_version") != "protection-v1"
    ):
        return None
    symbol = str(event.get("symbol") or "").strip()
    event_id = str(event.get("reason_id") or "").strip()
    futu_code = str(event.get("futu_code") or "").strip().upper()
    try:
        recorded_at = datetime.fromisoformat(str(event["recorded_at"]))
        from .futu_symbols import to_futu_symbol

        expected_futu_code = to_futu_symbol(market, symbol)
    except (KeyError, TypeError, ValueError):
        return None
    report_sha = _report_hash(_protection_report(symbol, event_id))
    if (
        not symbol
        or not event_id
        or event.get("market") != market
        or event.get("date") != execution_date
        or event.get("action_index") != 0
        or event.get("side") != "sell"
        or event.get("reason") != "protection_event"
        or event.get("sell_goal") != "position_zero"
        or futu_code != expected_futu_code
        or action_key
        != trend_action_key(market, execution_date, futu_code, "sell")
        or event.get("report_sha256") != report_sha
        or recorded_at.tzinfo is None
        or recorded_at.utcoffset() is None
    ):
        return None
    return report_sha, 0, "protection-v1"


def _protection_fact_identities(
    data_dir: Path, *, market: str, execution_date: str
) -> set[tuple[str, str, str]]:
    identities: set[tuple[str, str, str]] = set()
    actions_root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
    )
    for action_root in sorted(actions_root.glob("*")):
        if not action_root.is_dir():
            continue
        for event in _action_events(action_root):
            identity = _protection_event_identity(
                event,
                market=market,
                execution_date=execution_date,
                action_key=action_root.name,
            )
            if identity is not None:
                identities.add(
                    (
                        identity[0],
                        str(event["futu_code"]).strip().upper(),
                        "sell",
                    )
                )
    return identities


def _result_path(intent_path: Path) -> Path:
    return intent_path.with_name(intent_path.name.replace("-intent", "-result"))


def _intent_path(result_path: Path) -> Path:
    return result_path.with_name(result_path.name.replace("-result", "-intent"))


def _ledger_fact_paths(root: Path) -> list[Path]:
    intents = list(root.glob("*-intent.json"))
    results = [
        path for path in root.glob("*-result.json") if not _intent_path(path).exists()
    ]
    return sorted([*intents, *results])


def _result_request(path: Path, payload: object) -> dict[str, object]:
    request = payload.get("request") if isinstance(payload, Mapping) else None
    response = payload.get("response") if isinstance(payload, Mapping) else None
    try:
        quantity = _required_decimal(
            request.get("qty") if isinstance(request, Mapping) else None,
            "result quantity",
        )
    except ValueError:
        quantity = Decimal("0")
    if (
        not isinstance(request, dict)
        or not isinstance(response, Mapping)
        or not str(request.get("futu_code") or "").strip()
        or not str(request.get("side") or "").strip()
        or not str(request.get("remark") or "").strip()
        or quantity <= 0
    ):
        raise ValueError(f"invalid trend review result: {path}")
    return request


def _ledger_fact_attempt(
    path: Path, payload: Mapping[str, object], request: Mapping[str, object]
) -> int:
    candidates: list[int] = []
    raw_attempt = payload.get("attempt")
    if raw_attempt is not None:
        if isinstance(raw_attempt, bool):
            raise ValueError(f"invalid trend review attempt: {path}")
        try:
            candidates.append(int(raw_attempt))
        except (TypeError, ValueError):
            raise ValueError(f"invalid trend review attempt: {path}") from None
    marker = "-attempt-"
    if marker in path.name:
        try:
            candidates.append(int(path.name.rsplit(marker, 1)[1].split("-", 1)[0]))
        except ValueError:
            raise ValueError(f"invalid trend review attempt: {path}") from None
    remark = str(request.get("remark") or "")
    if remark.startswith("trend:"):
        try:
            candidates.append(int(remark.rsplit(":", 1)[1]))
        except ValueError:
            raise ValueError(f"invalid trend review attempt: {path}") from None
    attempts = set(candidates or [1])
    if len(attempts) != 1 or next(iter(attempts)) <= 0:
        raise ValueError(f"invalid trend review attempt: {path}")
    return attempts.pop()


def trend_action_key(
    market: str, execution_date: str, futu_code: str, side: str
) -> str:
    identity = ":".join(
        (
            _market(market),
            date.fromisoformat(execution_date).isoformat(),
            futu_code.strip().upper(),
            side.strip().lower(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def trend_attempt_remark(
    market: str, execution_date: str, action_key: str, attempt: int
) -> str:
    if attempt <= 0:
        raise ValueError("attempt must be positive")
    remark = f"trend:{_market(market)}:{execution_date}:{action_key[:20]}:{attempt}"
    if len(remark.encode("utf-8")) > 64:
        raise ValueError("trend order remark exceeds Futu's 64-byte limit")
    return remark


def _validate_execution_batch(
    payload: object, *, market: str, execution_date: str
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("trend execution batch must be a JSON object")
    try:
        locked_at = datetime.fromisoformat(str(payload["locked_at"]))
    except (KeyError, ValueError):
        raise ValueError("trend execution batch has an invalid locked_at") from None
    report_sha = payload.get("report_sha256")
    if (
        payload.get("schema_version") != "open_trader.trend_review.batch.v1"
        or payload.get("market") != market
        or payload.get("execution_date") != execution_date
        or not isinstance(payload.get("report_path"), str)
        or not payload["report_path"]
        or not isinstance(report_sha, str)
        or len(report_sha) != 64
        or any(character not in "0123456789abcdef" for character in report_sha)
        or locked_at.tzinfo is None
        or locked_at.utcoffset() is None
    ):
        raise ValueError("trend execution batch is invalid")
    return payload


def lock_trend_execution_batch(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    report_path: Path,
    report: Mapping[str, object],
    locked_at: str,
) -> dict[str, object]:
    market = _market(market)
    execution_date = date.fromisoformat(execution_date).isoformat()
    path = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "batches"
        / f"{execution_date}.json"
    )
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid trend execution batch: {path}") from exc
        return _validate_execution_batch(
            existing, market=market, execution_date=execution_date
        )
    legacy_facts: list[tuple[datetime, str]] = []
    protection_facts = _protection_fact_identities(
        data_dir, market=market, execution_date=execution_date
    )
    ledger_root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date
    )
    for fact_path in _ledger_fact_paths(ledger_root):
        try:
            fact = json.loads(fact_path.read_text(encoding="utf-8"))
            if not isinstance(fact, dict):
                raise TypeError
            timestamp_field = (
                "submitted_at"
                if fact_path.name.endswith("-result.json")
                else "created_at"
            )
            request = (
                _result_request(fact_path, fact)
                if timestamp_field == "submitted_at"
                else fact["request"]
            )
            if not isinstance(request, Mapping):
                raise TypeError
            created_at = datetime.fromisoformat(str(fact[timestamp_field]))
            report_sha = fact["report_sha256"]
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"trend execution batch is blocked by invalid ledger fact: {fact_path}"
            ) from exc
        if (
            created_at.tzinfo is None
            or created_at.utcoffset() is None
            or not isinstance(report_sha, str)
            or len(report_sha) != 64
            or any(character not in "0123456789abcdef" for character in report_sha)
        ):
            raise ValueError(
                f"trend execution batch is blocked by invalid ledger fact: {fact_path}"
            )
        fact_identity = (
            report_sha,
            str(request.get("futu_code") or "").strip().upper(),
            str(request.get("side") or "").strip().lower(),
        )
        exact_protection_fact = False
        if fact_identity in protection_facts:
            try:
                quantity = _required_decimal(request.get("qty"), "target quantity")
                attempt = _ledger_fact_attempt(fact_path, fact, request)
                action_key = trend_action_key(
                    market, execution_date, fact_identity[1], fact_identity[2]
                )
            except (TypeError, ValueError):
                pass
            else:
                exact_protection_fact = (
                    fact.get("market") == market
                    and fact.get("date") == execution_date
                    and fact.get("action_index") == 0
                    and request.get("market") == market
                    and request.get("remark")
                    == trend_attempt_remark(
                        market, execution_date, action_key, attempt
                    )
                    and quantity > 0
                )
        if not exact_protection_fact:
            legacy_facts.append((created_at, report_sha))
    selected_path = report_path
    selected_sha = _report_hash(report)
    if legacy_facts:
        selected_sha = min(legacy_facts, key=lambda item: item[0])[1]
        matches: list[Path] = []
        for candidate in sorted(report_path.parent.glob("*.json")):
            try:
                candidate_report = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(candidate_report, Mapping)
                and _report_hash(candidate_report) == selected_sha
            ):
                matches.append(candidate)
        if not matches:
            raise ValueError(
                "trend execution batch is blocked: no matching report artifact"
            )
        selected_path = matches[0]
    payload = _validate_execution_batch(
        {
            "schema_version": "open_trader.trend_review.batch.v1",
            "market": market,
            "execution_date": execution_date,
            "report_path": str(selected_path),
            "report_sha256": selected_sha,
            "locked_at": locked_at,
        },
        market=market,
        execution_date=execution_date,
    )
    try:
        _write_immutable(path, _canonical_json_bytes(payload))
    except FileExistsError:
        try:
            concurrent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid trend execution batch: {path}") from exc
        return _validate_execution_batch(
            concurrent, market=market, execution_date=execution_date
        )
    return payload


def _valid_rotation_reservation(
    payload: object,
    *,
    market: str,
    account_key: str,
    execution_date: str,
    pair_index: int,
    allocation_sha256: str,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("rotation reservation is invalid")
    try:
        reserved_at = datetime.fromisoformat(str(payload["reserved_at"]))
    except (KeyError, ValueError):
        raise ValueError("rotation reservation is invalid") from None
    stored_allocation_sha256 = payload.get("allocation_sha256")
    pair = payload.get("pair")
    if (
        payload.get("schema_version") != "open_trader.trend_review.rotation_plan.v1"
        or payload.get("market") != market
        or payload.get("account_key") != account_key
        or payload.get("execution_date") != execution_date
        or payload.get("pair_index") != pair_index
        or reserved_at.tzinfo is None
        or reserved_at.utcoffset() is None
        or not isinstance(stored_allocation_sha256, str)
        or len(stored_allocation_sha256) != 64
        or any(character not in "0123456789abcdef" for character in stored_allocation_sha256)
        or stored_allocation_sha256 != allocation_sha256
        or not _valid_rotation_pair(pair, pair_index)
    ):
        raise ValueError("rotation reservation is invalid")
    return pair


def _valid_rotation_pair(pair: object, pair_index: int) -> bool:
    if (
        isinstance(pair_index, bool)
        or not isinstance(pair, dict)
        or pair.get("pair_index") != pair_index
    ):
        return False
    if any(
        not isinstance(pair.get(field), str) or not pair[field].strip()
        for field in (
            "sell_symbol", "sell_name", "sell_futu_symbol", "buy_symbol",
            "buy_name", "buy_futu_symbol", "reason",
        )
    ):
        return False
    try:
        weight = Decimal(str(pair.get("target_weight")))
        amount = Decimal(str(pair.get("target_amount")))
        atr = Decimal(str(pair.get("atr")))
    except (InvalidOperation, ValueError):
        return False
    if (
        not all(value.is_finite() for value in (weight, amount, atr))
        or not Decimal("0") < weight <= Decimal("1")
        or amount <= 0
        or atr <= 0
        or pair["sell_symbol"] == pair["buy_symbol"]
        or pair["sell_futu_symbol"] == pair["buy_futu_symbol"]
        or pair["reason"] != "relative_rotation"
    ):
        return False
    basis = pair.get("strength_basis")
    if basis is None:
        try:
            sell_strength = Decimal(str(pair.get("sell_global_strength")))
            buy_strength = Decimal(str(pair.get("buy_global_strength")))
            gap = Decimal(str(pair.get("strength_gap")))
        except (InvalidOperation, ValueError):
            return False
        if (
            not all(value.is_finite() for value in (sell_strength, buy_strength, gap))
            or not Decimal("0") <= sell_strength <= Decimal("100")
            or not Decimal("0") <= buy_strength <= Decimal("100")
            or gap != buy_strength - sell_strength
            or gap < Decimal("20")
        ):
            return False
    elif basis in {"local", "global"}:
        sell_asset = str(pair.get("sell_asset") or "")
        buy_asset = str(pair.get("buy_asset") or "")
        if not sell_asset or not buy_asset:
            return False
        try:
            optional = lambda name: (
                None
                if pair.get(name) is None or str(pair.get(name)).strip() == ""
                else Decimal(str(pair.get(name)))
            )
            sell_local = optional("sell_local_strength")
            buy_local = optional("buy_local_strength")
            sell_global = optional("sell_global_strength")
            buy_global = optional("buy_global_strength")
            sell_compared = optional("sell_compared_strength")
            buy_compared = optional("buy_compared_strength")
            gap = optional("strength_gap")
            threshold = Decimal(str(pair.get("threshold")))
        except (InvalidOperation, ValueError):
            return False
        if threshold != Decimal("20") or gap is None:
            return False
        if basis == "local":
            if sell_asset != buy_asset or sell_compared != sell_local or buy_compared != buy_local:
                return False
        else:
            if sell_asset == buy_asset or sell_compared != sell_global or buy_compared != buy_global:
                return False
        if (
            sell_compared is None
            or buy_compared is None
            or not all(value.is_finite() for value in (sell_compared, buy_compared, gap))
            or not Decimal("0") <= sell_compared <= Decimal("100")
            or not Decimal("0") <= buy_compared <= Decimal("100")
            or gap != buy_compared - sell_compared
            or gap < threshold
        ):
            return False
    else:
        return False
    valid_sizes = all(
        isinstance(pair.get(field), int)
        and not isinstance(pair.get(field), bool)
        and pair[field] > 0
        for field in ("estimated_shares", "lot_size")
    )
    return valid_sizes and pair["estimated_shares"] % pair["lot_size"] == 0


def reserve_rotation_pairs(
    data_dir: Path,
    *,
    market: str,
    account_key: str,
    execution_date: str,
    pairs: Sequence[Mapping[str, object]],
    allocation_sha256: str,
    reserved_at: str,
) -> tuple[dict[str, object], ...]:
    """Freeze at most two account-specific relative-rotation pairs."""
    market = _market(market)
    execution_date = date.fromisoformat(execution_date).isoformat()
    if (
        not account_key
        or not account_key.startswith(("simulate-", "real-"))
        or account_key in {"simulate-", "real-"}
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in account_key
        )
        or not isinstance(allocation_sha256, str)
        or len(allocation_sha256) != 64
        or any(character not in "0123456789abcdef" for character in allocation_sha256)
    ):
        raise ValueError("rotation reservation facts are invalid")
    try:
        parsed_reserved_at = datetime.fromisoformat(reserved_at)
    except ValueError:
        raise ValueError("rotation reservation facts are invalid") from None
    if parsed_reserved_at.tzinfo is None or parsed_reserved_at.utcoffset() is None:
        raise ValueError("rotation reservation facts are invalid")
    proposed: dict[int, dict[str, object]] = {}
    proposed_symbols: set[str] = set()
    for pair in pairs:
        value = dict(pair)
        pair_index = value.get("pair_index")
        if (
            isinstance(pair_index, bool)
            or not isinstance(pair_index, int)
            or pair_index not in {0, 1}
            or pair_index in proposed
            or not _valid_rotation_pair(value, pair_index)
        ):
            raise ValueError("rotation reservation facts are invalid")
        pair_symbols = {str(value["sell_symbol"]), str(value["buy_symbol"])}
        if proposed_symbols & pair_symbols:
            raise ValueError("rotation reservation facts conflict")
        proposed_symbols.update(pair_symbols)
        proposed[pair_index] = value
    root = (
        data_dir
        / "trend_review"
        / "rotation_plans"
        / market
        / account_key
        / execution_date
    )
    root.mkdir(parents=True, exist_ok=True)
    lock = os.open(root / ".reservation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reserved: dict[int, dict[str, object]] = {}
        for pair_index in (0, 1):
            path = root / f"{pair_index}.json"
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("rotation reservation is invalid") from exc
                reserved[pair_index] = _valid_rotation_reservation(
                    payload,
                    market=market,
                    account_key=account_key,
                    execution_date=execution_date,
                    pair_index=pair_index,
                    allocation_sha256=allocation_sha256,
                )
        used_symbols = [
            str(value[field])
            for value in reserved.values()
            for field in ("sell_symbol", "buy_symbol")
        ]
        if len(used_symbols) != len(set(used_symbols)):
            raise ValueError("rotation reservation facts conflict")
        unused_slots = [index for index in (0, 1) if index not in reserved]
        if not unused_slots:
            return tuple(reserved[index] for index in sorted(reserved))

        def identity(value: Mapping[str, object]) -> tuple[str, str]:
            return (
                str(value.get("sell_symbol") or ""),
                str(value.get("buy_symbol") or ""),
            )

        retained_identities = {identity(value) for value in reserved.values()}
        remaining = [
            value
            for _, value in sorted(proposed.items())
            if identity(value) not in retained_identities
        ]
        used = set(used_symbols)
        for value in remaining:
            if used & {str(value["sell_symbol"]), str(value["buy_symbol"])}:
                raise ValueError("rotation reservation facts conflict")
        for pair_index, original in zip(unused_slots, remaining):
            path = root / f"{pair_index}.json"
            pair = {**original, "pair_index": pair_index}
            if not _valid_rotation_pair(pair, pair_index):
                raise ValueError("rotation reservation facts are invalid")
            payload = {
                "schema_version": "open_trader.trend_review.rotation_plan.v1",
                "market": market,
                "account_key": account_key,
                "execution_date": execution_date,
                "pair_index": pair_index,
                "allocation_sha256": allocation_sha256,
                "reserved_at": reserved_at,
                "pair": pair,
            }
            try:
                _write_immutable(path, _canonical_json_bytes(payload))
            except FileExistsError:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("rotation reservation is invalid") from exc
            reserved[pair_index] = _valid_rotation_reservation(
                payload,
                market=market,
                account_key=account_key,
                execution_date=execution_date,
                pair_index=pair_index,
                allocation_sha256=allocation_sha256,
            )
            used.update((str(pair["sell_symbol"]), str(pair["buy_symbol"])))
        return tuple(reserved[index] for index in sorted(reserved))
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _positive_positions(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = snapshot.get("positions")
    if not isinstance(raw, list):
        raise TrendReviewAccountStateError("simulate account positions are unavailable")
    positions: list[Mapping[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TrendReviewAccountStateError("simulate account positions are invalid")
        if _required_decimal(item.get("qty", item.get("quantity", "0")), "position qty") > 0:
            positions.append(item)
    return positions


def _ensure_discipline_account(
    data_dir: Path,
    market: str,
    snapshot: Mapping[str, object],
) -> None:
    root = data_dir / "trend_review" / "ledgers" / market
    started = root / "started.json"
    account_id = int(snapshot.get("acc_id") or 0)
    if account_id <= 0:
        raise TrendReviewAccountStateError("simulate account ID is unavailable")
    if not started.exists():
        _write_immutable(
            started,
            _canonical_json_bytes(
                {"market": market, "acc_id": account_id, "started_at": "first-open"}
            ),
        )
        return
    existing = json.loads(started.read_text(encoding="utf-8"))
    if existing.get("acc_id") != account_id:
        raise TrendReviewAccountStateError("configured simulate account changed")


def _order_matches_request(
    order: Mapping[str, object], request: Mapping[str, object]
) -> bool:
    order_side = str(order.get("trd_side", order.get("side", ""))).strip()
    request_side = str(request.get("side") or "").strip()
    try:
        quantity_matches = _required_decimal(
            order.get("qty"), "broker order quantity"
        ) == _required_decimal(request.get("qty"), "request quantity")
    except ValueError:
        return False
    return bool(request.get("remark")) and all(
        (
            str(order.get("remark") or "") == str(request["remark"]),
            str(order.get("code", order.get("futu_code", ""))).strip().upper()
            == str(request.get("futu_code") or "").strip().upper(),
            order_side.rsplit(".", 1)[-1].upper()
            == request_side.rsplit(".", 1)[-1].upper(),
            quantity_matches,
        )
    )


def _rotation_exact_full_fill(
    order: Mapping[str, object],
    request: Mapping[str, object],
    *,
    expected_side: str,
) -> tuple[Decimal, Decimal] | None:
    """Return target/dealt quantities only for an exact, full broker fill.

    Rotation facts are durable execution evidence, so a matching remark alone
    is insufficient.  The broker row must identify the same side/code/remark,
    carry the requested quantity, and report a non-empty order id with the
    entire requested quantity dealt.
    """
    if not _order_matches_request(order, request):
        return None
    try:
        target = _required_decimal(request.get("qty"), "rotation target quantity")
        broker_qty = _required_decimal(order.get("qty"), "broker order quantity")
        dealt = _required_decimal(order.get("dealt_qty"), "broker dealt quantity")
    except ValueError:
        return None
    if target <= 0 or broker_qty != target or dealt != target:
        return None
    order_id = str(order.get("order_id") or order.get("orderid") or "").strip()
    order_side = str(order.get("trd_side", order.get("side", ""))).strip()
    request_side = str(request.get("side") or "").strip()
    order_code = str(order.get("code", order.get("futu_code", ""))).strip().upper()
    request_code = str(request.get("futu_code") or "").strip().upper()
    if (
        not order_id
        or str(order.get("remark") or "") != str(request.get("remark") or "")
        or order_side.rsplit(".", 1)[-1].upper() != expected_side.upper()
        or request_side.rsplit(".", 1)[-1].upper() != expected_side.upper()
        or order_code != request_code
    ):
        return None
    return target, dealt


def _normalized_rotation_order_status(order: Mapping[str, object]) -> str:
    """Normalize broker status aliases and reject conflicting aliases."""
    aliases: list[str] = []
    for field in ("status", "order_status"):
        raw = str(order.get(field) or "").strip().upper()
        if not raw:
            continue
        aliases.append({
            "FILLED_ALL": "FILLED",
            "DEALT_ALL": "FILLED",
            "FILLED_PART": "PARTIAL",
            "CANCELLED_PART": "PARTIAL",
            "CANCELLED_ALL": "CANCELLED",
            "CANCELLED": "CANCELLED",
        }.get(raw, raw))
    if len(set(aliases)) > 1:
        raise ValueError("conflicting broker order status aliases")
    return aliases[0] if aliases else ""


def _order_has_action_identity(
    order: Mapping[str, object], request: Mapping[str, object]
) -> bool:
    return all(
        (
            bool(request.get("remark")),
            str(order.get("remark") or "") == str(request.get("remark") or ""),
            str(order.get("code", order.get("futu_code", ""))).strip().upper()
            == str(request.get("futu_code") or "").strip().upper(),
            str(order.get("trd_side", order.get("side", "")))
            .strip()
            .rsplit(".", 1)[-1]
            .upper()
            == str(request.get("side") or "").strip().rsplit(".", 1)[-1].upper(),
        )
    )


def _action_facts(
    root: Path, *, futu_code: str, side: str
) -> list[tuple[Path, dict[str, object], dict[str, object], int]]:
    facts: list[tuple[Path, dict[str, object], dict[str, object], int]] = []
    for path in _ledger_fact_paths(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            kind = "result" if path.name.endswith("-result.json") else "intent"
            raise ValueError(f"invalid trend review {kind}: {path}") from exc
        if not isinstance(payload, dict):
            kind = "result" if path.name.endswith("-result.json") else "intent"
            raise ValueError(f"invalid trend review {kind}: {path}")
        if path.name.endswith("-result.json"):
            request = _result_request(path, payload)
        else:
            request = payload.get("request")
        if not isinstance(request, dict):
            raise ValueError(f"invalid trend review intent: {path}")
        attempt = _ledger_fact_attempt(path, payload, request)
        if (
            str(request.get("futu_code") or "").strip().upper()
            == futu_code.strip().upper()
            and str(request.get("side") or "").strip().rsplit(".", 1)[-1].lower()
            == side.strip().rsplit(".", 1)[-1].lower()
        ):
            facts.append((path, payload, request, attempt))
    return sorted(
        facts,
        key=lambda item: (
            item[3],
            str(item[1].get("created_at") or item[1].get("submitted_at") or ""),
            item[0].name,
        ),
    )


def _listed_orders(
    client: object, *, start: str, end: str
) -> list[Mapping[str, object]]:
    listed = client.list_orders(start=start, end=end)
    orders = listed.get("orders") if isinstance(listed, Mapping) else None
    if not isinstance(orders, list) or not all(
        isinstance(order, Mapping) for order in orders
    ):
        raise ValueError("simulate broker orders are unavailable")
    return orders


def _broker_attempt_fact(
    orders: Sequence[Mapping[str, object]], request: Mapping[str, object]
) -> tuple[str, Mapping[str, object] | None]:
    same_remark = [
        order
        for order in orders
        if str(order.get("remark") or "") == str(request.get("remark") or "")
    ]
    exact = [
        order for order in same_remark if _order_matches_request(order, request)
    ]
    if not same_remark:
        return "absent", None
    if len(same_remark) == len(exact) == 1:
        return "exact", exact[0]
    return "conflict", None


def _write_reconciled_result(
    intent_path: Path,
    *,
    market: str,
    execution_date: str,
    request: Mapping[str, object],
    response: Mapping[str, object],
    report_sha: str,
    action_index: int,
    reconciled_at: str,
    metadata: Mapping[str, object] | None = None,
) -> Path:
    return _write_immutable(
        _result_path(intent_path),
        _canonical_json_bytes(
            {
                "market": market,
                "date": execution_date,
                "report_sha256": report_sha,
                "action_index": action_index,
                "request": request,
                "response": response,
                "reconciled": True,
                "submitted_at": reconciled_at,
                **(metadata or {}),
            }
        ),
    )


def _write_action_event(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    action_key: str,
    payload: Mapping[str, object],
    recorded_at: str,
) -> Path:
    event = {**payload, "recorded_at": recorded_at}
    body = _canonical_json_bytes(event)
    filename = (
        f"{recorded_at.replace(':', '-')}-{hashlib.sha256(body).hexdigest()[:12]}.json"
    )
    return _write_immutable(
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
        / action_key
        / filename,
        body,
    )


def _action_events(
    root: Path, *, progress: Callable[[], None] | None = None
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid trend action event: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid trend action event: {path}")
        events.append(payload)
        if progress is not None:
            progress()
    return events


def _write_uncertain_action_event_once(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    action_key: str,
    action_root: Path,
    evidence: Mapping[str, object],
    attempt: int,
    reason: str,
    recorded_at: str,
    target_qty: str | None = None,
) -> Path | None:
    if any(
        event.get("status") == "uncertain"
        and int(event.get("attempt") or 1) == attempt
        and event.get("reason") == reason
        for event in _action_events(action_root)
    ):
        return None
    payload = {
        **evidence,
        "status": "uncertain",
        "attempt": attempt,
        "reason": reason,
    }
    if target_qty is not None:
        payload["target_qty"] = target_qty
    return _write_action_event(
        data_dir=data_dir,
        market=market,
        execution_date=execution_date,
        action_key=action_key,
        payload=payload,
        recorded_at=recorded_at,
    )


def _write_action_status_once(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    action_key: str,
    action_root: Path,
    evidence: Mapping[str, object],
    status: str,
    reason: str,
    recorded_at: str,
) -> Path | None:
    if any(
        event.get("status") == status and event.get("reason") == reason
        for event in _action_events(action_root)
    ):
        return None
    return _write_action_event(
        data_dir=data_dir,
        market=market,
        execution_date=execution_date,
        action_key=action_key,
        payload={**evidence, "status": status, "reason": reason},
        recorded_at=recorded_at,
    )


def record_trend_review_missed_buys(
    *,
    data_dir: Path,
    report: Mapping[str, object],
    market: str,
    execution_date: str,
    now: str,
) -> int:
    market = _market(market)
    actions, strategy_version = _preflight_open_actions(report, market)
    current = datetime.fromisoformat(now).astimezone(MARKET_TIMEZONES[market])
    execution_day = date.fromisoformat(execution_date)
    window_end = time(16) if market == "US" else time(10)
    if current.date() < execution_day or (
        current.date() == execution_day
        and current.time().replace(tzinfo=None) <= window_end
    ):
        return 0
    report_sha = _report_hash(report)
    sell_symbols = {
        trend_action_futu_symbol(report, action, market)
        for action in actions
        if action.get("action") in {"SELL_ALL", "SELL_PARTIAL"}
    }
    completed = 0
    for index, action in enumerate(actions):
        symbol = str(action.get("symbol") or "").strip()
        if (
            action.get("action") != "BUY"
            or trend_action_futu_symbol(report, action, market) in sell_symbols
        ):
            continue
        futu_code = trend_action_futu_symbol(report, action, market)
        action_key = trend_action_key(
            market, execution_date, futu_code, "buy"
        )
        facts = _action_facts(
            data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "open"
            / execution_date,
            futu_code=futu_code,
            side="buy",
        )
        if facts:
            continue
        root = (
            data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "actions"
            / execution_date
            / action_key
        )
        _write_action_status_once(
            data_dir=data_dir,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            action_root=root,
            evidence={
                "market": market,
                "date": execution_date,
                "strategy_version": strategy_version,
                "report_sha256": report_sha,
                "action_index": index,
                "symbol": symbol,
                "futu_code": futu_code,
                "side": "buy",
            },
            status="missed",
            reason="buy_window_closed",
            recorded_at=now,
        )
        completed += 1
    return completed


def _write_broker_observation(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    action_key: str,
    evidence: Mapping[str, object],
    snapshot: Mapping[str, object],
    orders: Sequence[Mapping[str, object]],
    recorded_at: str,
) -> dict[str, object]:
    futu_code = str(evidence["futu_code"])
    position_qty = sum(
        (
            _required_decimal(
                item.get("qty", item.get("quantity", "0")), "position qty"
            )
            for item in _positive_positions(snapshot)
            if str(item.get("code", item.get("futu_code", ""))).strip().upper()
            == futu_code.upper()
        ),
        start=Decimal("0"),
    )
    observation = {
        "schema_version": "open_trader.trend_review.action_observation.v1",
        **evidence,
        "account_id": int(snapshot.get("acc_id") or 0),
        "position_qty": format(position_qty, "f"),
        "orders": [dict(order) for order in orders],
        "observed_at": recorded_at,
    }
    body = _canonical_json_bytes(observation)
    path = _write_immutable(
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date
        / (
            f"{action_key}-observation-"
            f"{hashlib.sha256(body).hexdigest()[:12]}.json"
        ),
        body,
    )
    return {
        "observation_path": path.name,
        "observation_sha256": hashlib.sha256(body).hexdigest(),
    }


def _action_resolutions(
    root: Path,
    *,
    market: str,
    execution_date: str,
    action_key: str,
    symbol: str,
    futu_code: str,
    side: str,
    action_attempts: set[int],
) -> list[dict[str, object]]:
    resolutions: list[dict[str, object]] = []
    resolved_attempts: set[int] = set()
    for path in sorted((root / "resolutions").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            resolved = datetime.fromisoformat(str(payload["resolved_at"]))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(f"invalid trend action resolution: {path}") from exc
        resolution = payload.get("resolution")
        order_id = payload.get("futu_order_id")
        attempt_no = payload.get("attempt_no")
        if (
            payload.get("schema_version")
            != "open_trader.trend_review.resolution.v1"
            or payload.get("market") != market
            or payload.get("execution_date") != execution_date
            or payload.get("action_key") != action_key
            or payload.get("symbol") != symbol
            or payload.get("futu_code") != futu_code
            or payload.get("side") != side
            or not isinstance(attempt_no, int)
            or isinstance(attempt_no, bool)
            or attempt_no <= 0
            or attempt_no not in action_attempts
            or attempt_no in resolved_attempts
            or resolution not in RESOLUTION_STATUSES
            or payload.get("status") != RESOLUTION_STATUSES.get(resolution)
            or not str(payload.get("actor") or "").strip()
            or not str(payload.get("reason") or "").strip()
            or resolved.tzinfo is None
            or resolved.utcoffset() is None
            or resolution == "confirm-submitted"
            and not str(order_id or "").strip()
            or resolution != "confirm-submitted"
            and order_id is not None
        ):
            raise ValueError(f"invalid trend action resolution: {path}")
        resolved_attempts.add(attempt_no)
        resolutions.append(payload)
    return resolutions


def _report_revision(path: Path, as_of_date: str) -> int:
    if path.stem == as_of_date:
        return 0
    prefix = f"{as_of_date}-r"
    suffix = path.stem.removeprefix(prefix)
    if (
        not path.stem.startswith(prefix)
        or not suffix.isdigit()
        or int(suffix) <= 0
    ):
        raise ValueError("invalid corrected trend report revision")
    return int(suffix)


def _late_buy_authorization_context(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    report_sha: str,
) -> tuple[dict[str, object], dict[str, object], Mapping[str, object]] | None:
    path = (
        data_dir
        / "trend_controller"
        / market
        / "late_buy_authorizations"
        / f"{execution_date}.json"
    )
    if not path.exists():
        return None
    batch_path = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "batches"
        / f"{execution_date}.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        batch = _validate_execution_batch(
            json.loads(batch_path.read_text(encoding="utf-8")),
            market=market,
            execution_date=execution_date,
        )
        as_of_date = date.fromisoformat(str(payload["as_of_date"])).isoformat()
        execution_day = date.fromisoformat(execution_date)
        authorized_at = datetime.fromisoformat(str(payload["authorized_at"]))
        report_path = Path(str(payload["report_path"]))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        batch_report_path = Path(str(batch["report_path"]))
        batch_report = json.loads(batch_report_path.read_text(encoding="utf-8"))
        if not isinstance(report, Mapping) or not isinstance(
            batch_report, Mapping
        ):
            raise ValueError("trend report must be a JSON object")
        corrected = (
            payload.get("report_path") != batch["report_path"]
            or payload.get("report_sha256") != batch["report_sha256"]
        )
        process_version = str(report.get("process_version") or "")
        valid_correction = (
            report_path.resolve().parent == batch_report_path.resolve().parent
            and _report_revision(report_path, as_of_date)
            > _report_revision(batch_report_path, as_of_date)
            and report.get("as_of_date") == as_of_date
            and report.get("execution_date") == execution_date
            and len(process_version) == 40
            and all(
                character in "0123456789abcdef"
                for character in process_version
            )
        )
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise ValueError(f"invalid late buy authorization: {path}") from exc
    actor = payload.get("actor")
    reason = payload.get("reason")
    window_end = time(16) if market == "US" else time(10)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version")
        != "open_trader.trend_controller.late_buy_authorization.v1"
        or payload.get("market") != market
        or payload.get("execution_date") != execution_date
        or _report_hash(report) != payload.get("report_sha256")
        or _report_hash(batch_report) != batch["report_sha256"]
        or (corrected and not valid_correction)
        or not isinstance(actor, str)
        or not actor
        or actor != actor.strip()
        or not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or date.fromisoformat(as_of_date) >= execution_day
        or authorized_at.tzinfo is None
        or authorized_at.utcoffset() is None
        or payload.get("authorized_at")
        != authorized_at.isoformat(timespec="seconds")
        or authorized_at.astimezone(MARKET_TIMEZONES[market]).date()
        != execution_day
        or authorized_at.astimezone(MARKET_TIMEZONES[market]).time() <= window_end
    ):
        raise ValueError(f"invalid late buy authorization: {path}")
    if payload["report_sha256"] != report_sha:
        return None
    return payload, batch, report


def _locked_action_context(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    symbol: str,
    side: str,
    report_sha_hint: str | None = None,
) -> tuple[str, int, Mapping[str, object], str]:
    batch_path = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "batches"
        / f"{execution_date}.json"
    )
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch = _validate_execution_batch(
            batch, market=market, execution_date=execution_date
        )
        report_path = Path(str(batch["report_path"]))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid trend execution batch: {batch_path}") from exc
    if (
        not isinstance(report, Mapping)
        or _report_hash(report) != batch["report_sha256"]
    ):
        raise ValueError(f"invalid trend execution batch: {batch_path}")
    if report_sha_hint is not None and report_sha_hint != batch["report_sha256"]:
        if side != "buy":
            raise ValueError("invalid trend action identity")
        authorization = _late_buy_authorization_context(
            data_dir,
            market=market,
            execution_date=execution_date,
            report_sha=report_sha_hint,
        )
        if authorization is None:
            raise ValueError("invalid trend action identity")
        _, _, authorized_report = authorization
        original_actions, _ = _preflight_open_actions(report, market)
        if any(
            action.get("action") == "BUY"
            and str(action.get("symbol") or "").strip() == symbol
            for action in original_actions
        ):
            raise ValueError("invalid trend action identity")
        report = authorized_report
    actions, strategy_version = _preflight_open_actions(report, market)
    expected_actions = {"BUY"} if side == "buy" else {"SELL_ALL", "SELL_PARTIAL"}
    matches = [
        (index, action)
        for index, action in enumerate(actions)
        if action.get("action") in expected_actions
        and str(action.get("symbol") or "").strip() == symbol
    ]
    if len(matches) != 1:
        raise ValueError("invalid trend action identity")
    index, action = matches[0]
    return _report_hash(report), index, action, strategy_version


def _valid_late_buy_authorization(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    report_sha: str,
    symbol: str,
    events: Sequence[Mapping[str, object]],
    facts: Sequence[tuple[Path, dict[str, object], dict[str, object], int]],
) -> bool:
    missed = [event for event in events if event.get("status") == "missed"]
    if not missed:
        return False
    path = (
        data_dir
        / "trend_controller"
        / market
        / "late_buy_authorizations"
        / f"{execution_date}.json"
    )
    if not path.exists():
        return False
    try:
        authorization = _late_buy_authorization_context(
            data_dir,
            market=market,
            execution_date=execution_date,
            report_sha=report_sha,
        )
        if authorization is None:
            return False
        payload, batch, report = authorization
        authorized_at = datetime.fromisoformat(str(payload["authorized_at"]))
        batch_report = json.loads(
            Path(str(batch["report_path"])).read_text(encoding="utf-8")
        )
        if report_sha != batch["report_sha256"]:
            original_actions, _ = _preflight_open_actions(batch_report, market)
            if any(
                action.get("action") == "BUY"
                and str(action.get("symbol") or "").strip() == symbol
                for action in original_actions
            ):
                raise ValueError(f"invalid late buy authorization: {path}")
        authorized_actions, _ = _preflight_open_actions(report, market)
        if (
            sum(
                action.get("action") == "BUY"
                and str(action.get("symbol") or "").strip() == symbol
                for action in authorized_actions
            )
            != 1
        ):
            raise ValueError(f"invalid late buy authorization: {path}")
        missed_at = [
            datetime.fromisoformat(str(event["recorded_at"])) for event in missed
        ]
        fact_times: list[datetime] = []
        for fact_path, fact, _, _ in facts:
            fact_times.append(
                datetime.fromisoformat(
                    str(
                        fact[
                            "submitted_at"
                            if fact_path.name.endswith("-result.json")
                            else "created_at"
                        ]
                    )
                )
            )
            result_path = (
                fact_path
                if fact_path.name.endswith("-result.json")
                else _result_path(fact_path)
            )
            if result_path.exists() and result_path != fact_path:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                fact_times.append(
                    datetime.fromisoformat(str(result["submitted_at"]))
                )
        action_times: list[datetime] = []
        observation_times: list[datetime] = []
        for event in events:
            if event.get("status") not in {"submitted", "partially_filled", "filled"}:
                continue
            action_times.append(datetime.fromisoformat(str(event["recorded_at"])))
            observation_name = event.get("observation_path")
            if observation_name is None:
                continue
            if (
                not isinstance(observation_name, str)
                or Path(observation_name).name != observation_name
            ):
                raise ValueError("invalid observation path")
            observation = json.loads(
                (
                    data_dir
                    / "trend_review"
                    / "ledgers"
                    / market
                    / "open"
                    / execution_date
                    / observation_name
                ).read_text(encoding="utf-8")
            )
            observation_times.append(
                datetime.fromisoformat(str(observation["observed_at"]))
            )
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise ValueError(f"invalid late buy authorization: {path}") from exc
    if (
        any(item.tzinfo is None or item.utcoffset() is None for item in missed_at)
        or any(item > authorized_at for item in missed_at)
        or any(item.tzinfo is None or item.utcoffset() is None for item in fact_times)
        or any(item < authorized_at for item in fact_times)
        or any(
            item.tzinfo is None or item.utcoffset() is None for item in action_times
        )
        or any(item < authorized_at for item in action_times)
        or any(
            item.tzinfo is None or item.utcoffset() is None
            for item in observation_times
        )
        or any(item < authorized_at for item in observation_times)
    ):
        raise ValueError(f"invalid late buy authorization: {path}")
    return True


def _valid_sell_goal_metadata(
    payload: Mapping[str, object],
    *,
    action: Mapping[str, object],
    protection_identity: bool,
    allow_zero_lifecycle_target: bool = False,
) -> bool:
    if protection_identity:
        expected_goal = "position_zero"
    elif action.get("action") == "SELL_PARTIAL":
        expected_goal = "partial_30"
    else:
        expected_goal = None
    if expected_goal == "position_zero":
        return (
            payload.get("sell_goal") == expected_goal
            and "position_started_for" not in payload
            and "lifecycle_target_qty" not in payload
        )
    if expected_goal is None:
        return (
            payload.get("sell_goal") in {None, "position_zero"}
            and "position_started_for" not in payload
            and "lifecycle_target_qty" not in payload
        )
    try:
        target = _required_decimal(
            payload.get("lifecycle_target_qty"), "lifecycle target quantity"
        )
        lot_size = _required_decimal(action.get("lot_size"), "lot size")
    except ValueError:
        return False
    return (
        payload.get("sell_goal") == expected_goal
        and payload.get("position_started_for")
        == action.get("position_started_for")
        and (target >= 0 if allow_zero_lifecycle_target else target > 0)
        and target == target.to_integral_value()
        and lot_size > 0
        and lot_size == lot_size.to_integral_value()
        and target % lot_size == 0
    )


def _strict_action_facts(
    facts: Sequence[tuple[Path, dict[str, object], dict[str, object], int]],
    *,
    market: str,
    execution_date: str,
    action_key: str,
    futu_code: str,
    side: str,
    report_sha: str,
    action_index: int,
    strategy_version: str,
    action: Mapping[str, object],
    protection_identities: set[tuple[str, int, str]],
) -> tuple[list[Mapping[str, object]], set[str]]:
    requests: list[Mapping[str, object]] = []
    result_order_ids: set[str] = set()
    legacy_key = hashlib.sha256(
        f"{market}:{execution_date}:{strategy_version}:{futu_code}:{side}".encode(
            "utf-8"
        )
    ).hexdigest()
    legacy_remark = f"trend-review:{market}:{execution_date}:{legacy_key[:24]}"
    protection_fact_identities = {
        (item[0], item[1]) for item in protection_identities
    }
    allowed_fact_identities = {
        (report_sha, action_index),
        *protection_fact_identities,
    }
    for path, payload, request, attempt in facts:
        timestamp_name = (
            "submitted_at" if path.name.endswith("-result.json") else "created_at"
        )
        try:
            timestamp = datetime.fromisoformat(str(payload[timestamp_name]))
            quantity = _required_decimal(request.get("qty"), "target quantity")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid trend action fact: {path}") from exc
        legacy_name = (
            f"{legacy_key}-intent.json"
            if attempt == 1
            else f"{legacy_key}-attempt-{attempt}-intent.json"
        )
        fact_identity = (
            payload.get("report_sha256"),
            payload.get("action_index"),
        )
        if (
            payload.get("market") != market
            or payload.get("date") != execution_date
            or fact_identity not in allowed_fact_identities
            or side == "sell"
            and not _valid_sell_goal_metadata(
                payload,
                action=action,
                protection_identity=fact_identity in protection_fact_identities,
            )
            or request.get("market") != market
            or str(request.get("futu_code") or "").strip().upper()
            != futu_code.upper()
            or str(request.get("side") or "").strip().lower() != side
            or (
                request.get("remark")
                != trend_attempt_remark(market, execution_date, action_key, attempt)
                and not (
                    path.name == legacy_name
                    and request.get("remark") == legacy_remark
                )
            )
            or quantity <= 0
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise ValueError(f"invalid trend action fact: {path}")
        result_path = path if path.name.endswith("-result.json") else _result_path(path)
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                submitted = datetime.fromisoformat(str(result["submitted_at"]))
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                raise ValueError(f"invalid trend action fact: {result_path}") from exc
            result_identity = (
                payload
                if path != result_path
                and "report_sha256" not in result
                and "action_index" not in result
                else result
            )
            if (
                not isinstance(result, Mapping)
                or result.get("market") != market
                or result.get("date") != execution_date
                or result_identity.get("report_sha256") != fact_identity[0]
                or result_identity.get("action_index") != fact_identity[1]
                or result.get("request") != request
                or not isinstance(result.get("response"), Mapping)
                or submitted.tzinfo is None
                or submitted.utcoffset() is None
            ):
                raise ValueError(f"invalid trend action fact: {result_path}")
            response = result["response"]
            assert isinstance(response, Mapping)
            order_id = str(
                response.get("order_id") or response.get("futu_order_id") or ""
            ).strip()
            if not order_id:
                raise ValueError(f"invalid trend action fact: {result_path}")
            result_order_ids.add(order_id)
        requests.append(request)
    return requests, result_order_ids


def _validate_broker_evidence(
    data_dir: Path,
    event: Mapping[str, object],
    *,
    market: str,
    execution_date: str,
    action_key: str,
    symbol: str,
    futu_code: str,
    side: str,
    report_sha: str,
    action_index: int,
    requests: Sequence[Mapping[str, object]],
    result_order_ids: set[str],
) -> tuple[Decimal, Decimal, list[str]]:
    name = event.get("observation_path")
    digest = event.get("observation_sha256")
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or not name.startswith(f"{action_key}-observation-")
        or not isinstance(digest, str)
        or name != f"{action_key}-observation-{digest[:12]}.json"
    ):
        raise ValueError("invalid trend action event evidence")
    path = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date
        / name
    )
    try:
        body = path.read_bytes()
        observation = json.loads(body)
        observed = datetime.fromisoformat(str(observation["observed_at"]))
        position_qty = _required_decimal(
            observation.get("position_qty"), "position quantity"
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError("invalid trend action event evidence") from exc
    orders = observation.get("orders")
    if (
        not isinstance(observation, Mapping)
        or hashlib.sha256(body).hexdigest() != digest
        or observation.get("schema_version")
        != "open_trader.trend_review.action_observation.v1"
        or observation.get("market") != market
        or observation.get("date") != execution_date
        or observation.get("symbol") != symbol
        or observation.get("futu_code") != futu_code
        or observation.get("side") != side
        or observation.get("report_sha256") != report_sha
        or observation.get("action_index") != action_index
        or not isinstance(observation.get("account_id"), int)
        or isinstance(observation.get("account_id"), bool)
        or int(observation["account_id"]) <= 0
        or position_qty < 0
        or observed.tzinfo is None
        or observed.utcoffset() is None
        or not isinstance(orders, list)
        or not all(isinstance(order, Mapping) for order in orders)
    ):
        raise ValueError("invalid trend action event evidence")
    order_ids: list[str] = []
    filled = Decimal("0")
    for order in orders:
        order_id = str(order.get("order_id") or "").strip()
        try:
            dealt = _required_decimal(
                order.get("dealt_qty", "0"), "broker dealt quantity"
            )
        except ValueError as exc:
            raise ValueError("invalid trend action event evidence") from exc
        if (
            not order_id
            or order_id in order_ids
            or order_id not in result_order_ids
            or dealt < 0
            or not any(_order_matches_request(order, request) for request in requests)
        ):
            raise ValueError("invalid trend action event evidence")
        order_ids.append(order_id)
        filled += dealt
    if orders and not requests:
        raise ValueError("invalid trend action event evidence")
    return position_qty, filled, order_ids


def load_trend_action_audit(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    symbol: str,
    side: str,
    futu_symbol: str | None = None,
    progress: Callable[[], None] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    market = _market(market)
    execution_date = date.fromisoformat(execution_date).isoformat()
    symbol = symbol.strip()
    side = side.strip().lower()
    if not symbol or side not in {"buy", "sell"}:
        raise ValueError("trend action identity is invalid")
    from .futu_symbols import to_futu_symbol

    futu_code = to_futu_symbol(
        market, futu_symbol if futu_symbol is not None else symbol
    )
    if futu_symbol is not None and futu_code != futu_symbol:
        raise ValueError("trend action frozen Futu symbol is invalid")
    action_key = trend_action_key(
        market, execution_date, futu_code, side
    )
    action_root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
        / action_key
    )
    facts = _action_facts(
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date,
        futu_code=futu_code,
        side=side,
    )
    events = _action_events(action_root, progress=progress)
    filled_terminal = False
    for event in events:
        try:
            recorded_at = datetime.fromisoformat(str(event["recorded_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid trend action event identity") from exc
        if (
            event.get("market") != market
            or event.get("date") != execution_date
            or event.get("symbol") != symbol
            or event.get("futu_code") != futu_code
            or event.get("side") != side
            or not str(event.get("status") or "").strip()
            or recorded_at.tzinfo is None
            or recorded_at.utcoffset() is None
        ):
            raise ValueError("invalid trend action event identity")
        status = event.get("status")
        if status == "filled":
            order_ids = event.get("order_ids")
            try:
                filled_qty = _required_decimal(
                    event.get("filled_qty"), "filled quantity"
                )
                target_qty = _required_decimal(
                    event.get("target_qty"), "target quantity"
                )
            except ValueError as exc:
                raise ValueError(
                    "invalid trend action event evidence"
                ) from exc
            if (
                target_qty <= 0
                or filled_qty < target_qty
                or not isinstance(order_ids, list)
                or not order_ids
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in order_ids
                )
            ):
                raise ValueError("invalid trend action event evidence")
            filled_terminal = True
        elif status == "missed" and (
            side != "buy" or event.get("reason") != "buy_window_closed"
        ):
            raise ValueError("invalid trend action event evidence")
        elif (
            status == "incomplete"
            and event.get("reason") == "position_zero_confirmed"
        ):
            order_ids = event.get("order_ids")
            try:
                filled_qty = _required_decimal(
                    event.get("filled_qty"), "filled quantity"
                )
                target_qty = _required_decimal(
                    event.get("target_qty"), "target quantity"
                )
            except ValueError as exc:
                raise ValueError(
                    "invalid trend action event evidence"
                ) from exc
            if (
                side != "sell"
                or target_qty < 0
                or filled_qty < 0
                or filled_qty > target_qty
                or not isinstance(order_ids, list)
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in order_ids
                )
            ):
                raise ValueError("invalid trend action event evidence")
    report_sha_hints = {
        str(value)
        for value in (
            *(
                fact.get("report_sha256")
                for _, fact, _, _ in facts
            ),
            *(event.get("report_sha256") for event in events),
        )
        if value not in {None, ""}
    }
    if side == "buy" and len(report_sha_hints) > 1:
        raise ValueError("invalid trend action event identity")
    report_sha, action_index, action, strategy_version = _locked_action_context(
        data_dir,
        market=market,
        execution_date=execution_date,
        symbol=symbol,
        side=side,
        report_sha_hint=(
            next(iter(report_sha_hints))
            if side == "buy" and report_sha_hints
            else None
        ),
    )
    below_lot_events = [
        event for event in events if event.get("status") == "below_lot"
    ]
    if below_lot_events:
        try:
            lot_size = _required_decimal(action.get("lot_size"), "lot size")
            target_fraction = _required_decimal(
                action.get("target_fraction"), "target fraction"
            )
        except ValueError as exc:
            raise ValueError("invalid trend action event evidence") from exc
        if (
            len(below_lot_events) != 1
            or action.get("action") != "SELL_PARTIAL"
            or side != "sell"
            or lot_size <= 0
            or lot_size != lot_size.to_integral_value()
            or target_fraction != Decimal("0.30")
            or facts
        ):
            raise ValueError("invalid trend action event evidence")
        event = below_lot_events[0]
        try:
            target_qty = _required_decimal(event.get("target_qty"), "target quantity")
            filled_qty = _required_decimal(event.get("filled_qty"), "filled quantity")
            lifecycle_target = _required_decimal(
                event.get("lifecycle_target_qty"), "lifecycle target quantity"
            )
        except ValueError as exc:
            raise ValueError("invalid trend action event evidence") from exc
        if (
            event.get("reason") != "overheat_target_below_lot"
            or event.get("sell_goal") != "partial_30"
            or event.get("position_started_for")
            != action.get("position_started_for")
            or target_qty != 0
            or filled_qty != 0
            or lifecycle_target != 0
            or event.get("order_ids") != []
        ):
            raise ValueError("invalid trend action event evidence")
        position_qty, broker_filled, broker_order_ids = _validate_broker_evidence(
            data_dir,
            event,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            symbol=symbol,
            futu_code=futu_code,
            side=side,
            report_sha=report_sha,
            action_index=action_index,
            requests=(),
            result_order_ids=set(),
        )
        if (
            position_qty <= 0
            or _overheat_trim_quantity(
                position_qty, target_fraction, int(lot_size)
            ) != 0
            or broker_filled != 0
            or broker_order_ids
        ):
            raise ValueError("invalid trend action event evidence")
    protection_identities = {
        identity
        for event in events
        if (
            identity := _protection_event_identity(
                event,
                market=market,
                execution_date=execution_date,
                action_key=action_key,
            )
        )
    }
    requests, result_order_ids = _strict_action_facts(
        facts,
        market=market,
        execution_date=execution_date,
        action_key=action_key,
        futu_code=futu_code,
        side=side,
        report_sha=report_sha,
        action_index=action_index,
        strategy_version=strategy_version,
        action=action,
        protection_identities=protection_identities,
    )
    late_buy_authorized = side == "buy" and _valid_late_buy_authorization(
        data_dir,
        market=market,
        execution_date=execution_date,
        report_sha=report_sha,
        symbol=symbol,
        events=events,
        facts=facts,
    )
    allowed_event_identities = {
        (report_sha, action_index, strategy_version),
        *protection_identities,
    }
    for event in events:
        if progress is not None:
            progress()
        event_identity = (
            event.get("report_sha256"),
            event.get("action_index"),
            event.get("strategy_version"),
        )
        if event_identity not in allowed_event_identities:
            raise ValueError("invalid trend action event identity")
        if side == "sell" and not _valid_sell_goal_metadata(
            event,
            action=action,
            protection_identity=event_identity in protection_identities,
            allow_zero_lifecycle_target=event.get("status") == "below_lot",
        ):
            raise ValueError("invalid trend action event evidence")
        status = event.get("status")
        if status == "missed" and facts and not late_buy_authorized:
            raise ValueError("invalid trend action event evidence")
        if status not in {"filled", "partially_filled", "submitted"} and not (
            status == "incomplete"
            and event.get("reason") == "position_zero_confirmed"
        ):
            continue
        if status == "submitted" and "observation_path" not in event:
            continue
        position_qty, broker_filled, broker_order_ids = _validate_broker_evidence(
            data_dir,
            event,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            symbol=symbol,
            futu_code=futu_code,
            side=side,
            report_sha=str(event_identity[0]),
            action_index=int(event_identity[1]),
            requests=requests,
            result_order_ids=result_order_ids,
        )
        filled_qty = _required_decimal(event.get("filled_qty"), "filled quantity")
        target_qty = _required_decimal(event.get("target_qty"), "target quantity")
        position_zero_requests = [
            fact[2]
            for fact in facts
            if fact[1].get("sell_goal") == "position_zero"
        ]
        expected_target = _required_decimal(
            (
                position_zero_requests[0]
                if event.get("sell_goal") == "position_zero"
                and position_zero_requests
                else requests[0]
                if requests
                else {"qty": "0"}
            ).get("qty"),
            "target quantity",
        )
        if (
            event.get("order_ids") != broker_order_ids
            or filled_qty != broker_filled
            or target_qty != expected_target
        ):
            raise ValueError("invalid trend action event evidence")
        if status in {"filled", "partially_filled"}:
            frozen_target = _required_decimal(
                action.get("estimated_shares"), "estimated shares"
            ) if side == "buy" else expected_target
            if (
                not requests
                or not broker_order_ids
                or (status == "filled" and filled_qty < target_qty)
                or (status == "partially_filled" and filled_qty >= target_qty)
                or target_qty > frozen_target
            ):
                raise ValueError("invalid trend action event evidence")
        elif status == "incomplete" and position_qty != 0:
            raise ValueError("invalid trend action event evidence")
    if filled_terminal and any(
        event.get("status") == "missed" for event in events
    ) and not late_buy_authorized:
        raise ValueError("invalid trend action event evidence")
    resolutions = _action_resolutions(
        action_root,
        market=market,
        execution_date=execution_date,
        action_key=action_key,
        symbol=symbol,
        futu_code=futu_code,
        side=side,
        action_attempts={item[3] for item in facts},
    )
    return events, resolutions


def overheat_trim_progress(
    data_dir: Path,
    *,
    market: str,
    symbol: str,
    position_started_for: str,
) -> dict[str, object]:
    """Rebuild one partial-sell lifecycle from validated immutable facts."""
    market = _market(market)
    symbol = symbol.strip()
    position_started_for = date.fromisoformat(position_started_for).isoformat()
    if not symbol:
        raise ValueError("trend action identity is invalid")
    from .futu_symbols import to_futu_symbol

    futu_code = to_futu_symbol(market, symbol)
    ledger = data_dir / "trend_review" / "ledgers" / market
    action_dates = ledger / "actions"
    open_dates = ledger / "open"
    targets: set[Decimal] = set()
    dealt_by_order: dict[str, Decimal] = {}
    below_lot = False
    source_paths: list[str] = []
    submitted_order_ids: set[str] = set()
    observed_order_statuses: dict[str, str] = {}
    uncertain_action = False
    date_names = {
        path.name
        for root in (action_dates, open_dates)
        for path in root.glob("*")
        if path.is_dir()
    }
    for date_name in sorted(date_names):
        try:
            execution_date = date.fromisoformat(date_name).isoformat()
        except ValueError:
            continue
        action_key = trend_action_key(market, execution_date, futu_code, "sell")
        action_root = action_dates / execution_date / action_key
        facts = _action_facts(
            open_dates / execution_date,
            futu_code=futu_code,
            side="sell",
        )
        if not facts and not action_root.is_dir():
            continue
        has_batch = (ledger / "batches" / f"{execution_date}.json").is_file()
        if has_batch:
            recorded_events = (
                _action_events(action_root) if action_root.is_dir() else []
            )
            if not any(
                payload.get("sell_goal") == "partial_30"
                for payload in [*(item[1] for item in facts), *recorded_events]
            ):
                continue
            events, resolutions = load_trend_action_audit(
                data_dir,
                market=market,
                execution_date=execution_date,
                symbol=symbol,
                side="sell",
            )
            _, _, action, _ = _locked_action_context(
                data_dir,
                market=market,
                execution_date=execution_date,
                symbol=symbol,
                side="sell",
            )
            if (
                action.get("action") != "SELL_PARTIAL"
                or action.get("position_started_for") != position_started_for
            ):
                continue
            action_abandoned = any(
                item.get("resolution") == "abandon" for item in resolutions
            )
            payloads = [item[1] for item in facts] + events
        else:
            events = []
            action_abandoned = False
            facts = [
                item
                for item in facts
                if item[1].get("sell_goal") == "partial_30"
                and item[1].get("position_started_for") == position_started_for
            ]
            payloads = [item[1] for item in facts]
            if not payloads:
                continue
        if not payloads:
            continue
        source_paths.append(
            str(
                (action_root if action_root.is_dir() else facts[0][0]).relative_to(
                    data_dir
                )
            )
        )
        for payload in payloads:
            if payload.get("sell_goal") == "position_zero":
                continue
            if payload.get("sell_goal") != "partial_30":
                raise ValueError("invalid overheat trim lifecycle fact")
            if payload.get("position_started_for") != position_started_for:
                raise ValueError("invalid overheat trim lifecycle fact")
            try:
                targets.add(
                    _required_decimal(
                        payload.get("lifecycle_target_qty"),
                        "lifecycle target quantity",
                    )
                )
            except ValueError as exc:
                raise ValueError("invalid overheat trim lifecycle fact") from exc
        if facts and not events and not action_abandoned:
            uncertain_action = True
        if any(
            event.get("sell_goal") == "partial_30"
            and event.get("status") == "below_lot"
            for event in events
        ):
            below_lot = True
        for event in events:
            if event.get("sell_goal") != "partial_30":
                continue
            status = event.get("status")
            order_ids = event.get("order_ids")
            if (
                not action_abandoned
                and status in {"submitted", "partially_filled"}
                and isinstance(order_ids, list)
            ):
                submitted_order_ids.update(
                    str(order_id).strip()
                    for order_id in order_ids
                    if str(order_id).strip()
                )
            uncertain_action = uncertain_action or (
                not action_abandoned and status == "uncertain"
            )
            if event.get("status") not in {"filled", "partially_filled"}:
                continue
            observation = event.get("observation_path")
            if not isinstance(observation, str):
                raise ValueError("invalid overheat trim lifecycle fact")
            observation_path = ledger / "open" / execution_date / observation
            try:
                payload = json.loads(observation_path.read_text(encoding="utf-8"))
                orders = payload["orders"]
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
            ) as exc:
                raise ValueError("invalid overheat trim lifecycle fact") from exc
            if not isinstance(orders, list):
                raise ValueError("invalid overheat trim lifecycle fact")
            for order in orders:
                if not isinstance(order, Mapping):
                    raise ValueError("invalid overheat trim lifecycle fact")
                order_id = str(order.get("order_id") or "").strip()
                if not order_id:
                    raise ValueError("invalid overheat trim lifecycle fact")
                dealt_by_order[order_id] = max(
                    dealt_by_order.get(order_id, Decimal("0")),
                    _required_decimal(
                        order.get("dealt_qty", "0"), "broker dealt quantity"
                    ),
                )
        for event in events:
            if event.get("sell_goal") != "partial_30":
                continue
            observation = event.get("observation_path")
            if not isinstance(observation, str):
                continue
            observation_path = ledger / "open" / execution_date / observation
            try:
                payload = json.loads(observation_path.read_text(encoding="utf-8"))
                orders = payload["orders"]
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
            ) as exc:
                raise ValueError("invalid overheat trim lifecycle fact") from exc
            if not isinstance(orders, list):
                raise ValueError("invalid overheat trim lifecycle fact")
            for order in orders:
                if not isinstance(order, Mapping):
                    raise ValueError("invalid overheat trim lifecycle fact")
                order_id = str(order.get("order_id") or "").strip()
                broker_status = str(
                    order.get("order_status") or order.get("status") or ""
                ).strip().upper()
                if not order_id:
                    raise ValueError("invalid overheat trim lifecycle fact")
                observed_order_statuses[order_id] = broker_status or "AMBIGUOUS"
    if len(targets) > 1:
        raise ValueError("conflicting overheat trim lifecycle targets")
    target = next(iter(targets), Decimal("0"))
    filled = sum(dealt_by_order.values(), start=Decimal("0"))
    if filled > target:
        raise ValueError("overheat trim fills exceed lifecycle target")
    status = (
        "below_lot"
        if below_lot
        else "complete"
        if target and filled >= target
        else "pending"
    )
    terminal_order_ids = {
        order_id
        for order_id, broker_status in observed_order_statuses.items()
        if broker_status in TERMINAL_ORDER_STATUSES
    }
    active_order = any(
        broker_status in ACTIVE_ORDER_STATUSES
        for broker_status in observed_order_statuses.values()
    )
    ambiguous_order = any(
        broker_status not in TERMINAL_ORDER_STATUSES | ACTIVE_ORDER_STATUSES
        for broker_status in observed_order_statuses.values()
    )
    has_unresolved_order = active_order or ambiguous_order or uncertain_action or bool(
        submitted_order_ids - terminal_order_ids
    )
    return {
        "lifecycle_target_qty": format(target, "f"),
        "filled_qty": format(filled, "f"),
        "status": status,
        "source_paths": source_paths,
        "has_unresolved_order": has_unresolved_order,
    }


def rebuild_overheat_trim_projection(
    data_dir: Path,
    *,
    market: str,
    state_path: Path,
) -> dict[str, object]:
    """Persist only derived trim fields; immutable action facts stay authoritative."""
    from .a_share_trend import load_protection_state, write_protection_state

    state = load_protection_state(state_path)
    positions = state.get("positions")
    if not isinstance(positions, dict):
        raise ValueError("protection state positions must be an object")
    rebuilt_positions: dict[str, object] = {}
    rebuilt = {"schema_version": 1, "positions": rebuilt_positions}
    changed = False
    for symbol, raw_state in positions.items():
        if not isinstance(raw_state, Mapping):
            raise ValueError(f"protection state for {symbol} must be an object")
        position = dict(raw_state)
        started_for = position.get("position_started_for")
        if not isinstance(started_for, str) or not started_for:
            rebuilt_positions[symbol] = position
            continue
        progress = overheat_trim_progress(
            data_dir,
            market=market,
            symbol=symbol,
            position_started_for=started_for,
        )
        fields = {
            "overheat_trim_status": progress["status"],
            "overheat_trim_target_qty": progress["lifecycle_target_qty"],
            "overheat_trim_filled_qty": progress["filled_qty"],
            "overheat_trim_started_for": started_for,
        }
        if progress["lifecycle_target_qty"] == "0" and not progress["source_paths"]:
            fields = {}
        for key in (
            "overheat_trim_status",
            "overheat_trim_target_qty",
            "overheat_trim_filled_qty",
            "overheat_trim_started_for",
        ):
            if key not in fields:
                changed = changed or key in position
                position.pop(key, None)
        if fields:
            changed = changed or any(
                position.get(key) != value for key, value in fields.items()
            )
            position.update(fields)
        rebuilt_positions[symbol] = position
    if changed:
        write_protection_state(state_path, rebuilt)
    return rebuilt


def resolve_trend_action(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    symbol: str,
    side: str,
    resolution: Literal["confirm-submitted", "authorize-retry", "abandon"],
    actor: str,
    reason: str,
    resolved_at: str,
    futu_order_id: str | None = None,
) -> Path:
    market = _market(market)
    execution_date = date.fromisoformat(execution_date).isoformat()
    symbol = symbol.strip()
    side = side.strip().lower()
    if not symbol or side not in {"buy", "sell"}:
        raise ValueError("trend action identity is invalid")
    if resolution not in RESOLUTION_STATUSES:
        raise ValueError("trend action resolution is invalid")
    actor = actor.strip()
    reason = reason.strip()
    if not actor or not reason:
        raise ValueError("resolution actor and reason are required")
    try:
        resolved = datetime.fromisoformat(resolved_at)
    except ValueError:
        raise ValueError("resolution timestamp is invalid") from None
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("resolution timestamp is invalid")
    order_id = str(futu_order_id or "").strip()
    if resolution == "confirm-submitted" and not order_id:
        raise ValueError("confirm-submitted requires a Futu order ID")
    if resolution != "confirm-submitted" and order_id:
        raise ValueError("only confirm-submitted accepts a Futu order ID")

    from .futu_symbols import to_futu_symbol

    futu_code = to_futu_symbol(market, symbol)
    action_key = trend_action_key(market, execution_date, futu_code, side)
    action_root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "actions"
        / execution_date
        / action_key
    )
    action_root.mkdir(parents=True, exist_ok=True)
    lock = os.open(action_root / ".resolution.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        facts = _action_facts(
            data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "open"
            / execution_date,
            futu_code=futu_code,
            side=side,
        )
        resolutions = _action_resolutions(
            action_root,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            symbol=symbol,
            futu_code=futu_code,
            side=side,
            action_attempts={item[3] for item in facts},
        )
        action_attempt = max((item[3] for item in facts), default=0)
        resolved_attempts = {
            int(item["attempt_no"])
            for item in resolutions
        }
        unresolved_attempts = {
            int(event.get("attempt") or 1)
            for event in _action_events(action_root)
            if event.get("status") == "uncertain"
            and int(event.get("attempt") or 1) not in resolved_attempts
        }
        partial_retry_attempt = 0
        if resolution == "authorize-retry" and any(
            payload.get("sell_goal") == "partial_30"
            for _, payload, _, _ in facts
        ):
            events, _ = load_trend_action_audit(
                data_dir,
                market=market,
                execution_date=execution_date,
                symbol=symbol,
                side=side,
            )
            for event in events:
                observation = event.get("observation_path")
                if not isinstance(observation, str):
                    continue
                path = (
                    data_dir
                    / "trend_review"
                    / "ledgers"
                    / market
                    / "open"
                    / execution_date
                    / observation
                )
                try:
                    observed = json.loads(path.read_text(encoding="utf-8"))
                    orders = observed["orders"]
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                ) as exc:
                    raise ValueError("invalid trend action event evidence") from exc
                if not isinstance(orders, list):
                    raise ValueError("invalid trend action event evidence")
                for order in orders:
                    if not isinstance(order, Mapping):
                        raise ValueError("invalid trend action event evidence")
                    broker_status = str(
                        order.get("order_status") or order.get("status") or ""
                    ).strip().upper()
                    if broker_status not in TERMINAL_ORDER_STATUSES:
                        continue
                    matching_attempts = [
                        attempt
                        for _, _, request, attempt in facts
                        if _order_matches_request(order, request)
                    ]
                    if matching_attempts:
                        partial_retry_attempt = max(
                            partial_retry_attempt, *matching_attempts
                        )
        attempt_no = partial_retry_attempt or max(unresolved_attempts, default=0)
        if (
            not attempt_no
            or attempt_no != action_attempt
            or attempt_no in resolved_attempts
            or any(
                item.get("resolution") in {"confirm-submitted", "abandon"}
                for item in resolutions
            )
        ):
            raise ValueError("trend action is not uncertain or is already resolved")
        payload = {
            "schema_version": "open_trader.trend_review.resolution.v1",
            "market": market,
            "execution_date": execution_date,
            "action_key": action_key,
            "symbol": symbol,
            "futu_code": futu_code,
            "side": side,
            "attempt_no": attempt_no,
            "resolution": resolution,
            "status": RESOLUTION_STATUSES[resolution],
            "actor": actor,
            "reason": reason,
            "futu_order_id": order_id if resolution == "confirm-submitted" else None,
            "resolved_at": resolved_at,
        }
        body = _canonical_json_bytes(payload)
        path = (
            action_root
            / "resolutions"
            / (
                f"{resolved_at.replace(':', '-')}-"
                f"{hashlib.sha256(body).hexdigest()[:12]}.json"
            )
        )
        return _write_immutable(path, body)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _floor_to_lot(value: Decimal, lot_size: int) -> int:
    if lot_size <= 0 or not value.is_finite() or value <= 0:
        return 0
    return int(value // Decimal(lot_size)) * lot_size


def _rotation_pair_key(
    market: str,
    account_id: int,
    execution_date: str,
    report_sha: str,
    pair_index: int,
) -> str:
    identity = f"{market}:{account_id}:{execution_date}:{report_sha}:{pair_index}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _rotation_position(
    snapshot: Mapping[str, object], futu_code: str
) -> Mapping[str, object] | None:
    return next(
        (
            item
            for item in _positive_positions(snapshot)
            if str(
                item.get("code", item.get("futu_code", item.get("symbol", "")))
            ).strip().upper()
            == futu_code.strip().upper()
        ),
        None,
    )


def _rotation_quantity(
    pair: Mapping[str, object],
    report: Mapping[str, object],
    snapshot: Mapping[str, object],
    price: Decimal,
    cash: Decimal,
) -> int:
    from .portfolio_risk import size_entry_by_risk

    nav = _required_decimal(snapshot.get("net_value"), "simulate net value")
    weight = _required_decimal(pair.get("target_weight"), "rotation target weight")
    atr = _required_decimal(pair.get("atr"), "rotation ATR")
    lot_size = int(pair.get("lot_size") or 0)
    metadata = report.get("metadata")
    fx = _required_decimal(
        metadata.get("price_fx_to_account_currency", "1")
        if isinstance(metadata, Mapping)
        else "1",
        "price FX",
    )
    risk_summary = report.get("risk_summary")
    cost_rate = _required_decimal(
        risk_summary.get("normal_cost_rate")
        if isinstance(risk_summary, Mapping)
        else None,
        "normal cost rate",
    )
    remaining_risk: Decimal | None = None
    judgments = report.get("strategy_judgments")
    decisions = (
        judgments.get("holding_decisions")
        if isinstance(judgments, Mapping)
        else None
    )
    if isinstance(decisions, list):
        sell_code = str(
            pair.get("sell_futu_symbol")
            or pair.get("sell_symbol")
            or ""
        ).strip().upper()
        by_code = {
            str(item.get("futu_symbol") or item.get("symbol") or "").strip().upper(): item
            for item in decisions
            if isinstance(item, Mapping)
        }
        planned = Decimal("0")
        for position in _positive_positions(snapshot):
            code = str(
                position.get("code", position.get("futu_code", position.get("symbol", "")))
            ).strip().upper()
            # The sell leg releases its own risk before the buy is sized.
            # Counting it here makes a full 4% account look risk-full and
            # incorrectly suppresses the replacement.
            if code == sell_code:
                continue
            holding = by_code.get(code)
            if not isinstance(holding, Mapping):
                raise ValueError(f"holding risk decision missing for {code}")
            quantity = _required_decimal(
                position.get("qty", position.get("quantity")), "position quantity"
            )
            close = _required_decimal(holding.get("close"), "holding close")
            line = _required_decimal(holding.get("active_line"), "active protection line")
            planned += quantity * (
                max(Decimal("0"), close - line) * fx
                + close * fx * cost_rate
            )
        remaining_risk = max(Decimal("0"), nav * Decimal("0.04") - planned)
    if remaining_risk is None:
        remaining_risk = _required_decimal(
            risk_summary.get("portfolio_remaining_risk", nav * Decimal("0.04"))
            if isinstance(risk_summary, Mapping)
            else nav * Decimal("0.04"),
            "portfolio remaining risk",
        )
    if (
        nav <= 0
        or weight <= 0
        or atr <= 0
        or lot_size <= 0
        or price <= 0
        or cash < 0
        or cost_rate < 0
        or remaining_risk < 0
    ):
        raise ValueError("rotation sizing inputs are invalid")
    sized = size_entry_by_risk(
        entry_price=price,
        protection_line=max(Decimal("0"), price - Decimal("2") * atr),
        fx_to_account_currency=fx,
        portfolio_nav=nav,
        nominal_weight_limit=weight,
        single_entry_risk_limit=nav * Decimal("0.004"),
        portfolio_remaining_risk=remaining_risk,
        available_cash=cash,
        lot_size=Decimal(lot_size),
        normal_cost_rate=cost_rate,
    )
    return int(sized.final_quantity)


def _rotation_events(root: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid relative rotation fact: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid relative rotation fact: {path}")
        _validate_rotation_event(payload, path)
        events.append(payload)
    return events


def _validate_rotation_event(
    payload: Mapping[str, object], path: Path | None = None
) -> None:
    label = f"invalid relative rotation fact: {path}" if path else "invalid relative rotation fact"
    if payload.get("schema_version") != "open_trader.trend_review.rotation.v1":
        raise ValueError(label)
    market = payload.get("market")
    try:
        normalized_market = _market(market)
        trading_date = date.fromisoformat(str(payload.get("execution_date"))).isoformat()
    except (TypeError, ValueError):
        raise ValueError(label) from None
    account_id = payload.get("account_id")
    pair_index = payload.get("pair_index")
    report_sha = str(payload.get("report_sha256") or "").strip().lower()
    pair_key = str(payload.get("pair_key") or "").strip().lower()
    if (
        isinstance(account_id, bool)
        or not isinstance(account_id, int)
        or account_id <= 0
        or isinstance(pair_index, bool)
        or not isinstance(pair_index, int)
        or pair_index < 0
        or len(report_sha) != 64
        or any(char not in "0123456789abcdef" for char in report_sha)
        or len(pair_key) != 64
        or any(char not in "0123456789abcdef" for char in pair_key)
        or pair_key != _rotation_pair_key(
            normalized_market, account_id, trading_date, report_sha, pair_index
        )
        or payload.get("execution_date") != trading_date
    ):
        raise ValueError(label)
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError(label)
    if path is not None:
        # Rotation facts are scoped to their canonical pair directory.  A
        # copied fact in a sibling pair must never participate in sequencing
        # or completion decisions, even when its payload is otherwise valid.
        expected_pair_root = (
            path.parent.parent.parent.parent / "rotations"
            / trading_date / pair_key
        )
        if (
            path.parent != expected_pair_root
            or path.parent.name != pair_key
            or path.parent.parent.name != trading_date
            or path.parent.parent.parent.name != "rotations"
            or path.parent.parent.parent.parent.name != normalized_market
        ):
            raise ValueError(label)
        stem = path.stem
        valid_name = (
            stem == "terminal" if kind == "terminal" else
            (
                stem.startswith("pending-")
                or stem in {
                    "preflight-quote-pending",
                    "post-sell-account-pending",
                    "post-sell-quote-pending",
                }
            ) if kind == "pending" else
            stem == "sell-filled" if kind in {"sell_fill", "sell_observation"} else
            stem == "buy-filled" if kind == "buy_fill" else
            "sell-attempt-" in stem if kind in {"sell_intent", "sell_result"} else
            "buy-attempt-" in stem if kind in {"buy_intent", "buy_result"} else
            True
        )
        if not valid_name:
            raise ValueError(label)
    request = payload.get("request")
    if request is not None:
        if not isinstance(request, Mapping):
            raise ValueError(label)
        side = str(request.get("side") or request.get("trd_side") or "").upper()
        futu_code = str(request.get("futu_code") or request.get("code") or "").strip().upper()
        expected_side = (
            "SELL" if kind.startswith("sell_") else "BUY" if kind.startswith("buy_") else side
        )
        expected_code = str(
            payload.get("sell_futu_symbol")
            if kind.startswith("sell_")
            else payload.get("buy_futu_symbol")
            if kind.startswith("buy_")
            else ""
        ).strip().upper()
        if not futu_code or side != expected_side or (expected_code and futu_code != expected_code):
            raise ValueError(label)
    order = payload.get("order")
    if order is not None:
        if not isinstance(order, Mapping):
            raise ValueError(label)
        order_id = str(order.get("order_id") or order.get("orderid") or "").strip()
        side = str(order.get("side") or order.get("trd_side") or "").upper()
        expected_side = (
            "SELL" if kind.startswith("sell_") else "BUY" if kind.startswith("buy_") else side
        )
        expected_code = str(
            payload.get("sell_futu_symbol")
            if kind.startswith("sell_")
            else payload.get("buy_futu_symbol")
            if kind.startswith("buy_")
            else ""
        ).strip().upper()
        order_code = str(order.get("futu_code") or order.get("code") or "").strip().upper()
        if not order_id or side != expected_side or (expected_code and order_code != expected_code):
            raise ValueError(label)
    if kind in {"sell_fill", "sell_observation", "buy_fill"}:
        try:
            normalized_order_status = (
                _normalized_rotation_order_status(order)
                if isinstance(order, Mapping)
                else ""
            )
        except ValueError:
            raise ValueError(label) from None
        if (
            str(payload.get("status") or "") != "filled"
            or not isinstance(request, Mapping)
            or not isinstance(order, Mapping)
            or normalized_order_status != "FILLED"
        ):
            raise ValueError(label)
        try:
            target_qty = _required_decimal(payload.get("target_qty"), "rotation target quantity")
            filled_qty = _required_decimal(payload.get("filled_qty"), "rotation filled quantity")
            request_qty = _required_decimal(request.get("qty"), "request quantity")
            broker_qty = _required_decimal(order.get("qty"), "broker order quantity")
            dealt_qty = _required_decimal(order.get("dealt_qty"), "broker dealt quantity")
        except ValueError:
            raise ValueError(label) from None
        if (
            target_qty <= 0
            or target_qty != filled_qty
            or target_qty != request_qty
            or target_qty != broker_qty
            or target_qty != dealt_qty
            or not _order_matches_request(order, request)
        ):
            raise ValueError(label)


def _rotation_sibling_sell_inflight(
    data_dir: Path,
    *,
    market: str,
    execution_date: str,
    report_sha: str,
    account_id: int,
    pair_index: int,
    position_count: int,
) -> bool:
    root = (
        data_dir / "trend_review" / "ledgers" / market / "rotations"
        / execution_date
    )
    for sibling_root in root.glob("*"):
        if not sibling_root.is_dir():
            continue
        events = _rotation_events(sibling_root)
        if not any(
            event.get("market") == market
            and event.get("execution_date") == execution_date
            and event.get("report_sha256") == report_sha
            and event.get("account_id") == account_id
            and event.get("pair_index") != pair_index
            for event in events
        ):
            continue
        resolved = any(
            event.get("kind") == "terminal"
            and event.get("status") in {
                "complete", "skipped", "failed", "partial", "incomplete", "missed",
            }
            for event in events
        )
        sell_fill = any(
            event.get("kind") in {"sell_observation", "sell_fill"}
            and event.get("status") == "filled"
            for event in events
        )
        buy_fill = any(
            event.get("kind") == "buy_fill" and event.get("status") == "filled"
            for event in events
        )
        sell_intent = any(event.get("kind") == "sell_intent" for event in events)
        if sell_fill and (position_count != 10 or not buy_fill):
            return True
        if sell_intent and not resolved and not sell_fill:
            return True
    return False


def _rotation_pending(
    root: Path,
    evidence: Mapping[str, object],
    *,
    reason: str,
    recorded_at: str,
    uncertain: bool = False,
) -> Path:
    """Write one idempotent pending fact; uncertainty never becomes terminal."""
    suffix = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
    path = root / f"pending-{suffix}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid relative rotation fact: {path}") from exc
        if not isinstance(existing, Mapping):
            raise ValueError(f"invalid relative rotation fact: {path}")
        recorded_at = str(existing.get("recorded_at") or recorded_at)
    return _write_rotation_fact(
        root,
        f"pending-{suffix}",
        {
            **evidence,
            "kind": "pending",
            "status": "uncertain" if uncertain else "pending",
            "reason": reason,
            "recorded_at": recorded_at,
        },
    )


def _rotation_write_once(
    root: Path, name: str, payload: Mapping[str, object]
) -> Path:
    path = root / f"{name}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid relative rotation fact: {path}") from exc
        if not isinstance(existing, Mapping):
            raise ValueError(f"invalid relative rotation fact: {path}")
        if _canonical_json_bytes(existing) != _canonical_json_bytes(payload):
            raise FileExistsError(f"immutable artifact collision: {path}")
        return _write_rotation_fact(root, name, payload)
    return _write_rotation_fact(root, name, payload)


def _write_rotation_action_event_once(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    report_sha: str,
    pair_index: int,
    pair_key: str,
    symbol: object,
    futu_code: str,
    side: str,
    order_id: str,
    filled_qty: object,
    strategy_snapshot: Mapping[str, object] | None,
    recorded_at: str,
) -> Path | None:
    action_key = trend_action_key(market, execution_date, futu_code, side)
    action_root = (
        data_dir / "trend_review" / "ledgers" / market / "actions"
        / execution_date / action_key
    )
    for event_path in sorted(action_root.glob("*.json")):
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid trend action event: {event_path}") from exc
        if not isinstance(event, Mapping):
            raise ValueError(f"invalid trend action event: {event_path}")
        raw_order_ids = event.get("order_ids")
        if raw_order_ids is not None and not isinstance(raw_order_ids, list):
            raise ValueError(f"invalid trend action event: {event_path}")
        event_order_ids = {
            str(value).strip() for value in (raw_order_ids or []) if str(value).strip()
        }
        if order_id in event_order_ids:
            existing_filled_qty = event.get("filled_qty")
            if existing_filled_qty is not None:
                try:
                    if _required_decimal(existing_filled_qty, "rotation action filled quantity") != _required_decimal(
                        filled_qty, "rotation filled quantity"
                    ):
                        raise ValueError(
                            f"conflicting rotation action attribution: {event_path}"
                        )
                except ValueError as exc:
                    if str(exc).startswith("conflicting rotation action attribution"):
                        raise
                    raise ValueError(
                        f"invalid rotation action filled quantity: {event_path}"
                    ) from exc
            if (
                event.get("report_sha256") != report_sha
                or event.get("pair_key") != pair_key
                or event.get("pair_index", event.get("action_index")) != pair_index
                or event.get("status") != "filled"
                or str(event.get("futu_code") or "").strip().upper()
                != futu_code.strip().upper()
                or str(event.get("side") or "").strip().rsplit(".", 1)[-1].upper()
                != side.strip().rsplit(".", 1)[-1].upper()
            ):
                raise ValueError(f"conflicting rotation action attribution: {event_path}")
            return event_path
    strategy_id = str(
        strategy_snapshot.get("strategy_id")
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    strategy_version = str(
        strategy_snapshot.get("strategy_version")
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    if not strategy_id or not strategy_version:
        raise ValueError("rotation strategy snapshot is unavailable")
    return _write_action_event(
        data_dir=data_dir,
        market=market,
        execution_date=execution_date,
        action_key=action_key,
        payload={
            "market": market,
            "date": execution_date,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "report_sha256": report_sha,
            "action_index": pair_index,
            "symbol": str(symbol or ""),
            "futu_code": futu_code,
            "side": side,
            "status": "filled",
            "filled_qty": format(_required_decimal(filled_qty, "rotation filled quantity"), "f"),
            "order_ids": [order_id],
            "reason": "relative_rotation",
            "pair_key": pair_key,
        },
        recorded_at=recorded_at,
    )


def _ensure_rotation_action_attribution(
    *,
    data_dir: Path,
    market: str,
    execution_date: str,
    report_sha: str,
    pair: Mapping[str, object],
    pair_index: int,
    pair_key: str,
    event: Mapping[str, object],
    report: Mapping[str, object],
    recorded_at: str,
) -> Path | None:
    """Repair the action attribution after a fill fact was durably written.

    The fill fact and action ledger are separate immutable writes.  A crash
    between them must be recoverable without submitting another order, so each
    controller pass replays this idempotent attribution step for every fill.
    """
    order = event.get("order")
    if not isinstance(order, Mapping):
        raise ValueError("relative rotation fill order is invalid")
    is_sell = event.get("kind") in {"sell_fill", "sell_observation"}
    side = "sell" if is_sell else "buy"
    code = str(
        pair.get("sell_futu_symbol") if is_sell else pair.get("buy_futu_symbol")
        or order.get("futu_code") or order.get("code") or ""
    ).strip().upper()
    order_id = str(order.get("order_id") or order.get("orderid") or "").strip()
    if not code or not order_id:
        raise ValueError("relative rotation fill action identity is invalid")
    filled_qty = event.get("filled_qty", order.get("dealt_qty"))
    strategy_snapshot = event.get("strategy_snapshot")
    if not isinstance(strategy_snapshot, Mapping):
        strategy_snapshot = report.get("strategy_snapshot")
    return _write_rotation_action_event_once(
        data_dir=data_dir,
        market=market,
        execution_date=execution_date,
        report_sha=report_sha,
        pair_index=pair_index,
        pair_key=pair_key,
        symbol=pair.get("sell_symbol") if is_sell else pair.get("buy_symbol"),
        futu_code=code,
        side=side,
        order_id=order_id,
        filled_qty=filled_qty,
        strategy_snapshot=(
            strategy_snapshot if isinstance(strategy_snapshot, Mapping) else None
        ),
        recorded_at=str(event.get("recorded_at") or recorded_at),
    )


def _rotation_historical_opening_strategy_details(
    data_dir: Path,
    *,
    market: str,
    futu_code: str,
    execution_date: str,
) -> tuple[str, str] | None:
    """Recover an open simulated position's strategy from prior fill facts.

    Account snapshots and protection state deliberately contain no strategy
    provenance in production.  The validated discipline/action streams are
    the durable historical source: replay filled buys and sells for this
    code, retaining the strategy attached to the still-open buy lot.
    """
    records: dict[str, dict[str, object]] = {}

    def add_record(
        *,
        trading_date: str,
        recorded_at: str,
        order_id: str,
        side: str,
        quantity: object,
        opening_version: object,
        source: str,
    ) -> None:
        try:
            qty = _required_decimal(quantity, "historical rotation fill quantity")
        except ValueError:
            return
        if qty <= 0 or side not in {"BUY", "SELL"}:
            return
        key = (
            f"{trading_date}:{order_id}"
            if order_id
            else f"{source}:{trading_date}:{recorded_at}:{len(records)}"
        )
        value = str(opening_version or "").strip()
        existing = records.get(key)
        if existing is None or (
            not str(existing.get("opening_version") or "").strip() and value
        ):
            records[key] = {
                "date": trading_date,
                "recorded_at": recorded_at,
                "side": side,
                "quantity": qty,
                "opening_version": value,
                "source": source,
            }

    discipline_root = data_dir / "trend_review" / "facts" / "discipline" / market
    for path in sorted(discipline_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            trading_date = date.fromisoformat(path.stem).isoformat()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != "open_trader.trend_review.discipline.v1"
            or payload.get("market") != market
            or payload.get("date") != trading_date
            or trading_date >= execution_date
            or not isinstance(payload.get("orders"), list)
        ):
            continue
        fact_snapshot = payload.get("strategy_snapshot")
        for index, order in enumerate(payload["orders"]):
            if not isinstance(order, Mapping):
                continue
            status = str(order.get("status") or order.get("order_status") or "").upper()
            if status not in {"FILLED", "FILLED_ALL", "DEALT_ALL"}:
                continue
            code = str(order.get("code") or order.get("futu_code") or "").strip().upper()
            if code != futu_code:
                continue
            side = str(order.get("side") or order.get("trd_side") or "").upper()
            opening_snapshot = order.get("strategy_snapshot")
            opening_version = (
                order.get("opening_strategy_version")
                or (
                    opening_snapshot.get("strategy_version")
                    if isinstance(opening_snapshot, Mapping)
                    else None
                )
                or (
                    fact_snapshot.get("strategy_version")
                    if isinstance(fact_snapshot, Mapping)
                    else None
                )
            )
            add_record(
                trading_date=trading_date,
                recorded_at=str(order.get("updated_time") or order.get("create_time") or index),
                order_id=str(order.get("order_id") or order.get("orderid") or ""),
                side=side,
                quantity=order.get("dealt_qty", order.get("qty")),
                opening_version=opening_version,
                source="historical_discipline",
            )

    action_root = data_dir / "trend_review" / "ledgers" / market / "actions"
    for date_root in sorted(action_root.glob("*")):
        if not date_root.is_dir():
            continue
        try:
            trading_date = date.fromisoformat(date_root.name).isoformat()
        except ValueError:
            continue
        if trading_date >= execution_date:
            continue
        for path in sorted(date_root.glob("*/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping) or payload.get("status") != "filled":
                continue
            code = str(payload.get("futu_code") or "").strip().upper()
            if code != futu_code:
                continue
            side = str(payload.get("side") or "").strip().upper()
            order_ids = payload.get("order_ids")
            order_id = (
                str(order_ids[0]).strip()
                if isinstance(order_ids, list) and len(order_ids) == 1
                else ""
            )
            add_record(
                trading_date=trading_date,
                recorded_at=str(payload.get("recorded_at") or path.name),
                order_id=order_id,
                side=side,
                quantity=payload.get("filled_qty"),
                opening_version=payload.get("strategy_version") if side == "BUY" else "",
                source="historical_action",
            )

    ordered = sorted(
        records.values(),
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("recorded_at") or ""),
            str(item.get("source") or ""),
        ),
    )
    open_quantity = Decimal("0")
    opening_version = ""
    opening_source = ""
    for record in ordered:
        quantity = _required_decimal(record.get("quantity"), "historical rotation quantity")
        if record.get("side") == "BUY":
            if open_quantity <= 0:
                opening_version = str(record.get("opening_version") or "").strip() or "unknown"
                opening_source = str(record.get("source") or "historical")
            open_quantity += quantity
        elif open_quantity > 0:
            open_quantity = max(Decimal("0"), open_quantity - quantity)
            if open_quantity == 0:
                opening_version = ""
                opening_source = ""
    if open_quantity > 0:
        return opening_version or "unknown", opening_source or "historical_unknown"
    return None


def _rotation_opening_strategy_details(
    data_dir: Path,
    *,
    market: str,
    pair: Mapping[str, object],
    report: Mapping[str, object],
    snapshot: Mapping[str, object],
) -> tuple[str, str]:
    """Resolve the opening version from frozen/position state before closing fallback."""
    sell_code = str(
        pair.get("sell_futu_symbol") or pair.get("sell_symbol") or ""
    ).strip().upper()
    sell_symbol = str(pair.get("sell_symbol") or "").strip()
    candidates: list[tuple[object, str]] = [
        (pair.get("opening_strategy_version"), "frozen_pair"),
        (pair.get("sell_opening_strategy_version"), "frozen_pair"),
    ]
    judgments = report.get("strategy_judgments")
    holdings = judgments.get("holding_decisions") if isinstance(judgments, Mapping) else None
    if isinstance(holdings, list):
        for holding in holdings:
            if not isinstance(holding, Mapping):
                continue
            code = str(
                holding.get("futu_symbol") or holding.get("symbol") or ""
            ).strip().upper()
            if code in {sell_code, sell_symbol.upper()}:
                candidates.extend((
                    (holding.get("opening_strategy_version"), "frozen_holding"),
                    (holding.get("position_opening_strategy_version"), "frozen_holding"),
                ))
                break
    position = _rotation_position(snapshot, sell_code)
    position_provenance_present = isinstance(position, Mapping)
    if isinstance(position, Mapping):
        candidates.extend((
            (position.get("opening_strategy_version"), "account_position"),
            (position.get("position_opening_strategy_version"), "account_position"),
            (position.get("entry_strategy_version"), "account_position"),
            (position.get("opening_report_strategy_version"), "account_position"),
            (position.get("strategy_version"), "account_position"),
        ))
        position_strategy = position.get("strategy_snapshot")
        if isinstance(position_strategy, Mapping):
            candidates.extend((
                (position_strategy.get("opening_strategy_version"), "account_position"),
                (position_strategy.get("strategy_version"), "account_position"),
            ))
    states: list[object] = [report.get("protection_state")]
    state_path = (
        data_dir / PROTECTION_STATE_ROOTS[market] / "protection_state.json"
    )
    if state_path.exists():
        try:
            states.append(json.loads(state_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    for state in states:
        raw_positions = state.get("positions") if isinstance(state, Mapping) else None
        if isinstance(raw_positions, Mapping):
            state_position = raw_positions.get(sell_symbol) or raw_positions.get(sell_code)
            if isinstance(state_position, Mapping):
                position_provenance_present = True
                candidates.extend((
                    (state_position.get("opening_strategy_version"), "protection_state"),
                    (state_position.get("position_opening_strategy_version"), "protection_state"),
                    (state_position.get("entry_strategy_version"), "protection_state"),
                    (state_position.get("opening_report_strategy_version"), "protection_state"),
                    (state_position.get("strategy_version"), "protection_state"),
                ))
                state_strategy = state_position.get("strategy_snapshot")
                if isinstance(state_strategy, Mapping):
                    candidates.extend((
                        (state_strategy.get("opening_strategy_version"), "protection_state"),
                        (state_strategy.get("strategy_version"), "protection_state"),
                    ))
    for candidate, source in candidates:
        value = str(candidate or "").strip()
        if value:
            return value, source
    historical = _rotation_historical_opening_strategy_details(
        data_dir,
        market=market,
        futu_code=sell_code,
        execution_date=str(report.get("execution_date") or "9999-12-31"),
    )
    if historical is not None:
        return historical
    if position_provenance_present:
        # An existing holding without provenance must not be silently
        # attributed to the report that is closing it.  Keep the lifecycle
        # visible, but make its Kelly attribution explicitly unknown.
        return "unknown", "unattributed_existing_position"
    return "unknown", "unattributed_existing_position"


def _rotation_opening_strategy_version(
    data_dir: Path,
    *,
    market: str,
    pair: Mapping[str, object],
    report: Mapping[str, object],
    snapshot: Mapping[str, object],
) -> str:
    return _rotation_opening_strategy_details(
        data_dir,
        market=market,
        pair=pair,
        report=report,
        snapshot=snapshot,
    )[0]


def _write_rotation_fact(root: Path, name: str, payload: Mapping[str, object]) -> Path:
    body = _canonical_json_bytes(payload)
    return _write_immutable(root / f"{name}.json", body)


def _rotation_terminal(
    root: Path,
    evidence: Mapping[str, object],
    *,
    status: str,
    reason: str,
    recorded_at: str,
) -> Path:
    return _write_rotation_fact(
        root,
        "terminal",
        {**evidence, "status": status, "reason": reason, "recorded_at": recorded_at},
    )


def _continuous_session_open(market: str, current: datetime) -> bool:
    value = current.time().replace(tzinfo=None)
    return {
        "CN": time(9, 30) <= value <= time(11, 30)
        or time(13) <= value <= time(15),
        "HK": time(9, 30) <= value <= time(12)
        or time(13) <= value <= time(16),
        "US": time(9, 30) <= value <= time(16),
    }[market]


def execute_relative_rotations(
    *,
    data_dir: Path,
    report: Mapping[str, object],
    client: object,
    market: str,
    execution_date: str,
    now: str,
    quote_prices: Mapping[str, Decimal],
) -> dict[str, object]:
    """Execute frozen simulated rotation pairs; real pairs remain display-only."""
    market = _market(market)
    execution_date = date.fromisoformat(execution_date).isoformat()
    current = datetime.fromisoformat(now).astimezone(MARKET_TIMEZONES[market])
    judgments = report.get("strategy_judgments")
    pairs = (
        judgments.get("simulate_rotation_pairs")
        if isinstance(judgments, Mapping)
        else None
    )
    if not isinstance(pairs, list):
        raise ValueError("trend report simulated rotation pairs are unavailable")
    if not pairs:
        return {
            "status": "unchanged", "market": market, "date": execution_date,
            "submitted_count": 0, "artifact_paths": [],
        }

    report_sha = _report_hash(report)
    submitted = 0
    artifacts: list[str] = []
    terminal_count = 0
    uncertain_pending = False
    resolved_terminal_statuses = {
        "complete", "skipped", "failed", "partial", "incomplete", "missed",
    }
    for pair in pairs:
        sell_proved_now = False
        if not isinstance(pair, Mapping):
            raise ValueError("relative rotation pair is invalid")
        pair_index = pair.get("pair_index")
        if (
            isinstance(pair_index, bool)
            or not isinstance(pair_index, int)
            or pair.get("execution_mode") != "automatic"
            or pair.get("execution_date") != execution_date
            or pair.get("reason") != "relative_rotation"
        ):
            raise ValueError("relative rotation pair is invalid")
        snapshot = client.account_snapshot()
        if not isinstance(snapshot, Mapping):
            raise TrendReviewAccountStateError("simulate account snapshot is invalid")
        account_id = int(snapshot.get("acc_id") or 0)
        metadata = report.get("metadata")
        expected_account_id = (
            metadata.get("simulate_acc_id")
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            expected_account_id is not None
            and (
                isinstance(expected_account_id, bool)
                or not isinstance(expected_account_id, int)
                or expected_account_id != account_id
            )
        ):
            raise TrendReviewAccountStateError("configured simulate account changed")
        _ensure_discipline_account(data_dir, market, snapshot)
        pair_key = _rotation_pair_key(
            market, account_id, execution_date, report_sha, pair_index
        )
        root = (
            data_dir / "trend_review" / "ledgers" / market / "rotations"
            / execution_date / pair_key
        )
        evidence = {
            "schema_version": "open_trader.trend_review.rotation.v1",
            "market": market,
            "account_id": account_id,
            "execution_date": execution_date,
            "report_sha256": report_sha,
            "pair_index": pair_index,
            "pair_key": pair_key,
            "sell_symbol": pair.get("sell_symbol"),
            "sell_futu_symbol": pair.get("sell_futu_symbol"),
            "buy_symbol": pair.get("buy_symbol"),
            "buy_futu_symbol": pair.get("buy_futu_symbol"),
        }
        events = _rotation_events(root)
        # Reconcile any durable fill facts before checking terminal state.  A
        # process crash can leave the fill sidecar committed while its action
        # attribution is absent; replaying this step is idempotent and avoids
        # resubmitting the broker order.
        for fill_event in events:
            if (
                fill_event.get("kind") not in {"sell_observation", "sell_fill", "buy_fill"}
                or fill_event.get("status") != "filled"
            ):
                continue
            action_path = _ensure_rotation_action_attribution(
                data_dir=data_dir,
                market=market,
                execution_date=execution_date,
                report_sha=report_sha,
                pair=pair,
                pair_index=pair_index,
                pair_key=pair_key,
                event=fill_event,
                report=report,
                recorded_at=now,
            )
            if action_path is not None:
                artifacts.append(str(action_path))
        terminal = next(
            (
                event for event in events
                if event.get("kind") == "terminal"
                and event.get("status") in resolved_terminal_statuses
            ),
            None,
        )
        if terminal is not None:
            terminal_count += 1
            continue
        uncertain_pending = uncertain_pending or any(
            event.get("kind") == "pending" and event.get("status") == "uncertain"
            for event in events
        )
        if current.date() != date.fromisoformat(execution_date):
            if any(
                event.get("kind") == "pending" and event.get("status") == "uncertain"
                for event in events
            ):
                uncertain_pending = True
                continue
            status = "missed" if not any(
                event.get("kind") in {"sell_observation", "sell_fill"}
                and event.get("status") == "filled"
                for event in events
            ) else "incomplete"
            path = _rotation_terminal(
                root, {**evidence, "kind": "terminal"}, status=status,
                reason="execution_date_ended", recorded_at=now,
            )
            artifacts.append(str(path))
            terminal_count += 1
            continue

        sell_code = str(pair.get("sell_futu_symbol") or "").strip().upper()
        buy_code = str(pair.get("buy_futu_symbol") or "").strip().upper()
        sell_intent = next(
            (event for event in events if event.get("kind") == "sell_intent"), None
        )
        sell_filled = any(
            event.get("kind") in {"sell_observation", "sell_fill"}
            and event.get("status") == "filled"
            for event in events
        )
        if sell_intent is None and not sell_filled:
            stale_date = next(
                (
                    str(snapshot[field])
                    for field in ("source_date", "as_of_date", "trading_date")
                    if snapshot.get(field)
                ),
                execution_date,
            )
            positions = _positive_positions(snapshot)
            weak = _rotation_position(snapshot, sell_code)
            if (
                stale_date == execution_date
                and weak is not None
                and _rotation_sibling_sell_inflight(
                    data_dir,
                    market=market,
                    execution_date=execution_date,
                    report_sha=report_sha,
                    account_id=account_id,
                    pair_index=pair_index,
                    position_count=len(positions),
                )
            ):
                artifacts.append(str(_rotation_pending(
                    root,
                    evidence,
                    reason="account_temporarily_not_full",
                    recorded_at=now,
                )))
                continue
            reason = (
                "stale_account_state" if stale_date != execution_date
                else "weak_holding_absent" if weak is None
                else "candidate_already_held"
                if _rotation_position(snapshot, buy_code) is not None
                else "account_not_full" if len(positions) != 10
                else ""
            )
            if not reason and buy_code not in quote_prices:
                if not any(
                    event.get("kind") == "pending"
                    and event.get("reason") == "current_quote_unavailable"
                    for event in events
                ):
                    artifacts.append(str(_write_rotation_fact(
                        root, "preflight-quote-pending",
                        {**evidence, "kind": "pending", "status": "pending",
                         "reason": "current_quote_unavailable", "recorded_at": now},
                    )))
                continue
            if weak is None:
                sell_qty = Decimal("0")
                preflight_qty = 0
            else:
                sell_qty = _required_decimal(
                    weak.get("can_sell_qty", weak.get("sellable_qty", weak.get("qty"))),
                    "rotation sellable quantity",
                )
                cash = _required_decimal(
                    snapshot.get("available_cash", snapshot.get("cash")),
                    "simulate available cash",
                )
                market_value = _required_decimal(
                    weak.get("market_val", weak.get("market_value")),
                    "rotation holding market value",
                )
                risk_summary = report.get("risk_summary")
                cost = _required_decimal(
                    risk_summary.get("normal_cost_rate")
                    if isinstance(risk_summary, Mapping) else None,
                    "normal cost rate",
                )
                price = _required_decimal(quote_prices.get(buy_code), "current quote price")
                preflight_qty = _rotation_quantity(
                    pair, report, snapshot, price,
                    cash + market_value * max(Decimal("0"), Decimal("1") - cost),
                )
            if not reason and sell_qty <= 0:
                reason = "weak_holding_unsellable"
            if not reason and preflight_qty <= 0:
                reason = "candidate_quantity_zero"
            if reason:
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal"}, status="skipped",
                    reason=reason, recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                continue
            if not _continuous_session_open(market, current):
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal"}, status="missed",
                    reason="continuous_session_closed", recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                continue
            request = {
                "market": market, "futu_code": sell_code, "side": "SELL",
                "order_type": "MARKET", "price": "0", "qty": format(sell_qty, "f"),
                "remark": f"rotation:{market}:{execution_date}:{pair_key[:16]}:S:1",
            }
            opening_version, opening_source = _rotation_opening_strategy_details(
                data_dir,
                market=market,
                pair=pair,
                report=report,
                snapshot=snapshot,
            )
            sell_intent_payload = {
                **evidence,
                "kind": "sell_intent",
                "attempt": 1,
                "request": request,
                "opening_strategy_version": opening_version,
                "opening_strategy_version_source": opening_source,
                "recorded_at": now,
            }
            intent_path = _write_rotation_fact(
                root, "sell-attempt-1-intent",
                sell_intent_payload,
            )
            artifacts.append(str(intent_path))
            try:
                response = client.place_order(request)
                submitted += 1
            except Exception as exc:
                path = _rotation_pending(
                    root, evidence, reason=f"sell_submit_uncertain: {exc}",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                continue
            result_path = _write_rotation_fact(
                root, "sell-attempt-1-result",
                {**evidence, "kind": "sell_result", "attempt": 1,
                 "request": request, "response": response,
                 "opening_strategy_version": opening_version,
                 "opening_strategy_version_source": opening_source,
                 "recorded_at": now},
            )
            artifacts.append(str(result_path))
            sell_intent = sell_intent_payload

        if not sell_filled:
            request = sell_intent.get("request") if isinstance(sell_intent, Mapping) else None
            if not isinstance(request, Mapping):
                raise ValueError("relative rotation sell intent is invalid")
            orders = _listed_orders(client, start=execution_date, end=execution_date)
            fact, order = _broker_attempt_fact(orders, request)
            if fact != "exact" or order is None:
                path = _rotation_pending(
                    root, evidence, reason="sell_intent_without_broker_proof",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                continue
            target = _required_decimal(request.get("qty"), "rotation sell quantity")
            try:
                filled = _required_decimal(order.get("dealt_qty", "0"), "broker dealt quantity")
            except ValueError:
                path = _rotation_pending(
                    root, evidence, reason="sell_fill_quantity_invalid",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                continue
            broker_status = str(order.get("order_status", order.get("status", ""))).upper()
            full_fill = _rotation_exact_full_fill(
                order, request, expected_side="SELL"
            )
            if (
                full_fill is not None
                and broker_status in {"FILLED", "FILLED_ALL"}
            ):
                target, filled = full_fill
                strategy_snapshot = report.get("strategy_snapshot")
                closing_version = str(
                    strategy_snapshot.get("strategy_version") or ""
                    if isinstance(strategy_snapshot, Mapping)
                    else ""
                )
                opening_version = str(
                    sell_intent.get("opening_strategy_version")
                    if isinstance(sell_intent, Mapping)
                    else ""
                ).strip()
                opening_source = str(
                    sell_intent.get("opening_strategy_version_source")
                    if isinstance(sell_intent, Mapping)
                    else ""
                ).strip()
                if not opening_version:
                    opening_version, opening_source = _rotation_opening_strategy_details(
                        data_dir,
                        market=market,
                        pair=pair,
                        report=report,
                        snapshot=snapshot,
                    )
                observation = _write_rotation_fact(
                    root, "sell-filled",
                    {**evidence, "kind": "sell_fill", "status": "filled",
                     "target_qty": format(target, "f"), "filled_qty": format(filled, "f"),
                     "order_id": str(order.get("order_id") or ""),
                     "order": dict(order), "request": dict(request),
                     "strategy_snapshot": dict(strategy_snapshot)
                     if isinstance(strategy_snapshot, Mapping) else None,
                     "exit_reason": "relative_rotation",
                     "opening_strategy_version": opening_version,
                     "opening_strategy_version_source": opening_source,
                     "closing_strategy_version": closing_version,
                     "recorded_at": now},
                )
                artifacts.append(str(observation))
                action_path = _write_rotation_action_event_once(
                    data_dir=data_dir,
                    market=market,
                    execution_date=execution_date,
                    report_sha=report_sha,
                    pair_index=pair_index,
                    pair_key=pair_key,
                    symbol=pair.get("sell_symbol"),
                    futu_code=sell_code,
                    side="sell",
                    order_id=str(order.get("order_id") or ""),
                    filled_qty=filled,
                    strategy_snapshot=strategy_snapshot
                    if isinstance(strategy_snapshot, Mapping) else None,
                    recorded_at=now,
                )
                if action_path is not None:
                    artifacts.append(str(action_path))
                sell_filled = True
                sell_proved_now = True
            elif filled == target:
                path = _rotation_pending(
                    root, evidence, reason="sell_fill_proof_incomplete",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                continue
            elif filled > 0:
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal"}, status="partial",
                    reason="sell_partial_fill", recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                continue
            elif broker_status in REJECTED_ORDER_STATUSES | {"CANCELLED", "CANCELLED_ALL", "CANCELLED_PART"}:
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal"}, status="failed",
                    reason="sell_not_filled", recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                continue
            elif broker_status not in ACTIVE_ORDER_STATUSES:
                path = _rotation_pending(
                    root, evidence, reason="sell_status_uncertain",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                continue
            else:
                continue

        # The next controller pass refreshes both the account and quote after
        # the durable sell proof before any buy can be submitted.
        if sell_proved_now and _continuous_session_open(market, current):
            continue

        refreshed = client.account_snapshot()
        if not isinstance(refreshed, Mapping):
            raise TrendReviewAccountStateError("simulate account snapshot is invalid")
        if not _continuous_session_open(market, current):
            path = _rotation_terminal(
                root, {**evidence, "kind": "terminal"}, status="incomplete",
                reason="buy_session_closed", recorded_at=now,
            )
            artifacts.append(str(path))
            terminal_count += 1
            continue
        if _rotation_position(refreshed, sell_code) is not None:
            if not any(
                event.get("kind") == "pending"
                and event.get("reason") == "post_sell_account_not_refreshed"
                for event in _rotation_events(root)
            ):
                artifacts.append(str(_write_rotation_fact(
                    root, "post-sell-account-pending",
                    {**evidence, "kind": "pending", "status": "pending",
                     "reason": "post_sell_account_not_refreshed", "recorded_at": now},
                )))
            continue
        if _rotation_position(refreshed, buy_code) is not None:
            path = _rotation_terminal(
                root, {**evidence, "kind": "terminal"}, status="incomplete",
                reason="candidate_already_held_after_sell", recorded_at=now,
            )
            artifacts.append(str(path))
            terminal_count += 1
            continue
        if buy_code not in quote_prices:
            if not any(
                event.get("kind") == "pending"
                and event.get("reason") == "post_sell_quote_unavailable"
                for event in _rotation_events(root)
            ):
                artifacts.append(str(_write_rotation_fact(
                    root, "post-sell-quote-pending",
                    {**evidence, "kind": "pending", "status": "pending",
                     "reason": "post_sell_quote_unavailable", "recorded_at": now},
                )))
            continue
        buy_qty = _rotation_quantity(
            pair, report, refreshed,
            _required_decimal(quote_prices.get(buy_code), "current quote price"),
            _required_decimal(
                refreshed.get("available_cash", refreshed.get("cash")),
                "simulate available cash",
            ),
        )
        if buy_qty <= 0:
            path = _rotation_terminal(
                root, {**evidence, "kind": "terminal"}, status="incomplete",
                reason="post_sell_candidate_quantity_zero", recorded_at=now,
            )
            artifacts.append(str(path))
            terminal_count += 1
            continue

        events = _rotation_events(root)
        completed_pair = False
        for attempt in (1, 2):
            intent = next(
                (
                    event for event in events
                    if event.get("kind") == "buy_intent" and event.get("attempt") == attempt
                ),
                None,
            )
            if intent is None:
                request = {
                    "market": market, "futu_code": buy_code, "side": "BUY",
                    "order_type": "MARKET", "price": "0", "qty": str(buy_qty),
                    "remark": f"rotation:{market}:{execution_date}:{pair_key[:16]}:B:{attempt}",
                }
                intent_path = _write_rotation_fact(
                    root, f"buy-attempt-{attempt}-intent",
                    {**evidence, "kind": "buy_intent", "attempt": attempt,
                     "request": request, "recorded_at": now},
                )
                artifacts.append(str(intent_path))
                try:
                    response = client.place_order(request)
                    submitted += 1
                except Exception as exc:
                    path = _rotation_pending(
                        root, evidence, reason=f"buy_submit_uncertain: {exc}",
                        recorded_at=now, uncertain=True,
                    )
                    artifacts.append(str(path))
                    uncertain_pending = True
                    break
                result_path = _write_rotation_fact(
                    root, f"buy-attempt-{attempt}-result",
                    {**evidence, "kind": "buy_result", "attempt": attempt,
                     "request": request, "response": response, "recorded_at": now},
                )
                artifacts.append(str(result_path))
                intent = {"request": request, "recorded_at": now}
                events = _rotation_events(root)
            request = intent.get("request") if isinstance(intent, Mapping) else None
            if not isinstance(request, Mapping):
                raise ValueError("relative rotation buy intent is invalid")
            fact, order = _broker_attempt_fact(
                _listed_orders(client, start=execution_date, end=execution_date), request
            )
            if fact != "exact" or order is None:
                path = _rotation_pending(
                    root, evidence, reason="buy_intent_without_broker_proof",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                break
            target = _required_decimal(request.get("qty"), "rotation buy quantity")
            try:
                filled = _required_decimal(order.get("dealt_qty", "0"), "broker dealt quantity")
            except ValueError:
                path = _rotation_pending(
                    root, evidence, reason="buy_fill_quantity_invalid",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                break
            broker_status = str(order.get("order_status", order.get("status", ""))).upper()
            full_fill = _rotation_exact_full_fill(
                order, request, expected_side="BUY"
            )
            if (
                full_fill is not None
                and broker_status in {"FILLED", "FILLED_ALL"}
            ):
                target, filled = full_fill
                sell_fill = next(
                    (
                        event for event in _rotation_events(root)
                        if event.get("kind") in {"sell_observation", "sell_fill"}
                        and event.get("status") == "filled"
                    ),
                    {},
                )
                opening_version = str(
                    report.get("strategy_snapshot", {}).get("strategy_version", "")
                    if isinstance(report.get("strategy_snapshot"), Mapping)
                    else ""
                )
                opening_source = "current_report"
                strategy_snapshot = report.get("strategy_snapshot")
                closing_version = str(
                    strategy_snapshot.get("strategy_version")
                    if isinstance(strategy_snapshot, Mapping)
                    else ""
                )
                fill_recorded_at = str(
                    order.get("updated_time")
                    or order.get("create_time")
                    or (
                        intent.get("recorded_at")
                        if isinstance(intent, Mapping)
                        else None
                    )
                    or now
                )
                buy_fill_path = _rotation_write_once(
                    root,
                    "buy-filled",
                    {
                        **evidence,
                        "kind": "buy_fill",
                        "status": "filled",
                        "target_qty": format(target, "f"),
                        "filled_qty": format(filled, "f"),
                        "order_id": str(order.get("order_id") or ""),
                        "order": dict(order),
                        "request": dict(request),
                        "strategy_snapshot": dict(strategy_snapshot)
                        if isinstance(strategy_snapshot, Mapping) else None,
                        "opening_strategy_version": opening_version,
                        "opening_strategy_version_source": opening_source,
                        "closing_strategy_version": closing_version,
                        "recorded_at": fill_recorded_at,
                    },
                )
                artifacts.append(str(buy_fill_path))
                action_path = _write_rotation_action_event_once(
                    data_dir=data_dir,
                    market=market,
                    execution_date=execution_date,
                    report_sha=report_sha,
                    pair_index=pair_index,
                    pair_key=pair_key,
                    symbol=pair.get("buy_symbol"),
                    futu_code=buy_code,
                    side="buy",
                    order_id=str(order.get("order_id") or ""),
                    filled_qty=filled,
                    strategy_snapshot=strategy_snapshot
                    if isinstance(strategy_snapshot, Mapping) else None,
                    recorded_at=now,
                )
                if action_path is not None:
                    artifacts.append(str(action_path))
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal",
                           "exit_reason": "relative_rotation",
                           "opening_strategy_version": opening_version,
                           "closing_strategy_version": closing_version},
                    status="complete", reason="buy_filled", recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                completed_pair = True
                break
            if filled == target:
                path = _rotation_pending(
                    root, evidence, reason="buy_fill_proof_incomplete",
                    recorded_at=now, uncertain=True,
                )
                artifacts.append(str(path))
                uncertain_pending = True
                break
            if filled > 0:
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal"}, status="partial",
                    reason="buy_partial_fill", recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                break
            if broker_status in {"CANCELLED", "CANCELLED_ALL", "CANCELLED_PART"}:
                if attempt == 1:
                    continue
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal"}, status="incomplete",
                    reason="buy_zero_fill_after_retry", recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                break
            if broker_status in REJECTED_ORDER_STATUSES:
                path = _rotation_terminal(
                    root, {**evidence, "kind": "terminal"}, status="failed",
                    reason="buy_rejected", recorded_at=now,
                )
                artifacts.append(str(path))
                terminal_count += 1
                break
            if broker_status in ACTIVE_ORDER_STATUSES:
                break
            path = _rotation_pending(
                root, evidence, reason="buy_status_uncertain",
                recorded_at=now, uncertain=True,
            )
            artifacts.append(str(path))
            uncertain_pending = True
            break
        if completed_pair:
            continue

    return {
        "status": (
            "uncertain" if uncertain_pending
            else "complete" if terminal_count == len(pairs)
            else "submitted" if submitted else "pending"
        ),
        "market": market,
        "date": execution_date,
        "submitted_count": submitted,
        "artifact_paths": artifacts,
    }


def relative_rotations_completed(
    data_dir: Path,
    *,
    report: Mapping[str, object],
    market: str,
    execution_date: str,
) -> bool:
    """Return whether every frozen simulated pair has a durable terminal fact."""
    market = _market(market)
    judgments = report.get("strategy_judgments")
    pairs = (
        judgments.get("simulate_rotation_pairs", [])
        if isinstance(judgments, Mapping)
        else None
    )
    if not isinstance(pairs, list):
        raise ValueError("trend report simulated rotation pairs are unavailable")
    if not pairs:
        return True
    execution_date = date.fromisoformat(execution_date).isoformat()
    metadata = report.get("metadata")
    account_id = metadata.get("simulate_acc_id") if isinstance(metadata, Mapping) else None
    if (
        isinstance(account_id, bool)
        or not isinstance(account_id, int)
        or account_id <= 0
    ):
        return False
    report_sha = _report_hash(report)
    resolved_statuses = {
        "complete", "skipped", "failed", "partial", "incomplete", "missed",
    }
    for pair in pairs:
        pair_index = pair.get("pair_index") if isinstance(pair, Mapping) else None
        if isinstance(pair_index, bool) or not isinstance(pair_index, int):
            return False
        pair_key = _rotation_pair_key(
            market, account_id, execution_date, report_sha, pair_index
        )
        path = (
            data_dir / "trend_review" / "ledgers" / market / "rotations"
            / execution_date / pair_key / "terminal.json"
        )
        try:
            terminal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(terminal, Mapping):
            return False
        if not (
            terminal.get("schema_version")
            == "open_trader.trend_review.rotation.v1"
            and terminal.get("kind") == "terminal"
            and terminal.get("market") == market
            and terminal.get("account_id") == account_id
            and terminal.get("execution_date") == execution_date
            and terminal.get("report_sha256") == report_sha
            and terminal.get("pair_index") == pair_index
            and terminal.get("pair_key") == pair_key
            and terminal.get("status") in resolved_statuses
        ):
            return False
    return True


def _overheat_trim_quantity(
    position_qty: Decimal, fraction: Decimal, lot_size: int
) -> Decimal:
    return Decimal(_floor_to_lot(position_qty * fraction, lot_size))


def _remaining_buy_quantity(
    action: Mapping[str, object],
    report: Mapping[str, object],
    snapshot: Mapping[str, object],
    broker_orders: Sequence[Mapping[str, object]],
    current_price: Decimal,
) -> int:
    try:
        lot_size = int(action.get("lot_size") or 0)
    except (TypeError, ValueError):
        raise ValueError("trend review buy action is invalid") from None
    frozen_quantity = _required_decimal(
        action.get("estimated_shares"), "estimated shares"
    )
    target_amount = _required_decimal(action.get("target_amount"), "target amount")
    current_price = _required_decimal(current_price, "current price")
    metadata = report.get("metadata")
    fx = _required_decimal(
        metadata.get("price_fx_to_account_currency", "1")
        if isinstance(metadata, Mapping)
        else "1",
        "price FX",
    )
    cash = _required_decimal(
        snapshot.get("available_cash", snapshot.get("cash")),
        "simulate available cash",
    )
    if (
        lot_size <= 0
        or frozen_quantity <= 0
        or frozen_quantity != frozen_quantity.to_integral_value()
        or frozen_quantity % lot_size
        or target_amount <= 0
        or current_price <= 0
        or fx <= 0
    ):
        raise ValueError("trend review buy completion inputs are invalid")
    if cash <= 0:
        return 0

    fills: dict[str, tuple[Decimal, Decimal]] = {}
    for order in broker_orders:
        dealt = _required_decimal(
            order.get("dealt_qty", "0"), "broker dealt quantity"
        )
        if dealt < 0:
            raise ValueError("broker dealt quantity must be non-negative")
        if dealt == 0:
            continue
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("confirmed broker fill requires an order ID")
        price = _required_decimal(
            order.get("dealt_avg_price"), "broker average fill price"
        )
        if price <= 0:
            raise ValueError("broker average fill price must be positive")
        fact = (dealt, price)
        if order_id in fills and fills[order_id] != fact:
            raise ValueError("broker order ID has conflicting fill facts")
        fills[order_id] = fact

    confirmed_quantity = sum(
        (quantity for quantity, _ in fills.values()), Decimal("0")
    )
    confirmed_notional = sum(
        (quantity * price * fx for quantity, price in fills.values()),
        Decimal("0"),
    )
    remaining_quantity = frozen_quantity - confirmed_quantity
    remaining_amount = target_amount - confirmed_notional
    caps = [
        _floor_to_lot(remaining_quantity, lot_size),
        _floor_to_lot(remaining_amount / (current_price * fx), lot_size),
        _floor_to_lot(cash / (current_price * fx), lot_size),
    ]
    strategy_snapshot = report.get("strategy_snapshot")
    version = (
        str(strategy_snapshot.get("strategy_version") or "")
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    if version in {"v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13"}:
        risk_summary = report.get("risk_summary")
        if not isinstance(risk_summary, Mapping):
            raise ValueError("trend review risk summary is unavailable")
        atr = _required_decimal(action.get("atr"), "action ATR")
        planned_risk = _required_decimal(
            action.get("planned_stop_risk"), "planned stop risk"
        )
        cost_rate = _required_decimal(
            risk_summary.get("normal_cost_rate"), "normal cost rate"
        )
        if atr <= 0 or planned_risk <= 0 or cost_rate <= 0:
            raise ValueError("trend review buy completion risk is invalid")
        confirmed_risk = sum(
            (
                quantity
                * (
                    Decimal("2") * atr * fx
                    + price * fx * cost_rate
                )
                for quantity, price in fills.values()
            ),
            Decimal("0"),
        )
        remaining_risk = planned_risk - confirmed_risk
        unit_risk = (
            Decimal("2") * atr * fx
            + current_price * fx * cost_rate
        )
        caps.append(_floor_to_lot(remaining_risk / unit_risk, lot_size))
    return min(caps)


def _activate_fill_protection_line(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
    execution_date: str,
    atr: Decimal,
    active_line: str,
) -> None:
    from .a_share_trend import load_protection_state, write_protection_state

    state_path = (
        data_dir / PROTECTION_STATE_ROOTS[market] / "protection_state.json"
    )
    state = load_protection_state(state_path)
    positions = dict(state["positions"])
    existing = positions.get(symbol)
    prior = dict(existing) if isinstance(existing, Mapping) else {}
    positions[symbol] = {
        **prior,
        "initial_line": str(prior.get("initial_line") or active_line),
        "active_line": active_line,
        "atr14": format(atr, "f"),
        "position_started_for": str(
            prior.get("position_started_for") or execution_date
        ),
        "tracking_active": prior.get("tracking_active") is True,
        "updated_for": execution_date,
    }
    write_protection_state(state_path, {**state, "positions": positions})


def _preflight_open_actions(
    report: Mapping[str, object], market: str
) -> tuple[list[Mapping[str, object]], str]:
    judgments = report.get("strategy_judgments")
    actions = judgments.get("formal_actions") if isinstance(judgments, Mapping) else None
    if not isinstance(actions, list):
        raise ValueError("trend report formal actions are unavailable")
    strategy_snapshot = report.get("strategy_snapshot")
    strategy_version = (
        str(strategy_snapshot.get("strategy_version") or "")
        if isinstance(strategy_snapshot, Mapping)
        else ""
    )
    if not strategy_version:
        raise ValueError("trend report strategy version is unavailable")

    validated: list[Mapping[str, object]] = []
    sell_actions_by_symbol: set[str] = set()
    for action in actions:
        if not isinstance(action, Mapping):
            raise ValueError("trend review action is invalid")
        action_name = str(action.get("action") or "")
        symbol = str(action.get("symbol") or "").strip()
        if action_name not in {"BUY", "SELL_ALL", "SELL_PARTIAL"} or not symbol:
            raise ValueError("trend review action is invalid")
        futu_code = trend_action_futu_symbol(report, action, market)
        if action_name in {"SELL_ALL", "SELL_PARTIAL"}:
            if futu_code in sell_actions_by_symbol:
                raise ValueError("trend review has conflicting sell actions")
            sell_actions_by_symbol.add(futu_code)
        if action_name == "BUY":
            try:
                target_weight = _required_decimal(
                    action.get("target_weight"), "target weight"
                )
                atr = _required_decimal(action.get("atr"), "action ATR")
                lot_size = int(action.get("lot_size") or 0)
                quantity = _required_decimal(
                    action.get("estimated_shares"), "estimated shares"
                )
            except (TypeError, ValueError):
                raise ValueError("trend review buy action is invalid") from None
            if (
                target_weight <= 0
                or atr <= 0
                or lot_size <= 0
                or quantity <= 0
                or quantity != quantity.to_integral_value()
                or quantity % lot_size
            ):
                raise ValueError("trend review buy action is invalid")
        elif action_name == "SELL_PARTIAL":
            try:
                fraction = _required_decimal(
                    action.get("target_fraction"), "target fraction"
                )
                lot = _required_decimal(action.get("lot_size"), "lot size")
                if isinstance(action.get("lot_size"), bool):
                    raise TypeError
                lot_size = int(action.get("lot_size"))
                estimate = _required_decimal(
                    action.get("estimated_shares"), "estimated shares"
                )
                position_started_for = action.get("position_started_for")
                started = date.fromisoformat(str(position_started_for)).isoformat()
                signals = action.get("overheat_signals")
            except (TypeError, ValueError):
                raise ValueError("trend review partial sell action is invalid") from None
            if (
                fraction != Decimal("0.30")
                or lot <= 0
                or lot != lot.to_integral_value()
                or lot != Decimal(lot_size)
                or estimate < 0
                or estimate != estimate.to_integral_value()
                or estimate % lot
                or not isinstance(position_started_for, str)
                or position_started_for != started
                or not isinstance(signals, list)
                or not signals
                or not all(isinstance(signal, str) for signal in signals)
                or set(signals) - {"boiling", "champagne"}
                or len(signals) != len(set(signals))
            ):
                raise ValueError("trend review partial sell action is invalid")
        validated.append(action)
    return validated, strategy_version


def execute_trend_review_open(
    *,
    data_dir: Path,
    report: Mapping[str, object],
    client: object,
    market: str,
    execution_date: str,
    now: str,
    quote_prices: Mapping[str, Decimal],
    order_history_start: str | None = None,
    prior_sell_requests: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    market = _market(market)
    actions, strategy_version = _preflight_open_actions(report, market)
    current = datetime.fromisoformat(now)
    local_current = current.astimezone(MARKET_TIMEZONES[market])
    execution_day = date.fromisoformat(execution_date)
    order_history_start = (
        date.fromisoformat(order_history_start).isoformat()
        if order_history_start is not None
        else execution_date
    )
    same_day = local_current.date() == execution_day
    buy_window_end = "16:00" if market == "US" else "10:00"
    local_time = local_current.time().replace(tzinfo=None)
    buy_window_open = (
        datetime.strptime("09:30", "%H:%M").time()
        <= local_time
        <= datetime.strptime(buy_window_end, "%H:%M").time()
    )
    market_open = {
        "CN": (
            datetime.strptime("09:30", "%H:%M").time()
            <= local_time
            <= datetime.strptime("11:30", "%H:%M").time()
        ) or (
            datetime.strptime("13:00", "%H:%M").time()
            <= local_time
            <= datetime.strptime("15:00", "%H:%M").time()
        ),
        "HK": (
            datetime.strptime("09:30", "%H:%M").time()
            <= local_time
            <= datetime.strptime("12:00", "%H:%M").time()
        ) or (
            datetime.strptime("13:00", "%H:%M").time()
            <= local_time
            <= datetime.strptime("16:00", "%H:%M").time()
        ),
        "US": datetime.strptime("09:30", "%H:%M").time()
        <= local_time
        <= datetime.strptime("16:00", "%H:%M").time(),
    }[market]
    snapshot = client.account_snapshot()
    if not isinstance(snapshot, Mapping):
        raise TrendReviewAccountStateError("simulate account snapshot is invalid")
    _ensure_discipline_account(data_dir, market, snapshot)
    nav = _required_decimal(snapshot.get("net_value"), "simulate net value")
    if nav <= 0:
        raise TrendReviewAccountStateError("simulate net value must be positive")
    report_sha = _report_hash(report)
    submitted = 0
    artifacts: list[str] = []
    blocked_status: str | None = None
    sell_symbols = {
        trend_action_futu_symbol(report, action, market)
        for action in actions
        if (
            isinstance(action, Mapping)
            and action.get("action") in {"SELL_ALL", "SELL_PARTIAL"}
        )
    }
    root = (
        data_dir
        / "trend_review"
        / "ledgers"
        / market
        / "open"
        / execution_date
    )
    ordered_actions = sorted(
        enumerate(actions),
        key=lambda item: not (
            isinstance(item[1], Mapping)
            and item[1].get("action") in {"SELL_ALL", "SELL_PARTIAL"}
        ),
    )
    for index, action in ordered_actions:
        if not isinstance(action, Mapping):
            continue
        action_name = str(action.get("action") or "")
        symbol = str(action.get("symbol") or "").strip()
        if action_name not in {"BUY", "SELL_ALL", "SELL_PARTIAL"}:
            continue
        futu_code = trend_action_futu_symbol(report, action, market)
        side = "buy" if action_name == "BUY" else "sell"
        action_key = trend_action_key(market, execution_date, futu_code, side)
        action_evidence = {
            "market": market,
            "date": execution_date,
            "strategy_version": strategy_version,
            "report_sha256": report_sha,
            "action_index": index,
            "symbol": symbol,
            "futu_code": futu_code,
            "side": side,
        }
        stem = action_key
        intent_path = root / f"{stem}-intent.json"
        attempt = 1
        action_events_root = (
            data_dir
            / "trend_review"
            / "ledgers"
            / market
            / "actions"
            / execution_date
            / action_key
        )
        action_facts = _action_facts(root, futu_code=futu_code, side=side)
        late_buy_authorized = (
            action_name == "BUY"
            and _valid_late_buy_authorization(
                data_dir,
                market=market,
                execution_date=execution_date,
                report_sha=report_sha,
                symbol=symbol,
                events=_action_events(action_events_root),
                facts=action_facts,
            )
        )
        late_buy_ready = late_buy_authorized and same_day and market_open
        sell_metadata: dict[str, object] = (
            {"sell_goal": "position_zero"}
            if action_name == "SELL_ALL"
            else {}
        )
        if sell_metadata:
            action_evidence = {**action_evidence, **sell_metadata}
        if action_name == "SELL_PARTIAL":
            progress = overheat_trim_progress(
                data_dir,
                market=market,
                symbol=symbol,
                position_started_for=str(action["position_started_for"]),
            )
            lifecycle_target = _required_decimal(
                progress["lifecycle_target_qty"], "lifecycle target quantity"
            )
            lifecycle_filled = _required_decimal(
                progress["filled_qty"], "lifecycle filled quantity"
            )
            if not action_facts and progress["has_unresolved_order"]:
                blocked_status = "unresolved"
                continue
            if lifecycle_target > 0 or progress["source_paths"]:
                sell_metadata = {
                    "sell_goal": "partial_30",
                    "position_started_for": str(action["position_started_for"]),
                    "lifecycle_target_qty": format(lifecycle_target, "f"),
                }
                action_evidence = {**action_evidence, **sell_metadata}
                sell_quantity = int(lifecycle_target - lifecycle_filled)
                if (
                    progress["status"] in {"complete", "below_lot"}
                    or sell_quantity <= 0
                ):
                    continue
        resolutions = _action_resolutions(
            action_events_root,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            symbol=symbol,
            futu_code=futu_code,
            side=side,
            action_attempts={item[3] for item in action_facts},
        )
        authorized_attempts = {
            int(item["attempt_no"])
            for item in resolutions
            if item.get("resolution") == "authorize-retry"
        }
        partial_abandoned = action_name == "SELL_ALL" and any(
            item.get("resolution") == "abandon" for item in resolutions
        ) and any(
            item[1].get("sell_goal") == "partial_30" for item in action_facts
        )
        buy_window_event = None
        if action_name == "BUY" and futu_code not in sell_symbols:
            if same_day and local_time < datetime.strptime("09:30", "%H:%M").time():
                buy_window_event = ("pending", "buy_window_not_open")
            elif local_current.date() > execution_day or not buy_window_open:
                buy_window_event = ("missed", "buy_window_closed")
        if action_name == "SELL_ALL":
            reason_id = str(
                action.get("event_id") or action.get("reason") or ""
            ).strip()
            if reason_id and not any(
                event.get("status") == "reason_added"
                and event.get("reason_id") == reason_id
                for event in _action_events(action_events_root)
            ):
                _write_action_event(
                    data_dir=data_dir,
                    market=market,
                    execution_date=execution_date,
                    action_key=action_key,
                    payload={
                        **action_evidence,
                        "status": "reason_added",
                        "reason_id": reason_id,
                        "reason": str(action.get("reason") or "sell_all"),
                    },
                    recorded_at=now,
                )
        if any(
            item.get("resolution") == "confirm-submitted"
            or (
                item.get("resolution") == "abandon"
                and not partial_abandoned
            )
            for item in resolutions
        ):
            continue
        if local_current.date() < execution_day:
            continue
        sell_position = next(
            (
                item
                for item in _positive_positions(snapshot)
                if str(item.get("code") or item.get("futu_code") or "")
                .strip()
                .upper()
                == futu_code.upper()
            ),
            None,
        ) if action_name == "SELL_ALL" else None
        sell_quantity = (
            int(
                _required_decimal(
                    sell_position.get("qty", sell_position.get("quantity")),
                    "position qty",
                )
            )
            if sell_position is not None
            else 0
        )
        if action_name == "SELL_PARTIAL":
            if sell_metadata:
                sell_quantity = int(lifecycle_target - lifecycle_filled)
            if any(
                event.get("status") == "below_lot"
                for event in _action_events(action_events_root)
            ):
                continue
            if not action_facts and not sell_metadata:
                try:
                    live_positions = _positive_positions(snapshot)
                    matching_positions = [
                        item
                        for item in live_positions
                        if str(item.get("code") or item.get("futu_code") or "")
                        .strip()
                        .upper()
                        == futu_code.upper()
                    ]
                    live_quantity = sum(
                        (
                            _required_decimal(
                                item.get("qty", item.get("quantity")),
                                "position qty",
                            )
                            for item in matching_positions
                        ),
                        start=Decimal("0"),
                    )
                except ValueError as exc:
                    raise TrendReviewAccountStateError(
                        "simulate partial sell position is invalid"
                    ) from exc
                if (
                    not matching_positions
                    or live_quantity <= 0
                    or live_quantity != live_quantity.to_integral_value()
                ):
                    raise TrendReviewAccountStateError(
                        "simulate partial sell position is unavailable"
                    )
                target = _overheat_trim_quantity(
                    live_quantity,
                    _required_decimal(action.get("target_fraction"), "target fraction"),
                    int(action["lot_size"]),
                )
                sell_metadata = {
                    "sell_goal": "partial_30",
                    "position_started_for": str(action["position_started_for"]),
                    "lifecycle_target_qty": format(target, "f"),
                }
                action_evidence = {**action_evidence, **sell_metadata}
                sell_quantity = int(target)
                if target <= 0:
                    broker_evidence = _write_broker_observation(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        evidence=action_evidence,
                        snapshot=snapshot,
                        orders=(),
                        recorded_at=now,
                    )
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            **broker_evidence,
                            "status": "below_lot",
                            "reason": "overheat_target_below_lot",
                            "target_qty": "0",
                            "filled_qty": "0",
                            "order_ids": [],
                        },
                        recorded_at=now,
                    )
                    continue
        if action_name == "SELL_ALL":
            position_zero_complete = any(
                event.get("status") in {"filled", "incomplete"}
                and event.get("reason") == "position_zero_confirmed"
                and event.get("sell_goal") in {None, "position_zero"}
                for event in _action_events(action_events_root)
            )
            if position_zero_complete:
                continue
        if action_name == "SELL_ALL" and sell_quantity <= 0 and not action_facts:
            broker_evidence = _write_broker_observation(
                data_dir=data_dir,
                market=market,
                execution_date=execution_date,
                action_key=action_key,
                evidence=action_evidence,
                snapshot=snapshot,
                orders=(),
                recorded_at=now,
            )
            _write_action_status_once(
                data_dir=data_dir,
                market=market,
                execution_date=execution_date,
                action_key=action_key,
                action_root=action_events_root,
                evidence={
                    **action_evidence,
                    **broker_evidence,
                    "filled_qty": "0",
                    "target_qty": "0",
                    "order_ids": [],
                },
                status="incomplete",
                reason="position_zero_confirmed",
                recorded_at=now,
            )
            continue
        if (
            not same_day
            and action_name in {"SELL_ALL", "SELL_PARTIAL"}
            and not (local_current.date() > execution_day and bool(action_facts))
        ):
            continue
        if action_name == "BUY" and futu_code in sell_symbols:
            continue
        if (
            action_name == "BUY"
            and not action_facts
            and buy_window_event
            and not late_buy_ready
        ):
            event_status, event_reason = buy_window_event
            _write_action_status_once(
                data_dir=data_dir,
                market=market,
                execution_date=execution_date,
                action_key=action_key,
                action_root=action_events_root,
                evidence=action_evidence,
                status=event_status,
                reason=event_reason,
                recorded_at=now,
            )
            continue
        if (
            action_name in {"SELL_ALL", "SELL_PARTIAL"}
            and not market_open
            and sell_quantity > 0
        ):
            continue
        if action_facts:
            request = action_facts[0][2]
            partial_upgrade = action_name == "SELL_ALL" and any(
                item[1].get("sell_goal") == "partial_30"
                for item in action_facts
            )
            goal_facts = [
                item
                for item in action_facts
                if item[1].get("sell_goal") == "position_zero"
            ] if partial_upgrade else action_facts
            broker_order: Mapping[str, object] | None = None
            pending_intent = next(
                (
                    item[0]
                    for item in action_facts
                    if not _result_path(item[0]).exists()
                    and item[3] not in authorized_attempts
                    and (
                        not partial_abandoned
                        or item[1].get("sell_goal") != "partial_30"
                    )
                ),
                None,
            )
            if pending_intent is not None and (
                action_name != "SELL_ALL" or sell_quantity > 0
            ):
                pending_fact = next(
                    item for item in action_facts if item[0] == pending_intent
                )
                pending_payload = pending_fact[1]
                request = pending_fact[2]
                pending_attempt = pending_fact[3]
                pending_report_sha = pending_payload.get("report_sha256")
                pending_action_index = pending_payload.get("action_index")
                if (
                    not isinstance(pending_report_sha, str)
                    or len(pending_report_sha) != 64
                    or not isinstance(pending_action_index, int)
                    or isinstance(pending_action_index, bool)
                    or pending_action_index < 0
                ):
                    raise ValueError(
                        f"invalid trend action fact: {pending_intent}"
                    )
                orders = _listed_orders(
                    client,
                    start=order_history_start,
                    end=local_current.date().isoformat(),
                )
                broker_fact, broker_order = _broker_attempt_fact(orders, request)
                if broker_fact == "conflict":
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "conflict",
                            "target_qty": str(request.get("qty") or ""),
                            "reason": "broker order conflicts with immutable intent",
                        },
                        recorded_at=now,
                    )
                    blocked_status = "conflict"
                    break
                rejected_status = next(
                    (
                        str(
                            order.get("order_status")
                            or order.get("status")
                            or ""
                        )
                        .strip()
                        .upper()
                        for order in [broker_order]
                        if order is not None
                        if str(
                            order.get("order_status")
                            or order.get("status")
                            or ""
                        )
                        .strip()
                        .upper()
                        in REJECTED_ORDER_STATUSES
                    ),
                    None,
                )
                if broker_order is not None:
                    _write_reconciled_result(
                        pending_intent,
                        market=market,
                        execution_date=execution_date,
                        request=request,
                        response=broker_order,
                        report_sha=pending_report_sha,
                        action_index=pending_action_index,
                        reconciled_at=now,
                        metadata={
                            key: pending_payload[key]
                            for key in (
                                "sell_goal",
                                "position_started_for",
                                "lifecycle_target_qty",
                            )
                            if key in pending_payload
                        },
                    )
                if rejected_status is not None:
                    reason = f"simulate {side} order rejected: {rejected_status}"
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "failed",
                            "target_qty": str(request.get("qty") or ""),
                            "reason": reason,
                        },
                        recorded_at=now,
                    )
                    raise RuntimeError(reason)
                if broker_order is None:
                    _write_uncertain_action_event_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        attempt=pending_attempt,
                        reason="intent has no conclusive broker fact",
                        target_qty=str(request.get("qty") or ""),
                        recorded_at=now,
                    )
                    blocked_status = "uncertain"
                    break
            pending_sell_completed = (
                pending_intent is not None
                and action_name == "SELL_ALL"
                and sell_quantity <= 0
            )
            if (
                pending_intent is None
                or broker_order is not None
                or pending_sell_completed
            ):
                orders = _listed_orders(
                    client,
                    start=order_history_start,
                    end=local_current.date().isoformat(),
                )
                if action_name == "SELL_ALL" and partial_upgrade:
                    prior_orders = [
                        order
                        for order in orders
                        if any(
                            _order_has_action_identity(order, request)
                            for request in prior_sell_requests
                        )
                    ]
                    prior_statuses = {
                        str(order.get("order_status") or order.get("status") or "")
                        .strip()
                        .upper()
                        for order in prior_orders
                    }
                    if prior_statuses & ACTIVE_ORDER_STATUSES:
                        blocked_status = "submitted"
                        break
                    if prior_statuses - TERMINAL_ORDER_STATUSES:
                        blocked_status = "uncertain"
                        break
                requests_by_remark: dict[str, list[dict[str, object]]] = {}
                for _, _, intent_request, _ in action_facts:
                    requests_by_remark.setdefault(
                        str(intent_request.get("remark") or ""), []
                    ).append(intent_request)
                conflicting_order = next(
                    (
                        order
                        for order in orders
                        if str(order.get("remark") or "") in requests_by_remark
                        and not any(
                            _order_matches_request(order, candidate)
                            for candidate in requests_by_remark[
                                str(order.get("remark") or "")
                            ]
                        )
                    ),
                    None,
                )
                if conflicting_order is not None:
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "conflict",
                            "target_qty": str(request.get("qty") or ""),
                            "reason": "broker order conflicts with immutable intent",
                        },
                        recorded_at=now,
                    )
                    blocked_status = "conflict"
                    break
                matched = [
                    order
                    for order in orders
                    if any(
                        _order_has_action_identity(order, candidate)
                        for candidate in requests_by_remark.get(
                            str(order.get("remark") or ""), []
                        )
                    )
                ]
                goal_requests = [item[2] for item in goal_facts]
                goal_matched = [
                    order
                    for order in matched
                    if any(
                        _order_has_action_identity(order, candidate)
                        for candidate in goal_requests
                    )
                ]
                latest_attempt = max(item[3] for item in action_facts)
                latest_requests = [
                    intent_request
                    for _, _, intent_request, attempt_no in action_facts
                    if attempt_no == latest_attempt
                ]
                latest_is_abandoned_partial = partial_abandoned and all(
                    payload.get("sell_goal") == "partial_30"
                    for _, payload, _, attempt_no in action_facts
                    if attempt_no == latest_attempt
                )
                latest_matched = [
                    order
                    for order in orders
                    if any(
                        str(order.get("remark") or "")
                        == str(candidate.get("remark") or "")
                        and _order_has_action_identity(order, candidate)
                        for candidate in latest_requests
                    )
                ]
                latest_order_ids = {
                    str(order.get("order_id") or f"missing-{position}")
                    for position, order in enumerate(latest_matched)
                }
                position_zero = action_name == "SELL_ALL" and sell_quantity <= 0
                inconclusive_reason = (
                    "broker action attempt is ambiguous"
                    if len(latest_order_ids) > 1
                    else "broker order status is absent"
                    if not latest_matched
                    and not position_zero
                    and latest_attempt not in authorized_attempts
                    and not latest_is_abandoned_partial
                    else None
                )
                if inconclusive_reason is not None:
                    attempt = latest_attempt
                    _write_uncertain_action_event_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        attempt=attempt,
                        reason=inconclusive_reason,
                        recorded_at=now,
                    )
                    blocked_status = "uncertain"
                    break
                target_quantity = _required_decimal(
                    (goal_requests[0] if goal_requests else request).get("qty"),
                    "target quantity",
                )
                dealt_by_order = {
                    str(order.get("order_id") or index): _required_decimal(
                        order.get("dealt_qty", "0"), "broker dealt quantity"
                    )
                    for index, order in enumerate(
                        goal_matched if partial_upgrade else matched
                    )
                }
                broker_filled = sum(
                    dealt_by_order.values(), start=Decimal("0")
                )
                remaining = target_quantity - broker_filled
                filled = broker_filled
                if position_zero:
                    remaining = Decimal("0")
                elif action_name == "SELL_ALL":
                    remaining = Decimal(sell_quantity)
                order_ids = [
                    str(order.get("order_id"))
                    for order in (goal_matched if partial_upgrade else matched)
                    if order.get("order_id") not in {None, ""}
                ]
                weighted_prices = [
                    (
                        _required_decimal(order.get("dealt_qty"), "broker dealt quantity"),
                        _required_decimal(
                            order.get("dealt_avg_price"), "broker average fill price"
                        ),
                    )
                    for order in (goal_matched if partial_upgrade else matched)
                    if _required_decimal(
                        order.get("dealt_qty", "0"), "broker dealt quantity"
                    ) > 0
                    and order.get("dealt_avg_price") not in {None, ""}
                ]
                average_price = (
                    sum(
                        (quantity * price for quantity, price in weighted_prices),
                        start=Decimal("0"),
                    )
                    / sum(
                        (quantity for quantity, _ in weighted_prices),
                        start=Decimal("0"),
                    )
                    if weighted_prices
                    else None
                )
                protection_fact = {}
                if action_name == "BUY" and average_price is not None:
                    protection_fact = {
                        "active_protection_line": format(
                            average_price
                            - Decimal("2")
                            * _required_decimal(action.get("atr"), "action ATR"),
                            "f",
                        )
                    }
                if (position_zero or any(
                    order.get("order_id")
                    or order.get("order_status")
                    or order.get("dealt_qty")
                    for order in (goal_matched if partial_upgrade else matched)
                )) and (not partial_upgrade or goal_facts):
                    terminal_evidence = (
                        _write_broker_observation(
                            data_dir=data_dir,
                            market=market,
                            execution_date=execution_date,
                            action_key=action_key,
                            evidence=action_evidence,
                            snapshot=snapshot,
                            orders=(
                                goal_matched if partial_upgrade else matched
                            ),
                            recorded_at=now,
                        )
                        if (
                            position_zero
                            or filled > 0
                            or action_name == "SELL_PARTIAL"
                        )
                        else {}
                    )
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": (
                                "filled"
                                if filled >= target_quantity
                                else "incomplete"
                                if position_zero
                                else "partially_filled"
                                if filled > 0
                                else "submitted"
                            ),
                            "filled_qty": format(filled, "f"),
                            "target_qty": format(target_quantity, "f"),
                            "avg_fill_price": (
                                format(average_price, "f")
                                if average_price is not None
                                else ""
                            ),
                            **protection_fact,
                            **terminal_evidence,
                            "order_ids": order_ids,
                            **(
                                {"reason": "position_zero_confirmed"}
                                if position_zero
                                else {}
                            ),
                        },
                        recorded_at=now,
                    )
                    if protection_fact:
                        _activate_fill_protection_line(
                            data_dir=data_dir,
                            market=market,
                            symbol=symbol,
                            execution_date=execution_date,
                            atr=_required_decimal(action.get("atr"), "action ATR"),
                            active_line=protection_fact["active_protection_line"],
                        )
                if remaining <= 0:
                    continue
                if (
                    action_name == "SELL_PARTIAL"
                    and latest_attempt not in authorized_attempts
                ):
                    continue
                if (
                    action_name == "BUY"
                    and buy_window_event
                    and not late_buy_ready
                ):
                    event_status, event_reason = buy_window_event
                    _write_action_status_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        status=event_status,
                        reason=event_reason,
                        recorded_at=now,
                    )
                    continue
                broker_statuses = {
                    str(order.get("order_status") or order.get("status") or "")
                    .strip()
                    .upper()
                    for order in latest_matched
                }
                if broker_statuses & ACTIVE_ORDER_STATUSES:
                    if action_name == "SELL_ALL":
                        blocked_status = "submitted"
                    continue
                if broker_statuses - TERMINAL_ORDER_STATUSES:
                    attempt = latest_attempt
                    reason = "broker order status is inconclusive"
                    _write_uncertain_action_event_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        attempt=attempt,
                        reason=reason,
                        recorded_at=now,
                    )
                    blocked_status = "uncertain"
                    break
                attempt = latest_attempt + 1
                if action_name == "BUY":
                    if futu_code not in quote_prices:
                        _write_action_status_once(
                            data_dir=data_dir,
                            market=market,
                            execution_date=execution_date,
                            action_key=action_key,
                            action_root=action_events_root,
                            evidence=action_evidence,
                            status="pending",
                            reason="current_quote_unavailable",
                            recorded_at=now,
                        )
                        blocked_status = "quote_unavailable"
                        continue
                    remaining = Decimal(
                        _remaining_buy_quantity(
                            action,
                            report,
                            snapshot,
                            matched,
                            _required_decimal(
                                quote_prices.get(futu_code), "current quote price"
                            ),
                        )
                    )
                    if remaining <= 0:
                        continue
                request = {**request, "qty": format(remaining, "f")}
                request["remark"] = trend_attempt_remark(
                    market, execution_date, action_key, attempt
                )
                broker_fact, broker_order = _broker_attempt_fact(orders, request)
                if broker_fact == "conflict":
                    _write_action_event(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        payload={
                            **action_evidence,
                            "status": "conflict",
                            "target_qty": format(remaining, "f"),
                            "reason": "broker order conflicts with proposed attempt",
                        },
                        recorded_at=now,
                    )
                    blocked_status = "conflict"
                    break
                intent_path = root / f"{stem}-attempt-{attempt}-intent.json"
                _write_immutable(
                    intent_path,
                    _canonical_json_bytes(
                        {
                            "market": market,
                            "date": execution_date,
                            "report_sha256": report_sha,
                            "action_index": index,
                            "attempt": attempt,
                            "request": request,
                            "created_at": now,
                            **sell_metadata,
                        }
                    ),
                )
                if broker_order is not None:
                    _write_reconciled_result(
                        intent_path,
                        market=market,
                        execution_date=execution_date,
                        request=request,
                        response=broker_order,
                        report_sha=report_sha,
                        action_index=index,
                        reconciled_at=now,
                        metadata=sell_metadata,
                    )
                    continue
        else:
            if action_name == "BUY":
                if futu_code not in quote_prices:
                    _write_action_status_once(
                        data_dir=data_dir,
                        market=market,
                        execution_date=execution_date,
                        action_key=action_key,
                        action_root=action_events_root,
                        evidence=action_evidence,
                        status="pending",
                        reason="current_quote_unavailable",
                        recorded_at=now,
                    )
                    blocked_status = "quote_unavailable"
                    continue
                quantity = _remaining_buy_quantity(
                    action,
                    report,
                    snapshot,
                    (),
                    _required_decimal(
                        quote_prices.get(futu_code), "current quote price"
                    ),
                )
            else:
                quantity = sell_quantity
            if quantity <= 0:
                continue
            request = {
                "market": market,
                "futu_code": futu_code,
                "side": side,
                "order_type": "MARKET",
                "price": "0",
                "qty": str(quantity),
                "remark": trend_attempt_remark(
                    market, execution_date, action_key, 1
                ),
            }
            orders = _listed_orders(
                client,
                start=order_history_start,
                end=local_current.date().isoformat(),
            )
            same_remark = [
                order
                for order in orders
                if str(order.get("remark") or "") == request["remark"]
            ]
            legacy_prefix = f"trend-review:{market}:{execution_date}:"
            legacy_candidates = [
                order
                for order in orders
                if str(order.get("remark") or "").startswith(legacy_prefix)
                and _order_has_action_identity(
                    order,
                    {
                        **request,
                        "remark": str(order.get("remark") or ""),
                    },
                )
            ]
            candidates = [*same_remark, *legacy_candidates]
            exact = [
                order
                for order in candidates
                if _order_matches_request(
                    order,
                    {
                        **request,
                        "remark": str(order.get("remark") or ""),
                    },
                )
            ]
            if candidates and (len(candidates) != 1 or len(exact) != 1):
                _write_action_event(
                    data_dir=data_dir,
                    market=market,
                    execution_date=execution_date,
                    action_key=action_key,
                    payload={
                        **action_evidence,
                        "status": "conflict",
                        "target_qty": str(quantity),
                        "reason": "broker action candidate is conflicting or ambiguous",
                    },
                    recorded_at=now,
                )
                blocked_status = "conflict"
                break
            if exact:
                request["remark"] = str(exact[0].get("remark") or "")
            _write_immutable(
                intent_path,
                _canonical_json_bytes(
                    {
                        "market": market,
                        "date": execution_date,
                        "report_sha256": report_sha,
                        "action_index": index,
                        "request": request,
                        "created_at": now,
                        **sell_metadata,
                    }
                ),
            )
            if exact:
                _write_reconciled_result(
                    intent_path,
                    market=market,
                    execution_date=execution_date,
                    request=request,
                    response=exact[0],
                    report_sha=report_sha,
                    action_index=index,
                    reconciled_at=now,
                    metadata=sell_metadata,
                )
                continue
        base_request = (
            goal_facts[0][2]
            if action_facts and partial_upgrade and goal_facts
            else request
            if action_facts and partial_upgrade
            else action_facts[0][2]
            if action_facts
            else request
        )
        target_qty = str(base_request.get("qty") or request.get("qty") or "")
        try:
            response = client.place_order(request)
        except Exception as exc:
            _write_action_event(
                data_dir=data_dir,
                market=market,
                execution_date=execution_date,
                action_key=action_key,
                payload={
                    **action_evidence,
                    "status": "failed",
                    "attempt": attempt,
                    "target_qty": target_qty,
                    "reason": str(exc),
                },
                recorded_at=now,
            )
            raise
        result_path = _result_path(intent_path)
        _write_immutable(
            result_path,
            _canonical_json_bytes(
                {
                    "market": market,
                    "date": execution_date,
                    "report_sha256": report_sha,
                    "action_index": index,
                    "request": request,
                    "response": response,
                    "submitted_at": now,
                    **sell_metadata,
                }
            ),
        )
        order_id = str(response.get("futu_order_id") or "")
        _write_action_event(
            data_dir=data_dir,
            market=market,
            execution_date=execution_date,
            action_key=action_key,
            payload={
                **action_evidence,
                "status": "submitted",
                "attempt": attempt,
                "target_qty": target_qty,
                "order_ids": [order_id] if order_id else [],
            },
            recorded_at=now,
        )
        artifacts.append(str(result_path))
        submitted += 1
    state_path = data_dir / PROTECTION_STATE_ROOTS[market] / "protection_state.json"
    rebuild_overheat_trim_projection(data_dir, market=market, state_path=state_path)
    return {
        "status": (
            blocked_status
            if blocked_status is not None
            else "submitted"
            if submitted
            else "unchanged"
            if buy_window_open or market_open
            else "missed_window"
        ),
        "market": market,
        "date": execution_date,
        "submitted_count": submitted,
        "artifact_paths": artifacts,
    }


def execute_trend_review_stop(
    *,
    data_dir: Path,
    market: str,
    symbol: str,
    trading_date: str,
    event_id: str,
    client: object,
    now: str,
) -> dict[str, object]:
    market = _market(market)
    symbol = symbol.strip()
    event_id = event_id.strip()
    trading_date = date.fromisoformat(trading_date).isoformat()
    if not symbol or not event_id:
        raise ValueError("trend review protection event is invalid")
    from .a_share_trend import load_protection_state
    from .futu_symbols import to_futu_symbol

    futu_code = to_futu_symbol(market, symbol)
    ledger = data_dir / "trend_review" / "ledgers" / market
    protection_state = load_protection_state(
        data_dir / PROTECTION_STATE_ROOTS[market] / "protection_state.json"
    )
    position_state = protection_state["positions"].get(symbol)
    position_started_for = (
        date.fromisoformat(position_state["position_started_for"]).isoformat()
        if isinstance(position_state, Mapping)
        and isinstance(position_state.get("position_started_for"), str)
        and position_state["position_started_for"]
        else None
    )

    def belongs_to_current_position(payload: Mapping[str, object]) -> bool:
        return payload.get("sell_goal") == "partial_30" and (
            position_started_for is None
            or payload.get("position_started_for") == position_started_for
        )

    partial_dates: set[str] = set()
    partial_requests: dict[str, list[Mapping[str, object]]] = {}
    for root in sorted((ledger / "open").glob("*")):
        if not root.is_dir():
            continue
        try:
            execution_date = date.fromisoformat(root.name).isoformat()
        except ValueError:
            continue
        facts = [
            fact
            for fact in _action_facts(root, futu_code=futu_code, side="sell")
            if belongs_to_current_position(fact[1])
        ]
        if facts:
            partial_dates.add(execution_date)
            partial_requests[execution_date] = [fact[2] for fact in facts]
    for root in sorted((ledger / "actions").glob("*")):
        if not root.is_dir():
            continue
        try:
            execution_date = date.fromisoformat(root.name).isoformat()
        except ValueError:
            continue
        action_root = root / trend_action_key(
            market, execution_date, futu_code, "sell"
        )
        if action_root.is_dir() and any(
            belongs_to_current_position(event)
            for event in _action_events(action_root)
        ):
            partial_dates.add(execution_date)
    partial_execution_date = max(partial_dates, default=trading_date)
    order_history_start = min(partial_dates, default=trading_date)
    return execute_trend_review_open(
        data_dir=data_dir,
        report=_protection_report(symbol, event_id),
        client=client,
        market=market,
        execution_date=partial_execution_date,
        now=now,
        quote_prices={},
        order_history_start=order_history_start,
        prior_sell_requests=tuple(
            request
            for execution_date, requests in partial_requests.items()
            if execution_date < partial_execution_date
            for request in requests
        ),
    )


def capture_trend_review_close(
    *,
    data_dir: Path,
    market: str,
    trading_date: str,
    report: Mapping[str, object],
    simulate_snapshot: Mapping[str, object],
    orders: list[Mapping[str, object]],
    benchmark: Mapping[str, object],
) -> Path:
    market = _market(market)
    net_value = _required_decimal(
        simulate_snapshot.get("net_value"), "simulate net value"
    )
    discipline_equity = net_value.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    _validate_benchmark(benchmark, market=market, trading_date=trading_date)
    strategy_snapshot = report.get("strategy_snapshot")
    if (
        not isinstance(strategy_snapshot, Mapping)
        or not strategy_snapshot.get("strategy_id")
        or not strategy_snapshot.get("strategy_version")
        or not strategy_snapshot.get("process_version")
        or not isinstance(strategy_snapshot.get("parameters"), Mapping)
        or not strategy_snapshot.get("parameter_rows")
    ):
        raise ValueError("trend report strategy snapshot is unavailable")
    payload = {
        "schema_version": "open_trader.trend_review.daily.v1",
        "market": market,
        "date": trading_date,
        "simulate_acc_id": simulate_snapshot.get("acc_id"),
        "discipline_equity_after_fees": str(discipline_equity),
        "benchmark": dict(benchmark),
        "strategy_snapshot": dict(strategy_snapshot),
        "report_sha256": _report_hash(report),
        "orders": orders,
        "positions": simulate_snapshot.get("positions"),
    }
    path = (
        data_dir
        / "trend_review"
        / "daily"
        / market
        / f"{trading_date}.json"
    )
    return _write_immutable(path, _canonical_json_bytes(payload))


def validate_trend_review_close_report(
    report: Mapping[str, object], trading_date: str, market: str
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    market = _market(market)
    account = report.get("account")
    if not isinstance(account, Mapping):
        raise ValueError("trend report account is unavailable")
    strategy_snapshot = report.get("strategy_snapshot")
    try:
        strategy_snapshot = normalize_trend_strategy_snapshot(
            strategy_snapshot, market
        )
    except ValueError:
        raise ValueError("trend report strategy snapshot is unavailable")
    if account.get("fresh") is True and account.get("source_date") == trading_date:
        _required_decimal(account.get("net_value"), "actual net value")
        positions = account.get("positions")
        if not isinstance(positions, list) or any(
            not isinstance(position, Mapping) for position in positions
        ):
            raise ValueError("trend report account positions are unavailable")
    return account, strategy_snapshot


def _legacy_strategy_snapshot_variants(
    expected: Mapping[str, object], market: str,
) -> tuple[dict[str, object], ...]:
    """Return the immutable pre-v3 snapshot shapes still found on disk."""
    base = copy.deepcopy(dict(expected))
    base["strategy_id"] = f"trend_animals_warm_to_hot/{market}/v1"
    base["strategy_version"] = "v1"
    base["effective_from"] = "2026-07-14"
    parameters = base.get("parameters")
    rows = base.get("parameter_rows")
    if not isinstance(parameters, dict) or not isinstance(rows, list):
        return ()
    removed_parameters = {
        "single_entry_risk_limit",
        "portfolio_risk_limit",
        "abnormal_loss_buffer",
        "normal_cost_rate",
        "normal_cost_model",
        "overheat_trim_fraction",
        "overheat_trim_once_per_position",
        "overheat_trim_signals",
        "overheat_trim_rounding",
        "overheat_trim_below_lot",
        "full_exit_precedes_partial_exit",
        "kelly_sample_minimum",
        "kelly_rolling_window",
        "kelly_fraction",
        "kelly_optimizer",
        "kelly_sample_inherits",
        "kelly_sample_scope",
        "kelly_source",
    }
    for key in removed_parameters:
        parameters.pop(key, None)
    riskless_current_rows = copy.deepcopy(dict(expected))
    riskless_current_rows["strategy_id"] = (
        f"trend_animals_warm_to_hot/{market}/v1"
    )
    riskless_current_rows["strategy_version"] = "v1"
    riskless_current_rows["effective_from"] = "2026-07-14"
    riskless_parameters = riskless_current_rows.get("parameters")
    if isinstance(riskless_parameters, dict):
        for key in (
            "single_entry_risk_limit",
            "portfolio_risk_limit",
            "abnormal_loss_buffer",
            "normal_cost_rate",
            "normal_cost_model",
        ):
            riskless_parameters.pop(key, None)
    row_names_to_drop = {
        "单笔计划止损风险上限",
        "组合正常计划风险上限",
        "异常损失缓冲",
        "过热止盈比例",
        "过热止盈信号",
        "过热止盈次数",
        "过热止盈取整",
        "不足一手处理",
        "清仓优先级",
    }
    filtered_rows: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("name") in row_names_to_drop:
            continue
        item = dict(row)
        if item.get("name") == "买入数量":
            item["value"] = (
                "按 1 股整数倍向下取整"
                if market == "US"
                else "按 100 股整数倍向下取整"
                if market == "CN"
                else "按 Futu 返回的每标的整手股数向下取整"
            )
        elif item.get("name") == "过热跟踪":
            item["value"] = "此前 5 个完整交易日最低价；保护线只升不降"
        filtered_rows.append(item)
    base["parameter_rows"] = filtered_rows
    variants = [riskless_current_rows]
    mapped_riskless = copy.deepcopy(riskless_current_rows)
    mapped_riskless["effective_from"] = {
        "CN": "2026-07-16",
        "US": "2026-07-17",
        "HK": "2026-07-17",
    }[market]
    variants.append(mapped_riskless)
    variants.append(base)
    effective_from = {
        "CN": "2026-07-16",
        "US": "2026-07-17",
        "HK": "2026-07-17",
    }[market]
    mapped_base = copy.deepcopy(base)
    mapped_base["effective_from"] = effective_from
    variants.append(mapped_base)

    feature = copy.deepcopy(base)
    feature_parameters = feature["parameters"]
    assert isinstance(feature_parameters, dict)
    feature_parameters["use_available_cash"] = True
    feature_parameters["trailing_activation_signals"] = ["boiling", "champagne"]
    feature_rows = feature["parameter_rows"]
    assert isinstance(feature_rows, list)
    for row in feature_rows:
        if row.get("name") == "买入数量":
            row["value"] = f"使用已有现金，{row['value']}"
        elif row.get("name") == "过热跟踪":
            row["value"] = (
                "沸腾或开香槟触发后，活动保护线取原值与此前 5 个完整交易日最低价的较高者，只升不降"
            )
    variants.append(feature)
    mapped_feature = copy.deepcopy(feature)
    mapped_feature["effective_from"] = effective_from
    variants.append(mapped_feature)
    return tuple(variants)


def normalize_trend_strategy_snapshot(
    snapshot: object,
    market: str,
    *,
    expected_snapshot: Mapping[str, object] | None = None,
) -> dict[str, object]:
    market = _market(market)
    if not isinstance(snapshot, Mapping):
        raise ValueError("strategy snapshot is unavailable")
    if expected_snapshot is None:
        parameters = snapshot.get("parameters")
        pools = (
            parameters.get("candidate_pool_ids")
            if isinstance(parameters, Mapping)
            else None
        )
        process_version = snapshot.get("process_version")
        if (
            not isinstance(pools, list)
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in pools
            )
            or not isinstance(process_version, str)
        ):
            raise ValueError("strategy snapshot is unavailable")
        from .a_share_trend import (
            live_trend_strategy_snapshot,
            trend_strategy_snapshot,
        )

        version = str(snapshot.get("strategy_version") or "")
        allocation = None
        if (market, version) in {
            ("CN", "v11"), ("CN", "v12"), ("CN", "v13"),
            ("HK", "v9"), ("HK", "v10"), ("HK", "v11"),
            ("US", "v9"), ("US", "v10"), ("US", "v11"),
        }:
            allocation = {
                "daily_path": parameters.get("allocation_snapshot_path"),
                "sha256": parameters.get("allocation_snapshot_sha256"),
                "snapshot": {
                    "markets": {
                        market: {
                            "rank": parameters.get("allocation_rank"),
                            "score": parameters.get("allocation_score"),
                            "score_source": parameters.get("allocation_score_source"),
                            "entry_weight": parameters.get("target_weight"),
                            "nominal_weight": parameters.get("nominal_weight"),
                        },
                    },
                },
            }
        if version in {
            "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13",
        }:
            expected_snapshot = live_trend_strategy_snapshot(
                market,
                process_version,
                pools,
                strategy_version=version,
                allocation=allocation,
            )
        else:
            expected_snapshot = trend_strategy_snapshot(
                market, process_version, pools
            )
            if snapshot.get("strategy_version") == "v2":
                expected_snapshot = {
                    **expected_snapshot,
                    "strategy_id": f"trend_animals_warm_to_hot/{market}/v2",
                    "strategy_version": "v2",
                }
    expected = copy.deepcopy(dict(expected_snapshot))
    if _canonical_json_bytes(dict(snapshot)) == _canonical_json_bytes(expected):
        return expected

    if any(
        _canonical_json_bytes(dict(snapshot)) == _canonical_json_bytes(variant)
        for variant in _legacy_strategy_snapshot_variants(expected, market)
    ):
        return expected

    legacy = copy.deepcopy(expected)
    legacy["effective_from"] = "2026-07-14"
    legacy_parameters = legacy.get("parameters")
    if not isinstance(legacy_parameters, dict):
        raise ValueError("strategy snapshot is unavailable")
    legacy_parameters.pop("use_available_cash", None)
    legacy_parameters.pop("trailing_activation_signals", None)
    old_buy_quantity = {
        "CN": "按 100 股整数倍向下取整",
        "US": "按 1 股整数倍向下取整",
        "HK": "按 Futu 返回的每标的整手股数向下取整",
    }[market]
    rows = legacy.get("parameter_rows")
    if not isinstance(rows, list):
        raise ValueError("strategy snapshot is unavailable")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("strategy snapshot is unavailable")
        if row.get("name") == "买入数量":
            row["value"] = old_buy_quantity
        elif row.get("name") == "过热跟踪":
            row["value"] = "此前 5 个完整交易日最低价；保护线只升不降"
    if _canonical_json_bytes(dict(snapshot)) != _canonical_json_bytes(legacy):
        raise ValueError(
            "strategy snapshot does not match current or known legacy rules"
        )
    return expected


def _strategy_identity(snapshot: Mapping[str, object]) -> bytes:
    parameters = snapshot.get("parameters")
    identity_parameters = (
        dict(parameters) if isinstance(parameters, Mapping) else parameters
    )
    if isinstance(identity_parameters, dict):
        for name in ALLOCATION_DYNAMIC_PARAMETER_NAMES:
            identity_parameters.pop(name, None)
    rows = snapshot.get("parameter_rows")
    identity_rows = (
        [
            row
            for row in rows
            if not (
                isinstance(row, Mapping)
                and (
                    row.get("group") == "市场资源配置"
                    or row.get("name") == "目标仓位"
                )
            )
        ]
        if isinstance(rows, list)
        else rows
    )
    return _canonical_json_bytes(
        {
            key: snapshot[key]
            for key in (
                "strategy_id",
                "strategy_name",
                "strategy_version",
                "market",
                "effective_from",
            )
        }
        | {
            "parameters": identity_parameters,
            "parameter_rows": identity_rows,
        }
    )


def _validate_benchmark(
    benchmark: object,
    *,
    market: str,
    trading_date: str,
    allow_legacy: bool = False,
) -> Mapping[str, object]:
    if not isinstance(benchmark, Mapping):
        raise ValueError("trend review benchmark must be an object")
    if benchmark.get("date") != trading_date:
        raise ValueError("benchmark date does not match trend review date")
    identity = BENCHMARK_IDENTITIES[market]
    expected = {
        "source_id": identity["source_id"],
        "futu_symbol": identity["futu_symbol"],
    }
    if allow_legacy and market in LEGACY_BENCHMARK_IDENTITIES:
        legacy = LEGACY_BENCHMARK_IDENTITIES[market]
        if all(benchmark.get(key) == value for key, value in legacy.items()):
            expected = legacy
    if benchmark.get("source_id") != expected["source_id"]:
        raise ValueError(
            f"benchmark source_id must be {expected['source_id']}"
        )
    if benchmark.get("futu_symbol") != expected["futu_symbol"]:
        raise ValueError("benchmark Futu symbol does not match market")
    if _required_decimal(benchmark.get("close"), "benchmark close") <= 0:
        raise ValueError("benchmark close must be positive")
    return benchmark


def _load_daily_facts(data_dir: Path, market: str) -> list[dict[str, object]]:
    root = data_dir / "trend_review" / "daily" / market
    facts: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "open_trader.trend_review.daily.v1"
            or payload.get("market") != market
            or payload.get("date") != path.stem
        ):
            raise ValueError(f"invalid trend review daily fact: {path}")
        _validate_benchmark(
            payload.get("benchmark"),
            market=market,
            trading_date=path.stem,
            allow_legacy=True,
        )
        facts.append(_merge_rotation_orders(payload, data_dir, market, path.stem))
    if not facts:
        raise ValueError(f"no trend review daily facts for {market}")
    return facts


def _load_dated_fact_stream(
    data_dir: Path,
    stream: str,
    market: str,
    schema_version: str,
) -> list[dict[str, object]]:
    root = data_dir / "trend_review" / "facts" / stream / market
    facts: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != schema_version
            or payload.get("market") != market
            or payload.get("date") != path.stem
        ):
            raise ValueError(f"invalid trend review {stream} fact: {path}")
        facts.append(
            _merge_rotation_orders(payload, data_dir, market, path.stem)
            if stream == "discipline"
            else payload
        )
    return facts


def _merge_rotation_orders(
    payload: Mapping[str, object],
    data_dir: Path,
    market: str,
    trading_date: str,
) -> dict[str, object]:
    """Merge validated rotation fills into the existing discipline fact stream."""
    orders = payload.get("orders")
    if not isinstance(orders, list) or not all(
        isinstance(order, Mapping) for order in orders
    ):
        raise ValueError("trend review daily orders must be a list")
    merged = [dict(order) for order in orders]
    known_ids = {
        str(order.get("order_id") or order.get("orderid") or ""): index
        for index, order in enumerate(merged)
        if str(order.get("order_id") or order.get("orderid") or "").strip()
    }
    for order in _rotation_fill_orders(data_dir, market, trading_date):
        order_id = str(order.get("order_id") or order.get("orderid") or "")
        if order_id and order_id in known_ids:
            index = known_ids[order_id]
            existing = merged[index]
            existing_side = str(
                existing.get("side") or existing.get("trd_side") or ""
            ).upper()
            rotation_side = str(
                order.get("side") or order.get("trd_side") or ""
            ).upper()
            existing_code = str(
                existing.get("code") or existing.get("futu_code") or ""
            ).strip().upper()
            rotation_code = str(
                order.get("code") or order.get("futu_code") or ""
            ).strip().upper()
            if existing_side != rotation_side or existing_code != rotation_code:
                raise ValueError("conflicting rotation fill order identity")
            for field in ("remark",):
                existing_value = str(existing.get(field) or "").strip()
                rotation_value = str(order.get(field) or "").strip()
                if existing_value and rotation_value and existing_value != rotation_value:
                    raise ValueError("conflicting rotation fill order identity")
            for field in ("dealt_qty",):
                existing_value = existing.get(field)
                rotation_value = order.get(field)
                if existing_value in (None, "") or rotation_value in (None, ""):
                    continue
                try:
                    if _required_decimal(existing_value, f"existing {field}") != _required_decimal(
                        rotation_value, f"rotation {field}"
                    ):
                        raise ValueError("conflicting rotation fill order identity")
                except ValueError as exc:
                    if str(exc) == "conflicting rotation fill order identity":
                        raise
                    raise ValueError("conflicting rotation fill order identity") from exc

            def execution_price(order_row: Mapping[str, object]) -> Decimal | None:
                for field in ("dealt_avg_price", "avg_price", "price"):
                    raw_value = order_row.get(field)
                    if raw_value in (None, ""):
                        continue
                    try:
                        value = _required_decimal(raw_value, f"{field} identity")
                    except ValueError as exc:
                        raise ValueError("conflicting rotation fill order identity") from exc
                    # A market order's request price is conventionally zero;
                    # it is not an execution-price identity.
                    if field == "price" and value == 0:
                        continue
                    return value
                return None

            existing_price = execution_price(existing)
            rotation_price = execution_price(order)
            if (
                existing_price is not None
                and rotation_price is not None
                and existing_price != rotation_price
            ):
                raise ValueError("conflicting rotation fill order identity")

            try:
                existing_status = _normalized_rotation_order_status(existing)
                rotation_status = _normalized_rotation_order_status(order)
            except ValueError as exc:
                raise ValueError("conflicting rotation fill order identity") from exc
            if (
                existing_status
                and rotation_status
                and existing_status != rotation_status
                and existing_status
                not in {"SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "ACTIVE"}
            ):
                raise ValueError("conflicting rotation fill order identity")
            execution_status = rotation_status
            execution_fields: dict[str, object] = {
                key: value
                for key, value in order.items()
                if key in {
                    "report_sha256", "pair_key", "pair_index", "account_id",
                    "execution_date", "opening_strategy_version",
                    "opening_strategy_version_source", "closing_strategy_version",
                    "strategy_snapshot", "exit_reason", "order_status", "status",
                    "dealt_qty", "dealt_avg_price", "avg_price",
                }
            }
            # `_completed_trades` gives `status` precedence over
            # `order_status`; mirror the validated full-fill status into both
            # aliases so a stale SUBMITTED row cannot hide the fill.
            if execution_status:
                execution_fields["status"] = execution_status
                execution_fields["order_status"] = execution_status
            merged[index] = {
                **existing,
                **execution_fields,
            }
            continue
        merged.append(order)
        if order_id:
            known_ids[order_id] = len(merged) - 1
    return {**dict(payload), "orders": merged}


def _rotation_fill_orders(
    data_dir: Path, market: str, trading_date: str
) -> list[dict[str, object]]:
    root = (
        data_dir / "trend_review" / "ledgers" / market / "rotations"
        / trading_date
    )
    fills: list[tuple[str, dict[str, object]]] = []
    for pair_root in sorted(root.glob("*")):
        if not pair_root.is_dir():
            continue
        for name, expected_side in (("sell-filled.json", "SELL"), ("buy-filled.json", "BUY")):
            path = pair_root / name
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid relative rotation fill: {path}") from exc
            if isinstance(payload, Mapping):
                try:
                    _validate_rotation_event(payload, path)
                except ValueError as exc:
                    raise ValueError(f"invalid relative rotation fill: {path}") from exc
            order = payload.get("order") if isinstance(payload, Mapping) else None
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema_version") != "open_trader.trend_review.rotation.v1"
                or payload.get("market") != market
                or payload.get("execution_date") != trading_date
                or payload.get("pair_key") != pair_root.name
                or (
                    isinstance(payload.get("account_id"), bool)
                    or not isinstance(payload.get("account_id"), int)
                    or payload.get("account_id") <= 0
                )
                or (
                    isinstance(payload.get("pair_index"), bool)
                    or not isinstance(payload.get("pair_index"), int)
                    or payload.get("pair_index") < 0
                )
                or not isinstance(payload.get("report_sha256"), str)
                or len(str(payload.get("report_sha256"))) != 64
                or payload.get("kind") not in {"sell_fill", "buy_fill"}
                or payload.get("status") != "filled"
                or not isinstance(order, Mapping)
                or str(order.get("side", order.get("trd_side", ""))).upper() != expected_side
                or not str(order.get("order_id") or order.get("orderid") or "").strip()
            ):
                raise ValueError(f"invalid relative rotation fill: {path}")
            try:
                if _required_decimal(order.get("dealt_qty"), "rotation fill quantity") <= 0:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"invalid relative rotation fill: {path}") from exc
            merged_order = dict(order)
            for field in (
                "report_sha256",
                "pair_key",
                "pair_index",
                "account_id",
                "execution_date",
                "opening_strategy_version",
                "opening_strategy_version_source",
                "closing_strategy_version",
                "strategy_snapshot",
                "exit_reason",
            ):
                if field in payload and field not in merged_order:
                    merged_order[field] = payload[field]
            fills.append((str(payload.get("recorded_at") or ""), merged_order))
    return [order for _, order in sorted(fills, key=lambda item: item[0])]


def _load_actual_fills(
    data_dir: Path, market: str
) -> tuple[list[dict[str, object]], list[tuple[str, str]]]:
    fills: list[dict[str, object]] = []
    root = data_dir / "trend_review" / "facts" / "actual_fills" / market
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version")
            != "open_trader.trend_review.fill.v1"
            or payload.get("market") != market
        ):
            raise ValueError(f"invalid trend review actual fill fact: {path}")
        fills.append(payload)
    coverage: list[tuple[str, str]] = []
    source_sequences: dict[tuple[str, str, str], set[int | None]] = {}
    completeness_root = (
        data_dir
        / "trend_review"
        / "facts"
        / "actual_fill_completeness"
        / market
    )
    for path in sorted(completeness_root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version")
            != "open_trader.trend_review.fill_completeness.v1"
            or payload.get("market") != market
        ):
            raise ValueError(f"invalid trend review fill completeness fact: {path}")
        coverage_end = str(
            payload.get("coverage_end") or payload.get("complete_through") or ""
        )
        coverage_start = str(payload.get("coverage_start") or coverage_end)
        try:
            start_date = date.fromisoformat(coverage_start)
            end_date = date.fromisoformat(coverage_end)
        except ValueError:
            raise ValueError(
                f"invalid trend review fill completeness fact: {path}"
            ) from None
        if (
            start_date.isoformat() != coverage_start
            or end_date.isoformat() != coverage_end
            or start_date > end_date
        ):
            raise ValueError(f"invalid trend review fill completeness fact: {path}")
        coverage.append((coverage_start, coverage_end))
        raw_order = payload.get("fill_order", [])
        if not isinstance(raw_order, list):
            raise ValueError(f"invalid trend review fill completeness fact: {path}")
        for item in raw_order:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("identity"), list)
                or len(item["identity"]) != 3
            ):
                raise ValueError(f"invalid trend review fill completeness fact: {path}")
            identity = tuple(str(value) for value in item["identity"])
            sequence = item.get("source_sequence")
            if sequence is not None and (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 0
            ):
                raise ValueError(f"invalid trend review fill completeness fact: {path}")
            source_sequences.setdefault(identity, set()).add(sequence)
    date_only_groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for fill in fills:
        executed_at = str(fill.get("executed_at") or "")
        if len(executed_at) == 10:
            date_only_groups.setdefault(
                (str(fill.get("symbol") or ""), executed_at), []
            ).append(_actual_fill_identity(fill))
    ordered_identities = {
        identity
        for identities in date_only_groups.values()
        if len(identities) > 1
        for identity in identities
    }
    for fill in fills:
        identity = _actual_fill_identity(fill)
        sequences = source_sequences.get(identity, set())
        if len(sequences) > 1 and identity in ordered_identities:
            raise ValueError(f"conflicting actual fill source order: {identity}")
        if len(sequences) == 1:
            sequence = next(iter(sequences))
            if sequence is not None:
                fill["source_sequence"] = sequence
    return fills, coverage


def _completed_cycles(
    fills: Sequence[Mapping[str, object]],
    opening_positions: Sequence[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    positions = {
        str(row["symbol"]): {
            "symbol": str(row["symbol"]),
            "entry_date": str(row["opened_at"])[:10],
            "quantity": _required_decimal(row["quantity"], "opening quantity"),
            "fills": [],
        }
        for row in opening_positions
    }
    completed: list[dict[str, object]] = []
    seen: dict[tuple[str, str, str], bytes] = {}
    unique_fills: list[Mapping[str, object]] = []
    for fill in fills:
        identity = _actual_fill_identity(fill)
        payload = _canonical_json_bytes(dict(fill))
        existing = seen.get(identity)
        if existing is None:
            seen[identity] = payload
            unique_fills.append(fill)
        elif existing != payload:
            raise ValueError(f"conflicting actual fill identity: {identity}")
    date_only_groups: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for fill in unique_fills:
        executed_at = str(fill["executed_at"])
        if len(executed_at) == 10:
            date_only_groups.setdefault(
                (str(fill["symbol"]), executed_at), []
            ).append(fill)
    for grouped in date_only_groups.values():
        if len(grouped) < 2:
            continue
        sequences = [fill.get("source_sequence") for fill in grouped]
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in sequences
        ) or len(set(sequences)) != len(sequences):
            raise ValueError("ambiguous actual fill order")

    def sort_key(row: Mapping[str, object]) -> tuple[float, int, str]:
        text = str(row["executed_at"])
        moment = (
            datetime.fromisoformat(f"{text}T00:00:00+00:00")
            if len(text) == 10
            else datetime.fromisoformat(text.replace("Z", "+00:00"))
        )
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        sequence = row.get("source_sequence")
        return (
            moment.timestamp(),
            sequence if isinstance(sequence, int) else 0,
            str(row["source_id"]),
        )

    for fill in sorted(
        unique_fills,
        key=sort_key,
    ):
        symbol = str(fill["symbol"])
        quantity = _required_decimal(fill["quantity"], "fill quantity")
        if quantity <= 0:
            raise ValueError("fill quantity must be positive")
        current = positions.get(symbol)
        if fill["side"] == "BUY":
            if current is None:
                current = {
                    "symbol": symbol,
                    "entry_date": str(fill["executed_at"])[:10],
                    "quantity": Decimal("0"),
                    "fills": [],
                }
                positions[symbol] = current
            current["quantity"] = (
                _required_decimal(current["quantity"], "open quantity") + quantity
            )
        elif fill["side"] == "SELL":
            if current is None or _required_decimal(
                current["quantity"], "open quantity"
            ) < quantity:
                raise ValueError("sell fill exceeds actual position")
            current["quantity"] = (
                _required_decimal(current["quantity"], "open quantity") - quantity
            )
        else:
            raise ValueError("actual fill side must be BUY or SELL")
        current["fills"].append(dict(fill))
        if current["quantity"] == 0:
            completed.append(
                {**current, "exit_date": str(fill["executed_at"])[:10]}
            )
            del positions[symbol]
    return completed


def _series_cutoff(
    effective_from: str,
    series_dates: set[str],
    benchmark_dates: set[str],
) -> str | None:
    if effective_from not in series_dates or effective_from not in benchmark_dates:
        return None
    cutoff: str | None = None
    for trading_date in sorted(
        day for day in benchmark_dates if day >= effective_from
    ):
        if trading_date not in series_dates:
            break
        cutoff = trading_date
    return cutoff


def _equity_cutoff(
    effective_from: str,
    fact_dates: set[str],
    equity_dates: set[str],
    expected_dates: set[str] | None = None,
) -> str | None:
    expected = fact_dates if expected_dates is None else expected_dates
    if effective_from not in expected or effective_from not in equity_dates:
        return None
    cutoff: str | None = None
    for trading_date in sorted(day for day in expected if day >= effective_from):
        if trading_date not in equity_dates:
            break
        cutoff = trading_date
    return cutoff


def _common_cutoff(
    effective_from: str,
    discipline_dates: set[str],
    actual_dates: set[str],
    benchmark_dates: set[str],
) -> str | None:
    return _series_cutoff(
        effective_from,
        discipline_dates & actual_dates,
        benchmark_dates,
    )


def _completed_trades(facts: list[dict[str, object]]) -> list[dict[str, object]]:
    open_by_symbol: dict[str, dict[str, object]] = {}
    completed: list[dict[str, object]] = []
    for fact in facts:
        raw_orders = fact.get("orders")
        if not isinstance(raw_orders, list):
            raise ValueError("trend review daily orders must be a list")
        for order in raw_orders:
            if not isinstance(order, Mapping):
                raise ValueError("trend review order must be an object")
            status = str(order.get("status") or order.get("order_status") or "").upper()
            if "FILLED" not in status and "DEALT_ALL" not in status:
                continue
            side = str(order.get("side") or order.get("trd_side") or "").upper()
            symbol = str(
                order.get("symbol")
                or str(order.get("code") or order.get("futu_code") or "").split(".")[-1]
            )
            quantity = _required_decimal(
                order.get("dealt_qty", order.get("qty")), "filled quantity"
            )
            if not symbol or quantity <= 0 or side not in {"BUY", "SELL"}:
                raise ValueError("filled trend review order is invalid")
            current = open_by_symbol.get(symbol)
            if side == "BUY":
                if current is None:
                    opening_snapshot = order.get("strategy_snapshot")
                    current = {
                        "symbol": symbol,
                        "entry_date": fact["date"],
                        "quantity": Decimal("0"),
                        "entry_quantity": Decimal("0"),
                        "strategy_snapshot": (
                            opening_snapshot
                            if isinstance(opening_snapshot, Mapping)
                            else fact.get("strategy_snapshot")
                        ),
                        "opening_strategy_version": str(
                            order.get("opening_strategy_version")
                            or fact.get("opening_strategy_version")
                            or (
                                fact.get("strategy_snapshot", {}).get("strategy_version")
                                if isinstance(fact.get("strategy_snapshot"), Mapping)
                                else ""
                            )
                            or ""
                        ),
                        "entry_report_sha256": fact.get("report_sha256"),
                        "orders": [],
                    }
                    open_by_symbol[symbol] = current
                current["quantity"] = _required_decimal(
                    current["quantity"], "open quantity"
                ) + quantity
                current["entry_quantity"] = _required_decimal(
                    current["entry_quantity"], "entry quantity"
                ) + quantity
                if not current.get("opening_strategy_version"):
                    current["opening_strategy_version"] = str(
                        order.get("opening_strategy_version") or ""
                    )
                current["orders"].append(dict(order))
                continue
            if current is None:
                continue
            if _required_decimal(current["quantity"], "open quantity") < quantity:
                raise ValueError("sell fill exceeds experiment position")
            if (
                not current.get("opening_strategy_version")
                or order.get("opening_strategy_version_source")
                in {"frozen_pair", "frozen_holding", "account_position", "protection_state"}
            ):
                current["opening_strategy_version"] = str(
                    order.get("opening_strategy_version") or ""
                )
            if not isinstance(current.get("strategy_snapshot"), Mapping):
                closing_snapshot = order.get("strategy_snapshot")
                if isinstance(closing_snapshot, Mapping):
                    current["strategy_snapshot"] = closing_snapshot
            if order.get("closing_strategy_version"):
                current["closing_strategy_version"] = str(
                    order["closing_strategy_version"]
                )
            current["quantity"] = _required_decimal(
                current["quantity"], "open quantity"
            ) - quantity
            current["orders"].append(dict(order))
            if current["quantity"] == 0:
                current["exit_date"] = fact["date"]
                current["quantity"] = format(
                    _required_decimal(current.pop("entry_quantity"), "entry quantity"),
                    "f",
                )
                completed.append(current)
                del open_by_symbol[symbol]
    return completed


def _normalized_curve(
    facts: list[dict[str, object]], field: str
) -> list[dict[str, str]] | None:
    if any(field not in fact for fact in facts):
        return None
    values = [_required_decimal(fact[field], field) for fact in facts]
    if not values or values[0] <= 0 or any(value <= 0 for value in values):
        raise ValueError(f"{field} must contain positive values")
    return [
        {
            "date": str(fact["date"]),
            "equity": str(value / values[0] * Decimal("100")),
        }
        for fact, value in zip(facts, values)
    ]


def _metric(value: object, reason: str | None = None) -> dict[str, object]:
    return {"value": value, "reason": reason if value is None else None}


def _load_dgs3mo_csv(path: Path) -> dict[date, Decimal]:
    rates: dict[date, Decimal] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            date_field = next(
                (field for field in ("DATE", "observation_date") if field in fields),
                None,
            )
            if date_field is None or "DGS3MO" not in fields:
                raise ValueError(
                    "DGS3MO CSV must contain DATE or observation_date and DGS3MO"
                )
            for row in reader:
                raw_rate = str(row.get("DGS3MO") or "").strip()
                if raw_rate in {"", "."}:
                    continue
                try:
                    observation_date = date.fromisoformat(
                        str(row.get(date_field) or "").strip()
                    )
                    rate = Decimal(raw_rate)
                except (InvalidOperation, ValueError) as exc:
                    raise ValueError(
                        "DGS3MO CSV contains an invalid observation"
                    ) from exc
                if observation_date in rates:
                    raise ValueError("DGS3MO CSV contains a duplicate date")
                if not rate.is_finite() or rate < 0:
                    raise ValueError("DGS3MO rate must be finite and non-negative")
                rates[observation_date] = rate
    except OSError as exc:
        raise ValueError(f"cannot read DGS3MO CSV: {path}") from exc
    if not rates:
        raise ValueError("DGS3MO series has no valid observations")
    return dict(sorted(rates.items()))


def _rate_on_or_before(
    rates: Mapping[date, Decimal], target: date
) -> Decimal:
    ordered_dates = sorted(rates)
    index = bisect_right(ordered_dates, target) - 1
    if index < 0:
        raise ValueError(
            f"DGS3MO has no observation on or before {target.isoformat()}"
        )
    return rates[ordered_dates[index]]


def _annualized_sharpe(excess_returns: Sequence[Decimal]) -> Decimal | None:
    if any(not value.is_finite() for value in excess_returns):
        raise ValueError("Sharpe returns must be finite")
    if len(excess_returns) < 2:
        return None
    mean = sum(excess_returns, Decimal("0")) / Decimal(len(excess_returns))
    variance = sum(
        ((value - mean) ** 2 for value in excess_returns), Decimal("0")
    ) / Decimal(len(excess_returns))
    if variance == 0:
        return None
    return mean / variance.sqrt() * Decimal(252).sqrt()


def _portfolio_metrics(
    curve: Sequence[Mapping[str, str]],
    rates: Mapping[date, Decimal],
    initial_cash: Decimal,
) -> dict[str, object]:
    if not curve:
        return {
            "total_return_pct": "0",
            "max_drawdown_pct": "0",
            "sharpe_ratio": None,
            "calmar_ratio": None,
        }
    if not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("initial cash must be finite and positive")
    equities = [
        _required_decimal(row["equity"], "portfolio equity") for row in curve
    ]
    if any(equity < 0 for equity in equities):
        raise ValueError("portfolio equity must be non-negative")
    dates = [date.fromisoformat(row["date"]) for row in curve]
    total_return = (equities[-1] / initial_cash - Decimal("1")) * Decimal("100")
    elapsed_days = max(1, (dates[-1] - dates[0]).days)
    annualized = (
        (equities[-1] / initial_cash) ** (Decimal("365") / Decimal(elapsed_days))
        - Decimal("1")
    ) * Decimal("100") if equities[-1] > 0 else Decimal("-100")
    peak = equities[0]
    max_drawdown = Decimal("0")
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(
                max_drawdown, (peak - equity) / peak * Decimal("100")
            )
    excess_returns: list[Decimal] = []
    for previous, current, previous_date, current_date in zip(
        equities, equities[1:], dates, dates[1:]
    ):
        if previous <= 0:
            continue
        risk_free = (
            Decimal("1") + _rate_on_or_before(rates, previous_date) / Decimal("100")
        ) ** (
            Decimal((current_date - previous_date).days) / Decimal("365")
        ) - Decimal("1")
        excess_returns.append(current / previous - Decimal("1") - risk_free)
    sharpe = _annualized_sharpe(excess_returns)
    calmar = annualized / max_drawdown if max_drawdown else None
    return {
        "total_return_pct": format(total_return, "f"),
        "max_drawdown_pct": format(max_drawdown, "f"),
        "sharpe_ratio": None if sharpe is None else format(sharpe, "f"),
        "calmar_ratio": None if calmar is None else format(calmar, "f"),
    }


def _curve_metrics(
    curve: list[dict[str, str]] | None,
    rates: Mapping[date, Decimal],
    *,
    missing_reason: str,
) -> dict[str, dict[str, object]]:
    if curve is None:
        return {
            key: _metric(None, missing_reason)
            for key in (
                "total_return_pct",
                "max_drawdown_pct",
                "calmar_ratio",
                "sharpe_ratio",
            )
        }
    values = _portfolio_metrics(curve, rates, Decimal("100"))
    dates = [date.fromisoformat(row["date"]) for row in curve]
    ratio_reason = "最大回撤为零或样本不足"
    sharpe_reason = "收益波动为零或样本不足"
    if (dates[-1] - dates[0]).days < 365:
        values["calmar_ratio"] = None
        values["sharpe_ratio"] = None
        ratio_reason = sharpe_reason = "观察期不足"
    return {
        "total_return_pct": _metric(values["total_return_pct"]),
        "max_drawdown_pct": _metric(values["max_drawdown_pct"]),
        "calmar_ratio": _metric(values["calmar_ratio"], ratio_reason),
        "sharpe_ratio": _metric(values["sharpe_ratio"], sharpe_reason),
    }


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(_canonical_json_bytes(payload))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def build_trend_review_projection(
    data_dir: Path, market: str
) -> dict[str, object]:
    market = _market(market)
    effective_from = TREND_V1_EFFECTIVE_FROM[market]
    daily_root = data_dir / "trend_review" / "daily" / market
    legacy = _load_daily_facts(data_dir, market) if any(daily_root.glob("*.json")) else []
    discipline_by_date = {str(fact["date"]): fact for fact in legacy}
    actual_by_date = {
        str(fact["date"]): {
            "date": fact["date"],
            "actual_equity": fact["actual_equity"],
            "strategy_snapshot": fact.get("strategy_snapshot"),
            "opening_positions": [],
            "new_fact": False,
        }
        for fact in legacy
        if "actual_equity" in fact
    }
    benchmark_by_date: dict[str, dict[str, object]] = {}
    for fact in _load_dated_fact_stream(
        data_dir,
        "discipline",
        market,
        "open_trader.trend_review.discipline.v1",
    ):
        discipline_by_date[str(fact["date"])] = {
            "date": fact["date"],
            "discipline_equity_after_fees": fact["equity_after_fees"],
            "orders": fact["orders"],
            "strategy_snapshot": fact.get("strategy_snapshot"),
        }
    for fact in _load_dated_fact_stream(
        data_dir,
        "actual_equity",
        market,
        "open_trader.trend_review.actual_equity.v1",
    ):
        actual_by_date[str(fact["date"])] = {
            "date": fact["date"],
            "actual_equity": fact["equity"],
            "strategy_snapshot": fact.get("strategy_snapshot"),
            "opening_positions": fact.get("opening_positions", []),
            "new_fact": True,
        }
    for fact in _load_dated_fact_stream(
        data_dir,
        "benchmark",
        market,
        "open_trader.trend_review.benchmark.v1",
    ):
        benchmark = _validate_benchmark(
            fact.get("benchmark"), market=market, trading_date=str(fact["date"])
        )
        benchmark_by_date[str(fact["date"])] = dict(benchmark)
    benchmark_reference_dates = set(benchmark_by_date)
    long_term_snapshot: dict[str, object] | None = None
    try:
        long_term_snapshot = read_long_term_benchmark_snapshot(data_dir, market)
    except ValueError:
        benchmark_by_date = {}
    else:
        closes = long_term_snapshot["daily_closes"]
        assert isinstance(closes, list)
        snapshot_benchmarks = {
            str(close["date"]): {
                "date": close["date"],
                "close": close["close"],
                "source_id": BENCHMARK_SOURCE_IDS[market],
                "futu_symbol": BENCHMARK_FUTU_SYMBOLS[market],
            }
            for close in closes
            if isinstance(close, Mapping)
        }
        benchmark_by_date = {**snapshot_benchmarks, **benchmark_by_date}
    if not discipline_by_date and not actual_by_date and not benchmark_by_date:
        raise ValueError(f"no trend review facts for {market}")

    def normalize_v1_snapshot_or_none(
        fact: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        snapshot = fact.get("strategy_snapshot")
        if not isinstance(snapshot, Mapping) or snapshot.get(
            "strategy_version"
        ) not in TREND_STRATEGY_VERSIONS:
            return None
        try:
            return normalize_trend_strategy_snapshot(snapshot, market)
        except ValueError:
            return None

    discipline_facts = [
        {
            **discipline_by_date[trading_date],
            "strategy_snapshot": normalized,
        }
        for trading_date in sorted(discipline_by_date)
        if (
            normalized := normalize_v1_snapshot_or_none(
                discipline_by_date[trading_date]
            )
        )
        is not None
    ]
    actual_facts = [
        {
            **actual_by_date[trading_date],
            "strategy_snapshot": normalized,
        }
        for trading_date in sorted(actual_by_date)
        if (
            normalized := normalize_v1_snapshot_or_none(
                actual_by_date[trading_date]
            )
        )
        is not None
    ]

    def fact_identity(fact: Mapping[str, object]) -> tuple[str, str, str]:
        snapshot = fact["strategy_snapshot"]
        assert isinstance(snapshot, Mapping)
        return (
            str(snapshot.get("market") or market),
            str(snapshot.get("strategy_id") or ""),
            str(snapshot.get("strategy_version") or ""),
        )

    effective_facts = [
        fact
        for fact in (*discipline_facts, *actual_facts)
        if str(fact["date"]) >= effective_from
    ]
    live_facts = [
        fact
        for fact in effective_facts
        if fact_identity(fact)[2] in {
            "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13",
        }
    ]
    target_candidates = live_facts or effective_facts
    if target_candidates:
        if live_facts:
            target_fact = max(target_candidates, key=lambda fact: str(fact["date"]))
        else:
            # v1-v3 have no inheritance map. Prefer the newest legacy rule
            # version when a late malformed/old fact is mixed into the stream.
            target_fact = max(
                target_candidates,
                key=lambda fact: (
                    int(fact_identity(fact)[2][1:]),
                    str(fact["date"]),
                ),
            )
    else:
        target_candidates = [*discipline_facts, *actual_facts]
        target_fact = (
            max(target_candidates, key=lambda fact: str(fact["date"]))
            if target_candidates
            else None
        )
    target_identity = fact_identity(target_fact) if target_candidates else None

    def is_target_identity(fact: Mapping[str, object]) -> bool:
        if target_identity is None or str(fact["date"]) < effective_from:
            return True
        return trend_kelly_identity_matches(fact_identity(fact), target_identity)

    discipline_facts = [fact for fact in discipline_facts if is_target_identity(fact)]
    actual_facts = [fact for fact in actual_facts if is_target_identity(fact)]
    identity_snapshots: dict[tuple[str, str, str], set[bytes]] = {}
    for fact in (*discipline_facts, *actual_facts):
        if str(fact["date"]) < effective_from:
            continue
        identity = fact_identity(fact)
        identity_snapshots.setdefault(identity, set()).add(
            _strategy_identity(fact["strategy_snapshot"])
        )
    if any(len(snapshots) > 1 for snapshots in identity_snapshots.values()):
        raise ValueError("strategy snapshot identity changed within version interval")
    snapshot_facts = sorted(
        (
            fact
            for fact in (*discipline_facts, *actual_facts)
            if effective_from <= str(fact["date"])
        ),
        key=lambda fact: str(fact["date"]),
    )
    snapshot = (
        dict(snapshot_facts[-1]["strategy_snapshot"])
        if snapshot_facts
        else next(
            (
                dict(fact["strategy_snapshot"])
                for fact in sorted(
                    (
                        fact
                        for fact in (*discipline_facts, *actual_facts)
                        if str(fact["date"]) < effective_from
                    ),
                    key=lambda fact: str(fact["date"]),
                    reverse=True,
                )
            ),
            {},
        )
    )

    discipline_disposition: dict[str, object] | None = None
    actual_disposition: dict[str, object] | None = None
    if target_identity is not None:
        try:
            # Local import avoids the existing trend_api_stats -> trend_review cycle.
            from .trend_api_stats import (
                read_trend_api_stats_snapshot,
                trend_statistics_disposition,
            )

            stats_payload, _ = read_trend_api_stats_snapshot(data_dir)
        except ValueError:
            pass
        else:
            disposition_args = {
                "market": market,
                "strategy_id": target_identity[1],
                "opening_strategy_version": target_identity[2],
            }
            discipline_disposition = trend_statistics_disposition(
                stats_payload, source="simulation", **disposition_args
            )
            actual_disposition = trend_statistics_disposition(
                stats_payload, source="actual", **disposition_args
            )

    rates_path = data_dir / "rates" / "DGS3MO.csv"
    rates = _load_dgs3mo_csv(rates_path)
    benchmark_dates = set(benchmark_by_date)
    discipline_dates = {
        trading_date
        for trading_date, fact in discipline_by_date.items()
        if "discipline_equity_after_fees" in fact
    }
    discipline_metric_cutoff = _equity_cutoff(
        effective_from,
        set(discipline_by_date),
        discipline_dates,
        benchmark_reference_dates or set(discipline_by_date),
    )
    actual_metric_cutoff = None
    if market == "US":
        _, actual_fill_coverage = _load_actual_fills(data_dir, market)
        actual_dates = {
            trading_date
            for trading_date, fact in actual_by_date.items()
            if "actual_equity" in fact
        }
        equity_cutoff = _equity_cutoff(
            effective_from,
            set(actual_by_date),
            actual_dates,
            benchmark_reference_dates or set(actual_by_date),
        )
        if equity_cutoff is not None:
            for trading_date in sorted(
                day
                for day in actual_dates
                if effective_from <= day <= equity_cutoff
            ):
                if not any(
                    start <= trading_date <= end
                    for start, end in actual_fill_coverage
                ):
                    break
                actual_metric_cutoff = trading_date
    common_cutoff = (
        min(discipline_metric_cutoff, actual_metric_cutoff)
        if discipline_metric_cutoff is not None and actual_metric_cutoff is not None
        else None
    )

    discipline_curve_dates = [
        trading_date
        for trading_date in sorted(discipline_by_date)
        if discipline_metric_cutoff is not None
        and effective_from <= trading_date <= discipline_metric_cutoff
    ]
    discipline_curve = _normalized_curve(
        [discipline_by_date[trading_date] for trading_date in discipline_curve_dates],
        "discipline_equity_after_fees",
    ) if discipline_curve_dates else None
    actual_curve_dates = [
        trading_date
        for trading_date in sorted(actual_by_date)
        if actual_metric_cutoff is not None
        and effective_from <= trading_date <= actual_metric_cutoff
    ]
    actual_curve = _normalized_curve(
        [actual_by_date[trading_date] for trading_date in actual_curve_dates],
        "actual_equity",
    ) if actual_curve_dates else None
    discipline_benchmark_curve = _normalized_curve(
        [
            {
                "date": trading_date,
                "benchmark_equity": benchmark_by_date[trading_date]["close"],
            }
            for trading_date in discipline_curve_dates
            if trading_date in benchmark_by_date
        ],
        "benchmark_equity",
    ) if discipline_curve_dates and all(
        trading_date in benchmark_by_date for trading_date in discipline_curve_dates
    ) else None
    actual_benchmark_curve = _normalized_curve(
        [
            {
                "date": trading_date,
                "benchmark_equity": benchmark_by_date[trading_date]["close"],
            }
            for trading_date in actual_curve_dates
            if trading_date in benchmark_by_date
        ],
        "benchmark_equity",
    ) if actual_curve_dates and all(
        trading_date in benchmark_by_date for trading_date in actual_curve_dates
    ) else None
    discipline_metrics = _curve_metrics(
        discipline_curve, rates, missing_reason="纪律模拟日终净值缺失"
    )
    actual_metrics = _curve_metrics(
        actual_curve, rates, missing_reason="实际执行日终净值缺失"
    )
    discipline_benchmark_metrics = _curve_metrics(
        discipline_benchmark_curve, rates, missing_reason="长期市场基准缺失"
    )
    actual_benchmark_metrics = _curve_metrics(
        actual_benchmark_curve, rates, missing_reason="长期市场基准缺失"
    )

    def market_metrics(label: str) -> dict[str, dict[str, object]]:
        if long_term_snapshot is None:
            return _curve_metrics(None, rates, missing_reason="长期市场基准缺失")
        windows = long_term_snapshot["windows"]
        assert isinstance(windows, Mapping)
        window = windows[label]
        assert isinstance(window, Mapping)
        source = window["metrics"]
        assert isinstance(source, Mapping)
        return {
            "total_return_pct": _metric(
                source[
                    "annualized_return_pct" if label == "5Y" else "total_return_pct"
                ]
            ),
            "max_drawdown_pct": _metric(source["max_drawdown_pct"]),
            "calmar_ratio": _metric(
                source["calmar_ratio"], "最大回撤为零或样本不足"
            ),
            "sharpe_ratio": _metric(
                source["sharpe_ratio"], "收益波动为零或样本不足"
            ),
        }

    market_1y_metrics = market_metrics("1Y")
    market_5y_metrics = market_metrics("5Y")

    def values(metric_name: str) -> dict[str, dict[str, object]]:
        return {
            "discipline": discipline_metrics[metric_name],
            "actual": actual_metrics[metric_name],
            "same_period_benchmark": discipline_benchmark_metrics[metric_name],
            "market_1y": market_1y_metrics[metric_name],
            "market_5y": market_5y_metrics[metric_name],
        }

    def excess(
        item: dict[str, object], benchmark_item: dict[str, object]
    ) -> dict[str, object]:
        if item["value"] is None:
            return item
        if benchmark_item["value"] is None:
            return benchmark_item
        return _metric(
            str(
                _required_decimal(item["value"], "return")
                - _required_decimal(benchmark_item["value"], "benchmark return")
            )
        )

    metrics = {
        "period_net_return": values("total_return_pct"),
        "market_excess_return": {
            "discipline": excess(
                discipline_metrics["total_return_pct"],
                discipline_benchmark_metrics["total_return_pct"],
            ),
            "actual": excess(
                actual_metrics["total_return_pct"],
                actual_benchmark_metrics["total_return_pct"],
            ),
            "same_period_benchmark": _metric(None, "基准自身"),
            "market_1y": _metric(None, "基准自身"),
            "market_5y": _metric(None, "基准自身"),
        },
        "max_drawdown": values("max_drawdown_pct"),
        "calmar": values("calmar_ratio"),
        "sharpe": values("sharpe_ratio"),
    }
    sample_counts: dict[str, int | None] = {
        "discipline": (
            int(discipline_disposition["eligible_sample_count"])
            if discipline_disposition is not None
            and discipline_disposition["available"] is True
            else None
        ),
        "actual": (
            int(actual_disposition["eligible_sample_count"])
            if actual_disposition is not None
            and actual_disposition["available"] is True
            else None
        ),
        "required": 30,
    }

    identity = BENCHMARK_IDENTITIES[market]
    if long_term_snapshot is None:
        benchmark_context = {
            **identity,
            "same_period_dates": [],
            "windows": {"1Y": None, "5Y": None},
        }
        benchmark_refresh = {"status": "unavailable", "reason": "长期市场基准缺失"}
    else:
        windows = long_term_snapshot["windows"]
        assert isinstance(windows, Mapping)
        benchmark_context = {
            **identity,
            "same_period_dates": (
                discipline_curve_dates if discipline_benchmark_curve is not None else []
            ),
            "windows": {
                label: {
                    "start": windows[label]["start"],
                    "cutoff": windows[label]["cutoff"],
                    "observation_count": windows[label]["observation_count"],
                    "return_basis": "CAGR" if label == "5Y" else "period_return",
                }
                for label in ("1Y", "5Y")
            },
        }
        benchmark_refresh = {
            "status": "available",
            "month": long_term_snapshot["month"],
            "completed_at": long_term_snapshot["completed_at"],
            "process_git_sha": long_term_snapshot["process_git_sha"],
            "cutoff": long_term_snapshot["cutoff"],
            "refresh": long_term_snapshot["refresh"],
        }

    projection = {
        "schema_version": "open_trader.trend_review.projection.v4",
        "available": True,
        "market": market,
        "market_label": {"CN": "A 股", "US": "美股", "HK": "港股"}[market],
        "broker": {"CN": "eastmoney", "US": "tiger", "HK": "phillips"}[market],
        "strategy_snapshot": snapshot,
        "sample_counts": sample_counts,
        "sample_details": {
            "discipline": discipline_disposition,
            "actual": actual_disposition,
        },
        "sample_cutoffs": {
            "discipline": (
                discipline_disposition["statistics_cutoff_at"]
                if discipline_disposition is not None
                and discipline_disposition["statistics_cutoff_at"]
                else None
            ),
            "actual": (
                actual_disposition["statistics_cutoff_at"]
                if actual_disposition is not None
                and actual_disposition["statistics_cutoff_at"]
                else None
            ),
        },
        "metric_cutoffs": {
            "discipline": discipline_metric_cutoff,
            "actual": actual_metric_cutoff,
        },
        "common_cutoff": common_cutoff,
        "interval": {"start": effective_from, "end": common_cutoff},
        "metrics": metrics,
        "benchmark_context": benchmark_context,
        "benchmark_refresh": benchmark_refresh,
    }
    _write_json_atomic(
        data_dir / "latest" / f"trend_review_{market.lower()}.json",
        projection,
    )
    return projection


def rebuild_trend_report_from_evidence(
    evidence: Mapping[str, object],
) -> dict[str, object]:
    inputs = evidence.get("rebuild_inputs")
    if not isinstance(inputs, Mapping):
        raise TrendReplayIncompleteError("missing original input: rebuild_inputs")
    snapshot = evidence.get("strategy_snapshot")
    if not isinstance(snapshot, Mapping):
        raise TrendReplayIncompleteError(
            "missing original input: strategy_snapshot"
        )
    strategy_version = str(snapshot.get("strategy_version") or "")
    required = {
        "as_of_date",
        "execution_date",
        "account",
        "candidates",
        "holding_snapshots",
        "bars_by_symbol",
        "prior_state",
        "watch_events",
        "market",
        "candidate_pool_ids",
        "metadata",
        "price_fx_to_account_currency",
    }
    if strategy_version in {
        "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13",
    }:
        required.add("normal_cost_rate")
    if strategy_version in {
        "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13",
    }:
        required.update({"kelly_rounds", "kelly_data_reason"})
    if strategy_version in {
        "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13",
    }:
        required.add("drawdown_summary")
    missing = sorted(required - inputs.keys())
    if missing:
        raise TrendReplayIncompleteError(
            f"missing original input: {missing[0]}"
        )
    from .a_share_trend import (
        AccountPosition,
        AccountSnapshot,
        CandidateInput,
        HoldingSnapshot,
        RealHoldingInput,
        RotationPair,
        RotationComparison,
        _finalize_market_report,
        _report_payload,
        build_report,
    )
    from .kline_technical_facts import DailyKlineBar
    from .trend_kelly import (
        TREND_API_STATS_SCHEMA_VERSION,
        trend_kelly_rounds_from_payload,
    )
    from .trend_industry_context import IndustryContext

    def decimal_or_none(value: object) -> Decimal | None:
        return None if value is None or value == "" else Decimal(str(value))

    def industry_context_from_raw(raw: object) -> IndustryContext:
        if not isinstance(raw, Mapping):
            raise TrendReplayIncompleteError(
                "invalid original input: industry_contexts"
            )
        try:
            raw_integer_fields = {
                field: raw[field]
                for field in (
                    "industry_tm_id",
                    "component_count",
                    "snapshot_count",
                    "tradable_count",
                    "valid_count",
                    "right_count",
                    "warm_to_hot_count",
                )
            }
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in raw_integer_fields.values()
            ):
                raise ValueError
            integer_fields = {
                field: int(value) for field, value in raw_integer_fields.items()
            }
            member_breadth_collected = raw.get("member_breadth_collected", True)
            if type(member_breadth_collected) is not bool:
                raise ValueError
            context = IndustryContext(
                **integer_fields,
                industry=str(raw["industry"]),
                as_of_date=str(raw["as_of_date"]),
                snapshot_coverage=Decimal(str(raw["snapshot_coverage"])),
                right_state_coverage=Decimal(str(raw["right_state_coverage"])),
                right_share=decimal_or_none(raw.get("right_share")),
                temperature=(
                    None
                    if raw.get("temperature") is None
                    else str(raw["temperature"])
                ),
                strength=decimal_or_none(raw.get("strength")),
                valid=raw["valid"] is True,
                invalid_reasons=tuple(str(item) for item in raw["invalid_reasons"]),
                member_breadth_collected=member_breadth_collected,
                prior_as_of_date=(
                    None
                    if raw.get("prior_as_of_date") is None
                    else str(raw["prior_as_of_date"])
                ),
                prior_temperature=(
                    None
                    if raw.get("prior_temperature") is None
                    else str(raw["prior_temperature"])
                ),
                prior_right_share=decimal_or_none(raw.get("prior_right_share")),
                aggregate_right_count_ratio=decimal_or_none(
                    raw.get("aggregate_right_count_ratio")
                ),
                aggregate_right_market_cap_ratio=decimal_or_none(
                    raw.get("aggregate_right_market_cap_ratio")
                ),
                prior_aggregate_right_count_ratio=decimal_or_none(
                    raw.get("prior_aggregate_right_count_ratio")
                ),
                prior_aggregate_right_market_cap_ratio=decimal_or_none(
                    raw.get("prior_aggregate_right_market_cap_ratio")
                ),
                temperature_direction=(
                    None
                    if raw.get("temperature_direction") is None
                    else str(raw["temperature_direction"])
                ),
                right_share_change_pp=decimal_or_none(
                    raw.get("right_share_change_pp")
                ),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise TrendReplayIncompleteError(
                "invalid original input: industry_contexts"
            ) from None
        if not isinstance(raw.get("valid"), bool) or not isinstance(
            raw.get("invalid_reasons"), list
        ):
            raise TrendReplayIncompleteError(
                "invalid original input: industry_contexts"
            )
        return context

    account_raw = inputs["account"]
    if not isinstance(account_raw, Mapping):
        raise TrendReplayIncompleteError("missing original input: account")
    if "position_count" not in account_raw:
        raise TrendReplayIncompleteError(
            "missing original input: account.position_count"
        )
    positions_raw = account_raw.get("positions")
    if not isinstance(positions_raw, list):
        raise TrendReplayIncompleteError("missing original input: account.positions")
    positions = tuple(
        AccountPosition(
            symbol=str(item["symbol"]),
            name=str(item["name"]),
            asset_class=str(item["asset_class"]),
            quantity=Decimal(str(item["quantity"])),
            avg_cost_price=decimal_or_none(item.get("avg_cost_price")),
            market_value=Decimal(str(item.get("market_value", "0"))),
            futu_symbol=(
                str(item["futu_symbol"])
                if isinstance(item.get("futu_symbol"), str)
                and item.get("futu_symbol")
                else None
            ),
        )
        for item in positions_raw
        if isinstance(item, Mapping)
    )
    position_count_raw = account_raw["position_count"]
    if (
        position_count_raw is not None
        and (
            isinstance(position_count_raw, bool)
            or not isinstance(position_count_raw, int)
            or position_count_raw < 0
        )
    ):
        raise TrendReplayIncompleteError(
            "invalid original input: account.position_count"
        )
    account = AccountSnapshot(
        source_date=str(account_raw["source_date"]),
        fresh=account_raw.get("fresh") is True,
        net_value=Decimal(str(account_raw["net_value"])),
        available_cash=Decimal(str(account_raw["available_cash"])),
        positions=positions,
        exceptions=tuple(str(item) for item in account_raw.get("exceptions", [])),
        position_count=position_count_raw,
    )

    decimal_fields = {
        "amount",
        "strength",
        "close",
        "atr",
        "filter_price",
        "market_cap",
        "global_strength",
        "strength_prev_week",
        "strength_prev_month",
    }
    candidates_raw = inputs["candidates"]
    if not isinstance(candidates_raw, list):
        raise TrendReplayIncompleteError("missing original input: candidates")
    candidates = []
    for raw in candidates_raw:
        if not isinstance(raw, Mapping):
            raise TrendReplayIncompleteError("missing original input: candidates")
        values = dict(raw)
        for field in decimal_fields:
            values[field] = decimal_or_none(values.get(field))
        values["pools"] = tuple(values.get("pools") or ())
        candidates.append(CandidateInput(**values))

    holdings_raw = inputs["holding_snapshots"]
    if not isinstance(holdings_raw, Mapping):
        raise TrendReplayIncompleteError(
            "missing original input: holding_snapshots"
        )
    holding_snapshots: dict[str, HoldingSnapshot | None] = {}
    for symbol, raw in holdings_raw.items():
        if raw is None:
            holding_snapshots[str(symbol)] = None
            continue
        if not isinstance(raw, Mapping):
            raise TrendReplayIncompleteError(
                "missing original input: holding_snapshots"
            )
        values = dict(raw)
        for field in (
            "filter_price", "market_cap", "strength", "global_strength",
            "strength_prev_week", "strength_prev_month",
        ):
            values[field] = decimal_or_none(values.get(field))
        holding_snapshots[str(symbol)] = HoldingSnapshot(**values)

    bars_raw = inputs["bars_by_symbol"]
    if not isinstance(bars_raw, Mapping):
        raise TrendReplayIncompleteError("missing original input: bars_by_symbol")
    bars_by_symbol = {
        str(symbol): (
            None
            if rows is None
            else tuple(DailyKlineBar(**dict(row)) for row in rows)
        )
        for symbol, rows in bars_raw.items()
        if rows is None or isinstance(rows, list)
    }
    real_holdings_input = None
    real_raw = inputs.get("real_holdings")
    if real_raw is not None:
        if not isinstance(real_raw, Mapping):
            raise TrendReplayIncompleteError(
                "invalid original input: real_holdings"
            )
        real_status = real_raw.get("status")
        real_reason = real_raw.get("reason", "")
        real_source = real_raw.get("source", {})
        real_excluded = real_raw.get("trend_excluded_symbols", [])
        if (
            real_status not in {"available", "unavailable"}
            or not isinstance(real_reason, str)
            or not isinstance(real_source, Mapping)
            or not all(isinstance(key, str) and isinstance(value, str)
                       for key, value in real_source.items())
            or not isinstance(real_excluded, list)
            or not all(
                isinstance(symbol, str) and symbol
                for symbol in real_excluded
            )
        ):
            raise TrendReplayIncompleteError(
                "invalid original input: real_holdings"
            )
        real_positions: list[AccountPosition] = []
        real_snapshots: dict[str, HoldingSnapshot | None] = {}
        real_bars: dict[str, tuple[DailyKlineBar, ...] | None] = {}
        real_prior_state = real_raw.get("prior_state")
        if real_status == "available":
            raw_positions = real_raw.get("positions")
            raw_snapshots = real_raw.get("holding_snapshots")
            raw_bars = real_raw.get("bars_by_symbol")
            if (
                not isinstance(raw_positions, list)
                or not isinstance(raw_snapshots, Mapping)
                or not isinstance(raw_bars, Mapping)
            ):
                raise TrendReplayIncompleteError(
                    "invalid original input: real_holdings"
                )
            try:
                for raw_position in raw_positions:
                    if not isinstance(raw_position, Mapping):
                        raise ValueError
                    real_positions.append(
                        AccountPosition(
                            symbol=str(raw_position["symbol"]),
                            name=str(raw_position["name"]),
                            asset_class=str(raw_position["asset_class"]),
                            quantity=Decimal(str(raw_position["quantity"])),
                            avg_cost_price=decimal_or_none(
                                raw_position.get("avg_cost_price")
                            ),
                            market_value=Decimal(
                                str(raw_position.get("market_value", "0"))
                            ),
                            futu_symbol=(
                                str(raw_position["futu_symbol"])
                                if isinstance(raw_position.get("futu_symbol"), str)
                                and raw_position.get("futu_symbol")
                                else None
                            ),
                        )
                    )
                for symbol, raw_snapshot in raw_snapshots.items():
                    if raw_snapshot is None:
                        real_snapshots[str(symbol)] = None
                        continue
                    if not isinstance(raw_snapshot, Mapping):
                        raise ValueError
                    snapshot_values = dict(raw_snapshot)
                    for field in (
                        "filter_price", "market_cap", "strength", "global_strength",
                        "strength_prev_week", "strength_prev_month",
                    ):
                        snapshot_values[field] = decimal_or_none(
                            snapshot_values.get(field)
                        )
                    real_snapshots[str(symbol)] = HoldingSnapshot(
                        **snapshot_values
                    )
                for symbol, raw_rows in raw_bars.items():
                    real_bars[str(symbol)] = (
                        None
                        if raw_rows is None
                        else tuple(
                            DailyKlineBar(**dict(row)) for row in raw_rows
                        )
                    )
            except (KeyError, TypeError, ValueError, InvalidOperation):
                raise TrendReplayIncompleteError(
                    "invalid original input: real_holdings"
                ) from None
            if real_prior_state is not None and not isinstance(
                real_prior_state, Mapping
            ):
                raise TrendReplayIncompleteError(
                    "invalid original input: real_holdings.prior_state"
                )
        else:
            real_prior_state = None
        instrument_ids = real_raw.get("instrument_ids_by_symbol") or {}
        blocked_instruments = real_raw.get("blocked_instrument_ids") or {}
        if not isinstance(instrument_ids, Mapping) or not isinstance(
            blocked_instruments, Mapping
        ):
            raise TrendReplayIncompleteError(
                "invalid original input: real_holdings"
            )
        real_holdings_input = RealHoldingInput(
            status=str(real_status),
            reason=real_reason,
            source={str(key): str(value) for key, value in real_source.items()},
            positions=tuple(real_positions),
            holding_snapshots=real_snapshots,
            bars_by_symbol=real_bars,
            prior_state=real_prior_state,
            trend_excluded_symbols=tuple(real_excluded),
            net_value=decimal_or_none(real_raw.get("net_value")) or Decimal("0"),
            available_cash=(
                decimal_or_none(real_raw.get("available_cash")) or Decimal("0")
            ),
            position_count=(
                int(real_raw["position_count"])
                if isinstance(real_raw.get("position_count"), int)
                and not isinstance(real_raw.get("position_count"), bool)
                else None
            ),
            instrument_ids_by_symbol={
                str(key): str(value)
                for key, value in instrument_ids.items()
            },
            blocked_instrument_ids={
                str(key): str(value)
                for key, value in blocked_instruments.items()
            },
        )
    process_version = str(evidence.get("process_version") or "")
    normalize_trend_strategy_snapshot(snapshot, str(inputs["market"]))
    replay_snapshot = {
        **dict(snapshot),
        "process_version": process_version,
    }
    price_fx = decimal_or_none(inputs["price_fx_to_account_currency"])
    if price_fx is None or not price_fx.is_finite() or price_fx <= 0:
        raise TrendReplayIncompleteError(
            "invalid original input: price_fx_to_account_currency"
        )
    normal_cost_rate = decimal_or_none(inputs.get("normal_cost_rate"))
    if strategy_version in {
        "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13",
    } and (
        normal_cost_rate is None
        or not normal_cost_rate.is_finite()
        or normal_cost_rate < 0
    ):
        raise TrendReplayIncompleteError("invalid original input: normal_cost_rate")
    kelly_rounds_raw = inputs.get("kelly_rounds", [])
    if not isinstance(kelly_rounds_raw, list):
        raise TrendReplayIncompleteError("invalid original input: kelly_rounds")
    try:
        kelly_rounds = trend_kelly_rounds_from_payload(
            {
                "schema_version": TREND_API_STATS_SCHEMA_VERSION,
                "rounds": kelly_rounds_raw,
            }
        )
    except ValueError:
        raise TrendReplayIncompleteError(
            "invalid original input: kelly_rounds"
        ) from None
    if len(kelly_rounds) != len(kelly_rounds_raw):
        raise TrendReplayIncompleteError("invalid original input: kelly_rounds")
    kelly_data_reason = inputs.get("kelly_data_reason", "")
    if not isinstance(kelly_data_reason, str):
        raise TrendReplayIncompleteError(
            "invalid original input: kelly_data_reason"
        )
    contexts_raw = inputs.get("industry_contexts", [])
    if not isinstance(contexts_raw, list):
        raise TrendReplayIncompleteError("invalid original input: industry_contexts")
    industry_contexts = tuple(industry_context_from_raw(item) for item in contexts_raw)
    status_raw = inputs.get("industry_context_status")
    if status_raw is not None and not isinstance(status_raw, Mapping):
        raise TrendReplayIncompleteError(
            "invalid original input: industry_context_status"
        )
    estimated_api_cost_complete = inputs.get("estimated_api_cost_complete", True)
    if not isinstance(estimated_api_cost_complete, bool):
        raise TrendReplayIncompleteError(
            "invalid original input: estimated_api_cost_complete"
        )
    allocation_reference = None
    if "allocation" in inputs:
        allocation_input = inputs["allocation"]
        try:
            if not isinstance(allocation_input, Mapping):
                raise ValueError
            frozen_reference = allocation_input.get("reference")
            daily_json = allocation_input.get("daily_json")
            if not isinstance(frozen_reference, Mapping) or not isinstance(daily_json, str):
                raise ValueError
            snapshot = json.loads(daily_json)
            if not isinstance(snapshot, Mapping):
                raise ValueError
            if hashlib.sha256(daily_json.encode("utf-8")).hexdigest() != frozen_reference.get("sha256"):
                raise ValueError
            from .a_share_trend import freeze_allocation_reference

            allocation_reference = {
                "daily_path": frozen_reference.get("daily_path"),
                "sha256": frozen_reference.get("sha256"),
                "snapshot": snapshot,
                "reused": frozen_reference.get("reused"),
                "stale_a_trading_days": frozen_reference.get("stale_a_trading_days"),
                "failure_reason": frozen_reference.get("failure_reason"),
            }
            if freeze_allocation_reference(allocation_reference) != dict(frozen_reference):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise TrendReplayIncompleteError(
                "invalid original input: allocation"
            ) from None
    historical_account_input = "account_input" not in inputs
    raw_account_input = inputs.get("account_input")
    if not historical_account_input and not isinstance(raw_account_input, Mapping):
        raise TrendReplayIncompleteError(
            "invalid original input: account_input"
        )
    metadata_input = inputs["metadata"]
    if not isinstance(metadata_input, Mapping):
        raise TrendReplayIncompleteError("invalid original input: metadata")
    critical_data_reason = metadata_input.get("industry_data_reason", "")
    if not isinstance(critical_data_reason, str):
        raise TrendReplayIncompleteError(
            "invalid original input: metadata.industry_data_reason"
        )
    report = build_report(
        as_of_date=str(inputs["as_of_date"]),
        execution_date=str(inputs["execution_date"]),
        account=account,
        candidates=candidates,
        holding_snapshots=holding_snapshots,
        bars_by_symbol=bars_by_symbol,
        prior_state=inputs["prior_state"]
        if isinstance(inputs["prior_state"], Mapping)
        else None,
        watch_events=inputs["watch_events"]
        if isinstance(inputs["watch_events"], list)
        else (),
        api_facts=tuple(str(item) for item in inputs.get("api_facts", [])),
        data_sources=tuple(str(item) for item in inputs.get("data_sources", [])),
        estimated_api_cost=decimal_or_none(inputs.get("estimated_api_cost")),
        actual_api_cost=decimal_or_none(inputs.get("actual_api_cost")),
        generated_at=str(inputs.get("generated_at") or "") or None,
        metadata={
            **dict(metadata_input),
            "process_version": process_version,
        },
        market=str(inputs["market"]),
        lot_sizes={
            str(key): int(value)
            for key, value in dict(inputs.get("lot_sizes") or {}).items()
        },
        position_weight=Decimal(str(inputs.get("position_weight") or "0.04")),
        position_weight_source=str(
            inputs.get("position_weight_source") or "fallback_4pct"
        ),
        price_fx_to_account_currency=price_fx,
        normal_cost_rate=normal_cost_rate or Decimal("0"),
        process_version=process_version,
        candidate_pool_ids=tuple(int(item) for item in inputs["candidate_pool_ids"]),
        strategy_snapshot=replay_snapshot,
        kelly_rounds=kelly_rounds,
        kelly_data_reason=kelly_data_reason,
        critical_data_reason=critical_data_reason,
        drawdown_summary=(
            inputs["drawdown_summary"]
            if strategy_version in {
                "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13",
            }
            and isinstance(inputs.get("drawdown_summary"), Mapping)
            else None
        ),
        industry_contexts=industry_contexts,
        industry_context_status=(
            dict(status_raw) if isinstance(status_raw, Mapping) else None
        ),
        estimated_api_cost_complete=estimated_api_cost_complete,
        real_holdings=real_holdings_input,
        allocation_reference=allocation_reference,
        account_input=(
            dict(raw_account_input)
            if isinstance(raw_account_input, Mapping)
            else None
        ),
        _allow_historical_account_input=historical_account_input,
    )
    market = str(inputs["market"]).upper()
    if market in {"US", "HK"}:
        managed_symbols = inputs.get("managed_symbols")
        if not isinstance(managed_symbols, list) or not all(
            isinstance(symbol, str) for symbol in managed_symbols
        ):
            raise TrendReplayIncompleteError(
                "missing original input: managed_symbols"
            )
        report = _finalize_market_report(
            report, managed_symbols=managed_symbols
        )
    decimal_pair_fields = {
        "sell_global_strength",
        "sell_local_strength",
        "buy_global_strength",
        "buy_local_strength",
        "sell_compared_strength",
        "buy_compared_strength",
        "strength_gap",
        "target_weight",
        "target_amount",
        "atr",
        "threshold",
    }

    def frozen_pairs(field: str) -> tuple[RotationPair, ...]:
        raw_pairs = inputs.get(field, [])
        if not isinstance(raw_pairs, list):
            raise TrendReplayIncompleteError(f"invalid original input: {field}")
        result: list[RotationPair] = []
        pair_indices: set[int] = set()
        used_symbols: set[str] = set()
        for raw_pair in raw_pairs:
            if not isinstance(raw_pair, Mapping):
                raise TrendReplayIncompleteError(f"invalid original input: {field}")
            values = dict(raw_pair)
            pair_index = values.get("pair_index")
            if (
                isinstance(pair_index, bool)
                or pair_index not in {0, 1}
                or pair_index in pair_indices
                or not _valid_rotation_pair(
                values, pair_index
                )
            ):
                raise TrendReplayIncompleteError(f"invalid original input: {field}")
            pair_symbols = {
                str(values["sell_symbol"]), str(values["buy_symbol"])
            }
            if used_symbols & pair_symbols:
                raise TrendReplayIncompleteError(f"invalid original input: {field}")
            pair_indices.add(pair_index)
            used_symbols.update(pair_symbols)
            result.append(
                RotationPair(
                    **{
                        key: Decimal(str(value))
                        if key in decimal_pair_fields and value is not None
                        else value
                        for key, value in values.items()
                    }
                )
            )
        return tuple(result)

    decimal_comparison_fields = {
        "sell_local_strength", "sell_global_strength",
        "buy_local_strength", "buy_global_strength",
        "sell_compared_strength", "buy_compared_strength",
        "strength_gap", "threshold",
    }

    def frozen_comparisons(field: str) -> tuple[RotationComparison, ...]:
        raw_comparisons = inputs.get(field, [])
        if not isinstance(raw_comparisons, list):
            raise TrendReplayIncompleteError(f"invalid original input: {field}")
        result: list[RotationComparison] = []
        for raw_comparison in raw_comparisons:
            if not isinstance(raw_comparison, Mapping):
                raise TrendReplayIncompleteError(f"invalid original input: {field}")
            result.append(
                RotationComparison(
                    **{
                        key: (
                            Decimal(str(value))
                            if key in decimal_comparison_fields and value is not None
                            else value
                        )
                        for key, value in raw_comparison.items()
                    }
                )
            )
        return tuple(result)

    if "simulate_rotation_pairs" in inputs or "real_rotation_pairs" in inputs:
        report = replace(
            report,
            simulate_rotation_pairs=frozen_pairs("simulate_rotation_pairs"),
            real_rotation_pairs=frozen_pairs("real_rotation_pairs"),
            simulate_rotation_comparisons=frozen_comparisons(
                "simulate_rotation_comparisons"
            ),
            real_rotation_comparisons=frozen_comparisons(
                "real_rotation_comparisons"
            ),
        )
    payload = _report_payload(
        report,
        _allow_historical_account_input=historical_account_input,
    )
    if market in {"US", "HK"}:
        attention_input = inputs.get("option_attention")
        if not isinstance(attention_input, Mapping):
            raise TrendReplayIncompleteError(
                "missing original input: option_attention"
            )
        previous_rows = attention_input.get("previous_rows")
        broker_label = attention_input.get("broker_label")
        if not isinstance(previous_rows, list) or not all(
            isinstance(row, Mapping) for row in previous_rows
        ):
            raise TrendReplayIncompleteError(
                "missing original input: option_attention.previous_rows"
            )
        if not isinstance(broker_label, str) or not broker_label:
            raise TrendReplayIncompleteError(
                "missing original input: option_attention.broker_label"
            )
        from .market_trend import (
            _attention_actions,
            _attention_rows,
            build_option_attention,
        )

        current_rows = _attention_rows(payload.get("signal_snapshots"))
        if current_rows is None:
            raise TrendReplayIncompleteError(
                "missing original input: signal_snapshots"
            )
        payload["option_attention"] = build_option_attention(
            current_rows,
            previous_rows,
            _attention_actions(payload),
            market,
            broker_label,
        )
    return payload
