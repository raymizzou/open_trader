from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_trader.kelly_order_risk import (
    build_kelly_order_risk_checks,
    build_kelly_order_risk_checks_payload,
    write_kelly_order_risk_checks,
)
from open_trader.kelly_strategy_stats import build_kelly_strategy_stats_payload


def _strategy_stats_index(
    *,
    experiment_id: str = "trend",
    suggested_position_pct: str = "4%",
) -> dict[str, dict[str, str]]:
    return {
        experiment_id: {
            "suggested_position_pct": suggested_position_pct,
            "parameter_source": "futu_paper_order_samples",
            "last_recomputed_at": "2026-07-11 12:01",
            "source_trade_samples_generated_at": "2026-07-11 12:00",
            "source_trade_samples_digest": "a" * 64,
        }
    }


def test_build_kelly_order_risk_checks_approves_valid_entry_and_exit() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 2,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "parameter_source": "futu_paper_order_samples",
                "strategy_stats_generated_at": "2026-07-11 12:01",
                "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                "source_trade_samples_digest": "a" * 64,
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "HK",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "exit",
                "side": "sell",
                "suggested_position_pct": "0%",
                "parameter_source": "futu_paper_order_samples",
                "strategy_stats_generated_at": "2026-07-11 12:01",
                "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_stats_by_experiment=_strategy_stats_index(),
    )

    assert payload == {
        "schema_version": "open_trader.kelly_order_risk_checks.v1",
        "checked_at": "2026-07-10 13:31",
        "max_entry_position_pct": "4",
        "intent_count": 2,
        "approved_count": 2,
        "blocked_count": 0,
        "checks": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "parameter_source": "futu_paper_order_samples",
                "strategy_stats_generated_at": "2026-07-11 12:01",
                "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                "source_trade_samples_digest": "a" * 64,
                "risk_status": "approved",
                "execution_status": "ready",
                "checked_at": "2026-07-10 13:31",
                "planned_notional": "1000",
                "budget_currency": "USD",
                "reason": "entry risk checks passed",
                "check_results": [
                    {
                        "check": "experiment_market_matches_symbol",
                        "status": "passed",
                        "detail": "US == US",
                    },
                    {
                        "check": "budget_currency_matches_market",
                        "status": "passed",
                        "detail": "USD == USD",
                    },
                    {
                        "check": "per_symbol_budget_positive",
                        "status": "passed",
                        "detail": "25000",
                    },
                    {
                        "check": "suggested_position_pct_positive",
                        "status": "passed",
                        "detail": "4",
                    },
                    {
                        "check": "strategy_stats_provenance",
                        "status": "passed",
                        "detail": "matches current stats for trend",
                    },
                    {
                        "check": "max_entry_position_pct",
                        "status": "passed",
                        "detail": "4 <= 4",
                    },
                ],
            },
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "exit",
                "side": "sell",
                "suggested_position_pct": "0%",
                "parameter_source": "futu_paper_order_samples",
                "strategy_stats_generated_at": "2026-07-11 12:01",
                "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                "source_trade_samples_digest": "",
                "risk_status": "approved",
                "execution_status": "ready",
                "checked_at": "2026-07-10 13:31",
                "planned_notional": "",
                "budget_currency": "USD",
                "reason": "exit intent reduces exposure",
                "check_results": [
                    {
                        "check": "experiment_market_matches_symbol",
                        "status": "passed",
                        "detail": "HK == HK",
                    },
                    {
                        "check": "exit_default_allow",
                        "status": "passed",
                        "detail": "sell/exit intents are not blocked in v1",
                    }
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    "field",
    [
        "suggested_position_pct",
        "parameter_source",
        "strategy_stats_generated_at",
        "strategy_stats_source_samples_generated_at",
        "source_trade_samples_digest",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "mismatched"])
