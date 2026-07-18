from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .futu_symbols import to_futu_symbol
from .kelly_order_execution import FutuSimulateOrderExecutionClient
from .trend_review import _report_hash


TREND_SIMULATE_BROKERS = {
    "tiger": ("US", "USD"),
    "phillips": ("HK", "HKD"),
    "eastmoney": ("CN", "CNY"),
}

_REPORT_DIRECTORIES = {
    "tiger": "trend_us_tiger",
    "phillips": "trend_hk_phillips",
    "eastmoney": "trend_a_share",
}
class TrendSimulatePositionService:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        account_ids: Mapping[str, int],
        fx_to_hkd: Mapping[str, Decimal],
        data_dir: Path,
        reports_dir: Path,
        client_factory: Callable[..., Any] = FutuSimulateOrderExecutionClient,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.host = host
        self.port = port
        self.account_ids = dict(account_ids)
        self.fx_to_hkd = dict(fx_to_hkd)
        self.data_dir = data_dir
        self.reports_dir = reports_dir
        self.client_factory = client_factory
        self.now = now

    def load(self, broker: str) -> dict[str, Any]:
        if broker not in TREND_SIMULATE_BROKERS:
            raise ValueError(f"unsupported trend simulate broker: {broker}")
        market, currency = TREND_SIMULATE_BROKERS[broker]
        account_id = self.account_ids.get(broker, 0)
        if account_id <= 0:
            return _unavailable(broker, market, "模拟账户未登记")
        client = None
        try:
            client = self.client_factory(
                host=self.host,
                port=self.port,
                simulate_acc_id=account_id,
                trd_market=market,
            )
            snapshot = client.account_snapshot()
            positions = _project_positions(
                snapshot,
                broker=broker,
                market=market,
                currency=currency,
                fx_to_hkd=self.fx_to_hkd,
                attributions=_position_attributions(
                    self.data_dir,
                    self.reports_dir,
                    broker=broker,
                    market=market,
                ),
            )
            return {
                "available": True,
                "broker": broker,
                "market": market,
                "synced_at": self.now().isoformat(timespec="seconds"),
                "positions": positions,
                "error": "",
            }
        except Exception as exc:
            return _unavailable(broker, market, str(exc))
        finally:
            if client is not None:
                client.close()


def _unavailable(broker: str, market: str, error: str) -> dict[str, Any]:
    return {
        "available": False,
        "broker": broker,
        "market": market,
        "synced_at": "",
        "positions": [],
        "error": error,
    }


def _project_positions(
    snapshot: object,
    *,
    broker: str,
    market: str,
    currency: str,
    fx_to_hkd: Mapping[str, Decimal],
    attributions: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(snapshot, Mapping):
        raise ValueError("simulate account snapshot is invalid")
    net_value, _ = _required_decimal(snapshot.get("net_value"), "simulate net value")
    if net_value <= 0:
        raise ValueError("simulate net value must be positive")
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        raise ValueError("simulate account positions are unavailable")
    fx = fx_to_hkd.get(currency)
    if fx is None or not fx.is_finite() or fx <= 0:
        raise ValueError(f"missing HKD conversion rate for {currency}")

    projected: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, Mapping):
            raise ValueError("simulate account position is invalid")
        quantity, quantity_text = _required_decimal(
            _first_nonempty(position, "qty", "quantity"), "position qty"
        )
        if quantity <= 0:
            continue
        code = str(
            _first_nonempty(position, "code", "futu_code") or ""
        ).strip().upper()
        symbol = _position_symbol(code, market)
        cost_price, cost_price_text = _required_decimal(
            _first_nonempty(position, "cost_price", "average_cost"),
            "position cost price",
        )
        _, last_price_text = _required_decimal(
            _first_nonempty(position, "nominal_price", "last_price", "price"),
            "position last price",
        )
        market_value, market_value_text = _required_decimal(
            _first_nonempty(position, "market_val", "market_value"),
            "position market value",
        )
        _, pnl_ratio_text = _required_decimal(
            _first_nonempty(position, "pl_ratio", "unrealized_pnl_pct"),
            "position P/L ratio",
        )
        attribution = attributions.get(
            symbol,
            {"attribution_status": "unlinked", "report": None},
        )
        weight = _percent(market_value / net_value)
        projected.append(
            {
                "broker": broker,
                "market": market,
                "symbol": symbol,
                "name": str(
                    _first_nonempty(
                        position, "stock_name", "name", "security_name"
                    )
                    or symbol
                ).strip(),
                "currency": currency,
                "quantity": quantity_text,
                "cost_price": cost_price_text,
                "last_price": last_price_text,
                "market_value": market_value_text,
                "cost_value": _money(quantity * cost_price),
                "market_value_hkd": _money(market_value * fx),
                "account_weight": weight,
                "portfolio_weight": weight,
                "unrealized_pnl_pct": f"{pnl_ratio_text}%",
                **attribution,
            }
        )
    return projected


