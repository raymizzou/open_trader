from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Mapping

from .futu_symbols import to_futu_symbol


HOLDING_SNAPSHOT_SCHEMA = "open_trader.account.holding_generation.v1"
SUPPORTED_BROKERS = {
    "phillips": ("HK", "HKD"),
    "eastmoney": ("CN", "CNY"),
}


class HoldingSnapshotImportService:
    def __init__(self, *, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._stage_lock = Lock()

    def stage_snapshot(
        self, broker: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        with self._stage_lock:
            canonical = _canonical_snapshot(broker, payload)
            generation = _content_sha256(canonical)
            generations = self.data_dir / "account_holdings/generations" / broker
            destination = generations / generation.removeprefix("sha256:")
            if destination.exists():
                existing = _load_manifest(destination)
                _validate_staged_manifest(existing, destination, broker)
                return existing

            generations.mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema_version": HOLDING_SNAPSHOT_SCHEMA,
                "status": "staged",
                **canonical,
                "holding_generation": generation,
                "staged_at": datetime.now().astimezone().isoformat(
                    timespec="microseconds"
                ),
            }
            manifest["manifest_integrity"] = _manifest_integrity(manifest)
            with TemporaryDirectory(prefix=".stage-", dir=generations) as name:
                root = Path(name)
                (root / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                try:
                    root.rename(destination)
                except FileExistsError:
                    pass
            staged = _load_manifest(destination)
            _validate_staged_manifest(staged, destination, broker)
            return staged


def load_staged_holding_snapshot(
    data_dir: Path, broker: str
) -> dict[str, object] | None:
    if broker not in SUPPORTED_BROKERS:
        raise ValueError("unsupported holding snapshot broker")
    generations = data_dir / "account_holdings/generations" / broker
    if not generations.is_dir():
        return None
    candidates: list[tuple[str, str, str, dict[str, object]]] = []
    for root in generations.iterdir():
        if not root.is_dir() or root.name.startswith(".stage-"):
            continue
        manifest = _load_manifest(root)
        _validate_staged_manifest(manifest, root, broker)
        data_as_of = manifest["data_as_of"]
        staged_at = manifest["staged_at"]
        assert isinstance(data_as_of, str)
        assert isinstance(staged_at, str)
        holding_generation = manifest["holding_generation"]
        assert isinstance(holding_generation, str)
        candidates.append((data_as_of, staged_at, holding_generation, manifest))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[:3])[3]


def _canonical_snapshot(
    broker: str, payload: Mapping[str, object]
) -> dict[str, object]:
    if broker not in SUPPORTED_BROKERS:
        raise ValueError("unsupported holding snapshot broker")
    if not isinstance(payload, Mapping):
        raise ValueError("holding snapshot must be an object")
    raw_positions = payload.get("positions")
    if not isinstance(raw_positions, list):
        raise ValueError("positions must be a list")
    if payload.get("confirmed") is not True:
        raise ValueError("holding snapshot is not confirmed")
    if payload.get("complete") is not True:
        raise ValueError("position set is incomplete")
    data_as_of = payload.get("data_as_of")
    if not isinstance(data_as_of, str):
        raise ValueError("data_as_of must be an ISO date")
    try:
        parsed_data_as_of = date.fromisoformat(data_as_of)
    except ValueError as error:
        raise ValueError("data_as_of must be an ISO date") from error
    if parsed_data_as_of.isoformat() != data_as_of:
        raise ValueError("data_as_of must be an ISO date")
    market, currency = SUPPORTED_BROKERS[broker]
    positions_by_symbol: dict[str, dict[str, str]] = {}
    for raw_position in raw_positions:
        position = _canonical_position(market, raw_position)
        previous = positions_by_symbol.get(position["symbol"])
        if previous is not None and previous != position:
            raise ValueError("conflicting normalized duplicate position")
        positions_by_symbol[position["symbol"]] = position
    return {
        "broker": broker,
        "data_as_of": data_as_of,
        "confirmed": True,
        "complete": payload.get("complete"),
        "positions": [positions_by_symbol[key] for key in sorted(positions_by_symbol)],
        "cash": _canonical_cash(payload, currency),
    }


def _canonical_position(market: str, value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("position must be an object")
    symbol = value.get("symbol")
    name = value.get("name")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("position symbol is required")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("position name is required")
    try:
        normalized = to_futu_symbol(market, symbol).split(".", 1)[1]
    except (IndexError, ValueError) as error:
        raise ValueError("invalid position symbol") from error
    return {
        "symbol": normalized,
        "name": name,
        "quantity": _decimal_text(
            value.get("quantity"), "quantity", require_positive=True
        ),
        "cost_price": _decimal_text(
            value.get("cost_price"), "cost_price", require_non_negative=True
        ),
    }


def _canonical_cash(
    payload: Mapping[str, object], expected_currency: str
) -> dict[str, str]:
    raw = payload.get("cash")
    if not isinstance(raw, Mapping):
        raise ValueError("cash policy is invalid")
    if raw.get("policy") == "preserve":
        return {"policy": "preserve"}
    if raw.get("policy") != "replace":
        raise ValueError("cash policy is invalid")
    if raw.get("currency") != expected_currency:
        raise ValueError("cash currency is invalid")
    balance = raw.get("balance")
    available = raw.get("available_balance")
    return {
        "policy": "replace",
        "currency": expected_currency,
        "balance": _decimal_text(balance, "cash balance"),
        "available_balance": _decimal_text(available, "available balance"),
    }


def _decimal_text(
    value: object,
    field: str,
    *,
    require_positive: bool = False,
    require_non_negative: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string")
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a decimal string") from error
    if not decimal.is_finite():
        raise ValueError(f"{field} must be finite")
    if require_positive and decimal <= 0:
        raise ValueError(f"{field} must be positive")
    if require_non_negative and decimal < 0:
        raise ValueError(f"{field} must be non-negative")
    return format(decimal.normalize(), "f")


def _content_sha256(value: object) -> str:
    body = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _manifest_integrity(manifest: Mapping[str, object]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_integrity", None)
    return _content_sha256(unsigned)


def _load_manifest(root: Path) -> dict[str, object]:
    payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid holding generation")
    return payload


def _validate_staged_manifest(
    manifest: Mapping[str, object], root: Path, broker: str
) -> None:
    if (
        manifest.get("schema_version") != HOLDING_SNAPSHOT_SCHEMA
        or manifest.get("status") != "staged"
        or manifest.get("broker") != broker
        or manifest.get("holding_generation") != f"sha256:{root.name}"
    ):
        raise ValueError("invalid holding generation")
    manifest_integrity = manifest.get("manifest_integrity")
    if (
        not isinstance(manifest_integrity, str)
        or _manifest_integrity(manifest) != manifest_integrity
    ):
        raise ValueError("invalid holding generation")
    canonical = {
        key: manifest.get(key)
        for key in (
            "broker",
            "data_as_of",
            "confirmed",
            "complete",
            "positions",
            "cash",
        )
    }
    if _content_sha256(canonical) != manifest["holding_generation"]:
        raise ValueError("invalid holding generation")