def test_build_kelly_order_risk_checks_blocks_entry_with_invalid_provenance(
    field: str,
    mutation: str,
) -> None:
    intent = {
        "intent_id": "trend:US:RAM:entry",
        "experiment_id": "trend",
        "experiment_market": "US",
        "market": "US",
        "symbol": "RAM",
        "intent_type": "entry",
        "side": "buy",
        "suggested_position_pct": "4%",
        "parameter_source": "futu_paper_order_samples",
        "strategy_stats_generated_at": "2026-07-11 12:01",
        "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
        "source_trade_samples_digest": "a" * 64,
        "per_symbol_budget": "25000",
        "budget_currency": "USD",
    }
    if mutation == "missing":
        del intent[field]
    else:
        intent[field] = "mismatch"

    payload = build_kelly_order_risk_checks_payload(
        {"intents": [intent]},
        checked_at="2026-07-11 12:02",
        strategy_stats_by_experiment=_strategy_stats_index(),
    )

    check = payload["checks"][0]
    assert check["risk_status"] == "blocked"
    provenance = next(
        item
        for item in check["check_results"]
        if item["check"] == "strategy_stats_provenance"
    )
    assert provenance["status"] == "failed"
    assert field in provenance["detail"]


def test_build_kelly_order_risk_checks_approves_exit_without_provenance() -> None:
    payload = build_kelly_order_risk_checks_payload(
        {
            "intents": [
                {
                    "intent_id": "trend:US:RAM:exit",
                    "experiment_id": "trend",
                    "experiment_market": "US",
                    "market": "US",
                    "symbol": "RAM",
                    "intent_type": "exit",
                    "side": "sell",
                    "budget_currency": "USD",
                }
            ]
        },
        checked_at="2026-07-11 12:02",
    )

    assert payload["approved_count"] == 1
    assert payload["checks"][0]["risk_status"] == "approved"


