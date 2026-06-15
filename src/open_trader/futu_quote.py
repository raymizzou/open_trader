from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from .futu_watch import QuoteSnapshot


class FutuQuoteError(RuntimeError):
    pass


def _default_context_factory(*, host: str, port: int) -> Any:
    try:
        from futu import OpenQuoteContext
    except ImportError as exc:
        raise FutuQuoteError(
            "futu-api is not installed. Install it with: "
            ".venv/bin/python -m pip install futu-api"
        ) from exc
    return OpenQuoteContext(host=host, port=port)


def _can_connect_to_opend(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


class FutuQuoteClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        context_factory: Callable[..., Any] = _default_context_factory,
        connectivity_checker: Callable[[str, int], bool] = _can_connect_to_opend,
    ) -> None:
        if not connectivity_checker(host, port):
            raise FutuQuoteError(
                f"Futu OpenD is not reachable at {host}:{port}. "
                "Start OpenD, log in, and check the configured host and port."
            )
        try:
            self.context = context_factory(host=host, port=port)
        except FutuQuoteError:
            raise
        except Exception as exc:
            raise FutuQuoteError(
                f"failed to connect to Futu OpenD at {host}:{port}: {exc}"
            ) from exc
        self.host = host
        self.port = port

    def get_snapshots(self, futu_symbols: Sequence[str]) -> dict[str, QuoteSnapshot]:
        ret_code, data = self.context.get_market_snapshot(list(futu_symbols))
        if ret_code != 0:
            raise FutuQuoteError(str(data))
        snapshots: dict[str, QuoteSnapshot] = {}
        for record in data.to_dict("records"):
            code = str(record.get("code", "")).strip()
            raw_price = record.get("last_price")
            if not code or raw_price in {None, ""}:
                continue
            try:
                price = Decimal(str(raw_price))
            except (InvalidOperation, ValueError):
                continue
            if price.is_finite():
                snapshots[code] = QuoteSnapshot(futu_symbol=code, last_price=price)
        return snapshots

    def close(self) -> None:
        self.context.close()
