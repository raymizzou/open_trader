from __future__ import annotations

import pytest

from open_trader.cli import build_parser


def test_generate_trade_actions_cli_is_disabled() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["generate-trade-actions"])

    assert exc_info.value.code == 2