def _position_symbol(code: str, market: str) -> str:
    canonical = to_futu_symbol(market, code)
    if canonical != code:
        raise ValueError(f"position code {code!r} does not belong to {market}")
    return canonical.split(".", 1)[1]


def _first_nonempty(values: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in values and values[key] not in (None, ""):
            return values[key]
    return None


def _required_decimal(value: object, field: str) -> tuple[Decimal, str]:
    text = str(value).strip() if value is not None else ""
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed, text


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _percent(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def _position_attributions(
    data_dir: Path,
    reports_dir: Path,
    *,
    broker: str,
    market: str,
) -> dict[str, dict[str, Any]]:
    reports = _reports_by_identity(
        reports_dir / _REPORT_DIRECTORIES[broker], broker=broker, market=market
    )
    active: dict[str, set[tuple[str, str] | None]] = {}
    for _, _, _, event in _action_events(data_dir, market):
        symbol = str(event["symbol"]).strip().upper()
        side = str(event.get("side") or "").strip().lower()
        status = str(event.get("status") or "").strip().lower()
        if side == "sell" and (
            status == "filled"
            or (
                status == "incomplete"
                and event.get("reason") == "position_zero_confirmed"
            )
        ):
            active.pop(symbol, None)
            continue
        if side != "buy" or status not in {"partially_filled", "filled"}:
            continue
        filled, _ = _required_decimal(event.get("filled_qty"), "filled quantity")
        report_sha256 = str(event.get("report_sha256") or "").strip().lower()
        strategy_version = str(event.get("strategy_version") or "").strip()
        if filled > 0:
            active.setdefault(symbol, set()).add(
                (report_sha256, strategy_version)
                if _is_sha256(report_sha256) and strategy_version
                else None
            )

    attributions: dict[str, dict[str, Any]] = {}
    for symbol, report_identities in active.items():
        valid_identities = {
            identity for identity in report_identities if identity in reports
        }
        if len(valid_identities) > 1:
            attributions[symbol] = {
                "attribution_status": "conflict",
                "report": None,
            }
            continue
        if report_identities - valid_identities:
            attributions[symbol] = {
                "attribution_status": "unlinked",
                "report": None,
            }
            continue
        report = reports.get(next(iter(valid_identities))) if valid_identities else None
        attributions[symbol] = (
            {"attribution_status": "linked", "report": report}
            if report is not None
            else {"attribution_status": "unlinked", "report": None}
        )
    return attributions


def _reports_by_identity(
    reports_dir: Path, *, broker: str, market: str
) -> dict[tuple[str, str], dict[str, str]]:
    reports: dict[tuple[str, str], dict[str, str]] = {}
    for path in sorted(reports_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
            snapshot = (
                payload.get("strategy_snapshot")
                if isinstance(payload, Mapping)
                else None
            )
            execution_date = str(payload.get("execution_date") or "")
            strategy_version = (
                str(snapshot.get("strategy_version") or "")
                if isinstance(snapshot, Mapping)
                else ""
            )
            if not (
                isinstance(payload, Mapping)
                and isinstance(metadata, Mapping)
                and str(metadata.get("market") or "").strip().upper() == market
                and str(metadata.get("broker") or "").strip().lower() == broker
                and _is_iso_date(execution_date)
                and strategy_version
            ):
                continue
            digest = _report_hash(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        reports.setdefault(
            (digest, strategy_version),
            {
                "artifact": path.name,
                "execution_date": execution_date,
                "strategy_version": strategy_version,
                "report_sha256": digest,
            },
        )
    return reports


def _reports_by_hash(
    reports_dir: Path, *, broker: str, market: str
) -> dict[str, dict[str, str]]:
    return {
        report_hash: report
        for (report_hash, _strategy_version), report in _reports_by_identity(
            reports_dir, broker=broker, market=market
        ).items()
    }


def _action_events(
    data_dir: Path, market: str
) -> list[tuple[str, str, str, Mapping[str, object]]]:
    root = data_dir / "trend_review" / "ledgers" / market / "actions"
    events: list[tuple[str, str, str, Mapping[str, object]]] = []
    for path in root.glob("*/*/*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError(f"action event {path} is unreadable") from None
        if not isinstance(event, Mapping):
            raise ValueError(f"action event {path} is invalid")
        event_date = str(event.get("date") or "")
        recorded_at = str(event.get("recorded_at") or "")
        symbol = event.get("symbol")
        if not (
            _is_iso_date(event_date)
            and event_date == path.parent.parent.name
            and _is_aware_datetime(recorded_at)
            and str(event.get("market") or "").strip().upper() == market
            and isinstance(symbol, str)
            and symbol.strip()
        ):
            raise ValueError(f"action event {path} has invalid identity")
        events.append((event_date, recorded_at, str(path), event))
    events.sort(key=lambda item: item[:3])
    return events


def _is_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _is_aware_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.isoformat() == value
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
