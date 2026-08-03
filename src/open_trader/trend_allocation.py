from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Callable
from zoneinfo import ZoneInfo

from .a_share_trend import _process_version
from .daily_premarket import (
    DailyPremarketConfig,
    RunLock,
    build_notifier,
    require_trend_executor,
    send_notification_with_results,
    trend_execution_mode,
)
from .futu_quote import FutuQuoteClient
from .trend_animals import TrendAnimalsClient, TrendAnimalsError


ROOT_ASSETS = {
    "CN": ("A股", "ETF基金"),
    "HK": ("港股", "香港ETF"),
    "US": ("美股", "美国ETF"),
}
ENTRY_WEIGHTS = {1: Decimal("0.06"), 2: Decimal("0.04"), 3: Decimal("0.02")}
NOMINAL_WEIGHTS = {1: Decimal("0.60"), 2: Decimal("0.40"), 3: Decimal("0.20")}
ROOT_FIELDS = (
    "tmId", "tickerName", "asset", "asOfDate", "trendStrengthGlobalCurr",
)
_ROOT_ASSET_ORDER = tuple(asset for assets in ROOT_ASSETS.values() for asset in assets)
_DAILY_PATH = re.compile(r"data/trend_allocation/daily/(\d{4}-\d{2}-\d{2})(?:-r[1-9]\d*)?\.json$")
ALLOCATION_STATUS_SCHEMA = "open_trader.trend_allocation.status.v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ATTEMPT_AT = time(16, 20)
_FALLBACK_AT = time(17, 45)


def _allocation_status_path(data_dir: Path) -> Path:
    return data_dir / "trend_allocation" / "controller_status.json"


def _allocation_status(
    config: DailyPremarketConfig,
    *,
    now: datetime,
    phase: str,
    attempted_for: str | None,
    reference: Mapping[str, object] | None,
    blocker: str | None,
    next_check_at: datetime,
    process_version: str,
) -> dict[str, object]:
    mode = trend_execution_mode(config)
    return {
        "schema_version": ALLOCATION_STATUS_SCHEMA,
        "effective_mode": mode.mode,
        "executor_host": mode.executor_host,
        "local_host": mode.local_host,
        "pid": os.getpid(),
        "working_directory": str(Path.cwd().resolve()),
        "git_sha": process_version,
        "phase": phase,
        "heartbeat_at": now.isoformat(timespec="seconds"),
        "attempted_for": attempted_for,
        "latest_daily_path": reference.get("daily_path") if reference else None,
        "latest_sha256": reference.get("sha256") if reference else None,
        "blocker": blocker,
        "next_check_at": next_check_at.isoformat(timespec="seconds"),
    }


def _write_allocation_status(config: DailyPremarketConfig, payload: Mapping[str, object]) -> dict[str, object]:
    _write_json_atomic(_allocation_status_path(config.data_dir), payload)
    return dict(payload)


def _read_allocation_status(data_dir: Path) -> dict[str, object]:
    path = _allocation_status_path(data_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrendAnimalsError("allocation controller status is invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != ALLOCATION_STATUS_SCHEMA:
        raise TrendAnimalsError("allocation controller status is invalid")
    return value


def _allocation_notification_path(data_dir: Path, allocation_date: str) -> Path:
    return data_dir / "trend_allocation" / "notifications" / f"{allocation_date}.json"


def _notify_allocation_failure_once(
    config: DailyPremarketConfig, *, allocation_date: str, reason: str
) -> bool:
    path = _allocation_notification_path(config.data_dir, allocation_date)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, Mapping) and existing.get("delivered") is True:
        return False
    try:
        attempts = send_notification_with_results(
            build_notifier(config), "趋势配置快照阻塞",
            f"{allocation_date} 配置刷新失败；沿用最近成功快照。原因：{reason}",
        )
    except Exception:
        return False
    if not any(attempt.success for attempt in attempts):
        return False
    _write_json_atomic(path, {
        "allocation_date": allocation_date,
        "reason": reason,
        "delivered": True,
    })
    return True


def _notify_allocation_recovery(
    config: DailyPremarketConfig, *, allocation_date: str
) -> bool:
    root = config.data_dir / "trend_allocation" / "notifications"
    if not root.exists():
        return False
    pending: list[tuple[Path, Mapping[str, object]]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, Mapping)
            or payload.get("delivered") is not True
            or payload.get("recovered") is True
        ):
            continue
        pending.append((path, payload))

    if not pending:
        return False

    failed_dates = ", ".join(
        str(payload.get("allocation_date") or path.stem)
        for path, payload in pending
    )
    try:
        attempts = send_notification_with_results(
            build_notifier(config), "趋势配置快照恢复",
            f"{failed_dates} 配置刷新阻塞已恢复；{allocation_date} 已生成新快照。",
        )
    except Exception:
        return False
    if not any(attempt.success for attempt in attempts):
        return False

    for path, payload in pending:
        updated = dict(payload)
        updated["recovered"] = True
        _write_json_atomic(path, updated)
    return True