def test_production_risk_approves_exit_without_stats_artifacts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    latest = data_dir / "latest"
    latest.mkdir(parents=True)
    (latest / "kelly_order_intents.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_order_intents.v1",
                "intents": [
                    {
                        "intent_id": "trend:US:RAM:exit",
                        "experiment_id": "trend",
                        "experiment_market": "US",
                        "market": "US",
                        "symbol": "RAM",
                        "intent_type": "exit",
                        "side": "sell",
                        "budget_currency": "USD",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_kelly_order_risk_checks(
        data_dir,
        checked_at="2026-07-11 12:03",
    )

    assert payload["approved_count"] == 1
    assert payload["checks"][0]["risk_status"] == "approved"


def test_production_risk_uses_current_validated_zero_sample_stats(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    latest = data_dir / "latest"
    latest.mkdir(parents=True)
    trade_samples = {
        "schema_version": "open_trader.kelly_trade_samples.v1",
        "generated_at": "2026-07-11 12:00",
        "source_orders_synced_at": "2026-07-11 11:59",
        "sample_count": 0,
        "open_position_count": 0,
        "skipped_order_count": 0,
        "samples": [],
        "open_positions": [],
        "diagnostics": {"skipped_orders": []},
        "stats_by_experiment": {},
    }
    stats_payload = build_kelly_strategy_stats_payload(
        [{"experiment_id": "trend", "market": "US"}],
        trade_samples,
        generated_at="2026-07-11 12:01",
    )
    stats = stats_payload["stats_by_experiment"]["trend"]
    intent = {
        "intent_id": "trend:US:RAM:entry",
        "experiment_id": "trend",
        "experiment_market": "US",
        "market": "US",
        "symbol": "RAM",
        "intent_type": "entry",
        "side": "buy",
        "suggested_position_pct": stats["suggested_position_pct"],
        "parameter_source": stats["parameter_source"],
        "strategy_stats_generated_at": stats["last_recomputed_at"],
        "strategy_stats_source_samples_generated_at": stats[
            "source_trade_samples_generated_at"
        ],
        "source_trade_samples_digest": stats["source_trade_samples_digest"],
        "per_symbol_budget": "25000",
        "budget_currency": "USD",
    }
    for name, payload in (
        ("kelly_trade_samples.json", trade_samples),
        ("kelly_strategy_stats.json", stats_payload),
        (
            "kelly_order_intents.json",
            {
                "schema_version": "open_trader.kelly_order_intents.v1",
                "intents": [intent],
            },
        ),
    ):
        (latest / name).write_text(json.dumps(payload), encoding="utf-8")

    payload = build_kelly_order_risk_checks(
        data_dir,
        checked_at="2026-07-11 12:03",
    )

    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    provenance = next(
        item
        for item in payload["checks"][0]["check_results"]
        if item["check"] == "strategy_stats_provenance"
    )
    assert provenance["status"] == "passed"


def test_build_kelly_order_risk_checks_blocks_entry_above_position_cap() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "breakout:US:TSM:entry",
                "experiment_id": "breakout",
                "experiment_name": "突破第一批",
                "strategy_id": "breakout_10d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "TSM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "8%",
                "per_symbol_budget": "15000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    assert payload["checks"][0]["risk_status"] == "blocked"
    assert payload["checks"][0]["execution_status"] == "risk_blocked"
    assert payload["checks"][0]["planned_notional"] == "1200"
    assert payload["checks"][0]["reason"] == "entry risk checks failed"
    assert payload["checks"][0]["check_results"][:2] == [
        {
            "check": "experiment_market_matches_symbol",
            "status": "passed",
            "detail": "US == US",
        },
        {
            "check": "budget_currency_matches_market",
            "status": "passed",
            "detail": "USD == USD",
        },
    ]
    assert payload["checks"][0]["check_results"][-1] == {
        "check": "max_entry_position_pct",
        "status": "failed",
        "detail": "8 > 4",
    }


def test_build_kelly_order_risk_checks_blocks_entry_when_strategy_capital_insufficient() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "parameter_source": "futu_paper_order_samples",
                "strategy_stats_generated_at": "2026-07-11 12:01",
                "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                "source_trade_samples_digest": "a" * 64,
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_capital_payload={
            "strategies": [
                {
                    "experiment_id": "trend",
                    "currency": "USD",
                    "available_notional": "500",
                }
            ]
        },
        strategy_stats_by_experiment=_strategy_stats_index(),
    )

    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    check = payload["checks"][0]
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["planned_notional"] == "1000"
    assert check["reason"] == "entry risk checks failed"
    assert check["check_results"][-1] == {
        "check": "strategy_available_capital",
        "status": "failed",
        "detail": "1000 <= 500 USD",
    }


def test_build_kelly_order_risk_checks_approves_entry_when_strategy_capital_sufficient() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "parameter_source": "futu_paper_order_samples",
                "strategy_stats_generated_at": "2026-07-11 12:01",
                "strategy_stats_source_samples_generated_at": "2026-07-11 12:00",
                "source_trade_samples_digest": "a" * 64,
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_capital_payload={
            "strategies": [
                {
                    "experiment_id": "trend",
                    "currency": "USD",
                    "available_notional": "1500",
                }
            ]
        },
        strategy_stats_by_experiment=_strategy_stats_index(),
    )

    assert payload["approved_count"] == 1
    assert payload["blocked_count"] == 0
    check = payload["checks"][0]
    assert check["risk_status"] == "approved"
    assert check["execution_status"] == "ready"
    assert check["planned_notional"] == "1000"
    assert check["reason"] == "entry risk checks passed"
    assert check["check_results"][-1] == {
        "check": "strategy_available_capital",
        "status": "passed",
        "detail": "1000 <= 1500 USD",
    }


def test_build_kelly_order_risk_checks_preserves_exit_allow_with_strategy_capital() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "exit",
                "side": "sell",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_capital_payload={
            "strategies": [
                {
                    "experiment_id": "trend",
                    "currency": "USD",
                    "available_notional": "0",
                }
            ]
        },
    )

    check = payload["checks"][0]
    assert check["risk_status"] == "approved"
    assert check["execution_status"] == "ready"
    assert check["reason"] == "exit intent reduces exposure"
    assert "strategy_available_capital" not in [
        result["check"] for result in check["check_results"]
    ]
    assert check["check_results"][-1] == {
        "check": "exit_default_allow",
        "status": "passed",
        "detail": "sell/exit intents are not blocked in v1",
    }


@pytest.mark.parametrize(
    ("strategy_capital_payload", "expected_detail"),
    [
        ({"strategies": "bad"}, "missing capital snapshot for trend"),
        (
            {
                "strategies": [
                    {
                        "experiment_id": "different",
                        "currency": "USD",
                        "available_notional": "0",
                    },
                    "bad",
                    {},
                ]
            },
            "missing capital snapshot for trend",
        ),
    ],
)
def test_build_kelly_order_risk_checks_fails_closed_without_valid_matching_strategy_capital(
    strategy_capital_payload: dict[str, object],
    expected_detail: str,
) -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_capital_payload=strategy_capital_payload,
    )

    check = payload["checks"][0]
    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["planned_notional"] == "1000"
    assert check["check_results"][-1] == {
        "check": "strategy_available_capital",
        "status": "failed",
        "detail": expected_detail,
    }


