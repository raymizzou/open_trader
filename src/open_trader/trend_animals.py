from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import urlopen

from .futu_symbols import (
    from_trend_animals_symbol,
    to_futu_symbol,
    to_trend_animals_symbol,
)


BASE_URL = "https://www.trendtrader.cn/apiData/data"
MAX_REQUEST_URL_LENGTH = 3_500
SEARCH_ASSETS_BY_MARKET = {
    "CN": frozenset({"A股", "ETF基金"}),
    "HK": frozenset({"港股", "香港ETF"}),
    "US": frozenset({"美股", "美国ETF"}),
}
TREND_SYMBOL_MAPPING_SCHEMA = "open_trader.trend_symbol_mapping.v1"
TREND_SYMBOL_DISCOVERY_RULE_VERSION = "trend_symbol_discovery.v1"
Transport = Callable[[str, float], dict[str, object]]


class TrendAnimalsError(RuntimeError):
    pass


class TrendAnimalsLookupError(TrendAnimalsError):
    pass


class TrendAnimalsNoCurrentRowsError(TrendAnimalsError):
    pass


@dataclass(frozen=True)
class TrendSymbolMapping:
    market: str
    futu_symbol: str
    trend_animals_symbol: str
    trend_animals_tm_id: int
    asset: str


def _default_transport(url: str, timeout: float) -> dict[str, object]:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False