def load_trend_allocation_status(
    config: DailyPremarketConfig, *, now: datetime | None = None
) -> dict[str, object]:
    current = (now or datetime.now(_SHANGHAI)).astimezone(_SHANGHAI)
    mode = trend_execution_mode(config)
    if mode.mode == "readonly":
        return _allocation_status(
            config, now=current, phase="readonly", attempted_for=None,
            reference=None, blocker=mode.reason, next_check_at=current,
            process_version=_process_version(config.repo),
        )
    return _read_allocation_status(config.data_dir)


def allocation_reference_for_report(
    config: DailyPremarketConfig,
    *,
    allocation_date: str,
    a_trading_days: Iterable[str],
) -> dict[str, object] | None:
    """Return the shared allocation only after that cycle made a terminal decision."""
    if not _allocation_status_path(config.data_dir).exists():
        raise TrendAnimalsError("allocation has not made a terminal attempt for this cycle")
    status = _read_allocation_status(config.data_dir)
    if status.get("attempted_for") != _date_text(allocation_date, "allocation_date") or status.get("phase") not in {"ready", "fallback", "holiday"}:
        raise TrendAnimalsError("allocation has not made a terminal attempt for this cycle")
    return load_allocation_reference(
        config.data_dir, allocation_date=allocation_date, a_trading_days=a_trading_days
    )