@pytest.mark.parametrize(
    ("capital_snapshot", "expected_detail"),
    [
        (
            {
                "experiment_id": "trend",
                "market": "HK",
                "currency": "USD",
                "available_notional": "1500",
            },
            "capital market HK != US",
        ),
        (
            {
                "experiment_id": "trend",
                "market": "US",
                "currency": "HKD",
                "available_notional": "1500",
            },
            "capital currency HKD != USD",
        ),
    ],
)
def test_build_kelly_order_risk_checks_blocks_strategy_capital_market_currency_mismatch(
    capital_snapshot: dict[str, str],
    expected_detail: str,
) -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_capital_payload={"strategies": [capital_snapshot]},
    )

    check = payload["checks"][0]
    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["planned_notional"] == "1000"
    assert check["check_results"][-1] == {
        "check": "strategy_available_capital",
        "status": "failed",
        "detail": expected_detail,
    }


@pytest.mark.parametrize(
    "strategy",
    [
        {
            "experiment_id": "trend",
            "currency": "USD",
        },
        {
            "experiment_id": "trend",
            "currency": "USD",
            "available_notional": "not-a-number",
        },
    ],
)
def test_build_kelly_order_risk_checks_blocks_entry_when_strategy_capital_available_invalid(
    strategy: dict[str, str],
) -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
        strategy_capital_payload={"strategies": [strategy]},
    )

    check = payload["checks"][0]
    assert payload["approved_count"] == 0
    assert payload["blocked_count"] == 1
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["planned_notional"] == "1000"
    assert check["check_results"][-1] == {
        "check": "strategy_available_capital",
        "status": "failed",
        "detail": "1000 <= 0 USD",
    }


def test_build_kelly_order_risk_checks_blocks_entry_with_invalid_budget() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
    )

    assert payload["blocked_count"] == 1
    assert payload["checks"][0]["planned_notional"] == ""
    assert payload["checks"][0]["check_results"][:2] == [
        {
            "check": "experiment_market_matches_symbol",
            "status": "passed",
            "detail": "US == US",
        },
        {
            "check": "budget_currency_matches_market",
            "status": "passed",
            "detail": "USD == USD",
        },
    ]
    assert payload["checks"][0]["check_results"][2] == {
        "check": "per_symbol_budget_positive",
        "status": "failed",
        "detail": "",
    }


def test_build_kelly_order_risk_checks_blocks_cross_market_entry() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:HK:02840:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload["blocked_count"] == 1
    check = payload["checks"][0]
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["reason"] == "market scope checks failed"
    assert check["planned_notional"] == ""
    assert check["check_results"][0] == {
        "check": "experiment_market_matches_symbol",
        "status": "failed",
        "detail": "HK != US",
    }


