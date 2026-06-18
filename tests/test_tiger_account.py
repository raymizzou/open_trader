from __future__ import annotations

from pathlib import Path

import pytest

from open_trader.tiger_account import (
    TigerAccountConfig,
    TigerAccountError,
    load_tiger_account_config,
    mask_account_id,
)


def test_mask_account_id_masks_short_and_long_values() -> None:
    assert mask_account_id("123456789") == "*****6789"
    assert mask_account_id("DU575569") == "***5569"
    assert mask_account_id("123") == "***"
    assert mask_account_id("") == ""


def test_load_config_prefers_cli_account_and_environment_private_key_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "tiger.pem"
    key_path.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TIGEROPEN_TIGER_ID", "tiger-123")
    monkeypatch.setenv("TIGEROPEN_ACCOUNT", "env-account")
    monkeypatch.setenv("TIGEROPEN_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("TIGEROPEN_SECRET_KEY", "secret-value")
    monkeypatch.setenv("TIGEROPEN_TOKEN", "token-value")

    config = load_tiger_account_config(
        config_dir=tmp_path / "missing-config-dir",
        account="cli-account",
        sandbox=True,
    )

    assert config == TigerAccountConfig(
        tiger_id="tiger-123",
        account="cli-account",
        private_key_path=key_path,
        private_key=None,
        secret_key="secret-value",
        token="token-value",
        sandbox=True,
        config_dir=tmp_path / "missing-config-dir",
    )


def test_load_config_reads_official_properties_file(tmp_path: Path) -> None:
    config_dir = tmp_path / ".tigeropen"
    config_dir.mkdir()
    properties_path = config_dir / "tiger_openapi_config.properties"
    properties_path.write_text(
        "\n".join(
            [
                "tiger_id=file-tiger-id",
                "account=file-account",
                "private_key_pk1=-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----",
            ]
        ),
        encoding="utf-8",
    )

    config = load_tiger_account_config(
        config_dir=config_dir,
        account=None,
        sandbox=False,
    )

    assert config.tiger_id == "file-tiger-id"
    assert config.account == "file-account"
    assert config.private_key == (
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    )
    assert config.private_key_path is None
    assert config.config_dir == config_dir


def test_load_config_requires_identity_and_private_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TIGEROPEN_TIGER_ID", raising=False)
    monkeypatch.delenv("TIGEROPEN_ACCOUNT", raising=False)
    monkeypatch.delenv("TIGEROPEN_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("TIGEROPEN_PRIVATE_KEY", raising=False)

    with pytest.raises(TigerAccountError) as exc_info:
        load_tiger_account_config(config_dir=tmp_path, account=None, sandbox=False)

    assert exc_info.value.error_type == "config_missing"
    assert "Tiger OpenAPI configuration is incomplete" in str(exc_info.value)


def test_load_config_rejects_missing_private_key_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIGEROPEN_TIGER_ID", "tiger-123")
    monkeypatch.setenv("TIGEROPEN_ACCOUNT", "env-account")
    missing_path = tmp_path / "missing-key.pem"
    monkeypatch.setenv("TIGEROPEN_PRIVATE_KEY_PATH", str(missing_path))

    with pytest.raises(TigerAccountError) as exc_info:
        load_tiger_account_config(config_dir=tmp_path, account=None, sandbox=False)

    assert exc_info.value.error_type == "config_invalid"
    assert "Tiger OpenAPI private key path is invalid" in str(exc_info.value)
    assert str(missing_path) in str(exc_info.value)
