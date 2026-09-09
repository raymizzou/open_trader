"""Manual Trend Animals curve collection."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
from http.client import HTTPSConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Mapping, NamedTuple, Sequence
from zoneinfo import ZoneInfo

from .futu_symbols import from_trend_animals_symbol, to_futu_symbol
from .trend_animals import TrendAnimalsClient


MINI_PROGRAM_APP_ID = "wx64e4edbab5e14356"
DEFAULT_MMKV_HELPER = Path("~/.local/bin/open-trader-mmkv-dump").expanduser()
DEFAULT_MAPPINGS_ROOT = Path("data/trend_animals/cache/symbol_mappings")
CURVE_ENDPOINT = "https://www.trendtrader.cn/mall4cloud_breed/breed/getVarietyCurve_V3"
CURVE_WINDOW = 0
CURVE_AES_KEY = "AFD3044276988A80"
CURVE_ASSET_ID = 10002
CURVE_GROUP_IDS = {
    "A股": 303121,
    "ETF基金": 377042,
    "港股": 329480,
    "香港ETF": 705189,
    "美股": 332171,
    "美国ETF": 704988,
}
CURVE_CURRENCY_IDS = {"CNY": 100, "USD": 101, "HKD": 104}
PORTFOLIO_MARKETS = frozenset({"CN", "HK", "US"})
PORTFOLIO_CURRENCIES = {"CN": "CNY", "HK": "HKD", "US": "USD"}
DEFAULT_PORTFOLIO_TREND_CURVE_EXCLUSIONS = Path(
    "config/trend_curve_portfolio_exclusions.json"
)
DAILY_REQUEST_LIMIT_MAX = 100_000
DAILY_REQUEST_INTERVAL_MAX_SECONDS = 3600.0
DAILY_MAX_DURATION_MAX_SECONDS = 86_400.0
DAILY_HERMES_TIMEOUT_MAX_SECONDS = 300.0
CURVE_ASSETS_BY_MARKET = {
    "CN": frozenset({"A股", "ETF基金"}),
    "HK": frozenset({"港股", "香港ETF"}),
    "US": frozenset({"美股", "美国ETF"}),
}
TREND_SYMBOL_MAPPING_SCHEMA = "open_trader.trend_symbol_mapping.v1"
TEMPERATURES = frozenset({"冻", "寒", "凉", "平", "温", "热", "沸"})
SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_START_HOUR = 12

CurveTransport = Callable[[str, bytes, dict[str, str]], Mapping[str, object]]


class WechatMiniCredentials(NamedTuple):
    token: str
    user_id: int | str


class TrendCurveReconciliationError(ValueError):
    pass


class _TrendCurveAuthBlocked(ValueError):
    pass


@dataclass(frozen=True)
class CollectionResult:
    database_path: Path
    target_count: int
    point_count: int
    targets: tuple[dict[str, object], ...] = ()
    snapshot_count: int = 0
    request_count: int = 0
    batch_id: str | None = None
    status: str = "complete"
    completed_count: int = 0
    pending_count: int = 0
    issues: tuple[dict[str, object], ...] = ()
    stop_reason: str | None = None


def read_wechat_mini_credentials(
    storage_path: Path | str | None = None,
    helper_path: Path | str | None = None,
    *,
    app_id: str = MINI_PROGRAM_APP_ID,
    storage_root: Path | str | None = None,
) -> WechatMiniCredentials:
    """Read credentials from a copied MMKV snapshot without retaining the copy."""
    if not isinstance(app_id, str) or not app_id.strip():
        raise ValueError("mini-program app id is required")
    source = _resolve_mmkv_path(storage_path, storage_root, app_id)
    source_crc = Path(f"{source}.crc")
    helper = Path(helper_path).expanduser() if helper_path is not None else DEFAULT_MMKV_HELPER
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise ValueError("MMKV helper is unavailable")
    if not source.is_file() or not source_crc.is_file():
        raise ValueError("MMKV snapshot is incomplete")

    try:
        with TemporaryDirectory(prefix="open-trader-mmkv-") as temporary:
            root = Path(temporary)
            shutil.copyfile(source, root / source.name)
            shutil.copyfile(source_crc, root / source_crc.name)
            try:
                completed = subprocess.run(
                    [str(helper), str(root), app_id, app_id[::2]],
                    capture_output=True,
                    check=False,
                    text=True,
                    encoding="utf-8",
                )
            except (OSError, UnicodeError) as exc:
                raise ValueError("MMKV helper failed") from exc
            if completed.returncode != 0:
                raise ValueError("MMKV helper failed")
            return _credentials_from_dump(completed.stdout)
    except ValueError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ValueError("MMKV snapshot could not be read") from exc


def _resolve_mmkv_path(
    storage_path: Path | str | None,
    storage_root: Path | str | None,
    app_id: str,
) -> Path:
    if storage_path is not None:
        candidate = Path(storage_path).expanduser()
        if candidate.is_dir():
            matches = sorted(candidate.glob(app_id))
            if len(matches) != 1:
                raise ValueError("MMKV snapshot is ambiguous or missing")
            return matches[0]
        return candidate
    root = Path(storage_root).expanduser() if storage_root is not None else Path(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/users"
    ).expanduser()
    matches = sorted(root.glob(f"*/applet/local/{app_id}/usrmmkvstorage0/{app_id}"))
    if len(matches) != 1:
        raise ValueError("MMKV snapshot is ambiguous or missing")
    return matches[0]


def _credentials_from_dump(output: str) -> WechatMiniCredentials:
    if not isinstance(output, str):
        raise ValueError("MMKV helper output is malformed")
    vuex_values: list[str] = []
    for line in output.splitlines():
        if not line or "\t" not in line:
            raise ValueError("MMKV helper output is malformed")
        key, value = line.split("\t", 1)
        if not key or not value:
            raise ValueError("MMKV helper output is malformed")
        if key == "vuex":
            vuex_values.append(value)
    if len(vuex_values) != 1:
        raise ValueError("MMKV helper output has no unique vuex record")
    try:
        payload: object = json.loads(vuex_values[0])
        if isinstance(payload, str):
            payload = json.loads(payload)
        if (
            isinstance(payload, dict)
            and set(payload) == {"data", "dataType"}
            and payload.get("dataType") == "String"
        ):
            payload = json.loads(payload["data"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("MMKV vuex record is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("MMKV vuex record is malformed")
    user = payload.get("user")
    if not isinstance(user, dict):
        raise ValueError("MMKV vuex record has no logged-in user")
    token = user.get("token")
    info = user.get("info")
    user_id = info.get("id") if isinstance(info, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("MMKV vuex record has no logged-in user")
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        raise ValueError("MMKV vuex record has no logged-in user")
    if isinstance(user_id, str) and not user_id.strip():
        raise ValueError("MMKV vuex record has no logged-in user")
    if isinstance(user_id, int) and user_id <= 0:
        raise ValueError("MMKV vuex record has no logged-in user")
    return WechatMiniCredentials(token=token, user_id=user_id)


def collect_trend_curves(
    watchlist: Path | str | None = None,
    *,
    portfolio: Path | str | None = None,
    mappings_root: Path | str | None = None,
    database: Path | str | None = None,
    credentials: WechatMiniCredentials | tuple[object, object] | None = None,
    transport: CurveTransport | None = None,
    mmkv_path: Path | str | None = None,
    mmkv_helper: Path | str | None = None,
    storage_root: Path | str | None = None,
    batch_id: str | None = None,
    require_snapshot: bool = False,
    expected_dates: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
    request_limit: int | None = None,
    request_interval_seconds: float = 0.0,
    max_duration_seconds: float | None = None,
) -> CollectionResult:
    target_database = Path(database or "data/trend_curve/history.sqlite3").expanduser()
    if (watchlist is None) == (portfolio is None):
        raise ValueError("exactly one of watchlist or portfolio is required")
    if portfolio is not None:
        targets = _load_portfolio_targets(
            portfolio,
            Path(mappings_root or DEFAULT_MAPPINGS_ROOT).expanduser(),
        )
    else:
        assert watchlist is not None
        targets = _load_watchlist(watchlist)
    _reject_duplicate_targets(targets)
    normalized_dates = _normalize_expected_dates(targets, expected_dates)
    normalized_observed_at = _normalize_observed_at(observed_at)
    if batch_id is not None and (not isinstance(batch_id, str) or not batch_id.strip()):
        raise ValueError("batch id is required")
    if request_limit is not None and (
        isinstance(request_limit, bool)
        or not isinstance(request_limit, int)
        or request_limit <= 0
        or request_limit > DAILY_REQUEST_LIMIT_MAX
    ):
        raise ValueError("daily request limit must be a positive integer")
    if (
        isinstance(request_interval_seconds, bool)
        or not isinstance(request_interval_seconds, (int, float))
        or not math.isfinite(float(request_interval_seconds))
        or request_interval_seconds < 0
        or request_interval_seconds > DAILY_REQUEST_INTERVAL_MAX_SECONDS
    ):
        raise ValueError("daily request interval must be a finite non-negative number")
    if max_duration_seconds is not None and (
        isinstance(max_duration_seconds, bool)
        or not isinstance(max_duration_seconds, (int, float))
        or not math.isfinite(float(max_duration_seconds))
        or max_duration_seconds <= 0
        or max_duration_seconds > DAILY_MAX_DURATION_MAX_SECONDS
    ):
        raise ValueError("daily max duration must be a positive finite number")
    sender = transport or _default_curve_transport
    target_database.parent.mkdir(parents=True, exist_ok=True)
    point_count = 0
    snapshot_count = 0
    request_count = 0
    issues: list[dict[str, object]] = []
    completed_count = 0
    pending_count = len(targets)
    status = "complete"
    stop_reason: str | None = None
    deadline = (
        time.monotonic() + float(max_duration_seconds)
        if max_duration_seconds is not None
        else None
    )

    try:
        with _collector_lock(target_database):
            with sqlite3.connect(target_database) as connection:
                _ensure_curve_schema(connection)
                connection.commit()
                manifest = _batch_manifest(targets, require_snapshot, normalized_dates)
                pending_targets = list(targets)
                if batch_id is not None:
                    pending_targets = _prepare_batch(
                        connection,
                        batch_id,
                        manifest,
                        targets,
                        require_snapshot,
                        normalized_dates,
                        normalized_observed_at,
                    )
                    connection.commit()
                    pending_count = len(pending_targets)
                    completed_count = len(targets) - pending_count
                    if not pending_targets:
                        return CollectionResult(
                            database_path=target_database,
                            target_count=len(targets),
                            point_count=0,
                            targets=tuple(dict(target) for target in targets),
                            snapshot_count=0,
                            request_count=0,
                            batch_id=batch_id,
                            status="complete",
                            completed_count=completed_count,
                            pending_count=0,
                            issues=(),
                            stop_reason=None,
                        )

                if credentials is None:
                    credentials = read_wechat_mini_credentials(
                        mmkv_path,
                        helper_path=mmkv_helper,
                        storage_root=storage_root,
                    )
                token, user_id = _normalize_credentials(credentials)

                for target_index, target in enumerate(pending_targets):
                    if request_limit is not None and request_count >= request_limit:
                        stop_reason = "request_limit"
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        stop_reason = "deadline_exceeded"
                        break
                    if target_index:
                        if request_interval_seconds:
                            time.sleep(float(request_interval_seconds))
                        if deadline is not None and time.monotonic() >= deadline:
                            stop_reason = "deadline_exceeded"
                            break
                    body = {
                        "assetId": target["asset_id"],
                        "groupId": target["group_id"],
                        "id": target["tm_id"],
                        "userId": user_id,
                        "selected": CURVE_WINDOW,
                        "ccyId": target["ccy_id"],
                        "code": str(user_id),
                    }
                    body_bytes = json.dumps(
                        body, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    request_count += 1
                    try:
                        response = sender(
                            CURVE_ENDPOINT,
                            body_bytes,
                            {"Authorization": token, "Content-Type": "application/json"},
                        )
                    except _TrendCurveAuthBlocked:
                        if batch_id is None:
                            raise ValueError("Trend Animals authentication blocked")
                        issues.append(_collection_issue(target, "auth_blocked"))
                        status = "auth_blocked"
                        stop_reason = "auth_blocked"
                        break
                    except Exception as exc:
                        if batch_id is None:
                            raise ValueError("Trend Animals curve request failed") from exc
                        issues.append(_collection_issue(target, "data_gap"))
                        continue

                    try:
                        points, snapshot = _validated_curve_response(
                            _decrypt_curve_response(_encrypted_response(response))
                        )
                    except _TrendCurveAuthBlocked:
                        if batch_id is None:
                            raise ValueError("Trend Animals authentication blocked")
                        issues.append(_collection_issue(target, "auth_blocked"))
                        status = "auth_blocked"
                        stop_reason = "auth_blocked"
                        break
                    except Exception as exc:
                        if batch_id is None:
                            if isinstance(exc, ValueError):
                                raise
                            raise ValueError("Trend Animals curve request failed") from exc
                        issues.append(_collection_issue(target, "data_gap"))
                        continue

                    expected_date = normalized_dates.get(_target_identity(target))
                    issue_reason = _snapshot_gap_reason(
                        snapshot, expected_date, require_snapshot
                    )
                    if issue_reason is not None and batch_id is None:
                        raise ValueError(f"Trend Animals {issue_reason}")
                    if issue_reason is not None and batch_id is not None:
                        try:
                            with connection:
                                _persist_target(
                                    connection,
                                    target,
                                    points,
                                    snapshot,
                                    normalized_observed_at,
                                    batch_id=batch_id,
                                    expected_date=expected_date,
                                    require_snapshot=require_snapshot,
                                    completed=False,
                                )
                        except sqlite3.Error as exc:
                            status = "partial"
                            issues.append(_collection_issue(target, "persistence_failed"))
                            break
                        point_count += len(points)
                        snapshot_count += int(snapshot is not None)
                        issues.append(_collection_issue(target, issue_reason))
                        continue

                    try:
                        with connection:
                            _persist_target(
                                connection,
                                target,
                                points,
                                snapshot,
                                normalized_observed_at,
                                batch_id=batch_id,
                                expected_date=expected_date,
                                require_snapshot=require_snapshot,
                                completed=True,
                            )
                    except sqlite3.Error as exc:
                        if batch_id is None:
                            raise ValueError("趋势曲线数据库写入失败") from exc
                        status = "partial"
                        issues.append(_collection_issue(target, "persistence_failed"))
                        break
                    point_count += len(points)
                    snapshot_count += int(snapshot is not None)

                    if deadline is not None and time.monotonic() >= deadline:
                        stop_reason = "deadline_exceeded"
                        break

                if batch_id is not None:
                    completed_count, pending_count = _batch_counts(connection, batch_id)
                    if status == "auth_blocked":
                        pass
                    elif pending_count:
                        status = "partial"
                    else:
                        status = "complete"
                    if not pending_count:
                        stop_reason = None
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("趋势曲线数据库写入失败") from exc

    if batch_id is None:
        completed_count = len(targets)
        pending_count = 0
    return CollectionResult(
        database_path=target_database,
        target_count=len(targets),
        point_count=point_count,
        targets=tuple(dict(target) for target in targets),
        snapshot_count=snapshot_count,
        request_count=request_count,
        batch_id=batch_id,
        status=status,
        completed_count=completed_count,
        pending_count=pending_count,
        issues=tuple(issues),
        stop_reason=stop_reason,
    )


def _target_identity(target: Mapping[str, object]) -> str:
    return f"{target['market']}.{target['symbol']}"


def _collection_issue(
    target: Mapping[str, object], reason: str
) -> dict[str, object]:
    return {
        "market": target["market"],
        "symbol": target["symbol"],
        "reason": reason,
    }


def _reject_duplicate_targets(targets: Sequence[Mapping[str, object]]) -> None:
    identities = [_target_identity(target) for target in targets]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate market or symbol")


def _normalize_expected_dates(
    targets: Sequence[Mapping[str, object]],
    expected_dates: Mapping[str, str] | None,
) -> dict[str, str]:
    if expected_dates is None:
        return {}
    if not isinstance(expected_dates, Mapping):
        raise ValueError("expected dates are malformed")
    normalized: dict[str, str] = {}
    for target in targets:
        identity = _target_identity(target)
        value = expected_dates.get(identity, expected_dates.get(str(target["market"])))
        if not isinstance(value, str):
            raise ValueError("expected dates are malformed")
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected dates are malformed") from exc
        if parsed.isoformat() != value:
            raise ValueError("expected dates are malformed")
        normalized[identity] = value
    return normalized


def _normalize_observed_at(value: datetime | None) -> str:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ValueError("observed_at must be an aware datetime")
    return current.astimezone(timezone.utc).isoformat()


@contextmanager
def _collector_lock(database: Path):
    lock_path = Path(f"{database}.lock")
    try:
        handle = lock_path.open("a+")
    except OSError as exc:
        raise ValueError("trend curve collector lock is unavailable") from exc
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise ValueError("trend curve collector is already running") from exc
        except OSError as exc:
            raise ValueError("trend curve collector lock is unavailable") from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _batch_manifest(
    targets: Sequence[Mapping[str, object]],
    require_snapshot: bool,
    expected_dates: Mapping[str, str],
) -> str:
    normalized_targets = [
        {
            "market": target["market"],
            "symbol": target["symbol"],
            "asset_id": target["asset_id"],
            "group_id": target["group_id"],
            "tm_id": target["tm_id"],
            "ccy_id": target["ccy_id"],
        }
        for target in sorted(targets, key=_target_identity)
    ]
    return json.dumps(
        {
            "targets": normalized_targets,
            "require_snapshot": bool(require_snapshot),
            "expected_dates": dict(sorted(expected_dates.items())),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _prepare_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    manifest: str,
    targets: Sequence[Mapping[str, object]],
    require_snapshot: bool,
    expected_dates: Mapping[str, str],
    observed_at: str,
) -> list[dict[str, object]]:
    row = connection.execute(
        "SELECT manifest FROM trend_curve_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO trend_curve_batches
            (batch_id, manifest, require_snapshot, expected_dates_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                manifest,
                int(require_snapshot),
                json.dumps(expected_dates, sort_keys=True, separators=(",", ":")),
                observed_at,
            ),
        )
        for target in sorted(targets, key=_target_identity):
            connection.execute(
                """
                INSERT INTO trend_curve_batch_items
                (batch_id, market, symbol, asset_id, group_id, tm_id, ccy_id, expected_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    target["market"],
                    target["symbol"],
                    target["asset_id"],
                    target["group_id"],
                    target["tm_id"],
                    target["ccy_id"],
                    expected_dates.get(_target_identity(target)),
                ),
            )
    elif row[0] != manifest:
        raise ValueError("batch request does not match frozen batch")

    pending: list[dict[str, object]] = []
    for target in targets:
        item = connection.execute(
            """
            SELECT completed_at
            FROM trend_curve_batch_items
            WHERE batch_id = ? AND market = ? AND symbol = ?
            """,
            (batch_id, target["market"], target["symbol"]),
        ).fetchone()
        if item is None:
            connection.execute(
                """
                INSERT INTO trend_curve_batch_items
                (batch_id, market, symbol, asset_id, group_id, tm_id, ccy_id, expected_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    target["market"],
                    target["symbol"],
                    target["asset_id"],
                    target["group_id"],
                    target["tm_id"],
                    target["ccy_id"],
                    expected_dates.get(_target_identity(target)),
                ),
            )
            pending.append(dict(target))
        elif not _batch_item_is_complete(
            connection, batch_id, target, require_snapshot
        ):
            if item[0] is not None:
                connection.execute(
                    """
                    UPDATE trend_curve_batch_items
                    SET point_count = NULL, snapshot_date = NULL, observed_at = NULL,
                        evidence_json = NULL, completed_at = NULL, issue_reason = NULL
                    WHERE batch_id = ? AND market = ? AND symbol = ?
                    """,
                    (batch_id, target["market"], target["symbol"]),
                )
            pending.append(dict(target))
    return pending


def _batch_item_is_complete(
    connection: sqlite3.Connection,
    batch_id: str,
    target: Mapping[str, object],
    require_snapshot: bool,
) -> bool:
    row = connection.execute(
        """
        SELECT completed_at, evidence_json
        FROM trend_curve_batch_items
        WHERE batch_id = ? AND market = ? AND symbol = ?
        """,
        (batch_id, target["market"], target["symbol"]),
    ).fetchone()
    if row is None or row[0] is None or not isinstance(row[1], str):
        return False
    try:
        evidence = json.loads(row[1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(evidence, Mapping):
        return False
    point_dates = evidence.get("point_dates")
    if (
        not isinstance(point_dates, list)
        or not point_dates
        or any(not isinstance(point_date, str) for point_date in point_dates)
        or len(set(point_dates)) != len(point_dates)
    ):
        return False
    points = connection.execute(
        """
        SELECT curve_date, price, temperature, strength, mom, yoy, bar,
               asset_id, group_id, tm_id, ccy_id, mom_delta, yoy_delta, yield_value
        FROM trend_curve_points
        WHERE market = ? AND symbol = ?
        ORDER BY curve_date
        """,
        (target["market"], target["symbol"]),
    ).fetchall()
    point_date_set = set(point_dates)
    points = [point for point in points if point[0] in point_date_set]
    if {point[0] for point in points} != point_date_set:
        return False
    try:
        point_digest = _rows_digest(points)
    except (TypeError, ValueError):
        return False
    if point_digest != evidence.get("point_digest"):
        return False
    if not require_snapshot:
        return True
    snapshot_date = evidence.get("snapshot_date")
    if not isinstance(snapshot_date, str):
        return False
    snapshot = connection.execute(
        """
        SELECT snapshot_date, solar_term, right_side_day, right_side_state, labels_json
        FROM trend_curve_daily_snapshots
        WHERE market = ? AND symbol = ? AND snapshot_date = ?
        """,
        (target["market"], target["symbol"], snapshot_date),
    ).fetchone()
    if snapshot is None:
        return False
    try:
        snapshot_digest = _rows_digest([snapshot])
    except (TypeError, ValueError):
        return False
    return snapshot_digest == evidence.get("snapshot_digest")


def _rows_digest(rows: Sequence[Sequence[object]]) -> str:
    payload = json.dumps(
        [list(row) for row in rows],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _persist_target(
    connection: sqlite3.Connection,
    target: Mapping[str, object],
    points: Sequence[Mapping[str, object]],
    snapshot: Mapping[str, object] | None,
    observed_at: str,
    *,
    batch_id: str | None,
    expected_date: str | None,
    require_snapshot: bool,
    completed: bool,
) -> None:
    connection.executemany(
        """
        INSERT INTO trend_curve_points
        (market, symbol, curve_date, price, temperature, strength, mom, yoy, bar,
         asset_id, group_id, tm_id, ccy_id, mom_delta, yoy_delta, yield_value)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (market, symbol, curve_date) DO UPDATE SET
            price = excluded.price,
            temperature = excluded.temperature,
            strength = excluded.strength,
            mom = excluded.mom,
            yoy = excluded.yoy,
            bar = excluded.bar,
            asset_id = excluded.asset_id,
            group_id = excluded.group_id,
            tm_id = excluded.tm_id,
            ccy_id = excluded.ccy_id,
            mom_delta = excluded.mom_delta,
            yoy_delta = excluded.yoy_delta,
            yield_value = excluded.yield_value
        """,
        [
            (
                target["market"],
                target["symbol"],
                point["curve_date"],
                point["price"],
                point["temperature"],
                point["strength"],
                point["mom"],
                point["yoy"],
                point["bar"],
                target["asset_id"],
                target["group_id"],
                target["tm_id"],
                target["ccy_id"],
                point["mom_delta"],
                point["yoy_delta"],
                point["yield_value"],
            )
            for point in points
        ],
    )
    if snapshot is not None:
        connection.execute(
            """
            INSERT INTO trend_curve_daily_snapshots
            (market, symbol, snapshot_date, solar_term, right_side_day,
             right_side_state, labels_json, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (market, symbol, snapshot_date) DO UPDATE SET
                solar_term = excluded.solar_term,
                right_side_day = excluded.right_side_day,
                right_side_state = excluded.right_side_state,
                labels_json = excluded.labels_json,
                observed_at = excluded.observed_at
            """,
            (
                target["market"],
                target["symbol"],
                snapshot["snapshot_date"],
                snapshot["solar_term"],
                snapshot["right_side_day"],
                snapshot["right_side_state"],
                snapshot["labels_json"],
                observed_at,
            ),
        )
    if batch_id is None:
        return
    point_rows = connection.execute(
        """
        SELECT curve_date, price, temperature, strength, mom, yoy, bar,
               asset_id, group_id, tm_id, ccy_id, mom_delta, yoy_delta, yield_value
        FROM trend_curve_points
        WHERE market = ? AND symbol = ?
        ORDER BY curve_date
        """,
        (target["market"], target["symbol"]),
    ).fetchall()
    point_dates = [str(point["curve_date"]) for point in points]
    point_date_set = set(point_dates)
    point_rows = [point_row for point_row in point_rows if point_row[0] in point_date_set]
    snapshot_date = str(snapshot["snapshot_date"]) if snapshot is not None else None
    snapshot_rows = []
    if snapshot_date is not None:
        snapshot_rows = connection.execute(
            """
            SELECT snapshot_date, solar_term, right_side_day, right_side_state, labels_json
            FROM trend_curve_daily_snapshots
            WHERE market = ? AND symbol = ? AND snapshot_date = ?
            """,
            (target["market"], target["symbol"], snapshot_date),
        ).fetchall()
    evidence = {
        "point_dates": point_dates,
        "point_digest": _rows_digest(point_rows),
        "snapshot_date": snapshot_date,
        "snapshot_digest": _rows_digest(snapshot_rows) if snapshot_rows else None,
        "expected_date": expected_date,
        "require_snapshot": require_snapshot,
    }
    connection.execute(
        """
        UPDATE trend_curve_batch_items
        SET point_count = ?, snapshot_date = ?, observed_at = ?, evidence_json = ?,
            completed_at = ?, issue_reason = ?
        WHERE batch_id = ? AND market = ? AND symbol = ?
        """,
        (
            len(points),
            snapshot_date,
            observed_at,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            observed_at if completed else None,
            None if completed else "snapshot_gap",
            batch_id,
            target["market"],
            target["symbol"],
        ),
    )


def _snapshot_gap_reason(
    snapshot: Mapping[str, object] | None,
    expected_date: str | None,
    require_snapshot: bool,
) -> str | None:
    if not require_snapshot:
        return None
    if snapshot is None:
        return "snapshot_missing"
    if expected_date is not None and snapshot.get("snapshot_date") != expected_date:
        return "snapshot_date_mismatch"
    return None


def _batch_counts(connection: sqlite3.Connection, batch_id: str) -> tuple[int, int]:
    completed = connection.execute(
        "SELECT COUNT(*) FROM trend_curve_batch_items WHERE batch_id = ? AND completed_at IS NOT NULL",
        (batch_id,),
    ).fetchone()[0]
    total = connection.execute(
        "SELECT COUNT(*) FROM trend_curve_batch_items WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()[0]
    return int(completed), int(total) - int(completed)


def reconcile_trend_curves(
    database: Path | str,
    targets: Sequence[Mapping[str, object]],
    *,
    api_key: str,
) -> tuple[str, str]:
    local_rows: list[dict[str, object]] = []
    with sqlite3.connect(Path(database).expanduser()) as connection:
        for target in targets:
            rows = connection.execute(
                """
                SELECT curve_date, temperature, strength
                FROM trend_curve_points
                WHERE market = ? AND symbol = ? AND tm_id = ?
                ORDER BY curve_date DESC
                LIMIT 2
                """,
                (target["market"], target["symbol"], target["tm_id"]),
            ).fetchall()
            if len(rows) < 2:
                raise ValueError(
                    f"{target['market']}.{target['symbol']} curve history is incomplete"
                )
            latest, previous = rows[0], rows[1]
            local_rows.append(
                {
                    "market": target["market"],
                    "symbol": target["symbol"],
                    "tm_id": target["tm_id"],
                    "curve_date": latest[0],
                    "previous_temperature": previous[1],
                    "current_temperature": latest[1],
                    "current_strength": latest[2],
                }
            )

    fields = (
        "tmId",
        "asOfDate",
        "trendTemperaturePrev",
        "trendTemperatureCurr",
        "trendStrengthLocalCurr",
    )
    snapshots_by_date: dict[str, list[dict[str, object]]] = {}
    issues: list[str] = []
    with TemporaryDirectory(prefix="open-trader-trend-reconcile-") as temporary_cache:
        client = TrendAnimalsClient(
            api_key=api_key,
            cache_dir=Path(temporary_cache),
        )
        for curve_date in sorted({row["curve_date"] for row in local_rows}):
            date_rows = [
                row for row in local_rows if row["curve_date"] == curve_date
            ]
            try:
                snapshots_by_date[curve_date] = client.get_snapshots(
                    tm_ids=[row["tm_id"] for row in date_rows],
                    fields=fields,
                    expected_date=curve_date,
                )
            except Exception as exc:
                issues.extend(
                    f"{row['market']}.{row['symbol']} {curve_date}：API 快照请求失败：{exc}"
                    for row in date_rows
                )

    missing_value = object()

    def shown(value: object) -> str:
        return "缺失" if value is missing_value else str(value)

    for curve_date in sorted({row["curve_date"] for row in local_rows}):
        snapshots = snapshots_by_date.get(curve_date)
        if snapshots is None:
            continue
        date_rows = [row for row in local_rows if row["curve_date"] == curve_date]
        expected_tm_ids = {row["tm_id"] for row in date_rows}
        rows_by_tm_id: dict[int, list[dict[str, object]]] = {}
        for snapshot in snapshots:
            snapshot_tm_id = snapshot.get("tmId", missing_value)
            if (
                isinstance(snapshot_tm_id, bool)
                or not isinstance(snapshot_tm_id, int)
                or snapshot_tm_id <= 0
            ):
                issues.append(
                    f"{curve_date}：API 快照 tmId 无效={shown(snapshot_tm_id)}"
                )
                continue
            if snapshot_tm_id not in expected_tm_ids:
                issues.append(
                    f"{curve_date}：API 快照意外标的 tmId={snapshot_tm_id}"
                )
                continue
            rows_by_tm_id.setdefault(snapshot_tm_id, []).append(snapshot)

        for row in date_rows:
            identity = f"{row['market']}.{row['symbol']} {curve_date}"
            matched_rows = rows_by_tm_id.get(row["tm_id"], [])
            if not matched_rows:
                issues.append(f"{identity}：API 快照缺失")
                continue
            if len(matched_rows) != 1:
                issues.append(
                    f"{identity}：API 快照重复 tmId={row['tm_id']}"
                )
                continue
            snapshot = matched_rows[0]
            if snapshot.get("asOfDate", missing_value) != curve_date:
                issues.append(
                    f"{identity}：API 日期={shown(snapshot.get('asOfDate', missing_value))}"
                )
                continue

            if snapshot.get("trendTemperaturePrev", missing_value) != row[
                "previous_temperature"
            ]:
                issues.append(
                    f"{identity} 前一温度：曲线={row['previous_temperature']}，"
                    f"API={shown(snapshot.get('trendTemperaturePrev', missing_value))}"
                )
            if snapshot.get("trendTemperatureCurr", missing_value) != row[
                "current_temperature"
            ]:
                issues.append(
                    f"{identity} 当前温度：曲线={row['current_temperature']}，"
                    f"API={shown(snapshot.get('trendTemperatureCurr', missing_value))}"
                )

            actual_strength = snapshot.get("trendStrengthLocalCurr", missing_value)
            try:
                parsed_strength = (
                    None
                    if isinstance(actual_strength, bool)
                    else Decimal(str(actual_strength))
                )
            except (InvalidOperation, TypeError, ValueError):
                parsed_strength = None
            if (
                parsed_strength is None
                or not parsed_strength.is_finite()
                or parsed_strength != Decimal(str(row["current_strength"]))
            ):
                issues.append(
                    f"{identity} 当前本地强度：曲线={row['current_strength']}，"
                    f"API={shown(actual_strength)}"
                )

    if issues:
        raise TrendCurveReconciliationError("\n".join(issues))

    dates = ", ".join(
        f"{market} {curve_date}"
        for market, curve_date in sorted(
            {(row["market"], row["curve_date"]) for row in local_rows}
        )
    )
    return (
        "趋势曲线采集对账一致",
        "\n".join(
            (
                f"数据日期：{dates}",
                f"标的：{len(local_rows)}/{len(local_rows)}",
                "对账字段：前一温度、当前温度、当前本地强度",
                "结果：全部一致",
            )
        ),
    )


def _load_portfolio_targets(
    portfolio: Path | str,
    mappings_root: Path,
) -> list[dict[str, object]]:
    if not isinstance(portfolio, (str, Path)):
        raise ValueError("portfolio must be a CSV path")
    exclusions = _load_portfolio_trend_curve_exclusions()
    selected: dict[tuple[str, str], str] = {}
    invalid_currency: set[str] = set()
    try:
        with Path(portfolio).expanduser().open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            for row in csv.DictReader(handle):
                market = str(row.get("market") or "").strip().upper()
                if market not in PORTFOLIO_MARKETS:
                    continue
                if str(row.get("ai_eligible") or "").strip().lower() != "true":
                    continue
                symbol = str(
                    row.get("analysis_symbol") or row.get("symbol") or ""
                ).strip().upper()
                identity = f"{market}.{symbol or '<blank>'}"
                if not symbol:
                    invalid_currency.add(identity)
                    continue
                if (market, symbol) in exclusions:
                    continue
                currency = str(row.get("currency") or "").strip().upper()
                expected_currency = PORTFOLIO_CURRENCIES[market]
                if currency and currency not in CURVE_CURRENCY_IDS:
                    invalid_currency.add(identity)
                    continue
                if currency and currency != expected_currency:
                    invalid_currency.add(identity)
                    continue
                selected.setdefault((market, symbol), expected_currency)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("portfolio is unreadable or malformed") from exc
    if invalid_currency:
        values = ", ".join(sorted(invalid_currency))
        raise ValueError(f"portfolio currency or symbol unsupported: {values}")

    mappings = _load_symbol_mappings(mappings_root)
    targets: list[dict[str, object]] = []
    unavailable: list[str] = []
    unsupported_assets: list[str] = []
    for (market, symbol), currency in sorted(selected.items()):
        matches = _matching_symbol_mappings(mappings, market, symbol)
        identity = f"{market}.{symbol}"
        if len(matches) != 1:
            unavailable.append(identity)
            continue
        mapping = matches[0]
        asset = mapping["asset"]
        if asset not in CURVE_GROUP_IDS or asset not in CURVE_ASSETS_BY_MARKET[market]:
            unsupported_assets.append(identity)
            continue
        targets.append(
            {
                "market": market,
                "symbol": symbol,
                "asset_id": CURVE_ASSET_ID,
                "group_id": CURVE_GROUP_IDS[asset],
                "tm_id": mapping["trend_animals_tm_id"],
                "ccy_id": CURVE_CURRENCY_IDS[currency],
            }
        )
    if unavailable:
        values = ", ".join(sorted(unavailable))
        raise ValueError(f"portfolio mapping unavailable: {values}")
    if unsupported_assets:
        values = ", ".join(sorted(unsupported_assets))
        raise ValueError(f"portfolio mapping asset unsupported: {values}")
    if not targets:
        raise ValueError("portfolio has no eligible CN/HK/US holdings")
    return targets


def _load_portfolio_trend_curve_exclusions() -> frozenset[tuple[str, str]]:
    try:
        payload = json.loads(
            DEFAULT_PORTFOLIO_TREND_CURVE_EXCLUSIONS.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "portfolio trend-curve exclusions are unreadable or malformed"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("portfolio trend-curve exclusions are unreadable or malformed")
    exclusions: set[tuple[str, str]] = set()
    for identity, reason in payload.items():
        if not isinstance(identity, str) or not isinstance(reason, str):
            raise ValueError(
                "portfolio trend-curve exclusions are unreadable or malformed"
            )
        if identity != identity.strip().upper() or not reason.strip():
            raise ValueError(
                "portfolio trend-curve exclusions are unreadable or malformed"
            )
        parts = identity.split(".", 1)
        if len(parts) != 2 or parts[0] not in PORTFOLIO_MARKETS or not parts[1]:
            raise ValueError(
                "portfolio trend-curve exclusions are unreadable or malformed"
            )
        exclusions.add((parts[0], parts[1]))
    return frozenset(exclusions)


def _load_symbol_mappings(mappings_root: Path) -> list[dict[str, object]]:
    mappings: list[dict[str, object]] = []
    by_futu: dict[tuple[str, str], dict[str, object]] = {}
    by_trend: dict[tuple[str, str], dict[str, object]] = {}
    by_tm_id: dict[tuple[str, int], dict[str, object]] = {}
    required = {
        "asset",
        "futu_symbol",
        "market",
        "schema_version",
        "trend_animals_symbol",
        "trend_animals_tm_id",
    }
    for market in sorted(PORTFOLIO_MARKETS):
        for path in sorted((mappings_root / market).glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("symbol mapping cache is unreadable or malformed") from exc
            if not isinstance(payload, dict) or not required.issubset(payload):
                raise ValueError("symbol mapping cache is malformed")
            futu_symbol = payload.get("futu_symbol")
            mapping_market = payload.get("market")
            asset = payload.get("asset")
            trend_symbol = payload.get("trend_animals_symbol")
            tm_id = payload.get("trend_animals_tm_id")
            try:
                canonical_futu = to_futu_symbol(market, futu_symbol)
                trend_futu = from_trend_animals_symbol(market, trend_symbol)
            except (AttributeError, ValueError):
                raise ValueError("symbol mapping cache is malformed") from None
            same_security = (
                canonical_futu.split(".", 1)[1] == trend_futu.split(".", 1)[1]
                if market == "CN"
                else canonical_futu == trend_futu
            )
            if (
                payload.get("schema_version") != TREND_SYMBOL_MAPPING_SCHEMA
                or mapping_market != market
                or not isinstance(futu_symbol, str)
                or not futu_symbol.strip()
                or path.stem != futu_symbol
                or not isinstance(trend_symbol, str)
                or not trend_symbol.strip()
                or not isinstance(asset, str)
                or isinstance(tm_id, bool)
                or not isinstance(tm_id, int)
                or tm_id <= 0
                or canonical_futu != futu_symbol
                or not same_security
            ):
                raise ValueError("symbol mapping cache is malformed")
            indexes = (
                (by_futu, (market, futu_symbol)),
                (by_trend, (market, trend_symbol)),
                (by_tm_id, (market, tm_id)),
            )
            mapping_identity = (futu_symbol, trend_symbol, tm_id, asset)
            for index, key in indexes:
                previous = index.get(key)
                if previous is not None and (
                    previous["futu_symbol"],
                    previous["trend_animals_symbol"],
                    previous["trend_animals_tm_id"],
                    previous["asset"],
                ) != mapping_identity:
                    raise ValueError("symbol mapping conflict")
            for index, key in indexes:
                index[key] = payload
            mappings.append(payload)
    return mappings


def _matching_symbol_mappings(
    mappings: Sequence[dict[str, object]], market: str, symbol: str
) -> list[dict[str, object]]:
    normalized = symbol.strip().upper()
    if "." in normalized and normalized.split(".", 1)[0] in {
        "CN",
        "HK",
        "US",
        "SH",
        "SZ",
        "BJ",
    }:
        return [
            mapping
            for mapping in mappings
            if mapping["market"] == market and mapping["futu_symbol"] == normalized
        ]
    return [
        mapping
        for mapping in mappings
        if mapping["market"] == market
        and isinstance(mapping["futu_symbol"], str)
        and mapping["futu_symbol"].split(".", 1)[1] == normalized
    ]


def load_cached_trend_curve_targets(
    mappings_root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Load the supported local mapping cache as an explicitly cached scope."""
    root = Path(mappings_root or DEFAULT_MAPPINGS_ROOT).expanduser()
    mappings = _load_symbol_mappings(root)
    targets: list[dict[str, object]] = []
    for mapping in mappings:
        market = mapping["market"]
        asset = mapping["asset"]
        if (
            not isinstance(market, str)
            or market not in PORTFOLIO_MARKETS
            or asset not in CURVE_ASSETS_BY_MARKET[market]
        ):
            continue
        futu_symbol = mapping["futu_symbol"]
        assert isinstance(futu_symbol, str)
        targets.append(
            {
                "market": market,
                "symbol": futu_symbol.split(".", 1)[1],
                "asset_id": CURVE_ASSET_ID,
                "group_id": CURVE_GROUP_IDS[asset],
                "tm_id": mapping["trend_animals_tm_id"],
                "ccy_id": CURVE_CURRENCY_IDS[PORTFOLIO_CURRENCIES[market]],
            }
        )
    _reject_duplicate_targets(targets)
    if not targets:
        raise ValueError("cached trend-curve mapping scope is empty")
    return sorted(targets, key=_target_identity)


def _daily_batch_state(
    database: Path,
    batch_id: str,
    manifest: str,
    targets: Sequence[Mapping[str, object]],
) -> tuple[int, int] | None:
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT manifest FROM trend_curve_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            if row[0] != manifest:
                raise ValueError("batch request does not match frozen batch")
            completed = sum(
                _batch_item_is_complete(connection, batch_id, target, True)
                for target in targets
            )
            return completed, len(targets) - completed
    except sqlite3.Error:
        return None


def _daily_not_due_result(
    *,
    batch_id: str,
    observation_date: str,
    target_count: int,
    provider_date_range: Mapping[str, str | None],
) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "observation_date": observation_date,
        "coverage": "cached",
        "freshness": "unknown",
        "status": "not_due",
        "target_count": target_count,
        "completed_count": 0,
        "pending_count": 0,
        "request_count": 0,
        "stop_reason": "before_noon",
        "provider_date_range": dict(provider_date_range),
        "issues": [],
        "delivery_status": "not_run",
    }


def _daily_paused_result(
    *,
    batch_id: str,
    observation_date: str,
    target_count: int,
    completed_count: int,
    pending_count: int,
    provider_date_range: Mapping[str, str | None],
) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "observation_date": observation_date,
        "coverage": "cached",
        "freshness": "unknown",
        "status": "paused",
        "target_count": target_count,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "request_count": 0,
        "stop_reason": "manual_pause",
        "provider_date_range": dict(provider_date_range),
        "issues": [],
        "control_status": "paused",
        "delivery_status": "not_run",
    }