def run_trend_allocation_controller(
    config: DailyPremarketConfig,
    *,
    once: bool = False,
    allocation_date: str | None = None,
    revision: bool = False,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep_fn: Callable[[float], None] = sleep,
    quote_factory: Callable[..., object] = FutuQuoteClient,
    api_factory: Callable[..., object] = TrendAnimalsClient,
) -> dict[str, object]:
    """Persist one post-close cross-market allocation, retrying only in its fixed window."""
    require_trend_executor(config)
    process_version = _process_version(config.repo)
    if not re.fullmatch(r"[0-9a-f]{40}", process_version):
        process_version = "0" * 40
    lock = RunLock(config.data_dir / "runs/.trend_allocation.lock")
    with lock:
        failures = 0
        failure_day: str | None = None
        while True:
            now = now_fn()
            now = (now.replace(tzinfo=_SHANGHAI) if now.tzinfo is None else now).astimezone(_SHANGHAI)
            day = _date_text(allocation_date or now.date().isoformat(), "allocation_date")
            if day != failure_day:
                failures = 0
                failure_day = day
            quote = quote_factory(host=config.futu_host, port=config.futu_port)
            try:
                days = sorted(quote.get_cn_trading_days(
                    start=(date.fromisoformat(day) - timedelta(days=35)).isoformat(),
                    end=(date.fromisoformat(day) + timedelta(days=14)).isoformat(),
                ))
            finally:
                close = getattr(quote, "close", None)
                if callable(close):
                    close()
            latest = load_allocation_reference(
                config.data_dir, allocation_date=day, a_trading_days=days
            )
            reference = (
                {"daily_path": latest["daily_path"], "sha256": latest["sha256"]}
                if latest else None
            )
            existing: Mapping[str, object] | None = None
            if _allocation_status_path(config.data_dir).exists():
                existing = _read_allocation_status(config.data_dir)
            if not once and now.time() < _ATTEMPT_AT:
                status = _write_allocation_status(config, _allocation_status(
                    config, now=now, phase="waiting", attempted_for=None,
                    reference=reference, blocker=None,
                    next_check_at=datetime.combine(now.date(), _ATTEMPT_AT, tzinfo=_SHANGHAI),
                    process_version=process_version,
                ))
                sleep_fn(5)
                continue
            if (
                not once
                and existing is not None
                and existing.get("attempted_for") == day
                and existing.get("phase") in {"ready", "fallback", "holiday"}
            ):
                sleep_fn(60)
                continue
            if day not in days:
                status = _write_allocation_status(config, _allocation_status(
                    config, now=now, phase="holiday", attempted_for=day,
                    reference=reference, blocker=None, next_check_at=now + timedelta(days=1),
                    process_version=process_version,
                ))
                if once:
                    return status
                sleep_fn(60)
                continue
            try:
                api = api_factory(
                    api_key=config.trend_animals_api_key,
                    cache_dir=config.data_dir / "trend_animals/cache",
                )
                previous = latest.get("snapshot") if latest else None
                snapshot = build_allocation_snapshot(
                    allocation_date=day, generated_at=now.isoformat(timespec="seconds"),
                    git_sha=process_version, roots=fetch_allocation_roots(api), previous=previous,
                )
                reference = write_allocation_snapshot(config.data_dir, snapshot, revision=revision)
            except Exception as exc:
                failures += 1
                reason = str(exc) or exc.__class__.__name__
                _notify_allocation_failure_once(
                    config, allocation_date=day, reason=reason
                )
                terminal = once or now.time() >= _FALLBACK_AT
                if terminal:
                    status = _write_allocation_status(config, _allocation_status(
                        config, now=now, phase="fallback", attempted_for=day,
                        reference=reference, blocker=reason, next_check_at=now + timedelta(days=1),
                        process_version=process_version,
                    ))
                    if once:
                        return status
                    sleep_fn(60)
                    continue
                retry_seconds = min(300, 5 * 2 ** min(failures, 6))
                status = _write_allocation_status(config, _allocation_status(
                    config, now=now, phase="retrying", attempted_for=day,
                    reference=reference, blocker=reason,
                    next_check_at=now + timedelta(seconds=retry_seconds),
                    process_version=process_version,
                ))
                sleep_fn(retry_seconds)
                continue
            status = _write_allocation_status(config, _allocation_status(
                config, now=now, phase="ready", attempted_for=day,
                reference=reference, blocker=None, next_check_at=now + timedelta(days=1),
                process_version=process_version,
            ))
            _notify_allocation_recovery(config, allocation_date=day)
            failures = 0
            if once:
                return status
            sleep_fn(60)


