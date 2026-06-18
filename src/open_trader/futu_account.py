from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any, Callable


TRD_ENV_REAL = "REAL"


class FutuAccountError(RuntimeError):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class FutuAccount:
    acc_id: int
    acc_index: int
    trd_env: str
    acc_type: str
    account_alias: str


@dataclass(frozen=True)
class FutuAccountSnapshot:
    accounts: list[FutuAccount]
    cash_records: list[dict[str, object]]
    position_records: list[dict[str, object]]


def _can_connect_to_opend(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _default_trade_context_factory(*, host: str, port: int) -> Any:
    try:
        from futu import OpenSecTradeContext
    except ImportError as exc:
        raise FutuAccountError(
            "futu-api is not installed. Install it with: .venv/bin/python -m pip install futu-api",
            error_type="trade_context_failed",
        ) from exc
    return OpenSecTradeContext(host=host, port=port)


def _records(data: object) -> list[dict[str, object]]:
    if hasattr(data, "to_dict"):
        rows = data.to_dict("records")
        return [dict(row) for row in rows]
    raise FutuAccountError(
        f"Futu returned an unsupported table payload: {type(data).__name__}",
        error_type="trade_context_failed",
    )


def _as_int(value: object, *, field_name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise FutuAccountError(
            f"Futu account field {field_name} is not an integer: {value!r}",
            error_type="account_query_failed",
        ) from exc


def _first_text(record: dict[str, object], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _account_from_record(record: dict[str, object]) -> FutuAccount:
    acc_id = _as_int(record.get("acc_id"), field_name="acc_id")
    acc_index = _as_int(record.get("acc_index", 0), field_name="acc_index")
    trd_env = _first_text(record, ("trd_env", "env", "trd_env_name")).upper()
    acc_type = _first_text(record, ("acc_type", "account_type"), "SECURITY").upper()
    return FutuAccount(
        acc_id=acc_id,
        acc_index=acc_index,
        trd_env=trd_env,
        acc_type=acc_type,
        account_alias=f"futu_{acc_id}",
    )


def _is_real_security_account(account: FutuAccount) -> bool:
    return account.trd_env == TRD_ENV_REAL


class FutuAccountClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        context_factory: Callable[..., Any] = _default_trade_context_factory,
        connectivity_checker: Callable[[str, int], bool] = _can_connect_to_opend,
    ) -> None:
        if not connectivity_checker(host, port):
            raise FutuAccountError(
                f"Futu OpenD is not reachable at {host}:{port}. Start OpenD, log in, and check host/port.",
                error_type="opend_unreachable",
            )
        try:
            self.context = context_factory(host=host, port=port)
        except FutuAccountError:
            raise
        except Exception as exc:
            raise FutuAccountError(
                f"failed to create Futu trade context at {host}:{port}: {exc}",
                error_type="trade_context_failed",
            ) from exc
        self.host = host
        self.port = port

    def fetch_snapshot(self) -> FutuAccountSnapshot:
        ret_code, data = self.context.get_acc_list()
        if ret_code != 0:
            raise FutuAccountError(str(data), error_type="account_query_failed")
        accounts = [
            account
            for account in (_account_from_record(record) for record in _records(data))
            if _is_real_security_account(account)
        ]
        if not accounts:
            raise FutuAccountError(
                "no REAL Futu securities accounts found",
                error_type="no_real_accounts",
            )

        cash_records: list[dict[str, object]] = []
        position_records: list[dict[str, object]] = []
        for account in accounts:
            cash_records.extend(self._fetch_cash_records(account))
            position_records.extend(self._fetch_position_records(account))
        return FutuAccountSnapshot(
            accounts=accounts,
            cash_records=cash_records,
            position_records=position_records,
        )

    def _fetch_cash_records(self, account: FutuAccount) -> list[dict[str, object]]:
        ret_code, data = self.context.accinfo_query(
            trd_env=TRD_ENV_REAL,
            acc_id=account.acc_id,
            acc_index=account.acc_index,
            refresh_cache=True,
            currency="HKD",
            asset_category="N/A",
        )
        if ret_code != 0:
            raise FutuAccountError(str(data), error_type="asset_query_failed")
        return [
            {**record, "_account_alias": account.account_alias, "_acc_id": account.acc_id}
            for record in _records(data)
        ]

    def _fetch_position_records(self, account: FutuAccount) -> list[dict[str, object]]:
        ret_code, data = self.context.position_list_query(
            trd_env=TRD_ENV_REAL,
            acc_id=account.acc_id,
            acc_index=account.acc_index,
            refresh_cache=True,
            position_market="N/A",
            asset_category="N/A",
            currency="USD",
        )
        if ret_code != 0:
            raise FutuAccountError(str(data), error_type="position_query_failed")
        return [
            {**record, "_account_alias": account.account_alias, "_acc_id": account.acc_id}
            for record in _records(data)
        ]

    def close(self) -> None:
        self.context.close()