class TrendAnimalsClient:
    def __init__(
        self,
        *,
        api_key: str,
        cache_dir: Path,
        transport: Transport = _default_transport,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("TREND_ANIMALS_API_KEY is required")
        if not isinstance(cache_dir, Path):
            raise TypeError("cache_dir must be a Path")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._api_key = api_key
        self.cache_dir = cache_dir
        self.transport = transport
        self.timeout_seconds = float(timeout_seconds)
        self._paid_cache_events: list[dict[str, str]] = []
        self._ignored_stale_components: list[dict[str, str]] = []
        self._symbol_mappings_loaded = False
        self._symbol_mappings_by_futu: dict[
            tuple[str, str], TrendSymbolMapping
        ] = {}
        self._symbol_mappings_by_trend: dict[
            tuple[str, str], TrendSymbolMapping
        ] = {}
        self._symbol_mappings_by_tm_id: dict[
            tuple[str, int], TrendSymbolMapping
        ] = {}

    @property
    def paid_cache_events(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(event) for event in self._paid_cache_events)

    @property
    def ignored_stale_components(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(row) for row in self._ignored_stale_components)

    def get_update_status(self) -> list[dict[str, object]]:
        return self._get("getUpdateStatus", {})

    def get_snapshot_billing(self) -> list[dict[str, object]]:
        return self._get("getSnapshotColumnBilling", {})

    def get_account_balance(self) -> Mapping[str, object]:
        rows = self._get("getAccountBalance", {"viewLevel": "summary"})
        if len(rows) != 1:
            raise TrendAnimalsError("getAccountBalance returned no unique summary")
        return rows[0]

    def symbol_mapping(
        self, symbol: str, *, market: str
    ) -> TrendSymbolMapping | None:
        normalized_market = self._normalize_market(market)
        try:
            futu_symbol = to_futu_symbol(normalized_market, symbol)
        except (AttributeError, ValueError):
            raise ValueError("symbol must be a valid Futu symbol") from None
        self._ensure_symbol_mappings_loaded()
        return self._symbol_mappings_by_futu.get(
            (normalized_market, futu_symbol)
        )

    def symbol_mapping_from_trend(
        self, symbol: str, *, market: str
    ) -> TrendSymbolMapping | None:
        normalized_market = self._normalize_market(market)
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a nonempty Trend Animals symbol")
        self._ensure_symbol_mappings_loaded()
        return self._symbol_mappings_by_trend.get(
            (normalized_market, symbol.strip())
        )

    def symbol_mapping_from_tm_id(
        self, tm_id: int, *, market: str
    ) -> TrendSymbolMapping | None:
        normalized_market = self._normalize_market(market)
        if not self._valid_tm_id(tm_id):
            raise ValueError("tm_id must be a positive integer")
        self._ensure_symbol_mappings_loaded()
        return self._symbol_mappings_by_tm_id.get((normalized_market, tm_id))

    def remember_symbol_row(
        self,
        *,
        market: str,
        expected_futu_symbol: str,
        row: Mapping[str, object],
    ) -> TrendSymbolMapping:
        normalized_market = self._normalize_market(market)
        if not isinstance(row, Mapping):
            raise TypeError("row must be a mapping")
        try:
            futu_symbol = to_futu_symbol(
                normalized_market, expected_futu_symbol
            )
        except (AttributeError, ValueError):
            raise ValueError("expected_futu_symbol must be a valid Futu symbol") from None
        payload = {
            "asset": row.get("asset"),
            "futu_symbol": futu_symbol,
            "market": normalized_market,
            "schema_version": TREND_SYMBOL_MAPPING_SCHEMA,
            "trend_animals_symbol": row.get("tickerSymbol"),
            "trend_animals_tm_id": row.get("tmId"),
        }
        mapping = self._mapping_from_payload(
            payload, directory_market=normalized_market
        )
        if self._contains_secret(payload):
            raise TrendAnimalsError("symbol mapping contains unsafe data")
        self._ensure_symbol_mappings_loaded()
        self._check_symbol_mapping_conflicts(mapping)
        existing = self._symbol_mappings_by_futu.get(
            (mapping.market, mapping.futu_symbol)
        )
        if existing == mapping:
            return existing
        path = (
            self.cache_dir
            / "symbol_mappings"
            / mapping.market
            / f"{mapping.futu_symbol}.json"
        )
        self._write_cache(path, payload)
        self._index_symbol_mapping(mapping)
        return mapping

    def search_exact_symbol(
        self,
        symbol: str,
        *,
        market: str,
        expected_date: str,
    ) -> int:
        if not isinstance(symbol, str):
            raise TypeError("symbol must be a string")
        self._validate_expected_date(expected_date)
        try:
            expected_day = date.fromisoformat(expected_date)
        except ValueError:
            expected_day = None
        if expected_day is None or expected_day.isoformat() != expected_date:
            raise ValueError("expected_date must be an ISO date")
        normalized_market = self._normalize_market(market)
        target = to_futu_symbol(normalized_market, symbol)
        normalized = target.split(".", 1)[1]
        query = (
            normalized
            if normalized_market == "CN"
            else to_trend_animals_symbol(normalized_market, target)
        )
        if self._api_key in normalized:
            raise ValueError("symbol conflicts with credentials")
        mapping = self.symbol_mapping(target, market=normalized_market)
        if mapping is not None:
            return mapping.trend_animals_tm_id

        cache_path = self.cache_dir / "symbols" / f"{normalized}.json"
        cached = self._read_cache(cache_path)
        if cached is not None:
            if (
                not isinstance(cached, dict)
                or cached.get("symbol") != normalized
                or not self._valid_tm_id(cached.get("tmId"))
            ):
                raise TrendAnimalsError("symbol cache has an invalid shape")
            return cached["tmId"]

        miss_payload = {
            "discovery_query": query,
            "discovery_rule_version": TREND_SYMBOL_DISCOVERY_RULE_VERSION,
            "error": "no_unique_exact_match",
            "futu_symbol": target,
            "market": normalized_market,
        }
        miss_path = (
            self.cache_dir
            / "symbol_misses"
            / normalized_market
            / f"{target}.json"
        )
        cached_miss = self._read_cache(miss_path)
        if cached_miss is not None:
            if (
                not isinstance(cached_miss, dict)
                or set(cached_miss) != set(miss_payload)
                or cached_miss.get("discovery_query") != query
                or cached_miss.get("error") != "no_unique_exact_match"
                or cached_miss.get("futu_symbol") != target
                or cached_miss.get("market") != normalized_market
                or not isinstance(
                    cached_miss.get("discovery_rule_version"), str
                )
            ):
                raise TrendAnimalsError("symbol miss cache has an invalid shape")
            if (
                cached_miss["discovery_rule_version"]
                == TREND_SYMBOL_DISCOVERY_RULE_VERSION
            ):
                raise TrendAnimalsLookupError(
                    f"searchTicker found no unique exact match for {normalized}"
                )

        rows = self._get("searchTicker", {"keyword": query})
        matches: dict[
            tuple[int, str, str], dict[str, object]
        ] = {}
        allowed_assets = SEARCH_ASSETS_BY_MARKET[normalized_market]
        for row in rows:
            ticker_symbol = row.get("tickerSymbol")
            tm_id = row.get("tmId")
            if not isinstance(ticker_symbol, str) or not self._valid_tm_id(tm_id):
                raise TrendAnimalsError("searchTicker returned an invalid row")
            asset = row.get("asset")
            if asset is None:
                continue
            if not isinstance(asset, str):
                raise TrendAnimalsError("searchTicker returned an invalid row")
            asset = asset.strip()
            if asset not in allowed_assets:
                continue
            try:
                candidate = from_trend_animals_symbol(
                    normalized_market, ticker_symbol
                )
            except ValueError:
                continue
            if self._same_security(normalized_market, candidate, target):
                matches[(tm_id, ticker_symbol, asset)] = row
        if len(matches) != 1:
            self._write_cache(miss_path, miss_payload)
            raise TrendAnimalsLookupError(
                f"searchTicker found no unique exact match for {normalized}"
            )
        row = next(iter(matches.values()))
        return self.remember_symbol_row(
            market=normalized_market,
            expected_futu_symbol=target,
            row=row,
        ).trend_animals_tm_id

    def get_components(
        self, *, tm_id: int, expected_date: str
    ) -> list[dict[str, object]]:
        if not self._valid_tm_id(tm_id):
            raise ValueError("tm_id must be a positive integer")
        self._validate_expected_date(expected_date)
        return self._cached_rows(
            "getComponentTicker",
            {"tmId": str(tm_id), "getAllBasicComponentsFlag": "0"},
            expected_date,
            ignore_older=True,
        )

    def get_snapshots(
        self,
        *,
        tm_ids: Sequence[int],
        fields: Sequence[str],
        expected_date: str,
    ) -> list[dict[str, object]]:
        if (
            not isinstance(tm_ids, Sequence)
            or isinstance(tm_ids, (str, bytes))
            or not tm_ids
            or any(not self._valid_tm_id(tm_id) for tm_id in tm_ids)
        ):
            raise ValueError("tm_ids must contain positive integers")
        if (
            not isinstance(fields, Sequence)
            or isinstance(fields, (str, bytes))
            or not fields
            or any(not isinstance(field, str) or not field.strip() for field in fields)
        ):
            raise ValueError("fields must contain nonempty strings")
        unique_ids = sorted(set(tm_ids))
        unique_fields = sorted(set(fields))
        self._validate_expected_date(expected_date)
        rows: list[dict[str, object]] = []
        batch: list[int] = []
        for tm_id in unique_ids:
            candidate = [*batch, tm_id]
            params = {
                "tmIds": ",".join(map(str, candidate)),
                "fields": ",".join(unique_fields),
            }
            url = (
                f"{BASE_URL}/getTickerSnapshot?"
                f"{urlencode({'apiKey': self._api_key, **params})}"
            )
            if batch and len(url) > MAX_REQUEST_URL_LENGTH:
                rows.extend(
                    self._cached_rows(
                        "getTickerSnapshot",
                        {
                            "tmIds": ",".join(map(str, batch)),
                            "fields": ",".join(unique_fields),
                        },
                        expected_date,
                    )
                )
                batch = [tm_id]
            else:
                if len(url) > MAX_REQUEST_URL_LENGTH:
                    raise ValueError("snapshot request parameters exceed URL limit")
                batch = candidate
        rows.extend(
            self._cached_rows(
                "getTickerSnapshot",
                {
                    "tmIds": ",".join(map(str, batch)),
                    "fields": ",".join(unique_fields),
                },
                expected_date,
            )
        )
        return rows

    def _get(
        self, endpoint: str, params: Mapping[str, str]
    ) -> list[dict[str, object]]:
        url = f"{BASE_URL}/{endpoint}?{urlencode({'apiKey': self._api_key, **params})}"
        try:
            response = self.transport(url, self.timeout_seconds)
        except Exception:
            raise TrendAnimalsError(f"{endpoint} request failed") from None
        if not isinstance(response, dict) or any(
            not isinstance(key, str) for key in response
        ):
            raise TrendAnimalsError(f"{endpoint} returned an invalid response")
        if response.get("success") is not True or response.get("code") != "00000":
            raise TrendAnimalsError(f"{endpoint} returned an unsuccessful response")
        data = response.get("data")
        if not isinstance(data, list) or any(
            not isinstance(row, dict) or not _is_json_value(row) for row in data
        ):
            raise TrendAnimalsError(f"{endpoint} returned invalid data")
        rows = list(data)
        if self._contains_secret(rows):
            raise TrendAnimalsError(f"{endpoint} returned unsafe data")
        return rows

    def _cached_rows(
        self,
        endpoint: str,
        params: Mapping[str, str],
        expected_date: str,
        *,
        ignore_older: bool = False,
    ) -> list[dict[str, object]]:
        cache_identity = {
            "date": expected_date,
            "endpoint": endpoint,
            "params": dict(sorted(params.items())),
        }
        digest = hashlib.sha256(
            json.dumps(
                cache_identity, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        cache_path = self.cache_dir / "responses" / f"{digest}.json"
        cached = self._read_cache(cache_path)
        self._paid_cache_events.append(
            {"endpoint": endpoint, "cache": "hit" if cached is not None else "miss"}
        )
        if cached is not None:
            if not isinstance(cached, list) or any(
                not isinstance(row, dict) or not _is_json_value(row) for row in cached
            ):
                raise TrendAnimalsError("response cache has an invalid shape")
            rows = list(cached)
        else:
            rows = self._get(endpoint, params)
        if self._contains_secret(rows):
            raise TrendAnimalsError(f"{endpoint} returned unsafe data")
        current_rows: list[dict[str, object]] = []
        ignored_rows: list[dict[str, str]] = []
        try:
            expected_day = date.fromisoformat(expected_date)
        except ValueError:
            expected_day = None
        expected_is_canonical = (
            expected_day is not None and expected_day.isoformat() == expected_date
        )
        for row in rows:
            actual_date = row.get("asOfDate")
            try:
                actual_day = (
                    date.fromisoformat(actual_date)
                    if isinstance(actual_date, str)
                    else None
                )
            except ValueError:
                actual_day = None
            actual_is_canonical = (
                actual_day is not None and actual_day.isoformat() == actual_date
            )
            if (
                expected_is_canonical
                and actual_is_canonical
                and actual_date == expected_date
            ):
                current_rows.append(row)
                continue
            symbol = row.get("tickerSymbol")
            if (
                ignore_older
                and expected_is_canonical
                and actual_is_canonical
                and actual_day < expected_day
                and isinstance(symbol, str)
                and symbol.strip()
            ):
                ignored_rows.append(
                    {"tickerSymbol": symbol.strip(), "asOfDate": actual_date}
                )
                continue
            safe_actual = (
                self._redact(actual_date)
                if isinstance(actual_date, str)
                else actual_date
            )
            raise TrendAnimalsError(
                f"{endpoint} returned data for {safe_actual!r}; "
                f"expected {self._redact(expected_date)}"
            )
        if ignore_older and not current_rows:
            tm_id = self._redact(str(params.get("tmId", "")))
            raise TrendAnimalsNoCurrentRowsError(
                f"{endpoint} tmId={tm_id} returned no current-date rows"
            )
        self._ignored_stale_components.extend(ignored_rows)
        if cached is None and not ignored_rows:
            self._write_cache(cache_path, current_rows)
        return current_rows

    def _read_cache(self, path: Path) -> object | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise TrendAnimalsError("cache is unreadable or malformed") from None

    def _ensure_symbol_mappings_loaded(self) -> None:
        if self._symbol_mappings_loaded:
            return
        by_futu: dict[tuple[str, str], TrendSymbolMapping] = {}
        by_trend: dict[tuple[str, str], TrendSymbolMapping] = {}
        by_tm_id: dict[tuple[str, int], TrendSymbolMapping] = {}
        for market in sorted(SEARCH_ASSETS_BY_MARKET):
            directory = self.cache_dir / "symbol_mappings" / market
            for path in sorted(directory.glob("*.json")):
                payload = self._read_cache(path)
                try:
                    mapping = self._mapping_from_payload(
                        payload, directory_market=market
                    )
                except TrendAnimalsError:
                    raise
                if path.stem != mapping.futu_symbol:
                    raise TrendAnimalsError("symbol mapping cache has an invalid path")
                self._check_symbol_mapping_conflicts(
                    mapping,
                    by_futu=by_futu,
                    by_trend=by_trend,
                    by_tm_id=by_tm_id,
                )
                by_futu[(mapping.market, mapping.futu_symbol)] = mapping
                by_trend[(mapping.market, mapping.trend_animals_symbol)] = mapping
                by_tm_id[(mapping.market, mapping.trend_animals_tm_id)] = mapping
        self._symbol_mappings_by_futu = by_futu
        self._symbol_mappings_by_trend = by_trend
        self._symbol_mappings_by_tm_id = by_tm_id
        self._symbol_mappings_loaded = True

    def _mapping_from_payload(
        self, payload: object, *, directory_market: str
    ) -> TrendSymbolMapping:
        required = {
            "asset",
            "futu_symbol",
            "market",
            "schema_version",
            "trend_animals_symbol",
            "trend_animals_tm_id",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise TrendAnimalsError("symbol mapping cache has an invalid shape")
        market = payload.get("market")
        futu_symbol = payload.get("futu_symbol")
        trend_symbol = payload.get("trend_animals_symbol")
        tm_id = payload.get("trend_animals_tm_id")
        asset = payload.get("asset")
        if (
            payload.get("schema_version") != TREND_SYMBOL_MAPPING_SCHEMA
            or market != directory_market
            or market not in SEARCH_ASSETS_BY_MARKET
            or not isinstance(futu_symbol, str)
            or not futu_symbol
            or not isinstance(trend_symbol, str)
            or not trend_symbol.strip()
            or trend_symbol != trend_symbol.strip()
            or not self._valid_tm_id(tm_id)
            or not isinstance(asset, str)
            or asset not in SEARCH_ASSETS_BY_MARKET[market]
        ):
            raise TrendAnimalsError("symbol mapping cache has an invalid shape")
        try:
            canonical_futu = to_futu_symbol(market, futu_symbol)
            trend_futu = from_trend_animals_symbol(market, trend_symbol)
        except ValueError:
            raise TrendAnimalsError("symbol mapping cache has an invalid shape") from None
        if canonical_futu != futu_symbol or not self._same_security(
            market, canonical_futu, trend_futu
        ):
            raise TrendAnimalsError("symbol mapping cache has an invalid shape")
        return TrendSymbolMapping(
            market=market,
            futu_symbol=futu_symbol,
            trend_animals_symbol=trend_symbol,
            trend_animals_tm_id=tm_id,
            asset=asset,
        )

    def _check_symbol_mapping_conflicts(
        self,
        mapping: TrendSymbolMapping,
        *,
        by_futu: dict[tuple[str, str], TrendSymbolMapping] | None = None,
        by_trend: dict[tuple[str, str], TrendSymbolMapping] | None = None,
        by_tm_id: dict[tuple[str, int], TrendSymbolMapping] | None = None,
    ) -> None:
        indexes = (
            (
                by_futu if by_futu is not None else self._symbol_mappings_by_futu,
                (mapping.market, mapping.futu_symbol),
            ),
            (
                by_trend if by_trend is not None else self._symbol_mappings_by_trend,
                (mapping.market, mapping.trend_animals_symbol),
            ),
            (
                by_tm_id if by_tm_id is not None else self._symbol_mappings_by_tm_id,
                (mapping.market, mapping.trend_animals_tm_id),
            ),
        )
        if any(index.get(key) not in (None, mapping) for index, key in indexes):
            raise TrendAnimalsError("symbol mapping conflict")

    def _index_symbol_mapping(self, mapping: TrendSymbolMapping) -> None:
        self._symbol_mappings_by_futu[
            (mapping.market, mapping.futu_symbol)
        ] = mapping
        self._symbol_mappings_by_trend[
            (mapping.market, mapping.trend_animals_symbol)
        ] = mapping
        self._symbol_mappings_by_tm_id[
            (mapping.market, mapping.trend_animals_tm_id)
        ] = mapping

    @staticmethod
    def _normalize_market(market: object) -> str:
        if not isinstance(market, str):
            raise TypeError("market must be a string")
        normalized = market.strip().upper()
        if normalized not in SEARCH_ASSETS_BY_MARKET:
            raise ValueError(f"unsupported market: {market}")
        return normalized

    @staticmethod
    def _same_security(market: str, left: str, right: str) -> bool:
        if market == "CN":
            return left.split(".", 1)[1] == right.split(".", 1)[1]
        return left == right

    def _write_cache(self, path: Path, payload: object) -> None:
        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=path.parent
            ) as temp:
                json.dump(
                    payload,
                    temp,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                temp_path = Path(temp.name)
            temp_path.replace(path)
        except (OSError, TypeError, ValueError):
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise TrendAnimalsError("cache write failed") from None

    @staticmethod
    def _valid_tm_id(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def _contains_secret(self, payload: object) -> bool:
        if isinstance(payload, str):
            return self._api_key in payload
        if isinstance(payload, list):
            return any(self._contains_secret(item) for item in payload)
        if isinstance(payload, dict):
            return any(
                self._api_key in key or self._contains_secret(item)
                for key, item in payload.items()
            )
        return False

    def _redact(self, value: str) -> str:
        return value.replace(self._api_key, "<redacted>")

    @staticmethod
    def _validate_expected_date(value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected_date must be a nonempty string")