def fetch_allocation_roots(api: object) -> dict[str, object]:
    """Read the six favorite roots in the smallest date-grouped snapshot calls."""
    get_updates = getattr(api, "get_update_status", None)
    get_favorites = getattr(api, "get_favorites_tickers", None)
    get_snapshots = getattr(api, "get_snapshots", None)
    if not all(callable(item) for item in (get_updates, get_favorites, get_snapshots)):
        raise TrendAnimalsError("Trend Animals allocation API boundary is unavailable")
    expected_dates = _root_dates(get_updates())
    favorite_by_asset = _favorite_roots(get_favorites())
    rows_by_id: dict[int, Mapping[str, object]] = {}
    groups: dict[str, list[int]] = {}
    for asset, row in favorite_by_asset.items():
        groups.setdefault(expected_dates[asset], []).append(_tm_id(row))
    for expected_date, tm_ids in groups.items():
        ids = sorted(tm_ids)
        try:
            rows = get_snapshots(
                tm_ids=ids, fields=ROOT_FIELDS, expected_date=expected_date
            )
        except Exception as exc:
            if isinstance(exc, TrendAnimalsError):
                raise
            raise TrendAnimalsError("allocation root snapshot request failed") from None
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise TrendAnimalsError("allocation root snapshot is invalid")
        returned: dict[int, Mapping[str, object]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise TrendAnimalsError("allocation root snapshot is invalid")
            tm_id = _tm_id(row)
            if tm_id in returned:
                raise TrendAnimalsError("allocation root snapshot has duplicate tmId")
            returned[tm_id] = row
        if set(returned) != set(ids):
            raise TrendAnimalsError("allocation root snapshot has mismatched tmId")
        rows_by_id.update(returned)
    roots: dict[str, object] = {}
    for market, (stock_asset, etf_asset) in ROOT_ASSETS.items():
        market_roots: dict[str, object] = {}
        for role, asset in (("stock", stock_asset), ("etf", etf_asset)):
            favorite = favorite_by_asset[asset]
            row = rows_by_id.get(_tm_id(favorite))
            if (
                row is None
                or row.get("asset") != asset
                or row.get("asOfDate") != expected_dates[asset]
                or not isinstance(row.get("tickerName"), str)
                or not str(row["tickerName"]).strip()
            ):
                raise TrendAnimalsError("allocation root snapshot does not match favorite")
            market_roots[role] = {
                "asset": asset,
                "tm_id": _tm_id(row),
                "as_of_date": expected_dates[asset],
                "global_strength": _decimal_text(row.get("trendStrengthGlobalCurr")),
            }
        roots[market] = market_roots
    _normalise_roots(roots, require_text=True)
    return roots


def build_allocation_snapshot(
    *,
    allocation_date: str,
    generated_at: str,
    git_sha: str,
    roots: Mapping[str, object],
    previous: Mapping[str, object] | None,
) -> dict[str, object]:
    allocation_date = _date_text(allocation_date, "allocation_date")
    _timestamp(generated_at)
    _git_sha(git_sha)
    normalized_roots = _normalise_roots(roots)
    order = _rank_markets(normalized_roots, previous)
    markets: dict[str, object] = {}
    for rank, market in enumerate(order, 1):
        stock, etf = normalized_roots[market].values()
        stock_strength = Decimal(stock["global_strength"])
        etf_strength = Decimal(etf["global_strength"])
        score_source, score = (
            (stock["asset"], stock_strength)
            if stock_strength >= etf_strength
            else (etf["asset"], etf_strength)
        )
        markets[market] = {
            "rank": rank,
            "score": _decimal_text(score),
            "score_source": score_source,
            "entry_weight": _decimal_text(ENTRY_WEIGHTS[rank]),
            "nominal_weight": _decimal_text(NOMINAL_WEIGHTS[rank]),
        }
    return {
        "version": 1,
        "allocation_date": allocation_date,
        "generated_at": generated_at,
        "generator_version": "trend-allocation-v1",
        "git_sha": git_sha,
        "roots": normalized_roots,
        "markets": markets,
    }


def write_allocation_snapshot(
    data_dir: Path, snapshot: Mapping[str, object], *, revision: bool = False
) -> dict[str, str]:
    if not isinstance(data_dir, Path):
        raise TypeError("data_dir must be a Path")
    _validate_snapshot(snapshot)
    allocation_date = str(snapshot["allocation_date"])
    body = canonical_json_bytes(snapshot)
    sha256 = hashlib.sha256(body).hexdigest()
    daily_root = data_dir / "trend_allocation" / "daily"
    daily = daily_root / f"{allocation_date}.json"
    if daily.exists() and daily.read_bytes() != body:
        if not revision:
            raise TrendAnimalsError("immutable allocation snapshot collision")
        _assert_revision_is_unlocked(data_dir, allocation_date)
        existing_revision = _matching_revision(daily_root, allocation_date, body)
        if existing_revision is not None:
            daily = existing_revision
        else:
            daily = daily_root / f"{allocation_date}-r{_next_revision(daily_root, allocation_date)}.json"
    _create_immutable(daily, body)
    reference = {
        "daily_path": "data/" + str(daily.relative_to(data_dir)),
        "sha256": sha256,
    }
    _write_json_atomic(data_dir / "trend_allocation" / "latest.json", reference)
    return reference


def load_allocation_reference(
    data_dir: Path,
    *,
    allocation_date: str,
    a_trading_days: Iterable[str],
) -> dict[str, object] | None:
    requested_date = _date_text(allocation_date, "allocation_date")
    latest = data_dir / "trend_allocation" / "latest.json"
    if not latest.exists():
        return None
    try:
        reference = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrendAnimalsError("allocation latest pointer is invalid") from exc
    daily_path, sha256 = _reference(reference)
    path = data_dir / PurePosixPath(daily_path).relative_to("data")
    try:
        body = path.read_bytes()
        snapshot = json.loads(body)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrendAnimalsError("allocation daily snapshot is invalid") from exc
    if hashlib.sha256(body).hexdigest() != sha256:
        raise TrendAnimalsError("allocation daily snapshot hash mismatch")
    _validate_snapshot(snapshot)
    snapshot_date = str(snapshot["allocation_date"])
    stale_days = 0
    for item in a_trading_days:
        day = _date_text(item, "A trading day")
        if snapshot_date < day <= requested_date:
            stale_days += 1
    return {
        "daily_path": daily_path,
        "sha256": sha256,
        "snapshot": snapshot,
        "reused": snapshot_date != requested_date,
        "stale_a_trading_days": stale_days,
        "failure_reason": _status_failure_reason(data_dir),
    }


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _root_dates(rows: object) -> dict[str, str]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TrendAnimalsError("allocation update status is invalid")
    expected = _ROOT_ASSET_ORDER
    found: dict[str, list[str]] = {asset: [] for asset in expected}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TrendAnimalsError("allocation update status is invalid")
        asset = row.get("asset")
        if isinstance(asset, str) and asset in found:
            value = next((row.get(key) for key in ("asOfDate", "updateDate", "latestDate", "date") if isinstance(row.get(key), str) and str(row[key]).strip()), None)
            found[asset].append(_date_text(value, "allocation root date"))
    if any(len(values) != 1 for values in found.values()):
        raise TrendAnimalsError("allocation root update status is incomplete")
    return {asset: values[0] for asset, values in found.items()}


def _favorite_roots(rows: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TrendAnimalsError("allocation favorites are invalid")
    expected = _ROOT_ASSET_ORDER
    found: dict[str, list[Mapping[str, object]]] = {asset: [] for asset in expected}
    ids: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise TrendAnimalsError("allocation favorites are invalid")
        asset = row.get("asset")
        name = row.get("tickername", row.get("tickerName"))
        if isinstance(asset, str) and asset in found and name == asset:
            tm_id = _tm_id(row)
            if tm_id in ids:
                raise TrendAnimalsError("allocation favorites have duplicate tmId")
            ids.add(tm_id)
            found[asset].append(row)
    if any(len(values) != 1 for values in found.values()):
        raise TrendAnimalsError("allocation favorites do not contain six unique roots")
    return {asset: values[0] for asset, values in found.items()}


def _normalise_roots(
    roots: Mapping[str, object], *, require_text: bool = False
) -> dict[str, dict[str, dict[str, str | int]]]:
    if not isinstance(roots, Mapping) or set(roots) != set(ROOT_ASSETS):
        raise TrendAnimalsError("allocation snapshot must contain six roots")
    result: dict[str, dict[str, dict[str, str | int]]] = {}
    seen_ids: set[int] = set()
    for market, assets in ROOT_ASSETS.items():
        root_set = roots.get(market)
        if not isinstance(root_set, Mapping) or set(root_set) != {"stock", "etf"}:
            raise TrendAnimalsError("allocation snapshot must contain six roots")
        result[market] = {}
        for role, asset in zip(("stock", "etf"), assets, strict=True):
            root = root_set.get(role)
            if not isinstance(root, Mapping) or set(root) != {"asset", "tm_id", "as_of_date", "global_strength"}:
                raise TrendAnimalsError("allocation root is invalid")
            tm_id = root.get("tm_id")
            if isinstance(tm_id, bool) or not isinstance(tm_id, int) or tm_id <= 0 or tm_id in seen_ids:
                raise TrendAnimalsError("allocation root tm_id is invalid")
            if root.get("asset") != asset:
                raise TrendAnimalsError("allocation root asset is invalid")
            strength = root.get("global_strength")
            if require_text and not isinstance(strength, str):
                raise TrendAnimalsError("allocation root strength is invalid")
            result[market][role] = {
                "asset": asset,
                "tm_id": tm_id,
                "as_of_date": _date_text(root.get("as_of_date"), "allocation root date"),
                "global_strength": _decimal_text(strength),
            }
            seen_ids.add(tm_id)
    return result


def _rank_markets(
    roots: Mapping[str, Mapping[str, Mapping[str, str | int]]], previous: Mapping[str, object] | None
) -> list[str]:
    pairs = {
        market: tuple(sorted((Decimal(str(root["global_strength"])) for root in values.values()), reverse=True))
        for market, values in roots.items()
    }
    previous_order = _previous_order(previous) if previous is not None else None
    ordered: list[str] = []
    for pair in sorted(set(pairs.values()), reverse=True):
        tied = [market for market, value in pairs.items() if value == pair]
        if len(tied) > 1:
            if previous_order is None:
                raise TrendAnimalsError("allocation market tie needs a previous snapshot")
            tied.sort(key=previous_order.index)
        ordered.extend(tied)
    return ordered


def _previous_order(previous: Mapping[str, object]) -> list[str]:
    _validate_snapshot(previous)
    if not isinstance(previous, Mapping) or not isinstance(previous.get("markets"), Mapping):
        raise TrendAnimalsError("previous allocation snapshot is invalid")
    markets = previous["markets"]
    if set(markets) != set(ROOT_ASSETS):
        raise TrendAnimalsError("previous allocation snapshot is invalid")
    try:
        order = [market for _rank, market in sorted((int(values["rank"]), market) for market, values in markets.items() if isinstance(values, Mapping))]
    except (KeyError, TypeError, ValueError):
        raise TrendAnimalsError("previous allocation snapshot is invalid") from None
    if len(order) != 3 or sorted(
        int(values["rank"])
        for values in markets.values()
        if isinstance(values, Mapping)
    ) != [1, 2, 3]:
        raise TrendAnimalsError("previous allocation snapshot is invalid")
    return order


def _validate_snapshot(snapshot: Mapping[str, object]) -> None:
    if not isinstance(snapshot, Mapping) or set(snapshot) != {"version", "allocation_date", "generated_at", "generator_version", "git_sha", "roots", "markets"}:
        raise TrendAnimalsError("allocation snapshot schema is invalid")
    if snapshot.get("version") != 1 or snapshot.get("generator_version") != "trend-allocation-v1":
        raise TrendAnimalsError("allocation snapshot schema is invalid")
    _date_text(snapshot.get("allocation_date"), "allocation_date")
    _timestamp(snapshot.get("generated_at"))
    _git_sha(snapshot.get("git_sha"))
    roots = _normalise_roots(snapshot.get("roots"), require_text=True)  # type: ignore[arg-type]
    markets = snapshot.get("markets")
    if not isinstance(markets, Mapping) or set(markets) != set(ROOT_ASSETS):
        raise TrendAnimalsError("allocation market mapping is invalid")
    ranks: set[int] = set()
    for market, values in markets.items():
        if not isinstance(values, Mapping) or set(values) != {"rank", "score", "score_source", "entry_weight", "nominal_weight"}:
            raise TrendAnimalsError("allocation market mapping is invalid")
        rank = values.get("rank")
        stock, etf = roots[market].values()
        stock_strength, etf_strength = Decimal(stock["global_strength"]), Decimal(etf["global_strength"])
        source, score = (stock["asset"], stock_strength) if stock_strength >= etf_strength else (etf["asset"], etf_strength)
        if (
            isinstance(rank, bool) or not isinstance(rank, int) or rank not in ENTRY_WEIGHTS
            or rank in ranks or values.get("score") != _decimal_text(score)
            or values.get("score_source") != source
            or values.get("entry_weight") != _decimal_text(ENTRY_WEIGHTS[rank])
            or values.get("nominal_weight") != _decimal_text(NOMINAL_WEIGHTS[rank])
        ):
            raise TrendAnimalsError("allocation market mapping is invalid")
        ranks.add(rank)
    if ranks != {1, 2, 3}:
        raise TrendAnimalsError("allocation market mapping is invalid")
    pairs = {
        market: tuple(
            sorted(
                (Decimal(str(root["global_strength"])) for root in values.values()),
                reverse=True,
            )
        )
        for market, values in roots.items()
    }
    for market, pair in pairs.items():
        for other, other_pair in pairs.items():
            if pair > other_pair and markets[market]["rank"] >= markets[other]["rank"]:  # type: ignore[index]
                raise TrendAnimalsError("allocation market mapping is invalid")


def _reference(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"daily_path", "sha256"}:
        raise TrendAnimalsError("allocation latest pointer is invalid")
    daily_path, sha256 = value.get("daily_path"), value.get("sha256")
    if not isinstance(daily_path, str) or _DAILY_PATH.fullmatch(daily_path) is None or not isinstance(sha256, str) or len(sha256) != 64 or any(item not in "0123456789abcdef" for item in sha256):
        raise TrendAnimalsError("allocation latest pointer is invalid")
    return daily_path, sha256


def _assert_revision_is_unlocked(data_dir: Path, allocation_date: str) -> None:
    for market in ROOT_ASSETS:
        for batch_path in (data_dir / "trend_review" / "ledgers" / market / "batches").glob("*.json"):
            try:
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
                report_path = Path(str(batch["report_path"]))
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise TrendAnimalsError("allocation revision is blocked by an invalid execution batch") from exc
            if isinstance(report, Mapping) and report.get("as_of_date") == allocation_date:
                raise TrendAnimalsError("allocation revision is blocked by a locked execution batch")


def _next_revision(daily_root: Path, allocation_date: str) -> int:
    revisions = [0]
    for path in daily_root.glob(f"{allocation_date}-r*.json"):
        match = re.fullmatch(rf"{re.escape(allocation_date)}-r([1-9]\d*)\.json", path.name)
        if match:
            revisions.append(int(match.group(1)))
    return max(revisions) + 1


def _matching_revision(daily_root: Path, allocation_date: str, body: bytes) -> Path | None:
    for revision in range(1, _next_revision(daily_root, allocation_date)):
        path = daily_root / f"{allocation_date}-r{revision}.json"
        if path.exists() and path.read_bytes() == body:
            return path
    return None


def _create_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != body:
            raise TrendAnimalsError("immutable allocation snapshot collision") from None
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _status_failure_reason(data_dir: Path) -> str | None:
    path = data_dir / "trend_allocation" / "controller_status.json"
    if not path.exists():
        return None
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrendAnimalsError("allocation controller status is invalid") from exc
    if not isinstance(status, Mapping):
        raise TrendAnimalsError("allocation controller status is invalid")
    reason = status.get("blocker", status.get("failure_reason"))
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise TrendAnimalsError("allocation controller status is invalid")
    return reason


def _tm_id(row: Mapping[str, object]) -> int:
    value = row.get("tmId")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TrendAnimalsError("allocation root tmId is invalid")
    return value


def _date_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TrendAnimalsError(f"{label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise TrendAnimalsError(f"{label} is invalid") from None
    if parsed.isoformat() != value:
        raise TrendAnimalsError(f"{label} is invalid")
    return value


def _timestamp(value: object) -> None:
    if not isinstance(value, str):
        raise TrendAnimalsError("generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise TrendAnimalsError("generated_at is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise TrendAnimalsError("generated_at is invalid")


def _git_sha(value: object) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(item not in "0123456789abcdef" for item in value):
        raise TrendAnimalsError("git_sha is invalid")


def _decimal_text(value: object) -> str:
    if isinstance(value, bool):
        raise TrendAnimalsError("allocation root strength is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TrendAnimalsError("allocation root strength is invalid") from None
    if not parsed.is_finite():
        raise TrendAnimalsError("allocation root strength is invalid")
    return format(parsed, "f")
