from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class TigerAccountError(RuntimeError):
    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class TigerAccountConfig:
    tiger_id: str
    account: str
    private_key_path: Path | None
    private_key: str | None
    secret_key: str | None
    token: str | None
    sandbox: bool
    config_dir: Path


def mask_account_id(account_id: str) -> str:
    text = str(account_id).strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    if len(text) <= 8:
        return f"{'*' * 3}{text[-4:]}"
    return f"{'*' * (len(text) - 4)}{text[-4:]}"


def _read_properties(config_dir: Path) -> dict[str, str]:
    path = config_dir.expanduser() / "tiger_openapi_config.properties"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().replace("\\n", "\n")
    return values


def load_tiger_account_config(
    *,
    config_dir: Path,
    account: str | None,
    sandbox: bool,
) -> TigerAccountConfig:
    expanded_config_dir = config_dir.expanduser()
    properties = _read_properties(expanded_config_dir)
    tiger_id = (
        os.environ.get("TIGEROPEN_TIGER_ID")
        or properties.get("tiger_id")
        or properties.get("tigerId")
        or ""
    ).strip()
    selected_account = (
        account
        or os.environ.get("TIGEROPEN_ACCOUNT")
        or properties.get("account")
        or ""
    ).strip()
    private_key_path_text = (
        os.environ.get("TIGEROPEN_PRIVATE_KEY_PATH")
        or properties.get("private_key_path")
        or ""
    ).strip()
    private_key = (
        os.environ.get("TIGEROPEN_PRIVATE_KEY")
        or properties.get("private_key_pk1")
        or properties.get("private_key")
        or None
    )
    private_key_path = Path(private_key_path_text).expanduser() if private_key_path_text else None
    secret_key = os.environ.get("TIGEROPEN_SECRET_KEY") or properties.get("secret_key")
    token = os.environ.get("TIGEROPEN_TOKEN") or properties.get("token")

    if private_key_path is not None:
        if not private_key_path.exists() or not private_key_path.is_file():
            raise TigerAccountError(
                (
                    f"Tiger OpenAPI private key path is invalid: {private_key_path}. "
                    "Set TIGEROPEN_PRIVATE_KEY_PATH or private_key_path to an existing file."
                ),
                error_type="config_invalid",
            )

    if not tiger_id or not selected_account or (private_key_path is None and not private_key):
        raise TigerAccountError(
            (
                "Tiger OpenAPI configuration is incomplete. Provide tiger_id, "
                "account, and a PKCS#1 private key via ~/.tigeropen/"
                "tiger_openapi_config.properties or TIGEROPEN_* environment variables."
            ),
            error_type="config_missing",
        )
    return TigerAccountConfig(
        tiger_id=tiger_id,
        account=selected_account,
        private_key_path=private_key_path,
        private_key=private_key,
        secret_key=secret_key,
        token=token,
        sandbox=sandbox,
        config_dir=expanded_config_dir,
    )