def test_build_kelly_order_risk_checks_blocks_cross_market_exit() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:HK:02840:exit",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "exit",
                "side": "sell",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload["blocked_count"] == 1
    check = payload["checks"][0]
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["reason"] == "market scope checks failed"
    assert "exit_default_allow" not in [
        result["check"] for result in check["check_results"]
    ]
    assert check["check_results"][0] == {
        "check": "experiment_market_matches_symbol",
        "status": "failed",
        "detail": "HK != US",
    }


def test_build_kelly_order_risk_checks_blocks_market_currency_mismatch() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "trend:HK:02840:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "HK",
                "market": "HK",
                "symbol": "02840",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            }
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload["blocked_count"] == 1
    check = payload["checks"][0]
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["reason"] == "market scope checks failed"
    assert check["planned_notional"] == ""
    assert check["check_results"][1] == {
        "check": "budget_currency_matches_market",
        "status": "failed",
        "detail": "USD != HKD",
    }


def test_build_kelly_order_risk_checks_blocks_malformed_market_scope() -> None:
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 2,
        "intents": [
            {
                "intent_id": "trend:XX:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "US",
                "market": "XX",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
            {
                "intent_id": "trend:US:RAM:entry",
                "experiment_id": "trend",
                "experiment_name": "趋势回调第一批",
                "strategy_id": "trend_pullback_20d",
                "strategy_version": "v1",
                "experiment_market": "moon",
                "market": "US",
                "symbol": "RAM",
                "intent_type": "entry",
                "side": "buy",
                "suggested_position_pct": "4%",
                "per_symbol_budget": "25000",
                "budget_currency": "USD",
            },
        ],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload["blocked_count"] == 2
    assert payload["checks"][0]["check_results"] == [
        {
            "check": "symbol_market_present",
            "status": "failed",
            "detail": "XX",
        }
    ]
    assert payload["checks"][1]["check_results"] == [
        {
            "check": "experiment_market_present",
            "status": "failed",
            "detail": "moon",
        }
    ]


@pytest.mark.parametrize(
    ("field", "expected_check"),
    [
        ("market", "symbol_market_present"),
        ("experiment_market", "experiment_market_present"),
    ],
)
def test_build_kelly_order_risk_checks_blocks_blank_market_scope(
    field: str,
    expected_check: str,
) -> None:
    intent = {
        "intent_id": "trend:US:RAM:entry",
        "experiment_id": "trend",
        "experiment_name": "趋势回调第一批",
        "strategy_id": "trend_pullback_20d",
        "strategy_version": "v1",
        "experiment_market": "US",
        "market": "US",
        "symbol": "RAM",
        "intent_type": "entry",
        "side": "buy",
        "suggested_position_pct": "4%",
        "per_symbol_budget": "25000",
        "budget_currency": "USD",
    }
    intent[field] = ""
    intent_payload = {
        "schema_version": "open_trader.kelly_order_intents.v1",
        "created_at": "2026-07-10 13:30",
        "intent_count": 1,
        "intents": [intent],
    }

    payload = build_kelly_order_risk_checks_payload(
        intent_payload,
        checked_at="2026-07-10 13:31",
        max_entry_position_pct="4",
    )

    assert payload["blocked_count"] == 1
    check = payload["checks"][0]
    assert check["risk_status"] == "blocked"
    assert check["execution_status"] == "risk_blocked"
    assert check["reason"] == "market scope checks failed"
    assert check["check_results"] == [
        {
            "check": expected_check,
            "status": "failed",
            "detail": "",
        }
    ]


def test_write_kelly_order_risk_checks_writes_latest_artifact(tmp_path: Path) -> None:
    payload = {
        "schema_version": "open_trader.kelly_order_risk_checks.v1",
        "checked_at": "2026-07-10 13:31",
        "max_entry_position_pct": "4",
        "intent_count": 0,
        "approved_count": 0,
        "blocked_count": 0,
        "checks": [],
    }

    path = write_kelly_order_risk_checks(tmp_path / "data", payload)

    assert path == tmp_path / "data/latest/kelly_order_risk_checks.json"
    assert json.loads(path.read_text(encoding="utf-8")) == payload