def _daily_waiting_after_failure_result(
    *,
    batch_id: str,
    observation_date: str,
    target_count: int,
    completed_count: int,
    pending_count: int,
    provider_date_range: Mapping[str, str | None],
    issues: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "observation_date": observation_date,
        "coverage": "cached",
        "freshness": "unknown",
        "status": "partial",
        "target_count": target_count,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "request_count": 0,
        "stop_reason": "data_gap",
        "provider_date_range": dict(provider_date_range),
        "issues": [dict(issue) for issue in issues],
        "failure_state": "waiting",
        "delivery_status": "not_run",
    }


def _daily_provider_date_range(
    database: Path,
    targets: Sequence[Mapping[str, object]],
) -> dict[str, str | None]:
    dates: list[str] = []
    if database.is_file():
        try:
            with sqlite3.connect(database) as connection:
                for target in targets:
                    rows = connection.execute(
                        "SELECT curve_date FROM trend_curve_points "
                        "WHERE market = ? AND symbol = ?",
                        (target["market"], target["symbol"]),
                    ).fetchall()
                    dates.extend(str(row[0]) for row in rows if row[0] is not None)
        except sqlite3.Error:
            dates = []
    return {
        "start": min(dates) if dates else None,
        "end": max(dates) if dates else None,
    }


