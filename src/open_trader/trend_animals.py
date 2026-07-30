from __future__ import annotations

import hashlib
import json
import math
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
Transport = Callable[[str, float], dict[str, object]]


class TrendAnimalsError(RuntimeError):
    pass


class TrendAnimalsLookupError(TrendAnimalsError):
    pass


class TrendAnimalsNoCurrentRowsError(TrendAnimalsError):
    pass


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
        target = to_futu_symbol(market, symbol)
        normalized_market = market.strip().upper()
        normalized = target.split(".", 1)[1]
        query = to_trend_animals_symbol(market, target)
        if self._api_key in normalized:
            raise ValueError("symbol conflicts with credentials")
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
            "market": normalized_market,
            "date": expected_date,
            "symbol": normalized,
            "error": "no_unique_exact_match",
        }
        miss_path = (
            self.cache_dir
            / "symbol_misses"
            / normalized_market
            / expected_date
            / f"{normalized}.json"
        )
        cached_miss = self._read_cache(miss_path)
        if cached_miss is not None:
            if cached_miss != miss_payload:
                raise TrendAnimalsError("symbol miss cache has an invalid shape")
            raise TrendAnimalsLookupError(
                f"searchTicker found no unique exact match for {normalized}"
            )

        rows = self._get("searchTicker", {"keyword": query})
        matches: set[int] = set()
        allowed_assets = SEARCH_ASSETS_BY_MARKET[normalized_market]
        for row in rows:
            ticker_symbol = row.get("tickerSymbol")
            tm_id = row.get("tmId")
            if not isinstance(ticker_symbol, str) or not self._valid_tm_id(tm_id):
                raise TrendAnimalsError("searchTicker returned an invalid row")
            asset = row.get("asset")
            if asset is not None and (
                not isinstance(asset, str) or asset.strip() not in allowed_assets
            ):
                continue
            try:
                candidate = from_trend_animals_symbol(market, ticker_symbol)
            except ValueError:
                continue
            if candidate == target:
                matches.add(tm_id)
        if len(matches) != 1:
            self._write_cache(miss_path, miss_payload)
            raise TrendAnimalsLookupError(
                f"searchTicker found no unique exact match for {normalized}"
            )
        tm_id = next(iter(matches))
        self._write_cache(cache_path, {"symbol": normalized, "tmId": tm_id})
        return tm_id

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
