from __future__ import annotations

from pathlib import Path

import pytest

import open_trader.cli as cli
from open_trader.frontend_gateway import FrontendGatewayConfig


def test_frontend_gateway_cli_uses_loopback_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_serve_frontend_gateway(
        *, config: FrontendGatewayConfig, host: str, port: int
    ) -> None:
        captured.update(config=config, host=host, port=port)

    monkeypatch.setattr(
        cli, "serve_frontend_gateway", fake_serve_frontend_gateway, raising=False
    )

    assert cli.main(["frontend-gateway"]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8766
    config = captured["config"]
    assert isinstance(config, FrontendGatewayConfig)
    assert config.static_dir == Path(cli.__file__).with_name("dashboard_static")
    assert config.upstream_host == "127.0.0.1"
    assert config.upstream_port == 8767
    assert config.public_origin == "http://127.0.0.1:8766"
    assert config.upstream_timeout_seconds == 30.0


def test_frontend_gateway_cli_dispatches_explicit_upstream_and_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_serve_frontend_gateway(
        *, config: FrontendGatewayConfig, host: str, port: int
    ) -> None:
        captured.update(config=config, host=host, port=port)

    monkeypatch.setattr(
        cli, "serve_frontend_gateway", fake_serve_frontend_gateway, raising=False
    )

    assert cli.main(
        [
            "frontend-gateway",
            "--host",
            "localhost",
            "--port",
            "18766",
            "--upstream-host",
            "localhost",
            "--upstream-port",
            "18767",
            "--public-origin",
            "http://localhost:18766",
            "--upstream-timeout",
            "2.5",
            "--static-dir",
            str(tmp_path),
        ]
    ) == 0

    assert captured["host"] == "localhost"
    assert captured["port"] == 18766
    config = captured["config"]
    assert isinstance(config, FrontendGatewayConfig)
    assert config.static_dir == tmp_path
    assert config.upstream_host == "localhost"
    assert config.upstream_port == 18767
    assert config.public_origin == "http://localhost:18766"
    assert config.upstream_timeout_seconds == 2.5