def _daily_auth_state_path(database: Path) -> Path:
    return Path(f"{database}.daily-auth.json")


def _daily_failure_state_path(database: Path) -> Path:
    return Path(f"{database}.daily-failure.json")


def _daily_control_state_path(database: Path) -> Path:
    return Path(f"{database}.daily-control.json")


def _read_daily_control_state(database: Path) -> str | None:
    path = _daily_control_state_path(database)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("daily control state is unreadable or malformed") from exc
    if not isinstance(payload, Mapping) or payload.get("status") != "paused":
        raise ValueError("daily control state is unreadable or malformed")
    if payload.get("reason") != "manual":
        raise ValueError("daily control state is unreadable or malformed")
    return "paused"


def pause_daily_trend_curve(database: Path | str) -> dict[str, str]:
    target_database = Path(database).expanduser()
    target_database.parent.mkdir(parents=True, exist_ok=True)
    path = _daily_control_state_path(target_database)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {"status": "paused", "reason": "manual"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise ValueError("daily control state could not be saved") from exc
    return {"status": "paused", "reason": "manual"}


def resume_daily_trend_curve(database: Path | str) -> dict[str, str]:
    target_database = Path(database).expanduser()
    try:
        _daily_control_state_path(target_database).unlink()
    except FileNotFoundError:
        pass
    return {"status": "resumed", "reason": "manual"}


def _daily_credential_fingerprint(
    credentials: WechatMiniCredentials | tuple[object, object],
) -> str:
    token, user_id = _normalize_credentials(credentials)
    return hashlib.sha256(f"{token}\0{user_id}".encode("utf-8")).hexdigest()


def _read_daily_auth_state(
    database: Path,
) -> tuple[str, str, list[dict[str, object]]] | None:
    path = _daily_auth_state_path(database)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("daily auth state is unreadable or malformed") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("daily auth state is unreadable or malformed")
    stored_batch_id = payload.get("batch_id")
    if not isinstance(stored_batch_id, str) or not stored_batch_id.strip():
        raise ValueError("daily auth state is unreadable or malformed")
    fingerprint = payload.get("credential_fingerprint")
    raw_issues = payload.get("issues", [])
    if (
        payload.get("status") != "auth_blocked"
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
        or not isinstance(raw_issues, list)
        or any(not isinstance(issue, Mapping) for issue in raw_issues)
    ):
        raise ValueError("daily auth state is unreadable or malformed")
    return stored_batch_id, fingerprint, [dict(issue) for issue in raw_issues]


def _write_daily_auth_state(
    database: Path,
    batch_id: str,
    fingerprint: str,
    issues: Sequence[Mapping[str, object]],
) -> None:
    path = _daily_auth_state_path(database)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "batch_id": batch_id,
        "credential_fingerprint": fingerprint,
        "status": "auth_blocked",
        "issues": [dict(issue) for issue in issues],
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise ValueError("daily auth state could not be saved") from exc


def _clear_daily_auth_state(database: Path) -> None:
    try:
        _daily_auth_state_path(database).unlink()
    except FileNotFoundError:
        pass


def _read_daily_failure_state(
    database: Path,
) -> tuple[str, str, list[dict[str, object]]] | None:
    path = _daily_failure_state_path(database)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("daily failure state is unreadable or malformed") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("daily failure state is unreadable or malformed")
    batch_id = payload.get("batch_id")
    observation_date = payload.get("observation_date")
    raw_issues = payload.get("issues")
    if (
        payload.get("status") != "data_gap"
        or not isinstance(batch_id, str)
        or not batch_id.strip()
        or not isinstance(observation_date, str)
        or not observation_date.strip()
        or not isinstance(raw_issues, list)
        or any(
            not isinstance(issue, Mapping)
            or not isinstance(issue.get("market"), str)
            or not isinstance(issue.get("symbol"), str)
            or not isinstance(issue.get("reason"), str)
            for issue in raw_issues
        )
    ):
        raise ValueError("daily failure state is unreadable or malformed")
    return batch_id, observation_date, [dict(issue) for issue in raw_issues]


def _write_daily_failure_state(
    database: Path,
    batch_id: str,
    observation_date: str,
    issues: Sequence[Mapping[str, object]],
) -> None:
    _write_daily_json(
        _daily_failure_state_path(database),
        {
            "batch_id": batch_id,
            "observation_date": observation_date,
            "status": "data_gap",
            "issues": [dict(issue) for issue in issues],
        },
    )


def _clear_daily_failure_state(database: Path) -> None:
    try:
        _daily_failure_state_path(database).unlink()
    except FileNotFoundError:
        pass


def _daily_pending_targets(
    database: Path,
    batch_id: str,
    targets: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    if not database.is_file():
        return [dict(target) for target in targets]
    pending: list[dict[str, object]] = []
    try:
        with sqlite3.connect(database) as connection:
            for target in targets:
                if not _batch_item_is_complete(connection, batch_id, target, True):
                    pending.append(dict(target))
    except sqlite3.Error:
        return [dict(target) for target in targets]
    return pending


def _daily_previous_pending_targets(
    database: Path,
    observation_date: str,
    targets: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    if not database.is_file():
        return []
    try:
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT batch_id, manifest, require_snapshot "
                "FROM trend_curve_batches "
                "WHERE batch_id LIKE 'trend-curve-daily-cached-%' "
                "ORDER BY batch_id DESC"
            ).fetchall()
    except sqlite3.Error:
        return []
    prefix = "trend-curve-daily-cached-"
    current_date = date.fromisoformat(observation_date)
    for candidate_batch_id, raw_manifest, raw_require_snapshot in rows:
        if not isinstance(candidate_batch_id, str):
            continue
        candidate_date_text = candidate_batch_id.removeprefix(prefix)
        try:
            candidate_date = date.fromisoformat(candidate_date_text)
        except ValueError:
            continue
        if candidate_date >= current_date:
            continue
        try:
            manifest = json.loads(raw_manifest)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, Mapping):
            continue
        frozen_targets = manifest.get("targets")
        manifest_require_snapshot = manifest.get("require_snapshot")
        if (
            not isinstance(frozen_targets, list)
            or not frozen_targets
            or not isinstance(manifest_require_snapshot, bool)
            or raw_require_snapshot not in (0, 1)
            or int(manifest_require_snapshot) != raw_require_snapshot
        ):
            continue
        try:
            frozen_targets = [
                {
                    "market": target["market"],
                    "symbol": target["symbol"],
                    "asset_id": target["asset_id"],
                    "group_id": target["group_id"],
                    "tm_id": target["tm_id"],
                    "ccy_id": target["ccy_id"],
                }
                for target in frozen_targets
                if isinstance(target, Mapping)
            ]
            _reject_duplicate_targets(frozen_targets)
        except (KeyError, TypeError, ValueError):
            continue
        try:
            pending_ids = {
                _target_identity(target)
                for target in frozen_targets
                if not _batch_item_is_complete(
                    connection,
                    candidate_batch_id,
                    target,
                    manifest_require_snapshot,
                )
            }
        except sqlite3.Error:
            return []
        if pending_ids:
            # A genuine older gap is authoritative even when today's scope
            # has no matching identity; do not fall back to an older batch.
            return [
                dict(target)
                for target in targets
                if _target_identity(target) in pending_ids
            ]
    return []


def _daily_summary_path(database: Path, batch_id: str) -> Path:
    root = database.parent / "trend_curve_daily"
    root.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.time_ns()}-{os.getpid()}"
    return root / f"{batch_id}-{stamp}.json"


