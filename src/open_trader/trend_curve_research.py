"""Manual Trend Animals curve collection."""

from __future__ import annotations

import base64
import csv
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Mapping, NamedTuple, Sequence
from zoneinfo import ZoneInfo

from .futu_symbols import from_trend_animals_symbol, to_futu_symbol


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
PORTFOLIO_TREND_CURVE_EXCLUSIONS = frozenset({("US", "AGRZ")})
CURVE_ASSETS_BY_MARKET = {
    "CN": frozenset({"A股", "ETF基金"}),
    "HK": frozenset({"港股", "香港ETF"}),
    "US": frozenset({"美股", "美国ETF"}),
}
TREND_SYMBOL_MAPPING_SCHEMA = "open_trader.trend_symbol_mapping.v1"
TEMPERATURES = frozenset({"冻", "寒", "凉", "平", "温", "热", "沸"})
SHANGHAI = ZoneInfo("Asia/Shanghai")

CurveTransport = Callable[[str, bytes, dict[str, str]], Mapping[str, object]]


class WechatMiniCredentials(NamedTuple):
    token: str
    user_id: int | str


@dataclass(frozen=True)
class CollectionResult:
    database_path: Path
    target_count: int
    point_count: int


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
    if credentials is None:
        credentials = read_wechat_mini_credentials(
            mmkv_path,
            helper_path=mmkv_helper,
            storage_root=storage_root,
        )
    token, user_id = _normalize_credentials(credentials)
    sender = transport or _default_curve_transport
    target_database.parent.mkdir(parents=True, exist_ok=True)
    point_count = 0
    try:
        with sqlite3.connect(target_database) as connection:
            _ensure_curve_schema(connection)
            connection.commit()
            for target in targets:
                body = {
                    "assetId": target["asset_id"],
                    "groupId": target["group_id"],
                    "id": target["tm_id"],
                    "userId": user_id,
                    "selected": CURVE_WINDOW,
                    "ccyId": target["ccy_id"],
                    "code": str(user_id),
                }
                body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                try:
                    response = sender(
                        CURVE_ENDPOINT,
                        body_bytes,
                        {"Authorization": token, "Content-Type": "application/json"},
                    )
                except Exception as exc:
                    raise ValueError("Trend Animals curve request failed") from exc
                points = _validated_curve_points(_decrypt_curve_response(_encrypted_response(response)))
                connection.executemany(
                    """
                    INSERT INTO trend_curve_points
                    (market, symbol, curve_date, price, temperature, strength, mom, yoy, bar,
                     asset_id, group_id, tm_id, ccy_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        ccy_id = excluded.ccy_id
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
                        )
                        for point in points
                    ],
                )
                point_count += len(points)
    except ValueError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("趋势曲线数据库写入失败") from exc
    return CollectionResult(target_database, len(targets), point_count)


def _load_portfolio_targets(
    portfolio: Path | str,
    mappings_root: Path,
) -> list[dict[str, object]]:
    if not isinstance(portfolio, (str, Path)):
        raise ValueError("portfolio must be a CSV path")
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
                if (market, symbol) in PORTFOLIO_TREND_CURVE_EXCLUSIONS:
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


def _validated_curve_points(payload: object) -> list[dict[str, object]]:
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
            }
        )
        previous_date = point_date
    return points


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
            PRIMARY KEY (market, symbol, curve_date)
        )
        """
    )