def _write_daily_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise ValueError("daily summary could not be saved") from exc


def _daily_issue_counts(issues: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        reason = issue.get("reason")
        if isinstance(reason, str) and reason:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _daily_delivery_status(
    executable: Path,
    summary_path: Path,
    timeout_seconds: float,
) -> str:
    argv = [
        str(executable.resolve()),
        "send",
        "--to",
        "feishu",
        "--subject",
        "Trend curve daily",
        "--file",
        str(summary_path.resolve()),
        "--json",
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return "unknown"
    except InterruptedError:
        # The child may have accepted the file before the interruption was
        # observed locally; retain the conservative pre-send state.
        return "unknown"
    except (OSError, UnicodeError):
        return "failed"
    if completed.returncode != 0:
        return "failed"
    try:
        response = json.loads(completed.stdout)
    except (TypeError, UnicodeError, json.JSONDecodeError):
        return "failed"
    if not isinstance(response, Mapping):
        return "failed"
    if response.get("error") or response.get("skipped") is True:
        return "failed"
    if (
        response.get("success") is True
        and response.get("platform") == "feishu"
        and isinstance(response.get("message_id"), str)
        and bool(response["message_id"].strip())
    ):
        return "accepted"
    return "failed"


def _validate_daily_runtime_config(
    *,
    request_interval_seconds: float,
    request_limit: int,
    max_duration_seconds: float,
    hermes_timeout_seconds: float,
    hermes_executable: Path | str,
) -> Path:
    if (
        isinstance(request_interval_seconds, bool)
        or not isinstance(request_interval_seconds, (int, float))
        or not math.isfinite(float(request_interval_seconds))
        or request_interval_seconds <= 0
        or request_interval_seconds > DAILY_REQUEST_INTERVAL_MAX_SECONDS
    ):
        raise ValueError("daily request interval must be finite, positive, and bounded")
    if (
        isinstance(request_limit, bool)
        or not isinstance(request_limit, int)
        or request_limit <= 0
        or request_limit > DAILY_REQUEST_LIMIT_MAX
    ):
        raise ValueError("daily request limit must be a positive bounded integer")
    if (
        isinstance(max_duration_seconds, bool)
        or not isinstance(max_duration_seconds, (int, float))
        or not math.isfinite(float(max_duration_seconds))
        or max_duration_seconds <= 0
        or max_duration_seconds > DAILY_MAX_DURATION_MAX_SECONDS
    ):
        raise ValueError("daily max duration must be finite, positive, and bounded")
    if (
        isinstance(hermes_timeout_seconds, bool)
        or not isinstance(hermes_timeout_seconds, (int, float))
        or not math.isfinite(float(hermes_timeout_seconds))
        or hermes_timeout_seconds <= 0
        or hermes_timeout_seconds > DAILY_HERMES_TIMEOUT_MAX_SECONDS
    ):
        raise ValueError("daily Hermes timeout must be finite, positive, and bounded")
    executable = Path(hermes_executable).expanduser()
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ValueError("daily Hermes executable is unavailable")
    return executable


def run_daily_trend_curve(
    *,
    mappings_root: Path | str | None = None,
    database: Path | str | None = None,
    mmkv_path: Path | str | None = None,
    mmkv_helper: Path | str | None = None,
    storage_root: Path | str | None = None,
    request_interval_seconds: float,
    request_limit: int,
    max_duration_seconds: float,
    hermes_timeout_seconds: float,
    hermes_executable: Path | str,
    check: bool = False,
    now: datetime,
) -> dict[str, object]:
    """Run one frozen, cached-scope daily collection."""
    hermes_executable = _validate_daily_runtime_config(
        request_interval_seconds=request_interval_seconds,
        request_limit=request_limit,
        max_duration_seconds=max_duration_seconds,
        hermes_timeout_seconds=hermes_timeout_seconds,
        hermes_executable=hermes_executable,
    )
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("daily trend-curve clock must be timezone-aware")
    observation_date = now.astimezone(SHANGHAI).date().isoformat()
    batch_id = f"trend-curve-daily-cached-{observation_date}"
    target_database = Path(database or "data/trend_curve/history.sqlite3").expanduser()
    control_state = _read_daily_control_state(target_database)
    targets = load_cached_trend_curve_targets(mappings_root)
    manifest = _batch_manifest(targets, True, {})
    state = _daily_batch_state(target_database, batch_id, manifest, targets)
    if control_state == "paused":
        if state is None:
            completed_count, pending_count = 0, len(targets)
        else:
            completed_count, pending_count = state
        return _daily_paused_result(
            batch_id=batch_id,
            observation_date=observation_date,
            target_count=len(targets),
            completed_count=completed_count,
            pending_count=pending_count,
            provider_date_range=_daily_provider_date_range(target_database, targets),
        )
    local_now = now.astimezone(SHANGHAI)
    if state is not None and state[1] == 0:
        completed_count, pending_count = state
        return {
            "batch_id": batch_id,
            "observation_date": observation_date,
            "coverage": "cached",
            "freshness": "unknown",
            "status": "complete",
            "target_count": len(targets),
            "completed_count": completed_count,
            "pending_count": pending_count,
            "request_count": 0,
            "stop_reason": None,
            "provider_date_range": _daily_provider_date_range(
                target_database, targets
            ),
            "issues": [],
            "delivery_status": "not_run",
        }
    auth_state = _read_daily_auth_state(target_database)
    if (
        check
        and local_now.hour < DAILY_START_HOUR
        and state is None
        and auth_state is None
    ):
        return _daily_not_due_result(
            batch_id=batch_id,
            observation_date=observation_date,
            target_count=len(targets),
            provider_date_range=_daily_provider_date_range(target_database, targets),
        )

    failure_state = _read_daily_failure_state(target_database)
    if (
        check
        and failure_state is not None
        and failure_state[0] == batch_id
        and failure_state[1] == observation_date
    ):
        if state is None:
            completed_count, pending_count = 0, len(targets)
        else:
            completed_count, pending_count = state
        return _daily_waiting_after_failure_result(
            batch_id=batch_id,
            observation_date=observation_date,
            target_count=len(targets),
            completed_count=completed_count,
            pending_count=pending_count,
            provider_date_range=_daily_provider_date_range(
                target_database, targets
            ),
            issues=failure_state[2],
        )

    previous_pending = _daily_previous_pending_targets(
        target_database, observation_date, targets
    )
    if previous_pending:
        previous_pending_ids = {
            _target_identity(target) for target in previous_pending
        }
        targets = [
            *[target for target in targets if _target_identity(target) in previous_pending_ids],
            *[target for target in targets if _target_identity(target) not in previous_pending_ids],
        ]

    credentials = read_wechat_mini_credentials(
        mmkv_path,
        helper_path=mmkv_helper,
        storage_root=storage_root,
    )
    fingerprint = _daily_credential_fingerprint(credentials)
    if auth_state is not None and auth_state[1] == fingerprint:
        if state is None:
            completed_count, pending_count = 0, len(targets)
        else:
            completed_count, pending_count = state
        return {
            "batch_id": batch_id,
            "observation_date": observation_date,
            "coverage": "cached",
            "freshness": "unknown",
            "status": "auth_blocked",
            "target_count": len(targets),
            "completed_count": completed_count,
            "pending_count": pending_count,
            "request_count": 0,
            "stop_reason": "auth_blocked",
            "provider_date_range": _daily_provider_date_range(
                target_database, targets
            ),
            "auth_blocked_batch_id": auth_state[0],
            "issues": auth_state[2],
            "delivery_status": "not_run",
        }

    with TemporaryDirectory(prefix="open-trader-trend-daily-") as temporary:
        watchlist = Path(temporary) / "targets.json"
        watchlist.write_text(json.dumps(targets, ensure_ascii=False), encoding="utf-8")
        result = collect_trend_curves(
            watchlist=watchlist,
            database=target_database,
            credentials=credentials,
            batch_id=batch_id,
            require_snapshot=True,
            observed_at=now,
            # The daily path intentionally reports provider dates as unknown
            # freshness unless an independently verified date is supplied.
            expected_dates=None,
            request_limit=request_limit,
            request_interval_seconds=request_interval_seconds,
            max_duration_seconds=max_duration_seconds,
        )
    completed_count = result.completed_count
    pending_count = result.pending_count
    result_status = result.status
    request_count = result.request_count
    stop_reason = result.stop_reason
    issues = [dict(issue) for issue in result.issues]
    if result_status == "auth_blocked":
        _write_daily_auth_state(target_database, batch_id, fingerprint, issues)
    else:
        _clear_daily_auth_state(target_database)

    provider_date_range = _daily_provider_date_range(target_database, targets)
    pending_targets = _daily_pending_targets(target_database, batch_id, targets)
    safe_issues = [
        {
            "market": issue.get("market"),
            "symbol": issue.get("symbol"),
            "reason": issue.get("reason"),
        }
        for issue in issues
        if isinstance(issue.get("market"), str)
        and isinstance(issue.get("symbol"), str)
        and isinstance(issue.get("reason"), str)
    ]
    if any(issue["reason"] == "data_gap" for issue in safe_issues):
        stop_reason = "data_gap"
        _write_daily_failure_state(
            target_database, batch_id, observation_date, safe_issues
        )
    else:
        _clear_daily_failure_state(target_database)
    summary_path = _daily_summary_path(target_database, batch_id)
    gap_file: Path | None = None
    if pending_targets:
        gap_file = summary_path.with_name(f"{batch_id}.gaps.json")
        issue_reasons = {
            (issue["market"], issue["symbol"]): issue["reason"]
            for issue in safe_issues
        }
        _write_daily_json(
            gap_file,
            {
                "schema_version": "open_trader.trend_curve_daily.gaps.v1",
                "batch_id": batch_id,
                "observation_date": observation_date,
                "pending_targets": [
                    {
                        "market": target["market"],
                        "symbol": target["symbol"],
                        "reason": issue_reasons.get(
                            (target["market"], target["symbol"]),
                            stop_reason or "pending",
                        ),
                    }
                    for target in pending_targets
                ],
                "stop_reason": stop_reason,
            },
        )

    summary: dict[str, object] = {
        "schema_version": "open_trader.trend_curve_daily.summary.v1",
        "batch_id": batch_id,
        "observation_date": observation_date,
        "coverage": "cached",
        "freshness": "unknown",
        "provider_date_range": provider_date_range,
        "target_count": len(targets),
        "completed_count": completed_count,
        "pending_count": pending_count,
        "request_count": request_count,
        "request_limit": request_limit,
        "request_interval_seconds": request_interval_seconds,
        "max_duration_seconds": max_duration_seconds,
        "stop_reason": stop_reason,
        "issue_counts": _daily_issue_counts(safe_issues),
        "gap_file": str(gap_file.resolve()) if gap_file is not None else None,
        # Unknown is durable before crossing the external Hermes boundary.
        # An interruption can happen after Hermes has received the file.
        "delivery_status": "unknown",
        "delivery_provider": None,
    }
    delivery_status = "failed"
    try:
        _write_daily_json(summary_path, summary)
        delivery_status = _daily_delivery_status(
            Path(hermes_executable), summary_path, hermes_timeout_seconds
        )
        summary["delivery_status"] = delivery_status
        summary["delivery_provider"] = "feishu" if delivery_status == "accepted" else None
        _write_daily_json(summary_path, summary)
    except ValueError:
        # Data and progress are already committed; a missing summary is a
        # delivery failure, never a reason to roll back collection.
        delivery_status = "failed"

    return {
        "batch_id": batch_id,
        "observation_date": observation_date,
        "coverage": "cached",
        "freshness": "unknown",
        "status": result_status,
        "target_count": len(targets),
        "completed_count": completed_count,
        "pending_count": pending_count,
        "request_count": request_count,
        "stop_reason": stop_reason,
        "provider_date_range": provider_date_range,
        "issues": issues,
        "summary_path": str(summary_path.resolve()),
        "gap_file": str(gap_file.resolve()) if gap_file is not None else None,
        "delivery_status": delivery_status,
    }


def _load_watchlist(watchlist: Path | str) -> list[dict[str, object]]:
    if isinstance(watchlist, (str, Path)):
        try:
            payload = json.loads(Path(watchlist).expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("watchlist is unreadable or malformed") from exc
    else:
        raise ValueError("watchlist must be a JSON path")
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)) or not payload:
        raise ValueError("watchlist must be a nonempty list")
    targets: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("watchlist contains an invalid target")
        market = item.get("market")
        symbol = item.get("symbol")
        if not isinstance(market, str) or market.strip().upper() not in {"CN", "HK", "US"}:
            raise ValueError("watchlist contains an invalid market")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("watchlist contains an invalid symbol")
        target: dict[str, object] = {
            "market": market.strip().upper(),
            "symbol": symbol.strip().upper(),
        }
        for name in ("asset_id", "group_id", "tm_id", "ccy_id"):
            value = item.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("watchlist contains an invalid target id")
            target[name] = value
        targets.append(target)
    return targets


def _normalize_credentials(
    credentials: WechatMiniCredentials | tuple[object, object],
) -> tuple[str, int | str]:
    if not isinstance(credentials, tuple) or len(credentials) != 2:
        raise ValueError("credentials are malformed")
    token, user_id = credentials
    if not isinstance(token, str) or not token.strip():
        raise ValueError("credentials are malformed")
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        raise ValueError("credentials are malformed")
    if isinstance(user_id, str) and not user_id.strip():
        raise ValueError("credentials are malformed")
    if isinstance(user_id, int) and user_id <= 0:
        raise ValueError("credentials are malformed")
    return token, user_id


def _default_curve_transport(
    url: str, body: bytes, headers: dict[str, str]
) -> Mapping[str, object]:
    connection = HTTPSConnection("www.trendtrader.cn", timeout=30)
    try:
        connection.request(
            "POST",
            "/mall4cloud_breed/breed/getVarietyCurve_V3",
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("curve request returned unexpected status")
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
    if not isinstance(payload, Mapping):
        raise ValueError("curve response is malformed")
    return payload


def _encrypted_response(response: Mapping[str, object]) -> str:
    if isinstance(response, Mapping) and response.get("code") == "A00004":
        raise _TrendCurveAuthBlocked("Trend Animals authentication blocked")
    if not isinstance(response, Mapping) or response.get("success") is not True or response.get("code") != "00000":
        raise ValueError("curve response was unsuccessful")
    data = response.get("data")
    encrypted = data.get("encryptedData") if isinstance(data, Mapping) else None
    if not isinstance(encrypted, str) or not encrypted.strip():
        raise ValueError("curve response has no encrypted data")
    return encrypted.strip()


def _decrypt_curve_response(encrypted: str) -> object:
    try:
        ciphertext = base64.b64decode(encrypted, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("curve response encryption is malformed") from exc
    try:
        completed = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-ecb", "-K", CURVE_AES_KEY.encode("ascii").hex()],
            input=ciphertext,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError("curve response decryption failed") from exc
    if completed.returncode != 0:
        raise ValueError("curve response decryption failed")
    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("curve response payload is malformed") from exc


def _validated_curve_response(
    payload: object,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    if isinstance(payload, Mapping) and payload.get("code") == "A00004":
        raise _TrendCurveAuthBlocked("Trend Animals authentication blocked")
    if not isinstance(payload, Mapping) or payload.get("code") != "00000":
        raise ValueError("curve response payload was unsuccessful")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) not in {4, 5}:
        raise ValueError("Trend Animals curve payload is malformed")
    history = None
    if isinstance(data[2], list):
        history = data[2]
    elif isinstance(data[2], Mapping):
        history = data[2].get("touchDetail")
    if not isinstance(history, list) or not history:
        raise ValueError("curve response history is empty")
    points: list[dict[str, object]] = []
    previous_date: date | None = None
    for item in history:
        if not isinstance(item, Mapping):
            raise ValueError("curve response history is malformed")
        try:
            rq = Decimal(str(item["rq"]))
            timestamp = int(rq)
            point_date = datetime.fromtimestamp(timestamp / 1000, tz=SHANGHAI).date()
            price = Decimal(str(item["px"]))
            strength = Decimal(str(item["rps"]))
        except (KeyError, InvalidOperation, TypeError, ValueError, OverflowError, OSError) as exc:
            raise ValueError("curve response history is malformed") from exc
        temperature = item.get("temperature")
        if (
            not rq.is_finite() or rq <= 0 or rq != timestamp
            or not price.is_finite() or price <= 0
            or not strength.is_finite()
            or not isinstance(temperature, str) or temperature not in TEMPERATURES
        ):
            raise ValueError("curve response history is malformed")
        if previous_date is not None and point_date <= previous_date:
            raise ValueError("curve response history date order is invalid")
        points.append(
            {
                "curve_date": point_date.isoformat(),
                "price": str(price),
                "temperature": temperature,
                "strength": str(strength),
                "mom": _optional_curve_value(item.get("mom")),
                "yoy": _optional_curve_value(item.get("yoy")),
                "bar": _optional_curve_value(item.get("bar")),
                "mom_delta": _optional_curve_text(item.get("momDelta")),
                "yoy_delta": _optional_curve_text(item.get("yoyDelta")),
                "yield_value": _optional_curve_value(item.get("yield")),
            }
        )
        previous_date = point_date
    return points, _validated_current_snapshot(data, points[-1]["curve_date"])


def _validated_curve_points(payload: object) -> list[dict[str, object]]:
    return _validated_curve_response(payload)[0]


def _validated_current_snapshot(
    data: list[object], latest_date: object
) -> dict[str, object] | None:
    if len(data) < 2:
        return None
    labels_section, summary_section = data[0], data[1]
    if (labels_section is None and summary_section is None) or (
        labels_section == {} and summary_section == {}
    ):
        return None
    if (
        labels_section is None
        or summary_section is None
        or labels_section == {}
        or summary_section == {}
    ):
        raise ValueError("curve response current snapshot is malformed")
    if not isinstance(labels_section, list) or not isinstance(summary_section, list):
        raise ValueError("curve response current snapshot is malformed")
    if len(summary_section) != 1 or not isinstance(summary_section[0], Mapping):
        raise ValueError("curve response current snapshot is malformed")
    summary = summary_section[0]
    try:
        rq = Decimal(str(summary["rq"]))
        timestamp = int(rq)
        snapshot_date = datetime.fromtimestamp(timestamp / 1000, tz=SHANGHAI).date()
    except (KeyError, InvalidOperation, TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("curve response current snapshot is malformed") from exc
    if (
        not rq.is_finite()
        or rq <= 0
        or rq != timestamp
        or snapshot_date.isoformat() != latest_date
    ):
        raise ValueError("curve response current snapshot is malformed")

    trend = summary.get("下行趋势")
    if not isinstance(trend, str):
        raise ValueError("curve response current snapshot is malformed")
    parts = trend.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n").split("\n", 1)
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError("curve response current snapshot is malformed")
    solar_term, right_side = (part.strip() for part in parts)
    solar_term = solar_term or None
    right_side_day: int | None = None
    right_side_state: str | None = right_side
    if right_side.startswith("右侧第") and right_side.endswith("天"):
        day_text = right_side[3:-1]
        if not day_text.isdigit() or int(day_text) <= 0:
            raise ValueError("curve response current snapshot is malformed")
        right_side_day = int(day_text)
        right_side_state = None

    labels: list[str] = []
    for item in labels_section:
        if not isinstance(item, Mapping):
            raise ValueError("curve response current snapshot is malformed")
        label_name = item.get("labelName")
        if not isinstance(label_name, str) or not label_name.strip():
            raise ValueError("curve response current snapshot is malformed")
        normalized_name = label_name.strip()
        if normalized_name != "温转热":
            labels.append(normalized_name)
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "solar_term": solar_term,
        "right_side_day": right_side_day,
        "right_side_state": right_side_state,
        "labels_json": json.dumps(labels, ensure_ascii=False, separators=(",", ":")),
    }


def _optional_curve_value(value: object) -> str | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("curve response history is malformed") from exc
    if not parsed.is_finite():
        raise ValueError("curve response history is malformed")
    return str(parsed)


def _optional_curve_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("curve response history is malformed")
    return value.strip()


def _ensure_curve_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_curve_points (
            market TEXT NOT NULL CHECK (market IN ('CN', 'HK', 'US')),
            symbol TEXT NOT NULL,
            curve_date TEXT NOT NULL,
            price TEXT NOT NULL,
            temperature TEXT NOT NULL
                CHECK (temperature IN ('冻', '寒', '凉', '平', '温', '热', '沸')),
            strength TEXT NOT NULL,
            mom TEXT,
            yoy TEXT,
            bar TEXT,
            asset_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            tm_id INTEGER NOT NULL,
            ccy_id INTEGER NOT NULL,
            mom_delta TEXT,
            yoy_delta TEXT,
            yield_value TEXT,
            PRIMARY KEY (market, symbol, curve_date)
        )
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(trend_curve_points)")
    }
    for column in ("mom_delta", "yoy_delta", "yield_value"):
        if column not in columns:
            connection.execute(f"ALTER TABLE trend_curve_points ADD COLUMN {column} TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_curve_daily_snapshots (
            market TEXT NOT NULL CHECK (market IN ('CN', 'HK', 'US')),
            symbol TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            solar_term TEXT,
            right_side_day INTEGER,
            right_side_state TEXT,
            labels_json TEXT NOT NULL,
            observed_at TEXT,
            PRIMARY KEY (market, symbol, snapshot_date)
        )
        """
    )
    snapshot_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(trend_curve_daily_snapshots)"
        )
    }
    if "observed_at" not in snapshot_columns:
        connection.execute(
            "ALTER TABLE trend_curve_daily_snapshots ADD COLUMN observed_at TEXT"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_curve_batches (
            batch_id TEXT PRIMARY KEY,
            manifest TEXT NOT NULL,
            require_snapshot INTEGER NOT NULL,
            expected_dates_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_curve_batch_items (
            batch_id TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            tm_id INTEGER NOT NULL,
            ccy_id INTEGER NOT NULL,
            expected_date TEXT,
            point_count INTEGER,
            snapshot_date TEXT,
            observed_at TEXT,
            evidence_json TEXT,
            completed_at TEXT,
            issue_reason TEXT,
            PRIMARY KEY (batch_id, market, symbol),
            FOREIGN KEY (batch_id) REFERENCES trend_curve_batches(batch_id)
        )
        """
    )
