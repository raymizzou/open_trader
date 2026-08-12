from __future__ import annotations

import copy
import csv
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import open_trader.dashboard as dashboard_module
import open_trader.a_share_trend as trend_module

from open_trader.advice.models import (
    PREMARKET_ACTION_FIELDNAMES,
    TRADING_ADVICE_FIELDNAMES,
)
from open_trader.dashboard import (
    BROKER_LABELS,    DashboardConfig,
    _futu_skill_signal_detail,
    load_dashboard_state,
)
from open_trader.account_sync_state import (
    build_dashboard_projection,
    empty_account_sync_state,
)
from open_trader.account_snapshot import build_instrument_id
from open_trader.decision_facts import (
    KLINE_FIELDS,
    MISSING_VALUE,
    NEWS_SENTIMENT_FIELDS,
    extract_decision_sources,
)
from open_trader.decision_plan import build_decision_plan, publish_decision_plans
from open_trader.kelly_strategy_stats import build_kelly_strategy_stats_payload
from open_trader.trend_api_stats import (
    build_trend_api_stats_payload,
    write_trend_api_stats,
)
from open_trader.plan_events import PlanEvent, append_plan_event
from open_trader.portfolio import PORTFOLIO_FIELDNAMES
from open_trader.technical_facts import source_hash
from open_trader.trade_actions import TRADE_ACTION_FIELDNAMES
from open_trader.trading_plan import TRADING_PLAN_FIELDNAMES
from open_trader.trend_allocation import build_allocation_snapshot


POSITION_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "quantity",
    "cost_price",
    "last_price",
    "market_value",
    "cost_value",
    "unrealized_pnl",
    "confidence",
    "notes",
]

CASH_FIELDNAMES = [
    "statement_id",
    "broker",
    "account_alias",
    "currency",
    "cash_balance",
    "available_balance",
    "confidence",
    "notes",
]
MISSING_FRESH = object()
MISSING_ATTENTION = object()


def seed_accepted_account_sync(
    config: DashboardConfig,
    *,
    tiger_position_count: int,
    now: datetime | None = None,
) -> dict[str, object]:
    accepted_at = (now or datetime.now().astimezone()).isoformat()
    state = empty_account_sync_state()
    brokers = state["brokers"]
    assert isinstance(brokers, dict)
    for broker, source in brokers.items():
        assert isinstance(source, dict)
        source.update(
            status="ok",
            attempted_at=accepted_at,
            last_success_at=accepted_at,
            data_as_of=accepted_at if broker in {"futu", "tiger"} else "2026-07-30",
            period="2026-07",
        )
    tiger = brokers["tiger"]
    assert isinstance(tiger, dict)
    tiger["positions"] = [
        {
            "statement_id": "2026-07-tiger-live",
            "broker": "tiger",
            "account_alias": "tiger_main",
            "market": "US",
            "asset_class": "stock",
            "symbol": f"ACCEPTED{index}",
            "name": f"Accepted {index}",
            "currency": "USD",
            "quantity": "1",
            "cost_price": "10",
            "last_price": "11",
            "market_value": "11",
            "cost_value": "10",
            "unrealized_pnl": "1",
            "confidence": "high",
            "notes": "",
        }
        for index in range(tiger_position_count)
    ]
    tiger["cash"] = [
        {
            "statement_id": "2026-07-tiger-live",
            "broker": "tiger",
            "account_alias": "tiger_main",
            "currency": "USD",
            "cash_balance": "100",
            "available_balance": "90",
            "confidence": "high",
            "notes": "",
        }
    ]
    tiger["fx_rates"] = [
        {
            "account_alias": "tiger_main",
            "currency": "USD",
            "rate_to_hkd": "7.8",
        }
    ]
    tiger["summary"] = {"position_count": tiger_position_count, "cash_count": 1}
    state["generation"] = accepted_at
    quotes = {
        f"US.ACCEPTED{index}": {
            "market": "US",
            "symbol": f"ACCEPTED{index}",
            "status": "ok",
            "last_price": "11",
            "price_session": "regular",
            "price_time": accepted_at,
            "fetched_at": accepted_at,
        }
        for index in range(tiger_position_count)
    }
    state["dashboard_projection"] = build_dashboard_projection(
        state,
        {"status": "ok", "last_success_at": accepted_at, "stale": False, "quotes": quotes},
        generated_at=accepted_at,
    )
    path = config.data_dir / "latest" / "account_sync_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return state


def test_dashboard_omits_controller_owned_account_fields(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    seed_accepted_account_sync(config, tiger_position_count=1)

    state = load_dashboard_state(config).to_dict()
    assert not {
        "summary", "broker_summaries", "broker_positions", "cash_details",
        "account_sync", "holdings",
    }.intersection(state)
def test_dashboard_does_not_project_futu_option_attention(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)

    state = load_dashboard_state(config).to_dict()

    assert "futu" not in state["trend_reports"]


def test_futu_signal_detail_marks_explicit_api_unsupported_reason() -> None:
    detail = _futu_skill_signal_detail(
        {
            "status": "error",
            "signal": "neutral",
            "confidence": "low",
            "summary": "富途接口不支持技术异动：US.BOTZ",
            "categories": [],
        },
        "2026-07-13",
        {"run_date": "2026-07-13"},
    )

    assert detail["available"] is False
    assert detail["unsupported"] is True
    assert detail["status"] == "not_applicable"
    assert detail["summary"] == "富途接口不支持技术异动：US.BOTZ"


def test_futu_signal_detail_marks_stale_api_unsupported_reason_blocking() -> None:
    detail = _futu_skill_signal_detail(
        {
            "status": "not_applicable",
            "summary": "富途接口不支持技术异动：US.BOTZ",
            "categories": [],
        },
        "2026-07-12",
        {"run_date": "2026-07-13"},
    )

    assert detail["available"] is False
    assert detail["unsupported"] is False
    assert detail["status"] == "stale_run_date"
    assert detail["error"] == "Futu facts run date does not match latest advice"


def write_csv(path: Path, fieldnames: list[str] | tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    if path.name == "portfolio.csv":
        module_path = path.with_name("watchlist.csv")
        with module_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def dashboard_config(
    tmp_path: Path,
    *,
    trend_review_cn_simulate_acc_id: int = 0,
    trend_review_us_simulate_acc_id: int = 0,
    trend_review_hk_simulate_acc_id: int = 0,
) -> DashboardConfig:
    return DashboardConfig(
        portfolio_path=tmp_path / "data" / "latest" / "portfolio.csv",
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        poll_seconds=1.5,
        futu_host="127.0.0.1",
        futu_port=11111,
        trend_review_cn_simulate_acc_id=trend_review_cn_simulate_acc_id,
        trend_review_us_simulate_acc_id=trend_review_us_simulate_acc_id,
        trend_review_hk_simulate_acc_id=trend_review_hk_simulate_acc_id,
        trend_cn_candidate_pool_ids=(622466, 697199),
        trend_us_candidate_pool_ids=(622460, 705013),
        trend_hk_candidate_pool_ids=(622494, 707617),
    )


def test_dashboard_config_defaults_simulate_account_ids_to_zero(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)

    assert config.trend_review_cn_simulate_acc_id == 0
    assert config.trend_review_us_simulate_acc_id == 0
    assert config.trend_review_hk_simulate_acc_id == 0


def test_dashboard_uses_only_module_artifacts_for_holding_enrichment(
    tmp_path: Path,
) -> None:
    """Account publications must never determine Legacy Dashboard rows."""
    config = dashboard_config(tmp_path)
    row = {field: "" for field in TRADE_ACTION_FIELDNAMES}
    row.update({
        "run_date": "2026-08-04",
        "market": "US",
        "symbol": "MODULE_ONLY",
        "action": "HOLD",
        "status": "ready",
    })
    write_csv(
        config.data_dir / "latest" / "US" / "trade_actions.csv",
        TRADE_ACTION_FIELDNAMES,
        [row],
    )
    seed_accepted_account_sync(config, tiger_position_count=1)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [portfolio_rows()[0]])

    payload = load_dashboard_state(config).to_dict()

    assert "ACCEPTED0" not in {item["symbol"] for item in payload["holding_enrichment"]}
    module_row = next(
        item for item in payload["holding_enrichment"] if item["symbol"] == "MODULE_ONLY"
    )
    assert module_row["instrument_id"] == build_instrument_id(
        "US", "stock", "MODULE_ONLY"
    )
    for removed in (
        "portfolio_path", "summary", "holdings", "broker_summaries",
        "source_statuses", "cash_rows", "broker_positions", "cash_details",
        "account_sync",
    ):
        assert removed not in payload
def serialized_trend_account(
    *, fresh: object = MISSING_FRESH,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_date": "2026-07-14",
        "net_value": "100000",
        "available_cash": "50000",
        "positions": [],
        "exceptions": [],
    }
    if fresh is not MISSING_FRESH:
        payload["fresh"] = fresh
    return payload


def serialized_trend_position() -> dict[str, object]:
    return {
        "symbol": "VIXY",
        "name": "ProShares VIX",
        "asset_class": "etf",
        "quantity": "10",
        "avg_cost_price": None,
        "market_value": "500",
    }


def write_trend_history_report(
    reports_dir: Path,
    artifact: str,
    *,
    execution_date: str,
    generated_at: str,
    market: str = "US",
    broker: str = "tiger",
    symbol: str = "VIXY",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "execution_date": execution_date,
        "as_of_date": "2026-07-17",
        "generated_at": generated_at,
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": market, "broker": broker},
        "strategy_snapshot": {"strategy_version": "v1"},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": symbol}],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [],
    }
    path = reports_dir / "trend_us_tiger" / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def write_buy_plan_history(
    root: Path,
    directory: str,
    artifact: str,
    *,
    market: str,
    actions: list[dict[str, object]],
) -> None:
    path = root / directory / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "metadata": {"market": market},
        "strategy_judgments": {"formal_actions": actions},
    }), encoding="utf-8")


@pytest.mark.parametrize(
    (
        "market", "directory", "older_buy", "revision_buy", "non_buy_symbols",
        "review_buy", "review_buy_key", "expected_symbols",
    ),
    [
        (
            "CN", "trend_a_share", "511190", "159915",
            ("600000", "000001", "601318"), "300750", "CN.300750",
            ["CN.159915", "CN.511190"],
        ),
        (
            "HK", "trend_hk_phillips", "622", "700",
            ("1", "2", "3"), "9988", "HK.09988",
            ["HK.00622", "HK.00700"],
        ),
        (
            "US", "trend_us_tiger", "adp", "msft",
            ("sell", "hold", "review"), "tsla", "US.TSLA",
            ["US.ADP", "US.MSFT"],
        ),
    ],
)
def test_historical_buy_plan_membership_keeps_every_formal_buy_revision(
    tmp_path: Path,
    market: str,
    directory: str,
    older_buy: str,
    revision_buy: str,
    non_buy_symbols: tuple[str, str, str],
    review_buy: str,
    review_buy_key: str,
    expected_symbols: list[str],
) -> None:
    write_buy_plan_history(
        tmp_path,
        directory,
        "2026-07-14.json",
        market=market,
        actions=[
            {"action": "BUY", "symbol": older_buy},
            {"action": "SELL_ALL", "symbol": non_buy_symbols[0]},
            {"action": "HOLD", "symbol": non_buy_symbols[1]},
            {"action": "MANUAL_REVIEW", "symbol": non_buy_symbols[2]},
            {"action": "BUY", "symbol": review_buy, "reason": "review_required"},
        ],
    )
    write_buy_plan_history(
        tmp_path, directory, "2026-07-15.json", market=market, actions=[]
    )
    write_buy_plan_history(
        tmp_path,
        directory,
        "2026-07-14-r1.json",
        market=market,
        actions=[
            {"action": "BUY", "symbol": older_buy},
            {"action": "BUY", "symbol": revision_buy},
        ],
    )

    membership = dashboard_module._historical_buy_plan_membership(
        tmp_path / directory, market=market
    )

    assert membership == {
        "available": True,
        "symbols": expected_symbols,
        "reason": "",
    }
    assert review_buy_key not in membership["symbols"]


def test_historical_buy_plan_membership_distinguishes_unavailable_from_empty(
    tmp_path: Path,
) -> None:
    missing = dashboard_module._historical_buy_plan_membership(
        tmp_path / "missing", market="US"
    )
    malformed_dir = tmp_path / "malformed"
    malformed_dir.mkdir()
    (malformed_dir / "broken.json").write_text("{broken", encoding="utf-8")
    malformed = dashboard_module._historical_buy_plan_membership(
        malformed_dir, market="US"
    )
    invalid_actions_dir = tmp_path / "invalid-actions"
    write_buy_plan_history(
        tmp_path, "invalid-actions", "report.json", market="US", actions=[]
    )
    (invalid_actions_dir / "report.json").write_text(json.dumps({
        "strategy_judgments": {"formal_actions": "invalid"},
    }), encoding="utf-8")
    invalid_formal_actions = dashboard_module._historical_buy_plan_membership(
        invalid_actions_dir, market="US"
    )
    invalid_symbol_dir = tmp_path / "invalid-symbol"
    write_buy_plan_history(
        tmp_path,
        "invalid-symbol",
        "report.json",
        market="US",
        actions=[{"action": "BUY", "symbol": "AAPL/2026"}],
    )
    invalid_buy_symbol = dashboard_module._historical_buy_plan_membership(
        invalid_symbol_dir, market="US"
    )
    valid_empty_dir = tmp_path / "valid-empty"
    write_buy_plan_history(
        tmp_path, "valid-empty", "report.json", market="US", actions=[]
    )
    valid_empty = dashboard_module._historical_buy_plan_membership(
        valid_empty_dir, market="US"
    )
    mixed_dir = tmp_path / "mixed"
    write_buy_plan_history(
        tmp_path,
        "mixed",
        "valid.json",
        market="US",
        actions=[{"action": "BUY", "symbol": "ADP"}],
    )
    (mixed_dir / "broken.json").write_text("{broken", encoding="utf-8")
    mixed = dashboard_module._historical_buy_plan_membership(mixed_dir, market="US")

    assert missing["available"] is False
    assert malformed["available"] is False
    assert invalid_formal_actions["available"] is False
    assert invalid_buy_symbol["available"] is False
    assert mixed["available"] is False
    assert valid_empty == {"available": True, "symbols": [], "reason": ""}
    for unavailable in (
        missing, malformed, invalid_formal_actions, invalid_buy_symbol, mixed,
    ):
        assert unavailable["symbols"] == []
        assert unavailable["reason"]


def test_historical_buy_plan_membership_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write_buy_plan_history(
        outside,
        "reports",
        "external.json",
        market="US",
        actions=[{"action": "BUY", "symbol": "LEAK"}],
    )
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "linked.json").symlink_to(outside / "reports/external.json")

    membership = dashboard_module._historical_buy_plan_membership(
        reports_dir, market="US"
    )

    assert membership == {
        "available": False,
        "symbols": [],
        "reason": "历史趋势报告不可读取",
    }


def test_historical_buy_plan_membership_rejects_symlink_loop(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    loop = reports_dir / "loop.json"
    loop.symlink_to(loop)

    membership = dashboard_module._historical_buy_plan_membership(
        reports_dir, market="US"
    )

    assert membership == {
        "available": False,
        "symbols": [],
        "reason": "历史趋势报告不可读取",
    }


def test_trend_report_history_uses_payload_date_and_keeps_revisions(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard import load_trend_report_history

    write_trend_history_report(
        tmp_path,
        "2026-07-17.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
    )
    write_trend_history_report(
        tmp_path,
        "2026-07-17-r1.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:30:00+08:00",
    )
    write_trend_history_report(
        tmp_path,
        "2026-07-16.json",
        execution_date="2026-07-17",
        generated_at="2026-07-17T09:00:00+08:00",
    )

    history = load_trend_report_history(tmp_path, broker="tiger")

    assert [row["execution_date"] for row in history[:2]] == [
        "2026-07-20",
        "2026-07-20",
    ]
    assert {row["artifact"] for row in history[:2]} == {
        "2026-07-17.json",
        "2026-07-17-r1.json",
    }
    assert history[0] == {
        "available": True,
        "artifact": "2026-07-17-r1.json",
        "execution_date": "2026-07-20",
        "data_date": "2026-07-17",
        "generated_at": "2026-07-18T09:30:00+08:00",
        "strategy_version": "v1",
        "revision": 1,
        "execution_counts": {"sell": 0, "buy": 1, "hold": 0, "review": 0},
    }


def test_trend_report_history_marks_corrupt_artifact_without_hiding_siblings(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard import load_trend_report_history

    write_trend_history_report(
        tmp_path,
        "valid.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
    )
    (tmp_path / "trend_us_tiger" / "broken.json").write_text(
        "{broken", encoding="utf-8"
    )

    history = load_trend_report_history(tmp_path, broker="tiger")

    assert history[0]["artifact"] == "valid.json"
    assert history[-1] == {
        "available": False,
        "artifact": "broken.json",
        "status_text": "报告不可读取",
    }


def test_trend_report_history_marks_symlink_escape_unreadable(tmp_path: Path) -> None:
    from open_trader.dashboard import load_trend_report_history

    outside = tmp_path / "outside"
    write_trend_history_report(
        outside,
        "external.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
    )
    reports_dir = tmp_path / "reports"
    linked = reports_dir / "trend_us_tiger" / "linked.json"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside / "trend_us_tiger" / "external.json")

    history = load_trend_report_history(reports_dir, broker="tiger")

    assert history == [{
        "available": False,
        "artifact": "linked.json",
        "status_text": "报告不可读取",
    }]


def test_exact_historical_report_includes_its_immutable_execution(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard import load_historical_trend_report
    from open_trader.trend_review import _report_hash

    config = dashboard_config(tmp_path)
    payload = write_trend_history_report(
        config.reports_dir,
        "2026-07-16.json",
        execution_date="2026-07-17",
        generated_at="2026-07-17T09:00:00+08:00",
    )
    event = (
        config.data_dir
        / "trend_review/ledgers/US/actions/2026-07-17/action-key/event.json"
    )
    event.parent.mkdir(parents=True)
    event.write_text(
        json.dumps({
            "report_sha256": _report_hash(payload),
            "symbol": "VIXY",
            "side": "buy",
            "status": "missed",
            "recorded_at": "2026-07-17T16:00:00-04:00",
            "reason": "buy_window_closed",
        }),
        encoding="utf-8",
    )

    report = load_historical_trend_report(
        config,
        broker="tiger",
        artifact="2026-07-16.json",
    )

    assert report["report_date"] == "2026-07-17"
    assert report["buy_actions"][0]["execution"]["status"] == "missed"
    assert report["audit"]["artifact"] == "2026-07-16.json"
    assert report["report_sha256"] == _report_hash(payload)
    assert report["strategy_version"] == "v1"
    assert report["real_position_status"] == "legacy"
    assert report["real_position_reason"] == "当前报告未包含真实持仓判断"


def test_trend_report_projects_only_same_day_futu_derivatives(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    payload = write_trend_history_report(
        config.reports_dir,
        "2026-07-15.json",
        execution_date="2026-07-15",
        generated_at="2026-07-15T09:00:00+08:00",
    )
    payload["as_of_date"] = "2026-07-15"
    payload["strategy_judgments"]["holding_decisions"] = [
        {"action": "HOLD", "reason": "trend_intact", "symbol": "SPY"},
    ]
    (config.reports_dir / "trend_us_tiger/2026-07-15.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    write_futu_skill_facts(
        config.data_dir / "latest/US/futu_skill_facts.json",
        run_date="2026-07-15",
    )

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["buy_actions"][0]["option_anomaly"]["available"] is True
    assert report["buy_actions"][0]["option_anomaly"]["summary"] == "期权波动率偏高。"
    assert report["hold_actions"][0]["option_anomaly"]["available"] is False
    assert report["hold_actions"][0]["option_anomaly"]["reason"] == "富途未返回该标的期权异动"


@pytest.mark.parametrize(
    ("market", "report_symbol", "holding_symbol"),
    [
        ("CN", "600000", "SH.600000"),
        ("HK", "700", "HK.00700"),
        ("US", "BRK.B", "US.BRK.B"),
    ],
)
def test_trend_holding_membership_state_is_market_aware(
    market: str,
    report_symbol: str,
    holding_symbol: str,
) -> None:
    included_symbols = {
        dashboard_module._canonical_trend_symbol(
            {"symbol": report_symbol}, market
        )
    }

    assert dashboard_module._project_trend_membership_state(
        {"symbol": holding_symbol},
        market=market,
        included_symbols=included_symbols,
    ) == "included"
    assert dashboard_module._project_trend_membership_state(
        {"symbol": "INVALID"},
        market=market,
        included_symbols=included_symbols,
    ) == "excluded"
    assert dashboard_module._project_trend_membership_state(
        {"symbol": holding_symbol, "reason": "holding_trend_excluded"},
        market=market,
        included_symbols=included_symbols,
    ) == "blacklisted"


def test_trend_report_projects_frozen_real_positions_separately_from_simulation(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    payload = write_trend_history_report(
        config.reports_dir,
        "2026-07-15.json",
        execution_date="2026-07-15",
        generated_at="2026-07-15T09:00:00+00:00",
    )
    payload["as_of_date"] = "2026-07-15"
    judgments = payload["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments.update({
        "holding_decisions": [
            {
                "action": "HOLD",
                "symbol": "SPY",
                "name": "标普ETF",
                "reason": "trend_intact",
                "strength": "50",
            },
        ],
        "real_holding_decisions_status": "available",
        "real_holding_decisions_source": {
            "broker": "tiger", "broker_label": "老虎",
            "snapshot_period": "2026-07-15", "source_kind": "statement",
            "freshness_text": "非实时", "read_only_text": "只读，不自动下单",
        },
        "real_holding_decisions": [
            {
                "action": "HOLD", "symbol": "SPY", "name": "标普ETF",
                "reason": "trend_intact", "strength": "50",
            },
            {
                "action": "SELL_ALL", "symbol": "VIXY", "name": "波动率ETF",
                "reason": "danger_signal", "strength": "20",
            },
            {
                "action": "MANUAL_REVIEW", "symbol": "QQQ", "name": "纳指ETF",
                "reason": "holding_signal_unknown", "strength": "90",
            },
            {
                "action": "MANUAL_REVIEW", "symbol": "EUV", "name": "EUV",
                "reason": "holding_signal_unknown", "strength": None,
            },
            {
                "action": "MANUAL_REVIEW", "symbol": "US.AGRZ", "name": "AGRZ",
                "reason": "holding_trend_excluded", "strength": "99",
            },
        ],
    })
    payload["signal_snapshots"] = {
        "real_holdings": {"SPY": {"industry": "ETF", "phase": "立夏"}}
    }
    (config.reports_dir / "trend_us_tiger/2026-07-15.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    write_futu_skill_facts(
        config.data_dir / "latest/US/futu_skill_facts.json",
        run_date="2026-07-15",
    )

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["tiger"]

    assert report["real_position_status"] == "available"
    assert report["real_position_reason"] == ""
    assert report["real_position_source"]["source_kind"] == "statement"
    assert [item["symbol"] for item in report["real_position_actions"]] == [
        "QQQ", "SPY", "VIXY", "EUV", "US.AGRZ",
    ]
    assert {
        item["symbol"]: item["trend_report_state"]
        for item in report["real_position_actions"]
    } == {
        "QQQ": "excluded",
        "SPY": "included",
        "VIXY": "included",
        "EUV": "excluded",
        "US.AGRZ": "blacklisted",
    }
    assert report["hold_actions"][0]["trend_report_state"] == "included"
    assert report["counts"] == {"sell": 0, "buy": 1, "hold": 1, "review": 0}


def test_project_rotation_execution_actions_surfaces_executed_legs() -> None:
    payload = {
        "strategy_judgments": {
            "simulate_rotation_pairs": [
                {
                    "pair_index": 0,
                    "execution_mode": "automatic",
                    "sell_symbol": "HIG",
                    "sell_name": "哈特福德保险",
                    "sell_futu_symbol": "US.HIG",
                    "buy_symbol": "PYPL",
                    "buy_name": "PayPal Holdings Inc",
                    "buy_futu_symbol": "US.PYPL",
                    "target_weight": "0.06",
                    "target_amount": "60308.86",
                    "estimated_shares": 1030,
                },
                {
                    "pair_index": 1,
                    "execution_mode": "manual",
                    "sell_symbol": "WAB",
                    "buy_symbol": "DDOG",
                },
            ],
        },
    }
    executions = {
        ("HIG", "sell"): {
            "status": "filled",
            "updated_at": "2026-08-05T10:13:36-04:00",
        },
        ("PYPL", "buy"): {
            "status": "filled",
            "updated_at": "2026-08-05T10:13:47-04:00",
        },
    }

    sell_actions, buy_actions = (
        dashboard_module._project_rotation_execution_actions(payload, executions)
    )

    assert [item["symbol"] for item in sell_actions] == ["HIG"]
    assert [item["symbol"] for item in buy_actions] == ["PYPL"]
    assert sell_actions[0]["action"] == "全部卖出"
    assert sell_actions[0]["futu_symbol"] == "US.HIG"
    assert sell_actions[0]["execution"]["status"] == "filled"
    assert buy_actions[0]["target_amount"] == "60308.86"
    assert buy_actions[0]["estimated_shares"] == 1030


def test_trend_report_disables_mismatched_futu_derivatives(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    payload = write_trend_history_report(
        config.reports_dir,
        "2026-07-15.json",
        execution_date="2026-07-15",
        generated_at="2026-07-15T09:00:00+08:00",
    )
    payload["as_of_date"] = "2026-07-15"
    (config.reports_dir / "trend_us_tiger/2026-07-15.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    write_futu_skill_facts(
        config.data_dir / "latest/US/futu_skill_facts.json",
        run_date="2026-07-14",
    )

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    option_anomaly = report["buy_actions"][0]["option_anomaly"]
    assert option_anomaly["available"] is False
    assert option_anomaly["status"] == "stale_run_date"
    assert option_anomaly["reason"] == "富途期权异动日期与趋势报告不一致"


def test_historical_trend_report_uses_archived_futu_derivatives(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    payload = write_trend_history_report(
        config.reports_dir,
        "2026-07-15.json",
        execution_date="2026-07-15",
        generated_at="2026-07-15T09:00:00+08:00",
    )
    payload["as_of_date"] = "2026-07-15"
    (config.reports_dir / "trend_us_tiger/2026-07-15.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    write_futu_skill_facts(
        config.data_dir / "latest/US/futu_skill_facts.json",
        run_date="2026-07-14",
    )
    write_futu_skill_facts(
        config.data_dir / "runs/2026-07-15/US/futu_skill_facts.json",
        run_date="2026-07-15",
    )

    report = dashboard_module.load_historical_trend_report(
        config,
        broker="tiger",
        artifact="2026-07-15.json",
    )

    option_anomaly = report["buy_actions"][0]["option_anomaly"]
    assert option_anomaly["available"] is True
    assert option_anomaly["run_date"] == "2026-07-15"


@pytest.mark.parametrize("artifact", ["../secret.json", "/tmp/secret.json"])
def test_historical_report_rejects_unsafe_artifact_paths(
    tmp_path: Path, artifact: str,
) -> None:
    from open_trader.dashboard import load_historical_trend_report

    config = dashboard_config(tmp_path)
    with pytest.raises(ValueError, match="unsafe trend report artifact"):
        load_historical_trend_report(
            config,
            broker="tiger",
            artifact=artifact,
        )


def test_historical_report_rejects_artifact_resolving_outside_broker_directory(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard import load_historical_trend_report

    config = dashboard_config(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    linked = config.reports_dir / "trend_us_tiger" / "linked.json"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe trend report artifact"):
        load_historical_trend_report(
            config,
            broker="tiger",
            artifact="linked.json",
        )


def test_historical_report_rejects_wrong_report_market(tmp_path: Path) -> None:
    from open_trader.dashboard import load_historical_trend_report

    config = dashboard_config(tmp_path)
    write_trend_history_report(
        config.reports_dir,
        "wrong-market.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
        market="HK",
    )

    with pytest.raises(ValueError, match="trend report artifact is unreadable"):
        load_historical_trend_report(
            config,
            broker="tiger",
            artifact="wrong-market.json",
        )


def test_trend_report_history_and_exact_loading_reject_missing_strategy_version(
    tmp_path: Path,
) -> None:
    from open_trader.dashboard import (
        load_historical_trend_report,
        load_trend_report_history,
    )

    config = dashboard_config(tmp_path)
    payload = write_trend_history_report(
        config.reports_dir,
        "missing-version.json",
        execution_date="2026-07-20",
        generated_at="2026-07-18T09:00:00+08:00",
    )
    payload.pop("strategy_snapshot")
    artifact = config.reports_dir / "trend_us_tiger" / "missing-version.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    assert load_trend_report_history(config.reports_dir, broker="tiger") == [{
        "available": False,
        "artifact": "missing-version.json",
        "status_text": "报告不可读取",
    }]
    with pytest.raises(ValueError, match="trend report artifact is unreadable"):
        load_historical_trend_report(
            config,
            broker="tiger",
            artifact="missing-version.json",
        )


@pytest.mark.parametrize("loader", ["history", "artifact"])
def test_trend_report_loaders_reject_unknown_broker(
    tmp_path: Path, loader: str,
) -> None:
    from open_trader.dashboard import (
        load_historical_trend_report,
        load_trend_report_history,
    )

    with pytest.raises(ValueError, match="unsupported trend report broker"):
        if loader == "history":
            load_trend_report_history(tmp_path, broker="unknown")
        else:
            load_historical_trend_report(
                dashboard_config(tmp_path),
                broker="unknown",
                artifact="report.json",
            )


def trend_review_projection(market: str, broker: str) -> dict[str, object]:
    effective_from = {"CN": "2026-07-16", "US": "2026-07-17", "HK": "2026-07-17"}[
        market
    ]
    return {
        "schema_version": "open_trader.trend_review.projection.v2",
        "available": True,
        "market": market,
        "market_label": {"CN": "A 股", "US": "美股", "HK": "港股"}[market],
        "broker": broker,
        "strategy_snapshot": {
            "strategy_id": f"trend_animals_warm_to_hot/{market}/v1",
            "strategy_name": f"{market} 短线右侧趋势",
            "strategy_version": "v1",
            "process_version": "abc1234",
            "effective_from": effective_from,
            "parameters": {"position_limit": 10},
            "parameter_rows": [
                {"group": "仓位执行", "name": "持仓上限", "value": "10 笔"},
                {"group": "退出保护", "name": "初始保护线", "value": "成交均价减 2.0 倍 ATR14"},
            ],
        },
        "batch": {
            "batch_number": 1,
            "completed_trade_count": 30,
            "start_date": "2026-01-01",
            "end_date": "2026-07-17",
        },
        "batch_path": "batch.json",
        "source_path": "/private/trend-review-source.json",
        "source_artifacts": ["private-fill-batch.json"],
        "sample_counts": {"discipline": 31, "actual": 29, "required": 30},
        "common_cutoff": "2026-07-17",
        "interval": {"start": effective_from, "end": "2026-07-17"},
        "metrics": {
            key: {
                series: {"value": value, "reason": None}
                for series, value in {
                    "discipline": "12.6",
                    "actual": "9.4",
                    "benchmark": "7.8",
                }.items()
            }
            for key in (
                "period_net_return",
                "market_excess_return",
                "max_drawdown",
                "calmar",
                "sharpe",
            )
        },
    }


def trend_review_projection_v2(market: str, broker: str) -> dict[str, object]:
    payload = trend_review_projection(market, broker)
    effective_from = {"CN": "2026-07-16", "US": "2026-07-17", "HK": "2026-07-17"}[
        market
    ]
    payload.update({
        "schema_version": "open_trader.trend_review.projection.v2",
        "sample_counts": {"discipline": 31, "actual": 29, "required": 30},
        "common_cutoff": "2026-07-17",
        "interval": {"start": effective_from, "end": "2026-07-17"},
    })
    payload["strategy_snapshot"]["effective_from"] = effective_from  # type: ignore[index]
    return payload


def trend_review_projection_v3(market: str, broker: str) -> dict[str, object]:
    payload = trend_review_projection_v2(market, broker)
    for key in ("batch", "batch_path", "source_path", "source_artifacts"):
        payload.pop(key)
    payload["schema_version"] = "open_trader.trend_review.projection.v4"
    payload["sample_details"] = {
        key: {
            "available": True,
            "eligible_sample_count": payload["sample_counts"][key],  # type: ignore[index]
            "discovered_candidate_count": payload["sample_counts"][key],  # type: ignore[index]
            "excluded_candidate_count": 0,
            "incomplete_open_candidate_count": 0,
            "exclusion_reasons": [],
            "statistics_cutoff_at": "2026-07-17T16:00:00+08:00",
            "reason": "",
        }
        for key in ("discipline", "actual")
    }
    payload["sample_cutoffs"] = {
        "discipline": "2026-07-17T16:00:00+08:00",
        "actual": "2026-07-17T16:00:00+08:00",
    }
    payload["metric_cutoffs"] = {
        "discipline": "2026-07-17",
        "actual": "2026-07-17",
    }
    for values in payload["metrics"].values():  # type: ignore[union-attr]
        benchmark = values.pop("benchmark")
        values["same_period_benchmark"] = dict(benchmark)
        values["market_1y"] = dict(benchmark)
        values["market_5y"] = dict(benchmark)
    for series in ("same_period_benchmark", "market_1y", "market_5y"):
        payload["metrics"]["market_excess_return"][series] = {  # type: ignore[index]
            "value": None,
            "reason": "基准自身",
        }
    identity = {
        "CN": {"name": "中证 500", "source_id": "CSI_500_PRICE", "futu_symbol": "SH.000905"},
        "HK": {"name": "恒生指数", "source_id": "HSI_PRICE", "futu_symbol": "HK.800000"},
        "US": {"name": "S&P 500 ETF", "source_id": "SPY_QFQ", "futu_symbol": "US.SPY"},
    }[market]
    payload["benchmark_context"] = {
        **identity,
        "same_period_dates": ["2026-07-16", "2026-07-17"],
        "windows": {
            "1Y": {
                "start": "2025-07-17",
                "cutoff": "2026-07-17",
                "observation_count": 252,
                "return_basis": "period_return",
            },
            "5Y": {
                "start": "2021-07-17",
                "cutoff": "2026-07-17",
                "observation_count": 1256,
                "return_basis": "CAGR",
            },
        },
    }
    payload["benchmark_refresh"] = {
        "status": "available",
        "month": "2026-07",
        "completed_at": "2026-07-17T16:00:00+08:00",
        "process_git_sha": "abc1234",
        "cutoff": "2026-07-17",
        "refresh": {"force": False, "actor": None, "reason": None},
    }
    return payload


def test_dashboard_keeps_report_available_when_statistics_cycle_failed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data/latest/trend_review_cn.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(trend_review_projection_v3("CN", "eastmoney")),
        encoding="utf-8",
    )
    state_path = tmp_path / "data/trend_api_stats/daily/CN/2026-08-08.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "schema_version": "open_trader.trend_api_stats.cycle.v1",
        "market": "CN",
        "as_of_date": "2026-08-08",
        "status": "failed",
        "attempted_at": "2026-08-08T16:05:00+08:00",
        "reason": "broker unavailable",
        "process_git_sha": "abc1234",
        "failure_notified_at": "2026-08-08T16:05:01+08:00",
        "recovery_notified_at": None,
    }), encoding="utf-8")

    review = dashboard_module._load_trend_reviews(tmp_path / "data")["eastmoney"]

    assert review["available"] is True
    assert review["statistics_status"] == "failed"
    assert review["statistics_reason"] == "broker unavailable"
    assert review["sample_counts"]["discipline"] == 31


def test_dashboard_projects_failed_forced_refresh_over_preserved_completed_state(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "latest/trend_review_cn.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(trend_review_projection_v3("CN", "eastmoney")),
        encoding="utf-8",
    )
    state_path = data_dir / "trend_api_stats/daily/CN/2026-08-08.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "schema_version": "open_trader.trend_api_stats.cycle.v1",
        "status": "completed",
        "market": "CN",
        "as_of_date": "2026-08-08",
        "attempt_count": 2,
        "completed_at": "2026-08-08T16:05:00+08:00",
        "process_git_sha": "accepted123",
        "statistics_cutoff_at": "2026-08-08T15:00:00+08:00",
        "artifact_sha256": "a" * 64,
        "last_forced_failure_status": "failed",
        "last_forced_failure_at": "2026-08-08T17:05:00+08:00",
        "last_forced_failure_actor": "ray",
        "last_forced_failure_reason": "repair stale facts",
        "last_forced_failure_process_git_sha": "forced123",
        "last_forced_failure_error": "broker unavailable",
    }), encoding="utf-8")

    review = dashboard_module._load_trend_reviews(data_dir)["eastmoney"]

    assert review["available"] is True
    assert review["statistics_status"] == "failed"
    assert review["statistics_reason"] == "broker unavailable"
    assert review["statistics_as_of_date"] == "2026-08-08"


def test_dashboard_overlays_latest_statistics_without_writing_files(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "latest/trend_review_cn.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(trend_review_projection_v3("CN", "eastmoney")),
        encoding="utf-8",
    )
    stats = build_trend_api_stats_payload(
        [],
        strategy_versions=[{
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v1",
            "strategy_version": "v1",
        }],
        generated_at="2026-08-08T16:05:00+08:00",
        statistics_cutoff_at="2026-08-08T15:00:00+08:00",
    )
    stats["sources"] = [{
        "source": "simulation",
        "source_id": "simulation:futu:101",
        "broker": "futu",
        "account_id": "101",
        "market": "CN",
        "orders_seen": 0,
        "fill_count": 0,
        "statistics_cutoff_at": "2026-08-08T15:00:00+08:00",
        "status": "available",
    }]
    write_trend_api_stats(data_dir, stats)
    before = {item: item.read_bytes() for item in data_dir.rglob("*") if item.is_file()}

    review = dashboard_module._load_trend_reviews(data_dir)["eastmoney"]

    assert review["sample_counts"]["discipline"] == 0
    assert review["sample_counts"]["actual"] is None
    assert review["sample_details"]["discipline"]["statistics_cutoff_at"] == (
        "2026-08-08T15:00:00+08:00"
    )
    assert review["sample_details"]["actual"]["available"] is False
    assert {item: item.read_bytes() for item in data_dir.rglob("*") if item.is_file()} == before


def test_dashboard_does_not_fall_back_to_older_statistics_cycle_state(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "latest/trend_review_cn.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(trend_review_projection_v3("CN", "eastmoney")),
        encoding="utf-8",
    )
    cycle_dir = data_dir / "trend_api_stats/daily/CN"
    cycle_dir.mkdir(parents=True)
    (cycle_dir / "2026-08-07.json").write_text(json.dumps({
        "schema_version": "open_trader.trend_api_stats.cycle.v1",
        "market": "CN",
        "as_of_date": "2026-08-07",
        "status": "completed",
        "completed_at": "2026-08-07T16:05:00+08:00",
        "process_git_sha": "abc1234",
        "statistics_cutoff_at": "2026-08-07T15:00:00+08:00",
        "artifact_sha256": "a" * 64,
    }), encoding="utf-8")
    (cycle_dir / "2026-08-08.json").write_text("{", encoding="utf-8")

    review = dashboard_module._load_trend_reviews(data_dir)["eastmoney"]

    assert review["statistics_status"] == "unavailable"
    assert review["statistics_as_of_date"] is None


def test_dashboard_marks_completed_cycle_stale_when_accepted_artifact_is_missing(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "latest/trend_review_cn.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(trend_review_projection_v3("CN", "eastmoney")),
        encoding="utf-8",
    )
    cycle_path = data_dir / "trend_api_stats/daily/CN/2026-08-08.json"
    cycle_path.parent.mkdir(parents=True)
    cycle_path.write_text(json.dumps({
        "schema_version": "open_trader.trend_api_stats.cycle.v1",
        "market": "CN",
        "as_of_date": "2026-08-08",
        "status": "completed",
        "completed_at": "2026-08-08T16:05:00+08:00",
        "process_git_sha": "abc1234",
        "statistics_cutoff_at": "2026-08-08T15:00:00+08:00",
        "artifact_sha256": "a" * 64,
    }), encoding="utf-8")

    review = dashboard_module._load_trend_reviews(data_dir)["eastmoney"]

    assert review["statistics_status"] == "stale"
    assert review["statistics_as_of_date"] == "2026-08-08"


def test_dashboard_loads_only_strict_market_matched_trend_reviews(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [])
    latest = config.data_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for market, broker in (("CN", "eastmoney"), ("US", "tiger"), ("HK", "phillips")):
        (latest / f"trend_review_{market.lower()}.json").write_text(
            json.dumps(trend_review_projection_v3(market, broker)), encoding="utf-8"
        )

    reviews = load_dashboard_state(config).to_dict()["trend_reviews"]

    assert set(reviews) == {"eastmoney", "tiger", "phillips"}
    assert reviews["eastmoney"]["market"] == "CN"
    assert reviews["tiger"]["strategy_snapshot"]["strategy_version"] == "v1"
    assert reviews["eastmoney"]["sample_counts"] == {
        "discipline": 31,
        "actual": 29,
        "required": 30,
    }
    assert reviews["eastmoney"]["common_cutoff"] == "2026-07-17"
    assert reviews["eastmoney"]["interval"] == {
        "start": "2026-07-16",
        "end": "2026-07-17",
    }
    assert reviews["phillips"]["metrics"]["calmar"]["actual"]["value"] == "9.4"
    assert not {
        "batch",
        "batch_path",
        "source_path",
        "source_artifacts",
    } & reviews["tiger"].keys()


def test_dashboard_keeps_null_common_cutoff_available(tmp_path: Path) -> None:
    payload = trend_review_projection_v3("US", "tiger")
    payload["common_cutoff"] = None
    payload["interval"] = {"start": "2026-07-17", "end": None}
    payload["metric_cutoffs"]["actual"] = None  # type: ignore[index]
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    review = dashboard_module._load_trend_reviews(tmp_path / "data")["tiger"]

    assert review["available"] is True
    assert review["sample_counts"] == {
        "discipline": 31,
        "actual": 29,
        "required": 30,
    }
    assert review["common_cutoff"] is None
    assert review["interval"] == {"start": "2026-07-17", "end": None}


@pytest.mark.parametrize(
    "snapshot",
    [{}, {"effective_from": "2026-07-17"}],
)
def test_dashboard_rejects_incomplete_snapshot_without_common_cutoff(
    tmp_path: Path, snapshot: dict[str, object]
) -> None:
    payload = trend_review_projection_v3("US", "tiger")
    payload["common_cutoff"] = None
    payload["interval"] = {"start": "2026-07-17", "end": None}
    payload["strategy_snapshot"] = snapshot
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    review = dashboard_module._load_trend_reviews(tmp_path / "data")["tiger"]

    assert review["available"] is False


def test_dashboard_accepts_strict_v4_trend_review_projection(tmp_path: Path) -> None:
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(trend_review_projection_v3("US", "tiger")), encoding="utf-8"
    )

    review = dashboard_module._load_trend_reviews(tmp_path / "data")["tiger"]

    assert review["available"] is True
    assert review["metrics"]["calmar"]["actual"]["value"] == "9.4"


def test_dashboard_accepts_current_snapshot_after_historical_interval_start(
    tmp_path: Path,
) -> None:
    payload = trend_review_projection_v3("US", "tiger")
    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot.update(
        {
            "strategy_id": "trend_animals_warm_to_hot/US/v4",
            "strategy_version": "v4",
            "effective_from": "2026-07-20",
        }
    )
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    review = dashboard_module._load_trend_reviews(tmp_path / "data")["tiger"]

    assert review["available"] is True
    assert review["interval"] == {"start": "2026-07-17", "end": "2026-07-17"}
    assert review["strategy_snapshot"]["effective_from"] == "2026-07-20"


def test_dashboard_accepts_failed_benchmark_refresh_with_prior_snapshot_metadata(
    tmp_path: Path,
) -> None:
    payload = trend_review_projection_v3("US", "tiger")
    payload["benchmark_refresh"] = {
        "status": "failed",
        "month": "2026-07",
        "completed_at": "2026-07-17T16:00:00+08:00",
        "process_git_sha": "abc1234",
        "cutoff": "2026-07-17",
        "refresh": {"force": False, "actor": None, "reason": None},
        "reason": "行情源不可用",
        "attempted_at": "2026-08-10T01:00:00+08:00",
        "attempt_process_git_sha": "failed-sha",
        "attempt_refresh": {"force": False, "actor": None, "reason": None},
    }
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    review = dashboard_module._load_trend_reviews(tmp_path / "data")["tiger"]

    assert review["available"] is True
    assert review["benchmark_refresh"]["status"] == "failed"
    assert review["benchmark_refresh"]["cutoff"] == "2026-07-17"


@pytest.mark.parametrize(
    ("mutation", "broker"),
    [
        (lambda payload: payload.update(schema_version="v1"), "tiger"),
        (lambda payload: payload.update(market="HK"), "tiger"),
        (lambda payload: payload["metrics"].pop("sharpe"), "tiger"),
        (
            lambda payload: payload["metrics"]["calmar"]["actual"].update(
                value="NaN"
            ),
            "tiger",
        ),
        (
            lambda payload: payload["strategy_snapshot"].update(parameter_rows=[]),
            "tiger",
        ),
        (
            lambda payload: payload["sample_counts"].update(discipline=True),
            "tiger",
        ),
        (
            lambda payload: payload["sample_counts"].update(actual=-1),
            "tiger",
        ),
        (
            lambda payload: payload["sample_counts"].update(required=29),
            "tiger",
        ),
        (
            lambda payload: payload["sample_counts"].update(internal=1),
            "tiger",
        ),
        (lambda payload: payload.update(common_cutoff="2026/07/17"), "tiger"),
        (lambda payload: payload.update(common_cutoff="2026-02-30"), "tiger"),
        (
            lambda payload: payload["interval"].update(start="2026-02-30"),
            "tiger",
        ),
        (
            lambda payload: payload["interval"].update(end="2026-07-18"),
            "tiger",
        ),
        (
            lambda payload: payload["interval"].update(source="internal"),
            "tiger",
        ),
        (
            lambda payload: payload["metric_cutoffs"].update(actual=None),
            "tiger",
        ),
        (
            lambda payload: payload["benchmark_context"].update(futu_symbol="US.QQQ"),
            "tiger",
        ),
        (
            lambda payload: payload["benchmark_context"]["same_period_dates"].append("2026-02-30"),
            "tiger",
        ),
        (
            lambda payload: payload["benchmark_context"]["windows"]["5Y"].update(return_basis="period_return"),
            "tiger",
        ),
        (
            lambda payload: payload["benchmark_context"]["windows"]["1Y"].update(start="2027-01-01"),
            "tiger",
        ),
        (
            lambda payload: payload["benchmark_refresh"].update(status="stale"),
            "tiger",
        ),
        (
            lambda payload: payload["benchmark_refresh"]["refresh"].update(actor="operator"),
            "tiger",
        ),
        (
            lambda payload: payload["benchmark_refresh"].update(extra="control"),
            "tiger",
        ),
        (lambda payload: payload.update(extra="control"), "tiger"),
    ],
)
def test_dashboard_rejects_invalid_trend_review_projection(
    tmp_path: Path, mutation: object, broker: str
) -> None:
    payload = trend_review_projection_v3("US", "tiger")
    mutation(payload)  # type: ignore[operator]
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    reviews = dashboard_module._load_trend_reviews(tmp_path / "data")

    assert reviews[broker] == {
        "available": False,
        "broker": "tiger",
        "broker_label": "老虎",
        "market": "US",
        "market_label": "美股",
        "status_text": "复盘数据无效",
    }


def option_attention(
    symbol: str, *, market: str = "US", source_broker: str = "老虎"
) -> dict[str, object]:
    unchanged = {"previous": False, "current": False, "changed": False}
    return {
        "market": market,
        "symbol": symbol,
        "name": symbol,
        "category": "watch",
        "right_side": unchanged,
        "temperature": {"previous": "温", "current": "热", "changed": True},
        "phase": {"previous": "谷雨", "current": "立夏", "changed": True},
        "local_strength": "95",
        "global_strength": "90",
        "strength_prev_week": "91",
        "strength_prev_month": "89",
        "strength_change": {"previous": "→", "current": "↑", "changed": True},
        "days": 1,
        "gain_since_entry": "0.02",
        "danger": unchanged,
        "boiling": unchanged,
        "champagne": unchanged,
        "source_broker": source_broker,
        "source_action": "WATCH",
    }


def test_dashboard_does_not_expose_retired_tiger_strategy(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [])

    state = load_dashboard_state(config).to_dict()

    assert "tiger_" + "long_term_strategy" not in state


def test_dashboard_projects_latest_same_day_trend_report_for_each_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_trader.trend_review import _report_hash

    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [])

    monkeypatch.setattr(
        dashboard_module,
        "_trend_market_date",
        lambda _market, *, now=None: date(2026, 7, 15),
    )
    for directory, account_source_date, data_sources in [
        (
            "trend_us_tiger",
            "2026-07-14",
            ["Trend Animals", "Tiger US daily K-line"],
        ),
        (
            "trend_hk_phillips",
            "2026-06",
            ["Trend Animals", "Futu HK daily K-line"],
        ),
        (
            "trend_a_share",
            "2026-07-14",
            ["Trend Animals"],
        ),
    ]:
        market, broker = {
            "trend_us_tiger": ("US", "tiger"),
            "trend_hk_phillips": ("HK", "phillips"),
            "trend_a_share": ("CN", "eastmoney"),
        }[directory]
        path = config.reports_dir / directory / "2026-07-15-b.json"
        path.parent.mkdir(parents=True)
        payload = {
            "execution_date": "2026-07-15",
            "as_of_date": "2026-07-14",
            "generated_at": "2026-07-15T11:30:36+08:00",
            "delivery_status": "sent",
            "account": {
                **serialized_trend_account(
                    fresh=directory != "trend_hk_phillips"
                ),
                "source_date": account_source_date,
                "exceptions": (
                    ["趋势判断不支持当前持仓：AAPL260717C200000（option）"]
                    if directory == "trend_us_tiger"
                    else []
                ),
            },
            "strategy_judgments": {
                "formal_actions": [
                    {"action": "SELL_ALL", "reason": "danger_signal", "symbol": "AAPL"},
                    {"action": "BUY", "symbol": "VIXY", "target_amount": "5000"},
                ],
                "holding_decisions": [
                    {"action": "SELL_ALL", "reason": "danger_signal", "symbol": "AAPL"},
                    {"action": "HOLD", "reason": "trend_intact", "symbol": "SPY"},
                ],
                "top10_candidates": [{"symbol": "VIXY", "strength": "95"}],
            },
            "industry_concentration": [["科技", 1, "0.25"]],
            "excluded": {"QQQ": ["already_held"]},
            "data_sources": data_sources,
            "estimated_api_cost": "1.20",
            "actual_api_cost": "1.00",
            "option_attention": (
                []
                if market == "CN"
                else [
                    option_attention(
                        "VIXY",
                        market=market,
                        source_broker="老虎" if market == "US" else "辉立",
                    )
                ]
            ),
            "metadata": {
                "market": market,
                "broker": broker,
                "delivery_status": "generated",
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        if directory == "trend_us_tiger":
            for filename, generated_at, symbol in (
                ("2026-07-15-a.json", "2026-07-15T11:30:36+08:00", "WRONG-A"),
                ("2026-07-15-z.json", "2026-07-15T10:00:00+08:00", "WRONG-Z"),
            ):
                revision = json.loads(json.dumps(payload))
                revision["generated_at"] = generated_at
                revision["strategy_judgments"]["formal_actions"][0]["symbol"] = symbol
                (path.parent / filename).write_text(json.dumps(revision), encoding="utf-8")
    events = config.data_dir / "trend_us_tiger/watch_events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(json.dumps({
        "event_type": "protection_triggered", "symbol": "AAPL",
        "occurred_at": "2026-07-15T22:00:00+08:00", "active_line": "190",
    }) + "\n", encoding="utf-8")
    log = config.data_dir / "trend_us_tiger/run.log"
    log.write_text(json.dumps({
        "event": "failed", "run_date": "2026-07-15",
    }) + "\n", encoding="utf-8")
    execution = (
        config.data_dir
        / "trend_review/ledgers/US/actions/2026-07-15/action-key"
        / "2026-07-15T10-00-00-04-00-event.json"
    )
    execution.parent.mkdir(parents=True)
    execution.write_text(
        json.dumps(
            {
                "market": "US",
                "date": "2026-07-15",
                "report_sha256": _report_hash(json.loads(
                    (
                        config.reports_dir
                        / "trend_us_tiger/2026-07-15-b.json"
                    ).read_text(encoding="utf-8")
                )),
                "symbol": "VIXY",
                "side": "buy",
                "status": "partially_filled",
                "filled_qty": "20",
                "target_qty": "40",
                "avg_fill_price": "50.25",
                "order_ids": ["SIM-1"],
                "recorded_at": "2026-07-15T10:00:00-04:00",
                "reason": "",
            }
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()
    reports = state["trend_reports"]

    assert set(reports) == {"tiger", "phillips", "eastmoney"}
    assert "trend_market_summaries" not in state
    assert reports["tiger"]["report_date"] == "2026-07-15"
    assert reports["tiger"]["data_date"] == "2026-07-14"
    assert reports["tiger"]["data_status"] == "current"
    assert reports["tiger"]["generated_at"] == "2026-07-15T11:30:36+08:00"
    assert reports["tiger"]["sell_actions"][0]["symbol"] == "AAPL"
    assert reports["tiger"]["buy_actions"][0]["execution"] == {
        "status": "partially_filled",
        "filled_qty": "20",
        "target_qty": "40",
        "avg_fill_price": "50.25",
        "order_ids": ["SIM-1"],
        "updated_at": "2026-07-15T10:00:00-04:00",
        "reason": "",
    }
    assert reports["tiger"]["counts"] == {"sell": 1, "buy": 1, "hold": 1, "review": 0}
    assert reports["tiger"]["run_status"] == "failed"
    assert reports["tiger"]["recent_protection_alert"] == (
        "AAPL · 2026-07-15T22:00:00+08:00 · 保护线 190"
    )
    assert reports["tiger"]["audit"] == {
        "candidates": [{"symbol": "VIXY", "strength": "95"}],
        "strategy_parameters": {},
        "excluded": {"QQQ": ["already_held"]},
        "industry_concentration": [["科技", 1, "0.25"]],
        "data_sources": ["Trend Animals", "Tiger US daily K-line"],
        "estimated_api_cost": "1.20",
        "actual_api_cost": "1.00",
        "account_exceptions": ["趋势判断不支持当前持仓：AAPL260717C200000（option）"],
        "artifact": "2026-07-15-b.json",
    }
    assert reports["phillips"]["buy_window"] == "09:30–10:00"
    assert reports["phillips"]["account_status"] == "账户数据非实时，执行前核对现金与持仓"
    assert reports["phillips"]["buy_actions"][0]["symbol"] == "VIXY"
    assert reports["phillips"]["counts"] == {"sell": 1, "buy": 1, "hold": 1, "review": 0}
    assert reports["eastmoney"]["market_label"] == "A股"
    assert reports["eastmoney"]["audit"]["data_sources"] == ["Trend Animals"]


def test_dashboard_projects_complete_candidate_audit_for_every_market(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [])
    top_ten = [{"symbol": "688046", "strength": "99.9"}]
    complete = [
        {
            "symbol": "688046", "eligible": True, "rank": 1,
            "excluded_reasons": [], "filter_price": "29.14",
        },
        {
            "symbol": "600000", "eligible": False, "rank": None,
            "excluded_reasons": ["strength_below_95"], "filter_price": "9.8",
        },
    ]
    for directory, market, broker in (
        ("trend_a_share", "CN", "eastmoney"),
        ("trend_us_tiger", "US", "tiger"),
    ):
        path = config.reports_dir / directory / "2026-07-15.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "execution_date": "2026-07-15",
            "as_of_date": "2026-07-14",
            "generated_at": "2026-07-15T20:00:00+08:00",
            "account": serialized_trend_account(fresh=True),
            "metadata": {"market": market, "broker": broker},
            "strategy_judgments": {
                "formal_actions": [],
                "holding_decisions": [],
                "top10_candidates": top_ten,
            },
            "signal_snapshots": {"candidates": complete},
            "option_attention": [],
        }), encoding="utf-8")

    reports = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )

    for broker in ("eastmoney", "tiger"):
        assert [item["symbol"] for item in reports[broker]["audit"]["candidates"]] == [
            "688046", "600000",
        ]


@pytest.mark.parametrize(
    ("market", "version", "broker"),
    [("CN", "v13", "eastmoney"), ("HK", "v11", "phillips"), ("US", "v11", "tiger")],
)
def test_dashboard_current_trend_uses_candidate_snapshot_and_fails_closed(
    tmp_path: Path,
    market: str,
    version: str,
    broker: str,
) -> None:
    config = dashboard_config(tmp_path)
    payload = _valid_v2_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    metadata = payload["metadata"]
    judgments = payload["strategy_judgments"]
    assert isinstance(snapshot, dict) and isinstance(metadata, dict)
    assert isinstance(judgments, dict)
    snapshot.update({
        "strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
        "strategy_version": version,
    })
    metadata.update({"market": market, "broker": broker})
    judgments["top10_candidates"] = [{"symbol": "TOP10-FALLBACK"}]

    payload["signal_snapshots"] = {}
    assert not dashboard_module._valid_trend_collections(payload, judgments)

    candidates = [{
        "symbol": "CURRENT",
        "name": "Current candidate",
        "eligible": True,
        "excluded_reasons": [],
        "rank": 1,
    }]
    payload["signal_snapshots"] = {"candidates": candidates}
    assert dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0]["excluded_reasons"] = ["contradictory"]
    assert not dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0]["excluded_reasons"] = []
    candidates[0]["rank"] = None
    assert not dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0].update({"eligible": False, "rank": 1, "excluded_reasons": ["not eligible"]})
    assert not dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0].update({"eligible": False, "rank": None, "excluded_reasons": []})
    assert not dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0].update({
        "eligible": True,
        "rank": 2,
        "excluded_reasons": [],
        "global_strength": None,
    })
    assert dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0].update({
        "eligible": False,
        "rank": None,
        "excluded_reasons": ["name_missing"],
    })
    candidates[0].pop("name")
    assert dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0]["name"] = ""
    assert dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0]["excluded_reasons"] = ["strength_below_95"]
    assert not dashboard_module._valid_trend_collections(payload, judgments)
    candidates[0].update({
        "eligible": True,
        "rank": 2,
        "excluded_reasons": [],
    })
    candidates[0]["name"] = "Current candidate"

    report = dashboard_module._project_broker_trend_report(
        selected=(
            config.reports_dir / f"{market}.json",
            payload,
            date(2026, 7, 15),
            date(2026, 7, 14),
            date(2026, 7, 15),
            datetime.fromisoformat("2026-07-15T20:00:00+08:00"),
        ),
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
        broker=broker,
        market=market,
        market_label=market,
        broker_label=broker,
        buy_window="常规交易时段",
        report_date="2026-07-15",
    )
    assert [item["symbol"] for item in report["audit"]["candidates"]] == ["CURRENT"]

    payload["signal_snapshots"] = {}
    report_without_snapshot = dashboard_module._project_broker_trend_report(
        selected=(
            config.reports_dir / f"{market}.json",
            payload,
            date(2026, 7, 15),
            date(2026, 7, 14),
            date(2026, 7, 15),
            datetime.fromisoformat("2026-07-15T20:00:00+08:00"),
        ),
        data_dir=config.data_dir,
        reports_dir=config.reports_dir,
        broker=broker,
        market=market,
        market_label=market,
        broker_label=broker,
        buy_window="常规交易时段",
        report_date="2026-07-15",
    )
    assert report_without_snapshot["audit"]["candidates"] == []


def test_dashboard_current_trend_risk_audit_requires_final_plan_rows() -> None:
    payload = _valid_v4_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    metadata = payload["metadata"]
    judgments = payload["strategy_judgments"]
    assert isinstance(snapshot, dict) and isinstance(metadata, dict)
    assert isinstance(judgments, dict)
    snapshot.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v13",
        "strategy_version": "v13",
    })
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters["target_weight"] = {"热": "0.04", "沸": "0.04"}
    metadata.update({"market": "CN", "broker": "eastmoney"})
    drawdown = payload["drawdown_summary"]
    assert isinstance(drawdown, dict)
    drawdown.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v13",
        "strategy_version": "v13",
        "kelly_sample_key": "CN|trend_animals_warm_to_hot/CN/v13|v13",
    })
    bootstrap = drawdown["bootstrap_event"]
    assert isinstance(bootstrap, dict)
    bootstrap.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v13",
        "strategy_version": "v13",
    })
    summary = payload["risk_summary"]
    assert isinstance(summary, dict)
    summary.update({
        "kelly_phase": "cold_start",
        "kelly_eligible_sample_count": 0,
        "kelly_selected_sample_count": 0,
        "kelly_cap": None,
        "kelly_reason": "Kelly 冷启动：0/30 个合格模拟闭环；继续使用固定风险仓位",
        "kelly_last_closed_at": "",
        "kelly_source": "合格的富途模拟闭环；实盘结果不参与计算",
    })
    judgments["risk_skips"].extend([
        {
            "symbol": "ROTATION",
            "target_weight": "0.04",
            "target_amount": "4000",
            "estimated_shares": 0,
            "reason": "relative_rotation",
            "decisive_constraint": "轮换终态",
        },
        {
            "symbol": "MAPPING",
            "target_weight": "0.04",
            "target_amount": None,
            "estimated_shares": 0,
            "reason": "symbol_mapping_unavailable",
            "decisive_constraint": "趋势代码映射",
        },
    ])
    judgments["risk_skips"].append({
        "symbol": "PLAN-SKIP",
        "target_weight": None,
        "target_amount": None,
        "estimated_shares": 0,
        "reason": "未纳入最终买入计划",
        "decisive_constraint": "买入计划",
    })
    assert dashboard_module._valid_trend_risk_summary(payload)

    judgments["risk_skips"][3]["estimated_shares"] = 1
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"][3]["estimated_shares"] = 0
    judgments["risk_skips"][3]["target_amount"] = "1"
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"][3]["target_amount"] = None
    judgments["risk_skips"][1]["estimated_shares"] = 1
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"][1]["estimated_shares"] = 0
    judgments["risk_skips"][1]["target_weight"] = "NaN"
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"][1]["target_weight"] = "0.04"
    judgments["risk_skips"][1]["target_weight"] = "0"
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"][1]["target_weight"] = "0.9"
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"][1]["target_weight"] = "0.04"

    pause_reason = "Kelly 上限为 0，仅暂停未来新开仓"
    judgments["formal_actions"] = []
    judgments["risk_skips"] = [{
        "symbol": "KELLY-PAUSE",
        "target_weight": "0",
        "target_amount": "0",
        "estimated_shares": 0,
        "reason": pause_reason,
        "decisive_constraint": "Kelly 上限",
    }]
    summary.update({
        "status": "paused",
        "status_label": "暂停新开仓",
        "pause_reason": pause_reason,
        "new_planned_risk": "0",
        "portfolio_planned_risk": "0",
        "portfolio_planned_risk_pct": "0",
        "portfolio_remaining_risk": "4000",
        "portfolio_remaining_risk_pct": "0.04",
        "kelly_phase": "active_all_samples",
        "kelly_eligible_sample_count": 30,
        "kelly_selected_sample_count": 30,
        "kelly_cap": "0.000000",
        "kelly_reason": pause_reason,
        "kelly_last_closed_at": "2026-07-14T16:00:00+00:00",
    })
    assert dashboard_module._valid_trend_risk_summary(payload)

    judgments.pop("risk_skips")
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"] = [{
        "symbol": "",
        "target_weight": None,
        "target_amount": None,
        "estimated_shares": 0,
        "reason": "未纳入最终买入计划",
        "decisive_constraint": "买入计划",
    }]
    assert not dashboard_module._valid_trend_risk_summary(payload)
    judgments["risk_skips"] = [{
        "symbol": "PLAN-SKIP",
        "target_weight": None,
        "target_amount": None,
        "estimated_shares": 0,
        "reason": "未纳入最终买入计划",
        "decisive_constraint": "买入计划",
    }]
    payload.pop("risk_summary")
    assert not dashboard_module._valid_trend_risk_summary(payload)


def test_dashboard_individual_global_context_mode_requires_current_only_facts() -> None:
    payload = _dashboard_frozen_report_payload()
    snapshot = payload["strategy_snapshot"]
    metadata = payload["metadata"]
    assert isinstance(snapshot, dict) and isinstance(metadata, dict)
    snapshot.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v13",
        "strategy_version": "v13",
    })
    metadata["market"] = "CN"
    status = payload["industry_context_status"]
    assert isinstance(status, dict)
    status.update({
        "ordering_mode": "individual_global",
        "current_complete": True,
        "history_complete": False,
        "fallback_reason": None,
    })
    assert dashboard_module._valid_frozen_trend_facts(payload)

    status["history_complete"] = True
    assert not dashboard_module._valid_frozen_trend_facts(payload)
    status["history_complete"] = False
    status["fallback_reason"] = "missing current context"
    assert not dashboard_module._valid_frozen_trend_facts(payload)

    current_without_facts = copy.deepcopy(payload)
    current_without_facts.pop("api_cost", None)
    current_without_facts.pop("industry_context_status", None)
    current_without_facts.pop("industry_contexts", None)
    assert not dashboard_module._valid_frozen_trend_facts(current_without_facts)
    current_legacy_cost = copy.deepcopy(current_without_facts)
    api_cost = current_legacy_cost["api_cost"] = {
        "actual": "0.479",
        "estimated": "0.479",
        "estimate_complete": False,
        "unit": "Trend Animals 余额单位",
    }
    assert isinstance(api_cost, dict)
    assert not dashboard_module._valid_frozen_trend_facts(current_legacy_cost)

    legacy = _dashboard_frozen_report_payload()
    legacy_status = legacy["industry_context_status"]
    assert isinstance(legacy_status, dict)
    legacy_status.update({
        "ordering_mode": "individual_global",
        "current_complete": True,
        "history_complete": False,
        "fallback_reason": None,
    })
    assert not dashboard_module._valid_frozen_trend_facts(legacy)
    legacy_without_facts = _dashboard_frozen_report_payload()
    legacy_without_facts.pop("api_cost", None)
    legacy_without_facts.pop("industry_context_status", None)
    legacy_without_facts.pop("industry_contexts", None)
    assert dashboard_module._valid_frozen_trend_facts(legacy_without_facts)


def test_dashboard_rejects_malformed_signal_candidate_audit_when_present(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T20:00:00+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "signal_snapshots": {"candidates": [None]},
        "option_attention": [],
    }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is False
    assert report["data_status"] == "unavailable"
    assert report["status_text"] == "暂时不可用"


def test_dashboard_trend_report_falls_back_to_latest_valid_stale_report(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_us_tiger" / "2026-07-14.json"
    path.parent.mkdir(parents=True)
    stale_attention = [option_attention("STALE-OPTION")]
    stale_payload = {
        "execution_date": "2026-07-14",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-14T18:00:00+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [
                {"action": "BUY", "symbol": "STALE-BUY"},
                {"action": "SELL_ALL", "reason": "danger_signal", "symbol": "STALE-SELL"},
            ],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": stale_attention,
    }
    path.write_text(json.dumps(stale_payload), encoding="utf-8")
    malformed_newest = json.loads(json.dumps(stale_payload))
    malformed_newest["execution_date"] = "2026-07-15"
    malformed_newest["generated_at"] = "2026-07-15T18:00:00+08:00"
    malformed_newest["option_attention"][0]["headline"] = "unknown field"
    (path.parent / "2026-07-15.json").write_text(
        json.dumps(malformed_newest), encoding="utf-8"
    )

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is True
    assert report["data_status"] == "stale"
    assert report["status_text"] == "数据截至 2026-07-14；今日未更新"
    assert report["option_attention"] == stale_attention
    assert report["sell_actions"][0]["symbol"] == "STALE-SELL"
    assert report["buy_actions"][0]["symbol"] == "STALE-BUY"


def test_dashboard_trend_report_returns_unavailable_without_a_valid_report(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_us_tiger" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text("{malformed", encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is False
    assert report["data_status"] == "unavailable"
    assert report["status_text"] == "暂时不可用"


def test_dashboard_trend_report_switches_from_stale_to_later_current_report(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    base = {
        "as_of_date": "2026-07-14",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
    }
    (reports_dir / "2026-07-14.json").write_text(json.dumps({
        **base,
        "execution_date": "2026-07-14",
        "generated_at": "2026-07-14T18:00:00+08:00",
        "option_attention": [option_attention("STALE")],
    }), encoding="utf-8")
    current_attention = [option_attention("CURRENT")]
    (reports_dir / "2026-07-15.json").write_text(json.dumps({
        **base,
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-15",
        "generated_at": "2026-07-15T18:00:00+08:00",
        "option_attention": current_attention,
    }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["data_status"] == "current"
    assert report["report_date"] == "2026-07-15"
    assert report["option_attention"] == current_attention


def test_dashboard_uses_market_local_date_for_current_us_report(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    (reports_dir / "2026-07-20.json").write_text(json.dumps({
        "execution_date": "2026-07-21",
        "as_of_date": "2026-07-20",
        "generated_at": "2026-07-21T22:44:00+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [option_attention("QQQ")],
    }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        now=datetime(
            2026, 7, 22, 1, 0, tzinfo=dashboard_module.SHANGHAI
        ),
    )["tiger"]

    assert report["data_status"] == "current"
    assert report["report_date"] == "2026-07-21"


def test_dashboard_hk_friday_report_is_current_then_stale_then_current_for_execution(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_hk_phillips"
    reports_dir.mkdir(parents=True)
    base = {
        "account": serialized_trend_account(fresh=False),
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
    }
    (reports_dir / "2026-07-16.json").write_text(json.dumps({
        **base,
        "execution_date": "2026-07-17",
        "as_of_date": "2026-07-16",
        "generated_at": "2026-07-16T18:00:00+08:00",
        "metadata": {
            "market": "HK", "broker": "phillips", "run_date": "2026-07-16",
        },
        "option_attention": [
            option_attention("STALE", market="HK", source_broker="辉立")
        ],
    }), encoding="utf-8")
    current_attention = [
        option_attention("CURRENT", market="HK", source_broker="辉立")
    ]
    (reports_dir / "2026-07-17.json").write_text(json.dumps({
        **base,
        "execution_date": "2026-07-20",
        "as_of_date": "2026-07-17",
        "generated_at": "2026-07-17T18:00:00+08:00",
        "metadata": {
            "market": "HK", "broker": "phillips", "run_date": "2026-07-17",
        },
        "option_attention": current_attention,
    }), encoding="utf-8")

    friday = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 17)
    )["phillips"]
    saturday = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 18)
    )["phillips"]
    monday_reports = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 20)
    )
    monday = monday_reports["phillips"]

    assert friday["data_status"] == "current"
    assert friday["report_date"] == "2026-07-20"
    assert friday["option_attention"] == current_attention
    assert saturday["data_status"] == "stale"
    assert monday["data_status"] == "current"
    assert monday["status_text"] == "今日执行（数据截至 2026-07-17）"


def test_dashboard_legacy_hk_friday_report_uses_generated_date_for_freshness(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_hk_phillips/2026-07-20.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "execution_date": "2026-07-20",
        "as_of_date": "2026-07-17",
        "generated_at": "2026-07-17T18:00:00+08:00",
        "account": serialized_trend_account(fresh=False),
        "metadata": {"market": "HK", "broker": "phillips"},
        "strategy_judgments": {
            "formal_actions": [], "holding_decisions": [], "top10_candidates": [],
        },
        "option_attention": [],
    }), encoding="utf-8")

    friday = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 17)
    )["phillips"]
    saturday = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 18)
    )["phillips"]

    assert friday["data_status"] == "current"
    assert saturday["data_status"] == "stale"
    assert saturday["report_date"] == "2026-07-20"
    assert saturday["status_text"] == "数据截至 2026-07-17；今日未更新"


def _valid_v2_dashboard_trend_payload() -> dict[str, object]:
    risk_summary = {
        "status": "active",
        "status_label": "风险预算内",
        "pause_reason": "",
        "existing_planned_risk": "0",
        "new_planned_risk": "303",
        "portfolio_planned_risk": "303",
        "portfolio_planned_risk_pct": "0.00303",
        "portfolio_risk_limit": "4000",
        "portfolio_risk_limit_pct": "0.04",
        "portfolio_remaining_risk": "3697",
        "portfolio_remaining_risk_pct": "0.03697",
        "single_entry_risk_limit": "400",
        "single_entry_risk_limit_pct": "0.004",
        "abnormal_loss_buffer": "1000",
        "abnormal_loss_buffer_pct": "0.01",
        "total_risk_budget_target_pct": "0.05",
        "normal_cost_rate": "0.001",
        "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
        "disclaimer": "5% 是风险预算目标，不是最大损失保证。",
        "portfolio_remaining_risk_note": (
            "组合剩余风险供本报告后续新仓共享，不等于单标的仓位上限。"
        ),
    }
    risk_skips = [{
        "symbol": "600002",
        "target_weight": "0.04",
        "target_amount": "4000",
        "estimated_shares": 0,
        "reason": "最小交易单位 100 股超过组合剩余风险",
        "decisive_constraint": "组合剩余风险",
    }]
    return {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T20:00:00+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_snapshot": {
            "strategy_id": "trend_animals_warm_to_hot/CN/v2",
            "strategy_version": "v2",
            "parameters": {
                "single_entry_risk_limit": "0.004",
                "portfolio_risk_limit": "0.04",
                "abnormal_loss_buffer": "0.01",
                "normal_cost_rate": "0.001",
                "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
            },
        },
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY",
                "symbol": "600001",
                "target_weight": "0.04",
                "target_amount": "4000",
                "estimated_shares": 300,
                "lot_size": 100,
                "close": "10",
                "planned_stop_risk": "303",
                "planned_stop_risk_pct": "0.00303",
                "normal_cost": "3",
                "decisive_constraint": "单笔风险上限",
            }],
            "holding_decisions": [],
            "top10_candidates": [],
            "risk_skips": risk_skips,
        },
        "risk_summary": risk_summary,
        "option_attention": [],
    }


def _valid_v3_dashboard_trend_payload() -> dict[str, object]:
    payload = copy.deepcopy(_valid_v2_dashboard_trend_payload())
    snapshot = payload["strategy_snapshot"]
    summary = payload["risk_summary"]
    assert isinstance(snapshot, dict) and isinstance(summary, dict)
    snapshot["strategy_version"] = "v3"
    snapshot["strategy_id"] = "trend_animals_warm_to_hot/CN/v3"
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters.update(
        {
            "kelly_sample_minimum": 30,
            "kelly_rolling_window": 200,
            "kelly_fraction": "0.25",
            "kelly_optimizer": "mean_log_growth_derivative_bisection_96_floor_1e-6",
            "kelly_sample_scope": "market+strategy_id+opening_strategy_version",
            "kelly_source": "cost_complete_attributed_simulation_closed_rounds",
        }
    )
    summary.update(
        {
            "kelly_phase": "cold_start",
            "kelly_eligible_sample_count": 0,
            "kelly_selected_sample_count": 0,
            "kelly_cap": None,
            "kelly_reason": "Kelly 冷启动：0/30 个合格模拟闭环；继续使用固定风险仓位",
            "kelly_last_closed_at": "",
            "kelly_source": "合格的富途模拟闭环；实盘结果不参与计算",
        }
    )
    return payload


def test_dashboard_v10_risk_items_accept_ranked_six_percent_target() -> None:
    payload = _valid_v2_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    judgments = payload["strategy_judgments"]
    assert isinstance(snapshot, dict) and isinstance(judgments, dict)
    snapshot["strategy_version"] = "v10"
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters["target_weight"] = "0.06"
    buy = judgments["formal_actions"][0]
    skip = judgments["risk_skips"][0]
    assert isinstance(buy, dict) and isinstance(skip, dict)
    buy.update({"target_weight": "0.06", "target_amount": "6000"})
    skip.update({"target_weight": "0.06", "target_amount": "6000"})
    summary = payload["risk_summary"]
    assert isinstance(summary, dict)
    summary["kelly_phase"] = "cold_start"

    assert dashboard_module._valid_v2_risk_items(
        payload,
        judgments,
        summary,
        strategy_version="v10",
    )


def test_dashboard_enforces_issue_4_and_kelly_contract_for_v3(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v3_dashboard_trend_payload()
    summary = payload["risk_summary"]
    assert isinstance(summary, dict)
    summary["normal_cost_rate"] = "0.009"
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is False


def test_dashboard_accepts_exact_v3_zero_kelly_pause(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v3_dashboard_trend_payload()
    judgments = payload["strategy_judgments"]
    summary = payload["risk_summary"]
    assert isinstance(judgments, dict) and isinstance(summary, dict)
    judgments["formal_actions"] = []
    judgments["risk_skips"][0].update(
        {
            "target_weight": "0",
            "target_amount": "0",
            "reason": "Kelly 上限为 0，仅暂停未来新开仓",
            "decisive_constraint": "Kelly 上限",
        }
    )
    summary.update(
        {
            "status": "paused",
            "status_label": "暂停新开仓",
            "pause_reason": "Kelly 上限为 0，仅暂停未来新开仓",
            "new_planned_risk": "0",
            "portfolio_planned_risk": "0",
            "portfolio_planned_risk_pct": "0",
            "portfolio_remaining_risk": "4000",
            "portfolio_remaining_risk_pct": "0.04",
            "kelly_phase": "active_all_samples",
            "kelly_eligible_sample_count": 30,
            "kelly_selected_sample_count": 30,
            "kelly_cap": "0.000000",
            "kelly_reason": "Kelly 上限为 0，仅暂停未来新开仓",
            "kelly_last_closed_at": "2026-07-14T16:00:00+00:00",
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is True
    assert report["risk_summary"]["kelly_cap"] == "0.000000"


def test_dashboard_projects_frozen_risk_summary_and_skips(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert {
        key: value
        for key, value in report["risk_summary"].items()
        if key != "trade_stats"
    } == payload["risk_summary"]
    assert report["risk_summary"]["trade_stats"] == {
        "available": False,
        "status_text": "交易统计暂不可用",
    }
    assert report["risk_skips"] == payload["strategy_judgments"]["risk_skips"]


def test_dashboard_projects_frozen_strategy_parameters_into_cn_audit(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters.update({
        "max_filter_price": "200",
        "min_strength": "95",
        "allowed_industry_temperatures": ["热", "沸"],
        "allowed_phases": ["谷雨", "立夏", "夏至"],
        "min_market_cap_100m": "100",
        "min_amount_100m": "2",
    })
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is True
    assert report["audit"]["strategy_parameters"] == parameters
    assert report["audit"]["strategy_parameters"] is not parameters


def _dashboard_frozen_report_payload(
    *,
    market: str = "CN",
    broker: str = "eastmoney",
    candidate_pool_ids: tuple[int, ...] = (),
) -> dict[str, object]:
    report = trend_module.build_report(
        as_of_date="2026-07-15",
        execution_date="2026-07-15",
        generated_at="2026-07-15T20:00:00+08:00",
        account=trend_module.AccountSnapshot(
            source_date="2026-07-15",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("50000"),
            positions=(),
            exceptions=(),
        ),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        market=market,
        metadata={"market": market, "broker": broker, "run_date": "2026-07-15"},
        estimated_api_cost=Decimal("0.479"),
        actual_api_cost=None,
        estimated_api_cost_complete=False,
        candidate_pool_ids=candidate_pool_ids,
        account_input={
            "snapshot_generation": "sha256:" + "a" * 64,
            "account_generation": "sha256:" + "b" * 64,
            "status": "healthy",
        },
    )
    payload = trend_module._report_payload(report)
    if market != "CN":
        payload["option_attention"] = []
    return payload


def test_dashboard_projects_frozen_cost_contexts_and_parameter_rows(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _dashboard_frozen_report_payload()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
        current_candidate_pool_ids={
            market: config.trend_candidate_pool_ids(market)
            for market in ("CN", "US", "HK")
        },
    )["eastmoney"]

    assert projected["api_cost"] == payload["api_cost"]
    assert projected["industry_context_status"] == payload["industry_context_status"]
    assert projected["industry_contexts"] == payload["industry_contexts"]
    assert projected["strategy_parameter_rows"] == payload["strategy_snapshot"][
        "parameter_rows"
    ]
    assert projected["current_strategy_version"] == "v10"
    current_rows = {
        row["name"]: row["value"]
        for row in projected["current_strategy_parameter_rows"]
    }
    assert current_rows["目标仓位"] == "账户净值的 4%"
    assert "沸状态仓位" not in current_rows
    assert "过热止盈比例" not in current_rows
    assert "过热跟踪" not in current_rows
    assert projected["audit"]["estimated_api_cost"] == payload[
        "estimated_api_cost"
    ]
    assert projected["audit"]["actual_api_cost"] == payload["actual_api_cost"]


def test_dashboard_projects_only_valid_frozen_allocation_contract(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    roots = {
        market: {
            "stock": {"asset": stock, "tm_id": index * 10, "as_of_date": "2026-08-03", "global_strength": stock_strength},
            "etf": {"asset": etf, "tm_id": index * 10 + 1, "as_of_date": "2026-08-03", "global_strength": etf_strength},
        }
        for index, (market, stock, etf, stock_strength, etf_strength) in enumerate(
            (("CN", "A股", "ETF基金", "90", "80"), ("HK", "港股", "香港ETF", "70", "60"), ("US", "美股", "美国ETF", "50", "40")), 1
        )
    }
    snapshot = build_allocation_snapshot(
        allocation_date="2026-08-03", generated_at="2026-08-03T16:18:00+08:00",
        git_sha="a" * 40, roots=roots, previous=None,
    )
    payload = _dashboard_frozen_report_payload()
    payload["allocation"] = {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json", "sha256": "b" * 64,
        "allocation_date": "2026-08-03", "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False, "stale_a_trading_days": 0, "failure_reason": "",
        "roots": snapshot["roots"], "markets": snapshot["markets"],
    }
    payload["strategy_snapshot"] = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199),
        allocation={
            "daily_path": payload["allocation"]["daily_path"],
            "sha256": payload["allocation"]["sha256"],
            "snapshot": snapshot,
        },
    )
    payload["drawdown_summary"] = copy.deepcopy(
        _valid_v4_dashboard_trend_payload()["drawdown_summary"]
    )
    drawdown = payload["drawdown_summary"]
    assert isinstance(drawdown, dict)
    drawdown.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v13",
        "strategy_version": "v13",
        "kelly_sample_key": "CN|trend_animals_warm_to_hot/CN/v13|v13",
    })
    bootstrap = drawdown["bootstrap_event"]
    assert isinstance(bootstrap, dict)
    bootstrap.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v13",
        "strategy_version": "v13",
    })
    payload["industry_context_status"] = {
        "ordering_mode": "individual_global",
        "current_complete": True,
        "history_complete": False,
        "fallback_reason": None,
    }
    judgments = payload["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["simulate_rotation_pairs"] = []
    judgments["simulate_rotation_comparisons"] = []
    judgments["real_rotation_pairs"] = []
    judgments["real_rotation_comparisons"] = []
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
        current_candidate_pool_ids={"CN": (622466, 697199)},
    )["eastmoney"]
    assert projected["allocation"] == payload["allocation"]
    assert projected["simulate_rotation_pairs"] == []
    assert projected["current_strategy_version"] == "v13"
    assert next(
        row["value"]
        for row in projected["current_strategy_parameter_rows"]
        if row["name"] == "目标仓位"
    ) == "账户净值的 6%"

    payload["allocation"]["daily_path"] = "data/trend_allocation/latest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15),
    )["eastmoney"]["available"] is False


def test_dashboard_projects_frozen_rotation_comparisons_and_signal_strengths(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    payload = _dashboard_frozen_report_payload()
    payload["allocation"] = {
        "daily_path": "data/trend_allocation/daily/2026-08-03.json",
        "sha256": "b" * 64,
        "allocation_date": "2026-08-03",
        "generated_at": "2026-08-03T16:18:00+08:00",
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
        "roots": {},
        "markets": {},
    }
    payload["signal_snapshots"] = {
        "holdings": {"SELL": {"strength": "76", "global_strength": "61"}},
        "candidates": [
            {"symbol": "BUY", "strength": "88", "global_strength": "96"},
        ],
    }
    judgments = payload["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["formal_actions"] = [{"action": "BUY", "symbol": "BUY", "strength": "88"}]
    judgments["simulate_rotation_pairs"] = []
    judgments["real_rotation_pairs"] = []
    comparison = {
        "pair_index": 0,
        "sell_symbol": "SELL",
        "sell_name": "弱势股票",
        "sell_asset": "A股",
        "sell_local_strength": "76",
        "sell_global_strength": "61",
        "buy_symbol": "BUY",
        "buy_name": "强势ETF",
        "buy_asset": "ETF基金",
        "buy_local_strength": "88",
        "buy_global_strength": "96",
        "strength_basis": "global",
        "sell_compared_strength": "61",
        "buy_compared_strength": "96",
        "strength_gap": "35",
        "threshold": "20",
        "outcome": "planned",
        "reason": "relative_rotation",
    }
    judgments["simulate_rotation_comparisons"] = [comparison]
    judgments["real_rotation_comparisons"] = []
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    projected = dashboard_module._project_broker_trend_report(
        selected=(
            path, payload, date(2026, 7, 15), date(2026, 7, 15),
            date(2026, 7, 15), datetime.fromisoformat("2026-07-15T20:00:00+08:00"),
        ),
        data_dir=config.data_dir,
        reports_dir=path.parent,
        broker="eastmoney", market="CN", market_label="A股", broker_label="东方财富",
        buy_window="09:30–10:00", report_date="2026-07-15",
    )
    assert projected["simulate_rotation_comparisons"] == [comparison]
    assert projected["buy_actions"][0]["global_strength"] == "96"

    historical = _dashboard_frozen_report_payload()
    path.write_text(json.dumps(historical, ensure_ascii=False), encoding="utf-8")
    historical_projected = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15),
    )["eastmoney"]
    assert historical_projected["available"] is True
    assert historical_projected["simulate_rotation_pairs"] == []
    assert historical_projected["real_rotation_pairs"] == []

    allocationless_pairs = _dashboard_frozen_report_payload()
    judgments = allocationless_pairs["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["simulate_rotation_pairs"] = []
    judgments["real_rotation_pairs"] = []
    path.write_text(json.dumps(allocationless_pairs, ensure_ascii=False), encoding="utf-8")
    assert dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15),
    )["eastmoney"]["available"] is False


def test_dashboard_accepts_frozen_provider_aggregate_industry_ratios(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15-r1.json"
    path.parent.mkdir(parents=True)
    payload = _dashboard_frozen_report_payload()
    payload["industry_contexts"] = [
        {
            "industry_tm_id": 339103,
            "industry": "银行",
            "as_of_date": "2026-07-15",
            "component_count": 42,
            "snapshot_count": 42,
            "tradable_count": 42,
            "valid_count": 42,
            "right_count": 8,
            "snapshot_coverage": "1",
            "right_state_coverage": "1",
            "right_share": "0.190476",
            "warm_to_hot_count": 6,
            "temperature": "热",
            "strength": "100",
            "valid": True,
            "invalid_reasons": [],
            "member_breadth_collected": False,
            "aggregate_right_count_ratio": "0.191",
            "aggregate_right_market_cap_ratio": "0.650",
            "prior_as_of_date": "2026-07-14",
            "prior_temperature": "温",
            "prior_right_share": "0.150",
            "prior_aggregate_right_count_ratio": "0.150",
            "prior_aggregate_right_market_cap_ratio": "0.600",
            "temperature_direction": "rising",
            "right_share_change_pp": "4.0476",
        }
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert projected["available"] is True
    assert projected["artifact"] == "2026-07-15-r1.json"
    assert projected["industry_contexts"][0][
        "aggregate_right_market_cap_ratio"
    ] == "0.650"
    assert projected["industry_contexts"][0]["member_breadth_collected"] is False


@pytest.mark.parametrize(
    ("market", "broker", "directory", "frozen_ids", "current_ids"),
    [
        ("US", "tiger", "trend_us_tiger", (622460,), (622460, 705013)),
        ("HK", "phillips", "trend_hk_phillips", (622494,), (622494, 707617)),
    ],
)
def test_dashboard_current_discipline_uses_configured_etf_pools_for_old_reports(
    tmp_path: Path,
    market: str,
    broker: str,
    directory: str,
    frozen_ids: tuple[int, ...],
    current_ids: tuple[int, ...],
) -> None:
    config = dashboard_config(tmp_path)
    payload = _dashboard_frozen_report_payload(
        market=market,
        broker=broker,
        candidate_pool_ids=frozen_ids,
    )
    path = config.reports_dir / directory / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
        current_candidate_pool_ids={market: current_ids},
    )[broker]

    source = next(
        row["value"]
        for row in report["current_strategy_parameter_rows"]
        if row["name"] == "趋势动物组合"
    )
    assert source == "、".join(str(pool_id) for pool_id in current_ids)
    frozen_source = next(
        row["value"]
        for row in report["strategy_parameter_rows"]
        if row["name"] == "趋势动物组合"
    )
    assert frozen_source == "、".join(str(pool_id) for pool_id in frozen_ids)


def test_dashboard_does_not_label_frozen_pools_as_current_without_config(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    payload = _dashboard_frozen_report_payload(
        market="US",
        broker="tiger",
        candidate_pool_ids=(622460,),
    )
    path = config.reports_dir / "trend_us_tiger/2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
        current_candidate_pool_ids={},
    )["tiger"]

    assert report["current_strategy_version"] == ""
    assert report["current_strategy_parameter_rows"] is None
    assert report["strategy_parameter_rows"]


def test_dashboard_rejects_malformed_frozen_cost_projection(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _dashboard_frozen_report_payload()
    api_cost = payload["api_cost"]
    assert isinstance(api_cost, dict)
    api_cost["estimated"] = "not-a-decimal"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert projected["available"] is False
    assert projected["status_text"] == "暂时不可用"


def test_dashboard_rejects_malformed_frozen_parameter_rows(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _dashboard_frozen_report_payload()
    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["parameter_rows"] = [{"group": "坏"}]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert projected["available"] is False
    assert projected["status_text"] == "暂时不可用"


def test_dashboard_accepts_pre_task5_api_cost_shape(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _dashboard_frozen_report_payload()
    api_cost = payload["api_cost"]
    assert isinstance(api_cost, dict)
    api_cost.pop("label")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert projected["available"] is True
    assert projected["api_cost"] == api_cost


def test_dashboard_accepts_legacy_api_cost_without_context_facts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    payload["api_cost"] = {
        "actual": "1.00",
        "estimated": "1.20",
        "estimate_complete": True,
        "unit": "Trend Animals 余额单位",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert projected["available"] is True
    assert projected["api_cost"] == payload["api_cost"]
    assert projected["industry_context_status"] == {}
    assert projected["industry_contexts"] == []
    assert projected["strategy_parameter_rows"] == []


def test_dashboard_legacy_projection_keeps_raw_cost_and_empty_new_facts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    payload["estimated_api_cost"] = "1.20"
    payload["actual_api_cost"] = "1.00"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    projected = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert projected["available"] is True
    assert projected["api_cost"] is None
    assert projected["industry_context_status"] == {}
    assert projected["industry_contexts"] == []
    assert projected["strategy_parameter_rows"] == []
    assert projected["audit"]["estimated_api_cost"] == "1.20"
    assert projected["audit"]["actual_api_cost"] == "1.00"


@pytest.mark.parametrize("parameters", ["missing", ["legacy"]])
def test_dashboard_projects_legacy_strategy_parameters_as_empty_dict(
    tmp_path: Path, parameters: object,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    payload = _valid_v2_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    if parameters == "missing":
        snapshot.pop("parameters")
    else:
        snapshot["parameters"] = parameters

    report = dashboard_module._project_broker_trend_report(
        selected=(
            path,
            payload,
            date(2026, 7, 15),
            date(2026, 7, 14),
            date(2026, 7, 15),
            datetime.fromisoformat("2026-07-15T20:00:00+08:00"),
        ),
        data_dir=config.data_dir,
        reports_dir=path.parent,
        broker="eastmoney",
        market="CN",
        market_label="A股",
        broker_label="东方财富",
        buy_window="09:30–10:00",
        report_date="2026-07-15",
    )

    assert report["available"] is True
    assert report["audit"]["strategy_parameters"] == {}


def _valid_v4_dashboard_trend_payload() -> dict[str, object]:
    payload = copy.deepcopy(_valid_v3_dashboard_trend_payload())
    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "strategy_version": "v4",
    })
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters.update({
        "drawdown_limit": "0.05",
        "drawdown_equity_source": "Futu SIMULATE strategy NAV",
        "drawdown_unlock": "manual_same_version_rebase",
    })
    payload["drawdown_summary"] = {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "strategy_version": "v4",
        "kelly_sample_key": "CN|trend_animals_warm_to_hot/CN/v4|v4",
        "state_status": "ok",
        "status": "active",
        "status_label": "纪律内",
        "entry_allowed": True,
        "current_equity": "100000",
        "high_water_mark": "100000",
        "drawdown_pct": "0",
        "drawdown_limit_pct": "0.05",
        "pause_reason": "",
        "paused_at": None,
        "observed_at": "2026-07-15T20:00:00+08:00",
        "bootstrap_event": {
            "event_id": "automatic-bootstrap-" + "1" * 64,
            "event_type": "automatic_bootstrap",
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v4",
            "strategy_version": "v4",
            "actor": "acceptance",
            "occurred_at": "2026-07-15T08:00:00+08:00",
            "baseline_equity": "100000",
            "source_date": "2026-07-14",
            "accepted_git_sha": "a" * 40,
            "parameter_hash": "b" * 64,
            "reason": "first_activation",
            "entry_eligible_from": "2026-07-15",
        },
        "recovery_event": None,
    }
    return payload


def _valid_v6_dashboard_trend_payload() -> dict[str, object]:
    payload = copy.deepcopy(_valid_v4_dashboard_trend_payload())
    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v6",
        "strategy_version": "v6",
    })
    drawdown = payload["drawdown_summary"]
    assert isinstance(drawdown, dict)
    drawdown.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v6",
        "strategy_version": "v6",
        "kelly_sample_key": "CN|trend_animals_warm_to_hot/CN/v6|v6",
    })
    bootstrap = drawdown["bootstrap_event"]
    assert isinstance(bootstrap, dict)
    bootstrap.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v6",
        "strategy_version": "v6",
    })
    return payload


def test_dashboard_accepts_cn_v6_risk_and_drawdown_contract() -> None:
    payload = _valid_v6_dashboard_trend_payload()
    assert dashboard_module._valid_trend_risk_summary(payload)

    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters.pop("single_entry_risk_limit")
    assert not dashboard_module._valid_trend_risk_summary(payload)


def test_dashboard_accepts_cn_v7_risk_and_drawdown_contract() -> None:
    payload = _valid_v6_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    drawdown = payload["drawdown_summary"]
    assert isinstance(snapshot, dict) and isinstance(drawdown, dict)
    snapshot.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v7",
        "strategy_version": "v7",
    })
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters["kelly_sample_inherits"] = [{
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v4",
        "opening_strategy_version": "v4",
    }]
    drawdown.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v7",
        "strategy_version": "v7",
        "kelly_sample_key": "CN|trend_animals_warm_to_hot/CN/v7|v7",
    })
    bootstrap = drawdown["bootstrap_event"]
    assert isinstance(bootstrap, dict)
    bootstrap.update({
        "strategy_id": "trend_animals_warm_to_hot/CN/v7",
        "strategy_version": "v7",
    })

    assert dashboard_module._valid_trend_risk_summary(payload)


@pytest.mark.parametrize("strategy_version", ["v5", "v8", "v9", "v10"])
def test_dashboard_accepts_current_live_risk_and_drawdown_contract(
    strategy_version: str,
) -> None:
    payload = _valid_v6_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    drawdown = payload["drawdown_summary"]
    assert isinstance(snapshot, dict) and isinstance(drawdown, dict)
    snapshot.update({
        "strategy_id": f"trend_animals_warm_to_hot/CN/{strategy_version}",
        "strategy_version": strategy_version,
    })
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters["kelly_sample_inherits"] = [
        {
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v4",
            "opening_strategy_version": "v4",
        },
        {
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v7",
            "opening_strategy_version": "v7",
        },
    ]
    drawdown.update({
        "strategy_id": f"trend_animals_warm_to_hot/CN/{strategy_version}",
        "strategy_version": strategy_version,
        "kelly_sample_key": (
            f"CN|trend_animals_warm_to_hot/CN/{strategy_version}|{strategy_version}"
        ),
    })
    bootstrap = drawdown["bootstrap_event"]
    assert isinstance(bootstrap, dict)
    bootstrap.update({
        "strategy_id": f"trend_animals_warm_to_hot/CN/{strategy_version}",
        "strategy_version": strategy_version,
    })

    assert dashboard_module._valid_trend_risk_summary(payload)
    parameters.pop("single_entry_risk_limit")
    assert not dashboard_module._valid_trend_risk_summary(payload)


def test_dashboard_v4_keeps_plan_risk_and_drawdown_as_separate_validated_facts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v4_dashboard_trend_payload()
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert {
        key: value
        for key, value in report["risk_summary"].items()
        if key != "trade_stats"
    } == payload["risk_summary"]
    assert report["drawdown_summary"] == payload["drawdown_summary"]


@pytest.mark.parametrize(
    ("market", "raw_market_cap", "raw_amount", "expected_market_cap", "expected_amount"),
    [
        ("US", "3610", "52", "26239.35185185185185185185185", "377.962962962962962962962963"),
        ("HK", "3610", "52", "3342.592592592592592592592592", "48.14814814814814814814814815"),
    ],
)
def test_dashboard_projects_legacy_trend_money_as_cny_billions(
    market: str,
    raw_market_cap: str,
    raw_amount: str,
    expected_market_cap: str,
    expected_amount: str,
) -> None:
    payload = {
        "metadata": {"market": market},
        "strategy_snapshot": {"strategy_version": "v1"},
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY",
                "symbol": "TARGET",
                "market_cap": raw_market_cap,
                "amount": raw_amount,
            }],
            "holding_decisions": [],
        },
    }

    _, buys, _, _ = dashboard_module._project_trend_actions(payload, {})

    assert buys[0]["market_cap_cny_100m"] == expected_market_cap
    assert buys[0]["amount_cny_100m"] == expected_amount


def test_dashboard_holding_phase_projection_uses_frozen_snapshot() -> None:
    payload = {
        "metadata": {"market": "HK"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [
                {"action": "HOLD", "symbol": "00939", "reason": "trend_intact"},
            ],
        },
        "signal_snapshots": {
            "holdings": {
                "00939": {"phase": "立夏"},
            },
        },
    }

    _, _, holds, _ = dashboard_module._project_trend_actions(payload, {})

    assert holds[0]["phase"] == "立夏"


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_dashboard_projects_holdings_in_strength_order(
    market: str,
) -> None:
    payload = {
        "metadata": {"market": market},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [
                {"action": "HOLD", "symbol": "MED", "strength": "95", "reason": "trend_intact"},
                {"action": "HOLD", "symbol": "FIN", "strength": "80", "reason": "trend_intact"},
            ],
        },
        "signal_snapshots": {
            "holdings": {
                "MED": {"industry": "医疗保健", "industry_tm_id": 1, "days": 7},
                "FIN": {"industry": "金融", "industry_tm_id": 2, "days": 8},
            },
        },
        "industry_contexts": [
            {
                "industry_tm_id": 1,
                "industry": "医疗保健",
                "temperature": "温",
                "strength": "90",
                "warm_to_hot_count": 8,
                "right_share": "0.19",
                "valid": True,
                "prior_as_of_date": "2026-07-21",
                "prior_temperature": "温",
                "prior_right_share": "0.18",
                "temperature_direction": "unchanged",
                "right_share_change_pp": "1",
            },
            {
                "industry_tm_id": 2,
                "industry": "金融",
                "temperature": "热",
                "strength": "100",
                "warm_to_hot_count": 11,
                "right_share": "0.25",
                "valid": True,
                "prior_as_of_date": "2026-07-21",
                "prior_temperature": "温",
                "prior_right_share": "0.22",
                "temperature_direction": "rising",
                "right_share_change_pp": "3",
            },
        ],
    }
    original_payload = copy.deepcopy(payload)

    _, _, holds, _ = dashboard_module._project_trend_actions(payload, {})

    assert [item["symbol"] for item in holds] == ["MED", "FIN"]
    assert holds[0]["industry"] == "医疗保健"
    assert holds[0]["industry_tm_id"] == 1
    assert holds[0]["days"] == 7
    assert payload == original_payload


def test_dashboard_preserves_frozen_trend_cny_money_fields() -> None:
    payload = {
        "metadata": {"market": "US"},
        "strategy_snapshot": {"strategy_version": "v5"},
        "strategy_judgments": {
            "formal_actions": [{
                "action": "BUY",
                "symbol": "TARGET",
                "market_cap": "3610",
                "amount": "52",
                "market_cap_cny_100m": "12345.678",
                "amount_cny_100m": "377.654",
                "cny_per_local_currency": "7.25",
            }],
            "holding_decisions": [],
        },
    }

    _, buys, _, _ = dashboard_module._project_trend_actions(payload, {})

    assert buys[0]["market_cap_cny_100m"] == "12345.678"
    assert buys[0]["amount_cny_100m"] == "377.654"


def test_dashboard_projects_only_strict_partial_sells_and_full_exit_wins() -> None:
    valid_partial = {
        "action": "SELL_PARTIAL",
        "symbol": "SH.600001",
        "reason": "overheat_take_profit",
        "target_fraction": "0.30",
        "position_started_for": "2026-07-01",
        "estimated_shares": 300,
        "lot_size": 100,
        "overheat_signals": ["boiling"],
    }
    invalid_partial = {
        **valid_partial,
        "symbol": "600002",
        "estimated_shares": 301,
    }
    payload = {
        "metadata": {"market": "CN"},
        "strategy_judgments": {
            "formal_actions": [
                valid_partial,
                {"action": "SELL_ALL", "symbol": "600001", "reason": "danger_signal"},
                invalid_partial,
            ],
            "holding_decisions": [],
        },
    }

    sells, _, _, reviews = dashboard_module._project_trend_actions(payload, {})

    assert [item["action"] for item in sells] == ["SELL_ALL"]
    assert reviews == [invalid_partial]


@pytest.mark.parametrize("missing_section", ["risk_summary", "drawdown_summary"])
def test_dashboard_v4_missing_risk_contract_fails_closed(
    tmp_path: Path, missing_section: str,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v4_dashboard_trend_payload()
    del payload[missing_section]
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is False


def test_dashboard_projects_exact_version_api_stats_into_risk_summary(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    path.write_text(json.dumps(payload), encoding="utf-8")
    stats = build_trend_api_stats_payload(
        [],
        strategy_versions=[{
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v2",
            "strategy_version": "v2",
        }],
        generated_at="2026-07-20T12:00:00+08:00",
        statistics_cutoff_at="2026-07-20T11:59:59+08:00",
    )
    stats["sources"] = [{
        "source": "actual",
        "source_id": "actual:eastmoney:eastmoney_main",
        "broker": "eastmoney",
        "account_id": "eastmoney_main",
        "market": "CN",
        "orders_seen": 0,
        "fill_count": 0,
        "statistics_cutoff_at": "2026-07-17T23:59:59+08:00",
        "status": "available",
    }]
    write_trend_api_stats(config.data_dir, stats)

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    trade_stats = report["risk_summary"]["trade_stats"]
    assert trade_stats["available"] is True
    assert trade_stats["strategy_id"] == "trend_animals_warm_to_hot/CN/v2"
    assert trade_stats["opening_strategy_version"] == "v2"
    assert trade_stats["statistics_cutoff_at"] == "2026-07-17T23:59:59+08:00"
    assert trade_stats["actual_broker"] == "eastmoney"
    assert trade_stats["actual_broker_label"] == "东方财富"
    assert trade_stats["simulation"]["eligible_sample_count"] == 0
    assert trade_stats["actual"]["eligible_sample_count"] == 0


def test_dashboard_api_stats_projection_fails_closed_for_malformed_artifact(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    report_path = config.reports_dir / "trend_a_share/2026-07-15.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(_valid_v2_dashboard_trend_payload()), encoding="utf-8"
    )
    stats_path = config.data_dir / "latest/trend_api_stats.json"
    stats_path.parent.mkdir(parents=True)
    stats_path.write_text('{"schema_version":"wrong"}', encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is True
    assert report["risk_summary"]["trade_stats"] == {
        "available": False,
        "status_text": "交易统计暂不可用",
    }


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("summary", "portfolio_remaining_risk", "-999"),
        ("summary", "single_entry_risk_limit_pct", "0.4"),
        ("summary", "abnormal_loss_buffer_pct", "10"),
        ("summary", "existing_planned_risk", "NaN"),
        ("summary", "portfolio_remaining_risk_pct", None),
        ("summary", "normal_cost_model", "bogus"),
        ("summary", "disclaimer", "guaranteed max loss"),
        ("parameters", "normal_cost_rate", "0.009"),
        ("risk_skip", "reason", ""),
        ("risk_skip", "estimated_shares", 1),
        ("buy", "planned_stop_risk", None),
        ("buy", "planned_stop_risk_pct", "0.4"),
    ],
)
def test_dashboard_v2_risk_contract_fails_closed(
    tmp_path: Path, section: str, key: str, value: object,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = copy.deepcopy(_valid_v2_dashboard_trend_payload())
    if section == "summary":
        target = payload["risk_summary"]
    elif section == "parameters":
        target = payload["strategy_snapshot"]["parameters"]
    elif section == "risk_skip":
        target = payload["strategy_judgments"]["risk_skips"][0]
    else:
        target = payload["strategy_judgments"]["formal_actions"][0]
    assert isinstance(target, dict)
    target[key] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is False


def test_dashboard_accepts_v2_paused_unknown_risk_amounts(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    judgments = payload["strategy_judgments"]
    assert isinstance(judgments, dict)
    judgments["formal_actions"] = []
    summary = payload["risk_summary"]
    assert isinstance(summary, dict)
    summary.update({
        "status": "paused",
        "status_label": "暂停新开仓",
        "pause_reason": "模拟持仓风险事实缺失，暂停新开仓",
        "existing_planned_risk": None,
        "new_planned_risk": "0",
        "portfolio_planned_risk": None,
        "portfolio_planned_risk_pct": None,
        "portfolio_remaining_risk": None,
        "portfolio_remaining_risk_pct": None,
    })
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is True


@pytest.mark.parametrize(
    "state",
    [
        "paused_with_buy",
        "active_over_limit",
        "amount_scale_drift",
        "buy_zero_risk",
        "buy_partial_lot",
    ],
)
def test_dashboard_v2_risk_state_invariants_fail_closed(
    tmp_path: Path, state: str,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    summary = payload["risk_summary"]
    assert isinstance(summary, dict)
    if state == "paused_with_buy":
        summary.update({
            "status": "paused",
            "status_label": "暂停新开仓",
            "pause_reason": "测试暂停",
        })
    elif state == "active_over_limit":
        summary.update({
            "existing_planned_risk": "4001",
            "new_planned_risk": "303",
            "portfolio_planned_risk": "4304",
            "portfolio_planned_risk_pct": "0.04304",
            "portfolio_remaining_risk": "0",
            "portfolio_remaining_risk_pct": "0",
        })
    elif state == "amount_scale_drift":
        judgments = payload["strategy_judgments"]
        assert isinstance(judgments, dict)
        judgments["formal_actions"] = []
        summary.update({
            "new_planned_risk": "0",
            "portfolio_planned_risk": "0",
            "portfolio_planned_risk_pct": "0",
            "portfolio_risk_limit": "8000",
            "portfolio_remaining_risk": "8000",
            "portfolio_remaining_risk_pct": "0.04",
            "single_entry_risk_limit": "800",
            "abnormal_loss_buffer": "2000",
        })
    else:
        judgments = payload["strategy_judgments"]
        assert isinstance(judgments, dict)
        buy = judgments["formal_actions"][0]
        assert isinstance(buy, dict)
        if state == "buy_zero_risk":
            buy.update({
                "planned_stop_risk": "0",
                "planned_stop_risk_pct": "0",
                "normal_cost": "0",
            })
            summary.update({
                "new_planned_risk": "0",
                "portfolio_planned_risk": "0",
                "portfolio_planned_risk_pct": "0",
                "portfolio_remaining_risk": "4000",
                "portfolio_remaining_risk_pct": "0.04",
            })
        else:
            buy.update({"estimated_shares": 350, "lot_size": 100})
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is False


def test_dashboard_trend_report_today_uses_shanghai_date_at_utc_boundary() -> None:
    assert dashboard_module._shanghai_date(
        datetime(2026, 7, 14, 16, 30, tzinfo=UTC)
    ) == date(2026, 7, 15)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("strategy_judgments", None),
        ("formal_actions", {}),
        ("holding_decisions", {}),
        ("top10_candidates", {}),
        ("account", []),
        ("execution_date", "not-a-date"),
        ("execution_date", "20260715"),
        ("as_of_date", ""),
        ("as_of_date", 20260714),
        ("as_of_date", "20260714"),
        ("generated_at", ""),
        ("generated_at", 20260715113036),
        ("generated_at", "not-a-timestamp"),
        ("generated_at", "2026-07-15T11:30:36+0800"),
        ("generated_at", "2026-07-15T11:30:36"),
        ("option_attention", [None]),
    ],
)
def test_dashboard_trend_report_skips_invalid_newest_candidate(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    valid_payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": "VALID-BUT-OLDER"}],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [option_attention("VALID-BUT-OLDER")],
    }
    invalid_payload = json.loads(json.dumps(valid_payload))
    invalid_payload["strategy_judgments"]["formal_actions"][0]["symbol"] = (
        "INVALID-NEWEST"
    )
    if field in {"formal_actions", "holding_decisions", "top10_candidates"}:
        invalid_payload["strategy_judgments"][field] = invalid_value
    else:
        invalid_payload[field] = invalid_value
    (reports_dir / "2026-07-15-b.json").write_text(
        json.dumps(invalid_payload), encoding="utf-8"
    )
    valid_payload["generated_at"] = "2026-07-15T10:00:00+08:00"
    (reports_dir / "2026-07-15-a.json").write_text(
        json.dumps(valid_payload), encoding="utf-8"
    )

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is True
    assert report["data_status"] == "current"
    assert report["buy_actions"][0]["symbol"] == "VALID-BUT-OLDER"


def test_dashboard_trend_report_ranks_revisions_by_generated_instant(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-15",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": "EARLIER"}],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [],
    }
    (reports_dir / "2026-07-15-a.json").write_text(json.dumps({
        **payload,
        "generated_at": "2026-07-15T10:00:00+08:00",
    }), encoding="utf-8")
    later = json.loads(json.dumps(payload))
    later["generated_at"] = "2026-07-15T09:30:00+07:00"
    later["strategy_judgments"]["formal_actions"][0]["symbol"] = "LATER"
    (reports_dir / "2026-07-15-b.json").write_text(
        json.dumps(later), encoding="utf-8"
    )

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["generated_at"] == "2026-07-15T09:30:00+07:00"
    assert report["buy_actions"][0]["symbol"] == "LATER"


def _write_valid_us_trend_report(
    reports_dir: Path,
    *,
    execution_date: str,
    as_of_date: str,
    generated_at: str,
) -> None:
    payload = {
        "account": serialized_trend_account(fresh=True),
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [],
    }
    (reports_dir / f"{execution_date}.json").write_text(json.dumps({
        **payload,
        "execution_date": execution_date,
        "as_of_date": as_of_date,
        "generated_at": generated_at,
        "metadata": {
            "market": "US",
            "broker": "tiger",
            "run_date": execution_date,
        },
    }), encoding="utf-8")


def test_dashboard_trend_report_selects_latest_valid_next_execution_day(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    for execution_date, as_of_date in (
        ("2026-07-15", "2026-07-14"),
        ("2026-07-16", "2026-07-15"),
    ):
        _write_valid_us_trend_report(
            reports_dir,
            execution_date=execution_date,
            as_of_date=as_of_date,
            generated_at=f"{execution_date}T07:30:00+08:00",
        )

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is True
    assert report["artifact"] == "2026-07-16.json"
    assert report["report_date"] == "2026-07-16"


def test_dashboard_us_main_view_uses_latest_report_before_new_york_midnight(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    for execution_date, as_of_date, generated_at in (
        ("2026-07-15", "2026-07-14", "2026-07-15T23:57:00+08:00"),
        ("2026-07-16", "2026-07-15", "2026-07-16T07:30:00+08:00"),
    ):
        _write_valid_us_trend_report(
            reports_dir,
            execution_date=execution_date,
            as_of_date=as_of_date,
            generated_at=generated_at,
        )

    now = datetime(2026, 7, 16, 7, 30, tzinfo=dashboard_module.SHANGHAI)
    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        now=now,
    )["tiger"]

    assert dashboard_module._trend_market_date("US", now=now) == date(2026, 7, 15)
    assert report["artifact"] == "2026-07-16.json"


@pytest.mark.parametrize("run_date", ["not-a-date", "2026-07-16"])
def test_dashboard_trend_report_rejects_invalid_source_run_date(
    tmp_path: Path, run_date: str,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    base = {
        "account": serialized_trend_account(fresh=True),
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [],
    }
    (reports_dir / "2026-07-14.json").write_text(json.dumps({
        **base,
        "execution_date": "2026-07-14",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-14T18:00:00+08:00",
        "metadata": {"market": "US", "broker": "tiger"},
    }), encoding="utf-8")
    (reports_dir / "2026-07-15.json").write_text(json.dumps({
        **base,
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-15",
        "generated_at": "2026-07-15T18:00:00+08:00",
        "metadata": {
            "market": "US", "broker": "tiger", "run_date": run_date,
        },
    }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["tiger"]

    assert report["data_status"] == "stale"
    assert report["report_date"] == "2026-07-14"


def test_dashboard_trend_report_routes_unknown_actions_and_reasons_to_review(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    unknown_action = {"action": "WAIT", "reason": "trend_intact", "symbol": "600001"}
    unknown_reason = {"action": "SELL_ALL", "reason": "new_reason", "symbol": "600002"}
    valid_buy = {"action": "BUY", "symbol": "600003"}
    unknown_buy_reason = {"action": "BUY", "reason": "new_reason", "symbol": "600004"}
    path.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "CN", "broker": "eastmoney"},
        "strategy_judgments": {
            "formal_actions": [
                unknown_action, unknown_reason, valid_buy, unknown_buy_reason,
            ],
            "holding_decisions": [unknown_reason],
            "top10_candidates": [],
        },
        "option_attention": [],
    }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert report["review_actions"] == [
        unknown_action, unknown_reason, unknown_buy_reason,
    ]
    assert report["counts"]["review"] == 3
    assert report["buy_actions"] == [valid_buy]


def test_dashboard_trend_report_rejects_misrouted_broker_metadata(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_us_tiger" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": {},
        "metadata": {"market": "HK", "broker": "phillips"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "industry_concentration": [],
        "excluded": {},
        "data_sources": [],
        "option_attention": [],
    }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is False
    assert report["data_status"] == "unavailable"
    assert report["status_text"] == "暂时不可用"


@pytest.mark.parametrize(
    "fresh", [False, MISSING_FRESH, None, "yes"]
)
def test_dashboard_trend_report_keeps_buy_for_non_realtime_account(
    tmp_path: Path, fresh: object,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_hk_phillips" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    stale_buy = {"action": "BUY", "symbol": "02800", "name": "盈富基金"}
    path.write_text(json.dumps({
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(fresh=fresh),
        "metadata": {"market": "HK", "broker": "phillips"},
        "strategy_judgments": {
            "formal_actions": [stale_buy],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "excluded": {},
        "industry_concentration": [],
        "data_sources": [],
        "option_attention": [],
    }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["phillips"]

    assert report["account_fresh"] is False
    assert report["account_status"] == "账户数据非实时，执行前核对现金与持仓"
    assert report["buy_actions"][0]["action"] == stale_buy["action"]
    assert report["buy_actions"][0]["symbol"] == stale_buy["symbol"]
    assert report["buy_actions"][0]["name"] == stale_buy["name"]
    assert report["buy_actions"][0]["option_anomaly"]["available"] is False
    assert report["review_actions"] == []
    assert report["counts"]["buy"] == 1
    assert report["counts"]["review"] == 0


@pytest.mark.parametrize(
    "account",
    [
        None,
        {},
        {**serialized_trend_account(), "source_date": ""},
        {**serialized_trend_account(), "source_date": "not-a-date"},
        {**serialized_trend_account(), "source_date": "2026-13"},
        {**serialized_trend_account(), "source_date": "2026-02-30"},
        {**serialized_trend_account(), "net_value": "Infinity"},
        {**serialized_trend_account(), "available_cash": None},
        {**serialized_trend_account(), "positions": ["not-a-position"]},
        {**serialized_trend_account(), "positions": [{}]},
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "symbol": ""}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [{**serialized_trend_position(), "name": ""}],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "asset_class": ""}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "quantity": "NaN"}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "market_value": None}
            ],
        },
        {
            **serialized_trend_account(),
            "positions": [
                {**serialized_trend_position(), "avg_cost_price": "Infinity"}
            ],
        },
        {**serialized_trend_account(), "exceptions": [1]},
    ],
)
def test_dashboard_trend_report_rejects_missing_or_malformed_account(
    tmp_path: Path, account: object,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_us_tiger" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [{"action": "BUY", "symbol": "VIXY"}],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "excluded": {},
        "industry_concentration": [],
        "data_sources": [],
        "option_attention": [],
    }
    if account is not None:
        payload["account"] = account
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is False
    assert report["status_text"] == "暂时不可用"
    assert "buy_actions" not in report


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("formal_actions", [None]),
        ("holding_decisions", [None]),
        ("top10_candidates", [None]),
        ("excluded", {"BAD": "not-a-list"}),
        ("industry_concentration", [None]),
        ("data_sources", "not-a-list"),
        ("api_facts", [None]),
    ],
)
def test_dashboard_trend_report_rejects_malformed_nested_audit_collections(
    tmp_path: Path, field: str, value: object,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_us_tiger" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-14",
        "generated_at": "2026-07-15T11:30:36+08:00",
        "account": serialized_trend_account(),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "excluded": {},
        "industry_concentration": [],
        "data_sources": [],
        "api_facts": [],
        "option_attention": [],
    }
    if field in payload["strategy_judgments"]:
        payload["strategy_judgments"][field] = value
    else:
        payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is False
    assert report["status_text"] == "暂时不可用"


@pytest.mark.parametrize(
    "attention",
    [
        MISSING_ATTENTION,
        None,
        {},
        [None],
        [
            {
                key: value
                for key, value in option_attention("QQQ").items()
                if key != "symbol"
            }
        ],
        [{**option_attention("QQQ"), "headline": "arbitrary markup"}],
        [{**option_attention("QQQ"), "category": []}],
        [{**option_attention("QQQ"), "name": {}}],
        [{**option_attention("QQQ"), "local_strength": []}],
        [{**option_attention("QQQ"), "days": {}}],
        [{**option_attention("QQQ"), "gain_since_entry": []}],
        [{**option_attention("QQQ"), "days": float("nan")}],
        [{**option_attention("QQQ"), "gain_since_entry": float("inf")}],
        [{**option_attention("QQQ"), "danger": {"current": True}}],
        [{
            **option_attention("QQQ"),
            "right_side": {
                "previous": [], "current": False, "changed": True,
            },
        }],
        [{
            **option_attention("QQQ"),
            "temperature": {
                "previous": "温", "current": {}, "changed": True,
            },
        }],
        [{
            **option_attention("QQQ"),
            "strength_change": {
                "previous": float("-inf"), "current": 1, "changed": True,
            },
        }],
        [{
            **option_attention("QQQ"),
            "phase": {
                "previous": "谷雨", "current": "立夏", "changed": "yes",
            },
        }],
    ],
)
def test_dashboard_trend_report_rejects_malformed_option_attention(
    tmp_path: Path, attention: object,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_us_tiger" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = {
        "execution_date": "2026-07-15",
        "as_of_date": "2026-07-15",
        "generated_at": "2026-07-15T18:00:00+08:00",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
    }
    if attention is not MISSING_ATTENTION:
        payload["option_attention"] = attention
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is False
    assert report["data_status"] == "unavailable"
    assert report["status_text"] == "暂时不可用"


@pytest.mark.parametrize(
    ("attention", "available"),
    [
        (MISSING_ATTENTION, True),
        ([], True),
        ([None], False),
        ([option_attention("600001", market="CN", source_broker="东方财富")], False),
    ],
)
def test_dashboard_real_cn_report_only_allows_empty_option_attention(
    tmp_path: Path, attention: object, available: bool,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share" / "2026-07-15.json"
    path.parent.mkdir(parents=True)
    report = trend_module.build_report(
        as_of_date="2026-07-15",
        execution_date="2026-07-15",
        generated_at="2026-07-15T18:00:00+08:00",
        account=trend_module.AccountSnapshot(
            source_date="2026-07-15",
            fresh=True,
            net_value=Decimal("100000"),
            available_cash=Decimal("50000"),
            positions=(),
            exceptions=(),
        ),
        candidates=(),
        holding_snapshots={},
        bars_by_symbol={},
        metadata={"market": "CN", "broker": "eastmoney"},
        account_input={
            "snapshot_generation": "sha256:" + "a" * 64,
            "account_generation": "sha256:" + "b" * 64,
            "status": "healthy",
        },
    )
    payload = trend_module._report_payload(report)
    assert "option_attention" not in payload
    if attention is not MISSING_ATTENTION:
        payload["option_attention"] = attention
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["eastmoney"]

    assert report["available"] is available
    assert report["data_status"] == ("current" if available else "unavailable")
    if available:
        assert report["option_attention"] == []


def dashboard_decision_plan(run_date: str) -> dict[str, object]:
    facts = {
        "ma20_distance_pct": {
            "formula": "(close / sma20 - 1) * 100",
            "inputs": {"close": "48.5", "sma20": "47"},
            "source_date": run_date,
            "calculated_value": "3.1915",
        },
        "rsi14": {
            "formula": "Wilder RSI(close, 14)",
            "inputs": {"period": "14"},
            "source_date": run_date,
            "calculated_value": "52",
        },
        "bollinger_position": {
            "formula": "compare(close, bollinger bands)",
            "inputs": {"close": "48.5"},
            "source_date": run_date,
            "calculated_value": "inside",
        },
        "relative_volume": {
            "formula": "volume / SMA(previous volume, 20)",
            "inputs": {"volume": "120", "average_volume": "100"},
            "source_date": run_date,
            "calculated_value": "1.2",
        },
    }
    snapshot = {
        "strategy": {"id": "trend_pullback/v1", "name_zh": "趋势回调"},
        "facts": facts,
        "conditions": [
            {
                "condition_id": "trend-exit",
                "priority": "risk",
                "operator": "<=",
                "calculated_value": "42",
                "target_weight": "0",
                "suggested_action": "退出",
                "formula": "min(sma50, active_stop)",
                "inputs": {"sma50": "43", "active_stop": "42"},
                "source_date": run_date,
            }
        ],
    }
    backtests = [
        {
            "strategy_id": "trend_pullback/v1",
            "range": range_name,
            "gate": {
                "passed": True,
                "policy_id": "benchmark_outperformance/v1",
                "reasons": [],
            },
            "strategy": {
                "total_return_pct": "8",
                "max_drawdown_pct": "6",
                "sharpe_ratio": "1.1",
            },
            "market_benchmark": {"symbol": "SPY", "total_return_pct": "5"},
            "market_excess_return_pct": "3",
        }
        for range_name in ("6M", "1Y")
    ]
    return build_decision_plan(
        run_date=run_date,
        market="US",
        symbol="VIXY",
        position={"quantity": "100", "weight": "0.08", "nav": "60625", "price": "48.5"},
        strategy_snapshots=[snapshot],
        backtests=backtests,
        technical_facts=facts,
        tradingagents_summary={"current_action": "观察"},
        effective_at=f"{run_date}T09:30:00-04:00",
        expires_at=f"{run_date}T16:00:00-04:00",
    )
def test_dashboard_exposes_eastmoney_statement_metadata() -> None:
    assert BROKER_LABELS["eastmoney"] == "东方财富"


def test_dashboard_backtest_universe_keeps_legacy_watchlist_only(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    rows: list[dict[str, str]] = []
    for market, symbol in [("US", "MSFT"), ("HK", "00700")]:
        row = {field: "" for field in PORTFOLIO_FIELDNAMES}
        row.update({"market": market, "symbol": symbol, "asset_class": "stock"})
        rows.append(row)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)
    write_csv(
        config.data_dir / "latest/watchlist.csv",
        ["market", "symbol"],
        [
            {"market": "US", "symbol": "MSFT"},
            {"market": "US", "symbol": "NVDA"},
            {"market": "HK", "symbol": "00700"},
        ],
    )

    payload = load_dashboard_state(config).to_dict()

    assert payload["backtest_universe"]["holdings"] == []
    assert [(row["market"], row["symbol"]) for row in payload["backtest_universe"]["watchlist"]] == [
        ("US", "MSFT"), ("US", "NVDA"), ("HK", "00700"),
    ]


def test_dashboard_keeps_other_holdings_out_of_scoped_market_loaders(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    rows: list[dict[str, str]] = []
    for market, symbol in [("US", "MSFT"), ("OTHER", "PRIVATE")]:
        row = {field: "" for field in PORTFOLIO_FIELDNAMES}
        row.update({
            "market": market,
            "symbol": symbol,
            "asset_class": "stock",
            "currency": "HKD",
            "market_value": "100",
            "market_value_hkd": "100",
            "portfolio_weight_hkd": "50.00%",
        })
        rows.append(row)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)

    payload = load_dashboard_state(config).to_dict()

    assert {(row["market"], row["symbol"]) for row in payload["holding_enrichment"]} == {
        ("US", "MSFT"),
    }


def raw_decision_with_market_report(report: str) -> str:
    return json.dumps({"state": {"market_report": report}}, ensure_ascii=False)


def raw_decision_with_all_reports() -> str:
    return json.dumps(
        {
            "state": {
                "market_report": "K report",
                "sentiment_report": "Sentiment report",
                "news_report": "News report",
            }
        },
        ensure_ascii=False,
    )


def write_decision_facts(path: Path, kline_hash: str, news_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.decision_facts.v1",
                "generated_at": "2026-06-19T08:31:00+08:00",
                "run_date": "2026-06-19",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.decision_facts.v1",
                        "run_date": "2026-06-19",
                        "market": "US",
                        "symbol": "VIXY",
                        "source_status": "ok",
                        "kline": {
                            "status": "ok",
                            "source_hash": kline_hash,
                            "fields": {
                                "trend": "趋势偏强",
                                "position": "价格处于均线附近",
                                "momentum": "动能温和",
                                "key_levels": "关键位置明确",
                                "risk": "波动风险较高",
                            },
                        },
                        "news_sentiment": {
                            "status": "ok",
                            "source_hash": news_hash,
                            "fields": {
                                "direction": "情绪偏谨慎",
                                "change": "变化有限",
                                "catalyst": "新闻催化有限",
                                "risk": "消息面风险存在",
                                "attention": "关注宏观波动",
                            },
                        },
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_futu_skill_facts(path: Path, *, run_date: str = "2026-07-01") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.futu_skill_facts.v1",
                "generated_at": "2026-07-01T09:15:00+08:00",
                "run_date": run_date,
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.futu_skill_facts.v1",
                        "run_date": run_date,
                        "market": "US",
                        "symbol": "VIXY",
                        "name": "ProShares VIX Short-Term Futures ETF",
                        "news_sentiment": {
                            "status": "ok",
                            "signal": "supportive",
                            "confidence": "medium",
                            "freshness": {
                                "generated_at": "2026-07-01T09:10:00+08:00",
                                "source_window": "latest",
                            },
                            "evidence": [
                                {
                                    "title": "Volatility ETF news digest",
                                    "summary": "市场波动相关讨论升温。",
                                    "url": "https://example.com/vixy",
                                    "source": "news",
                                }
                            ],
                            "domestic_discussion": {
                                "status": "ok",
                                "keyword_counts": [
                                    {"keyword": "震荡", "count": 2},
                                    {"keyword": "看空", "count": 1},
                                ],
                                "summary": "富途社区相关讨论较少，主要关注波动率 ETF 的短线风险。",
                                "focus": "关注波动率 ETF 与美股风险偏好的联动。",
                                "divergence_risk": "样本少且噪声高，不能代表稳定共识。",
                                "credibility": "低",
                                "trading_constraint": "仅作为国内讨论温度参考，不作为单独交易依据。",
                                "post_count": 3,
                                "relevant_post_count": 1,
                            },
                            "blocking_reason": "",
                            "suggested_constraint": "",
                        },
                        "technical_anomaly": {
                            "status": "ok",
                            "signal": "supportive",
                            "confidence": "medium",
                            "suggested_constraint": "",
                            "window_days": 7,
                            "summary": "技术信号支持趋势。",
                            "categories": [
                                {
                                    "name": "MACD",
                                    "state": "anomaly",
                                    "direction": "bullish",
                                    "detail": "金叉后继续放大。",
                                    "evidence_date": "2026-07-01",
                                }
                            ],
                        },
                        "capital_anomaly": {
                            "status": "ok",
                            "signal": "mixed",
                            "confidence": "medium",
                            "suggested_constraint": "no_add",
                            "window_days": 7,
                            "summary": "资金流向与加仓动作存在分歧。",
                            "categories": [
                                {
                                    "name": "资金流向",
                                    "state": "anomaly",
                                    "direction": "bearish",
                                    "detail": "主力资金连续净流出。",
                                    "evidence_date": "2026-07-02",
                                }
                            ],
                        },
                        "derivatives_anomaly": {
                            "status": "partial",
                            "signal": "risk_up",
                            "confidence": "low",
                            "suggested_constraint": "no_add",
                            "window_days": 7,
                            "summary": "期权波动率偏高。",
                            "categories": [
                                {
                                    "name": "期权波动率",
                                    "state": "anomaly",
                                    "direction": "risk_up",
                                    "detail": "IV 位于高位。",
                                    "evidence_date": "2026-07-02",
                                }
                            ],
                        },
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_technical_facts(
    path: Path,
    *,
    report_hash: str,
    market: str = "US",
    extraction_status: str = "ok",
    source_type: str = "tradingagents_market_report",
    timeframes: list[dict[str, object]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.technical_facts_cache.v1",
                "generated_at": "2026-06-19T08:30:00+08:00",
                "run_date": "2026-06-19",
                "market": "",
                "records": [
                    {
                        "run_date": "2026-06-19",
                        "market": market,
                        "symbol": "VIXY",
                        "source_status": "ok",
                        "source_advice_hash": report_hash,
                        "source_type": source_type,
                        "extraction_status": extraction_status,
                        "error": "" if extraction_status == "ok" else "llm unavailable",
                        "facts": {
                            "schema_version": "open_trader.technical_facts.v1",
                            "status": "present",
                            "source_date": "2026-06-19",
                            "market_data_as_of": "2026-06-18",
                            "symbol": f"{market}.VIXY",
                            "timeframes": timeframes
                            if timeframes is not None
                            else [
                                {
                                    "timeframe": "daily",
                                    "timeframe_label": "日线",
                                    "rsi": {"value": "56.88"},
                                }
                            ],
                        },
                        "freshness": {
                            "status": "fresh",
                            "message": "日线数据截至 2026-06-18",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_tradingagents_summary(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.tradingagents_summary.v1",
                "generated_at": "2026-06-23T18:37:04+08:00",
                "latest_run_date": "2026-06-23",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.tradingagents_summary.v1",
                        "market": "US",
                        "symbol": "VIXY",
                        "latest_run_date": "2026-06-23",
                        "ta_report_date": "2026-06-22",
                        "ta_view": "低配",
                        "current_action": "减仓",
                        "core_reason": "波动率仓位短期风险回报转差，所以 TA 建议降低仓位。",
                        "reason_fields": {
                            "main_judgment": "短期风险回报转差",
                            "evidence_1": "技术风险上升",
                            "evidence_2": "估值压力上升",
                            "risk_or_counterpoint": "长期主题仍在",
                            "action_logic": "降低仓位而不是清仓",
                        },
                        "source_hash": "sha256:" + "a" * 64,
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def write_t_signals(path: Path, *, symbol: str = "VIXY", action: str = "BUY_T") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suggested_ratio = "10" if action in {"BUY_T", "SELL_T"} else ""
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.t_signals_cache.v1",
                "generated_at": "2026-07-02T22:32:00+08:00",
                "run_date": "2026-07-02",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.t_signal.v1",
                        "run_date": "2026-07-02",
                        "market": "US",
                        "symbol": symbol,
                        "futu_symbol": f"US.{symbol}",
                        "name": "Volatility ETF",
                        "session_phase": "regular",
                        "updated_at": "2026-07-02T22:31:00+08:00",
                        "action": action,
                        "suggested_ratio": suggested_ratio,
                        "current_status": "BUY_T 条件满足，等待执行确认。",
                        "signal_summary_zh": "价格低于 VWAP 后回收，适合按 10% 底仓比例低吸买回。",
                        "price": {
                            "last_price": "48.50",
                            "day_change_pct": "-1.20",
                            "vwap": "49.10",
                            "ma_1m": "48.55",
                            "ma_5m": "48.85",
                            "day_low": "48.00",
                            "day_high": "50.20",
                        },
                        "liquidity": {
                            "bid": "48.49",
                            "ask": "48.50",
                            "spread_pct": "0.021",
                            "bid_depth": "5000",
                            "ask_depth": "4700",
                            "depth_status": "pass",
                        },
                        "technical": {
                            "rsi_5m": "34",
                            "volume_ratio_5m": "1.30",
                            "price_position": "below_vwap_reclaim",
                            "trend_state": "range_rebound",
                        },
                        "hard_gates": [
                            {
                                "name": "session_phase",
                                "status": "pass",
                                "message_zh": "当前处于盘中交易时段。",
                            }
                        ],
                        "evidence": [
                            {
                                "name": "vwap_reclaim",
                                "direction": "buy",
                                "strength": "medium",
                                "message_zh": "价格低于 VWAP 后回收。",
                            }
                        ],
                        "timeline": [
                            {
                                "event_at": "2026-07-02T22:31:00+08:00",
                                "event_type": "signal_created",
                                "action": action,
                                "suggested_ratio": suggested_ratio,
                                "message_zh": "生成 BUY_T 信号，建议比例 10%。",
                            }
                        ],
                        "notification": {
                            "should_notify": True,
                            "notified": False,
                            "dedupe_key": f"2026-07-02|US.{symbol}|{action}|{suggested_ratio}",
                            "last_notified_at": "",
                            "last_notified_dedupe_key": "",
                            "last_attempted_dedupe_key": "",
                        },
                        "status": "ok",
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def portfolio_rows() -> list[dict[str, str]]:
    return [
        {
            "sort_group": "4",
            "market": "US",
            "asset_class": "etf",
            "symbol": "VIXY",
            "name": "ProShares VIX Short-Term Futures ETF",
            "currency": "USD",
            "total_quantity": "100",
            "avg_cost_price": "45.00",
            "last_price": "48.50",
            "market_value": "4850.00",
            "cost_value": "4500.00",
            "unrealized_pnl": "350.00",
            "unrealized_pnl_pct": "7.78%",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "7.8",
            "market_value_hkd": "37830.00",
            "cost_value_hkd": "35100.00",
            "portfolio_weight_hkd": "97.80%",
            "brokers": "futu;tiger",
            "accounts": "main;growth",
            "ai_eligible": "true",
            "analysis_symbol": "VIXY",
            "risk_flag": "overweight",
            "confidence": "high",
            "notes": "",
        },
        {
            "sort_group": "6",
            "market": "CASH",
            "asset_class": "cash",
            "symbol": "HKD_CASH",
            "name": "HKD Cash",
            "currency": "HKD",
            "total_quantity": "1",
            "avg_cost_price": "",
            "last_price": "",
            "market_value": "850.00",
            "cost_value": "",
            "unrealized_pnl": "",
            "unrealized_pnl_pct": "",
            "fx_source": "fixture",
            "fx_date": "2026-05-31",
            "fx_to_hkd": "1",
            "market_value_hkd": "850.00",
            "cost_value_hkd": "",
            "portfolio_weight_hkd": "2.20%",
            "brokers": "futu",
            "accounts": "main",
            "ai_eligible": "false",
            "analysis_symbol": "",
            "risk_flag": "normal",
            "confidence": "high",
            "notes": "",
        },
    ]
def test_load_dashboard_state_merges_agent_report_strategy_and_actions(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        [*TRADING_ADVICE_FIELDNAMES, "advice_summary_zh"],
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "agent",
                "advice_action": "reduce",
                "advice_summary": "Trim volatility exposure.",
                "advice_summary_zh": "减低波动率仓位。",
                "raw_decision": '{"rating":"reduce"}',
                "status": "ok",
                "error": "",
                "source_status": "fresh",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "trading_plan.csv",
        [*TRADING_PLAN_FIELDNAMES, "plan_text_zh"],
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "source_status": "fresh",
                "fallback_reason": "",
                "fallback_from_date": "",
                "rating": "reduce",
                "entry_zone_low": "",
                "entry_zone_high": "",
                "add_price": "",
                "stop_loss": "42.00",
                "target_1": "50.00",
                "target_2": "55.00",
                "max_weight": "5%",
                "catalyst": "Volatility spike",
                "time_horizon": "short",
                "plan_text": "Reduce after target hit.",
                "plan_text_zh": "达到目标价后减仓。",
                "agent_reason": "Risk is elevated.",
                "agent_excerpt": "Trim exposure.",
                "status": "ok",
                "error": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "premarket_actions.csv",
        PREMARKET_ACTION_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "97.80%",
                "severity": "medium",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "Target hit.",
                "rationale": "Lock in gains.",
                "watch_trigger": "above 50",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "trade_actions.csv",
        TRADE_ACTION_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "futu_symbol": "US.VIXY",
                "action": "TRIM",
                "priority": "medium",
                "last_price": "48.50",
                "trigger_status": "target_1_hit",
                "suggested_quantity": "50",
                "status": "ready",
                "reason": "trim into strength",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["agent_report"] == {
        "available": True,
        "run_date": "2026-06-18",
        "market": "US",
        "symbol": "VIXY",
        "rating": "reduce",
        "summary": "Trim volatility exposure.",
        "summary_zh": "减低波动率仓位。",
        "raw_decision": '{"rating":"reduce"}',
        "source_status": "fresh",
        "fallback_reason": "",
        "fallback_from_date": "",
        "status": "ok",
        "error": "",
    }
    assert vixy["strategy"]["available"] is True
    assert vixy["strategy"]["stop_loss"] == "42.00"
    assert vixy["strategy"]["target_1"] == "50.00"
    assert vixy["strategy"]["plan_text"] == "Reduce after target hit."
    assert vixy["strategy"]["plan_text_zh"] == "达到目标价后减仓。"
    assert vixy["premarket_action"]["available"] is True
    assert vixy["premarket_action"]["suggested_action"] == "reduce"
    assert vixy["trade_action"]["available"] is True
    assert vixy["trade_action"]["action"] == "TRIM"
    assert vixy["trade_action"]["suggested_quantity"] == "50"


def test_load_dashboard_state_attaches_t_signal_from_market_scoped_latest(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_t_signals(config.data_dir / "latest" / "US" / "t_signals.json")

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["t_signal"]["available"] is True
    assert vixy["t_signal"]["action"] == "BUY_T"
    assert vixy["t_signal"]["suggested_ratio"] == "10"
    assert vixy["t_signal"]["signal_summary_zh"].startswith("价格低于 VWAP")
    assert vixy["t_signal"]["timeline"][0]["event_type"] == "signal_created"


def test_load_dashboard_state_marks_t_signal_unavailable_when_missing(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["t_signal"] == {"available": False, "error": ""}


def test_dashboard_attaches_tradingagents_summary_without_debug_fields_and_fallback(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = [
        portfolio_rows()[0],
        {
            **portfolio_rows()[0],
            "symbol": "DRAM",
            "name": "DRAM ETF",
            "portfolio_weight_hkd": "7.11%",
        },
    ]
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-23",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "Trim volatility exposure.",
                "raw_decision": '{"rating":"Underweight"}',
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            },
            {
                "run_date": "2026-06-23",
                "symbol": "DRAM",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "7.11%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "Trim memory exposure.",
                "raw_decision": '{"rating":"Underweight"}',
                "status": "ok",
                "error": "",
                "source_status": "fallback",
                "fallback_reason": "rate limited",
                "fallback_from_date": "2026-06-22",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trade_actions.csv",
        TRADE_ACTION_FIELDNAMES,
        [
            {
                "run_date": "2026-06-23",
                "symbol": "DRAM",
                "market": "US",
                "futu_symbol": "US.DRAM",
                "action": "TRIM",
                "priority": "medium",
                "last_price": "80.00",
                "trigger_status": "target_1_hit",
                "suggested_quantity": "10",
                "status": "ready",
                "reason": "target hit",
            }
        ],
    )
    write_tradingagents_summary(
        config.data_dir / "latest" / "US" / "tradingagents_summary.json"
    )

    state = load_dashboard_state(config).to_dict()

    holdings = {row["symbol"]: row for row in state["holding_enrichment"]}
    assert holdings["VIXY"]["tradingagents_summary"] == {
        "available": True,
        "status": "available",
        "error": "",
        "ta_view": "低配",
        "current_action": "减仓",
        "core_reason": "波动率仓位短期风险回报转差，所以 TA 建议降低仓位。",
        "ta_report_date": "2026-06-22",
        "latest_run_date": "2026-06-23",
    }
    assert set(holdings["VIXY"]["tradingagents_summary"]) == {
        "available",
        "status",
        "error",
        "ta_view",
        "current_action",
        "core_reason",
        "ta_report_date",
        "latest_run_date",
    }
    assert holdings["DRAM"]["tradingagents_summary"] == {
        "available": False,
        "status": "missing_current_summary",
        "error": "TradingAgents summary is unavailable for current advice",
        "ta_view": "低配",
        "current_action": "减仓",
        "core_reason": "缺失",
        "ta_report_date": "2026-06-22",
        "latest_run_date": "2026-06-23",
    }


def test_dashboard_ignores_stale_tradingagents_summary_latest(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    dram = {
        **portfolio_rows()[0],
        "symbol": "DRAM",
        "name": "DRAM ETF",
        "portfolio_weight_hkd": "7.11%",
    }
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [dram])
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-24",
                "symbol": "DRAM",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "7.11%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Overweight",
                "advice_summary": "Memory exposure remains constructive.",
                "raw_decision": '{"rating":"Overweight"}',
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    summary_path = config.data_dir / "latest" / "US" / "tradingagents_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.tradingagents_summary.v1",
                "generated_at": "2026-06-23T18:37:04+08:00",
                "latest_run_date": "2026-06-23",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.tradingagents_summary.v1",
                        "market": "US",
                        "symbol": "DRAM",
                        "latest_run_date": "2026-06-23",
                        "ta_report_date": "2026-06-22",
                        "ta_view": "低配",
                        "current_action": "减仓",
                        "core_reason": "旧摘要仍会被展示。",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    dram_holding = next(row for row in state["holding_enrichment"] if row["symbol"] == "DRAM")
    assert dram_holding["tradingagents_summary"] == {
        "available": False,
        "status": "missing_current_summary",
        "error": "TradingAgents summary is unavailable for current advice",
        "ta_view": "超配",
        "current_action": "缺失",
        "core_reason": "缺失",
        "ta_report_date": "2026-06-24",
        "latest_run_date": "2026-06-24",
    }
    assert "旧摘要仍会被展示。" not in json.dumps(
        dram_holding["tradingagents_summary"], ensure_ascii=False
    )


def test_dashboard_attaches_unscoped_tradingagents_summary_latest(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-23",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "normal",
                "source": "tradingagents",
                "advice_action": "Underweight",
                "advice_summary": "Trim volatility exposure.",
                "raw_decision": '{"rating":"Underweight"}',
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_tradingagents_summary(config.data_dir / "latest" / "tradingagents_summary.json")

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["tradingagents_summary"] == {
        "available": True,
        "status": "available",
        "error": "",
        "ta_view": "低配",
        "current_action": "减仓",
        "core_reason": "波动率仓位短期风险回报转差，所以 TA 建议降低仓位。",
        "ta_report_date": "2026-06-22",
        "latest_run_date": "2026-06-23",
    }
    assert set(vixy["tradingagents_summary"]) == {
        "available",
        "status",
        "error",
        "ta_view",
        "current_action",
        "core_reason",
        "ta_report_date",
        "latest_run_date",
    }


def test_load_dashboard_state_attaches_fresh_technical_facts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    report = "Daily RSI is 56.88 with price above the 50 day average."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision_with_market_report(report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"]["available"] is True
    assert vixy["technical_facts"]["status"] == "usable"
    assert vixy["technical_facts"]["run_date"] == "2026-06-19"
    assert vixy["technical_facts"]["data_date"] == "2026-06-18"
    assert vixy["technical_facts"]["source_hash"] == source_hash(report)
    assert vixy["technical_facts"]["facts"]["timeframes"][0]["timeframe"] == "daily"


def test_load_dashboard_state_accepts_kline_sourced_technical_facts_without_advice_hash(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision_with_market_report(""),
                "status": "error",
                "error": "daily deadline exceeded",
                "source_status": "error",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash="futu-kline:US.VIXY:2026-06-18",
        source_type="futu_kline",
        timeframes=[
            {
                "timeframe": "daily",
                "timeframe_label": "日线",
                "current_price": "18.82",
                "bollinger": {
                    "upper": "20.00",
                    "middle": "18.00",
                    "lower": "16.00",
                    "position": "middle_range",
                    "status": "neutral",
                    "reference_band": "",
                    "distance_pct": "",
                    "summary_zh": "当前价格位于日线布林带区间内",
                    "detail_zh": "价格未贴近上轨或下轨，布林带事实仅作背景展示。",
                },
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"]["available"] is True
    assert vixy["technical_facts"]["status"] == "usable"
    assert vixy["technical_facts"]["source_hash"] == "futu-kline:US.VIXY:2026-06-18"
    assert vixy["technical_facts"]["current_source_hash"] == ""


def test_decision_tab_marks_healthy_technical_facts_from_older_run_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    report = "Daily RSI is 56.88 with price above the 50 day average."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [{
            "run_date": "2026-06-20",
            "symbol": "VIXY",
            "market": "US",
            "asset_class": "etf",
            "portfolio_weight_hkd": "97.80%",
            "risk_flag": "overweight",
            "source": "tradingagents",
            "advice_action": "hold",
            "advice_summary": "Watch volatility.",
            "raw_decision": raw_decision_with_market_report(report),
            "status": "ok",
            "error": "",
            "source_status": "ok",
            "fallback_reason": "",
            "fallback_from_date": "",
        }],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash("Older report with a different source hash."),
    )

    technical = load_dashboard_state(config).to_dict()["holding_enrichment"][0]["technical_facts"]

    assert technical["available"] is False
    assert technical["status"] == "stale_run_date"
    assert technical["error"] == "technical facts run date does not match latest advice"


def test_load_dashboard_state_marks_missing_technical_facts_file_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"] == {
        "available": False,
        "status": "missing_file",
        "run_date": "",
        "data_date": "",
        "source_hash": "",
        "current_source_hash": "",
        "error": "technical_facts.json not found",
        "freshness": {},
        "facts": {},
    }


def test_load_dashboard_state_marks_stale_technical_facts_hash_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    current_report = "Current report says RSI is 40."
    old_report = "Old report says RSI is 70."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision_with_market_report(current_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(old_report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"]["available"] is False
    assert vixy["technical_facts"]["status"] == "stale_source_hash"
    assert vixy["technical_facts"]["run_date"] == "2026-06-19"
    assert vixy["technical_facts"]["data_date"] == "2026-06-18"
    assert vixy["technical_facts"]["source_hash"] == source_hash(old_report)
    assert vixy["technical_facts"]["current_source_hash"] == source_hash(current_report)
    assert vixy["technical_facts"]["facts"] == {}


def test_load_dashboard_state_prefers_market_scoped_technical_facts_and_advice(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    old_report = "Old unscoped report says RSI is 70."
    current_report = "Current scoped US report says RSI is 40."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Old advice.",
                "raw_decision": raw_decision_with_market_report(old_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Scoped advice.",
                "raw_decision": raw_decision_with_market_report(current_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(old_report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["agent_report"]["run_date"] == "2026-06-19"
    assert vixy["technical_facts"]["available"] is False
    assert vixy["technical_facts"]["status"] == "missing_file"
    assert vixy["technical_facts"]["current_source_hash"] == source_hash(current_report)


def test_load_dashboard_state_uses_scoped_facts_when_both_latest_layouts_exist(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    old_report = "Old unscoped report says RSI is 70."
    current_report = "Current scoped US report says RSI is 40."
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Old advice.",
                "raw_decision": raw_decision_with_market_report(old_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Scoped advice.",
                "raw_decision": raw_decision_with_market_report(current_report),
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_technical_facts(
        config.data_dir / "latest" / "technical_facts.json",
        report_hash=source_hash(old_report),
    )
    write_technical_facts(
        config.data_dir / "latest" / "US" / "technical_facts.json",
        report_hash=source_hash(current_report),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["technical_facts"]["available"] is True
    assert vixy["technical_facts"]["status"] == "usable"
    assert vixy["technical_facts"]["source_hash"] == source_hash(current_report)
    assert vixy["technical_facts"]["current_source_hash"] == source_hash(current_report)


def test_dashboard_attaches_hash_checked_decision_facts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    raw_decision = raw_decision_with_all_reports()
    decision_sources = extract_decision_sources(raw_decision)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision,
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_decision_facts(
        config.data_dir / "latest" / "US" / "decision_facts.json",
        decision_sources.kline_hash,
        decision_sources.news_sentiment_hash,
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["decision_facts"]["kline"]["available"] is True
    assert vixy["decision_facts"]["kline"]["fields"]["trend"] == "趋势偏强"
    assert vixy["decision_facts"]["news_sentiment"]["available"] is True
    assert (
        vixy["decision_facts"]["news_sentiment"]["fields"]["direction"]
        == "情绪偏谨慎"
    )


def test_dashboard_falls_back_to_unscoped_decision_facts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    raw_decision = raw_decision_with_all_reports()
    decision_sources = extract_decision_sources(raw_decision)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision,
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_decision_facts(
        config.data_dir / "latest" / "decision_facts.json",
        decision_sources.kline_hash,
        decision_sources.news_sentiment_hash,
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["decision_facts"]["kline"]["available"] is True
    assert vixy["decision_facts"]["kline"]["fields"]["trend"] == "趋势偏强"
    assert vixy["decision_facts"]["news_sentiment"]["available"] is True
    assert (
        vixy["decision_facts"]["news_sentiment"]["fields"]["direction"]
        == "情绪偏谨慎"
    )


def test_dashboard_stale_decision_facts_render_missing_fields(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    raw_decision = raw_decision_with_all_reports()
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-19",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "tradingagents",
                "advice_action": "hold",
                "advice_summary": "Watch volatility.",
                "raw_decision": raw_decision,
                "status": "ok",
                "error": "",
                "source_status": "ok",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )
    write_decision_facts(
        config.data_dir / "latest" / "US" / "decision_facts.json",
        source_hash("old K report"),
        source_hash("old news report"),
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["decision_facts"]["kline"]["available"] is False
    assert vixy["decision_facts"]["news_sentiment"]["available"] is False
    assert set(vixy["decision_facts"]["kline"]["fields"]) == set(KLINE_FIELDS)
    assert set(vixy["decision_facts"]["news_sentiment"]["fields"]) == set(
        NEWS_SENTIMENT_FIELDS
    )
    assert all(
        value == MISSING_VALUE
        for value in vixy["decision_facts"]["kline"]["fields"].values()
    )
    assert all(
        value == MISSING_VALUE
        for value in vixy["decision_facts"]["news_sentiment"]["fields"].values()
    )


def test_load_dashboard_state_attaches_futu_skill_facts(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [{
            "run_date": "2026-07-01",
            "symbol": "VIXY",
            "market": "US",
            "asset_class": "etf",
            "portfolio_weight_hkd": "97.80%",
            "risk_flag": "overweight",
            "source": "tradingagents",
            "advice_action": "hold",
            "advice_summary": "Watch volatility.",
            "raw_decision": "",
            "status": "ok",
            "error": "",
            "source_status": "ok",
            "fallback_reason": "",
            "fallback_from_date": "",
        }],
    )
    write_futu_skill_facts(
        config.data_dir / "latest" / "US" / "futu_skill_facts.json",
    )

    state = load_dashboard_state(config).to_dict()

    vixy = state["holding_enrichment"][0]
    news_sentiment = vixy["futu_skill_facts"]["news_sentiment"]
    assert news_sentiment["available"] is True
    assert news_sentiment["signal"] == "supportive"
    assert news_sentiment["confidence"] == "medium"
    assert news_sentiment["evidence"][0]["url"] == "https://example.com/vixy"
    assert news_sentiment["domestic_discussion"]["keyword_counts"] == [
        {"keyword": "震荡", "count": 2},
        {"keyword": "看空", "count": 1},
    ]
    assert news_sentiment["domestic_discussion"]["summary"] == "富途社区相关讨论较少，主要关注波动率 ETF 的短线风险。"
    assert news_sentiment["domestic_discussion"]["credibility"] == "低"
    technical = vixy["futu_skill_facts"]["technical_anomaly"]
    capital = vixy["futu_skill_facts"]["capital_anomaly"]
    derivatives = vixy["futu_skill_facts"]["derivatives_anomaly"]
    assert technical["available"] is True
    assert technical["signal"] == "supportive"
    assert technical["categories"][0]["name"] == "MACD"
    assert capital["suggested_constraint"] == "no_add"
    assert derivatives["status"] == "partial"


def test_decision_tab_marks_healthy_futu_facts_from_older_run_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "latest" / "US" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [{
            "run_date": "2026-07-02",
            "symbol": "VIXY",
            "market": "US",
            "asset_class": "etf",
            "portfolio_weight_hkd": "97.80%",
            "risk_flag": "overweight",
            "source": "tradingagents",
            "advice_action": "hold",
            "advice_summary": "Watch volatility.",
            "raw_decision": "",
            "status": "ok",
            "error": "",
            "source_status": "ok",
            "fallback_reason": "",
            "fallback_from_date": "",
        }],
    )
    write_futu_skill_facts(
        config.data_dir / "latest" / "US" / "futu_skill_facts.json",
        run_date="2026-07-01",
    )

    futu = load_dashboard_state(config).to_dict()["holding_enrichment"][0]["futu_skill_facts"]

    assert futu["technical_anomaly"]["available"] is False
    assert futu["technical_anomaly"]["status"] == "stale_run_date"
    assert futu["technical_anomaly"]["error"] == "Futu facts run date does not match latest advice"


def test_decision_tab_marks_futu_facts_without_current_advice_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_futu_skill_facts(
        config.data_dir / "latest" / "US" / "futu_skill_facts.json",
    )

    futu = load_dashboard_state(config).to_dict()["holding_enrichment"][0]["futu_skill_facts"]

    assert futu["technical_anomaly"]["available"] is False
    assert futu["technical_anomaly"]["status"] == "stale_run_date"
    assert futu["technical_anomaly"]["error"] == "Futu facts run date does not match latest advice"


def test_load_dashboard_state_marks_missing_anomaly_modules_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()
    vixy = state["holding_enrichment"][0]

    assert vixy["futu_skill_facts"]["technical_anomaly"]["available"] is False
    assert vixy["futu_skill_facts"]["technical_anomaly"]["status"] == "missing"
    assert vixy["futu_skill_facts"]["capital_anomaly"]["categories"] == []


def test_load_dashboard_state_hardens_malformed_cached_anomaly_module(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    path = config.data_dir / "latest" / "US" / "futu_skill_facts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.futu_skill_facts.v1",
                "generated_at": "2026-07-01T09:15:00+08:00",
                "run_date": "2026-07-01",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.futu_skill_facts.v1",
                        "run_date": "2026-07-01",
                        "market": "US",
                        "symbol": "VIXY",
                        "name": "ProShares VIX Short-Term Futures ETF",
                        "technical_anomaly": {
                            "status": "ok",
                            "signal": "supportive",
                            "confidence": "medium",
                            "suggested_constraint": "",
                            "window_days": "7d",
                            "summary": "技术信号支持趋势。",
                            "categories": [
                                None,
                                {
                                    "name": "MACD",
                                    "state": "anomaly",
                                },
                            ],
                        },
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    technical = state["holding_enrichment"][0]["futu_skill_facts"]["technical_anomaly"]
    assert technical["window_days"] == 0
    assert technical["categories"] == [
        {
            "name": "MACD",
            "state": "anomaly",
            "direction": "",
            "detail": "",
            "evidence_date": "",
        }
    ]
    assert all(isinstance(category, dict) for category in technical["categories"])
    assert all(
        isinstance(category[field], str)
        for category in technical["categories"]
        for field in ("name", "state", "direction", "detail", "evidence_date")
    )


def test_load_dashboard_state_hardens_non_finite_anomaly_window_days(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    path = config.data_dir / "latest" / "US" / "futu_skill_facts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
{
  "schema_version": "open_trader.futu_skill_facts.v1",
  "generated_at": "2026-07-01T09:15:00+08:00",
  "run_date": "2026-07-01",
  "market": "US",
  "records": [
    {
      "schema_version": "open_trader.futu_skill_facts.v1",
      "run_date": "2026-07-01",
      "market": "US",
      "symbol": "VIXY",
      "name": "ProShares VIX Short-Term Futures ETF",
      "technical_anomaly": {
        "status": "ok",
        "signal": "supportive",
        "confidence": "medium",
        "suggested_constraint": "",
        "window_days": Infinity,
        "summary": "技术信号支持趋势。",
        "categories": []
      },
      "error": ""
    }
  ]
}
""",
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    technical = state["holding_enrichment"][0]["futu_skill_facts"]["technical_anomaly"]
    assert technical["window_days"] == 0


def test_load_dashboard_state_marks_stale_anomaly_module_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    path = config.data_dir / "latest" / "US" / "futu_skill_facts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.futu_skill_facts.v1",
                "generated_at": "2026-07-01T09:15:00+08:00",
                "run_date": "2026-07-01",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.futu_skill_facts.v1",
                        "run_date": "2026-07-01",
                        "market": "US",
                        "symbol": "VIXY",
                        "name": "ProShares VIX Short-Term Futures ETF",
                        "technical_anomaly": {
                            "status": "stale",
                            "signal": "supportive",
                            "confidence": "medium",
                            "suggested_constraint": "",
                            "window_days": 7,
                            "summary": "技术信号来自旧缓存。",
                            "categories": [
                                {
                                    "name": "MACD",
                                    "state": "anomaly",
                                    "direction": "bullish",
                                    "detail": "旧窗口内金叉。",
                                    "evidence_date": "2026-06-28",
                                }
                            ],
                        },
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    technical = state["holding_enrichment"][0]["futu_skill_facts"]["technical_anomaly"]
    assert technical["available"] is False
    assert technical["status"] == "stale"
    assert technical["summary"] == "技术信号来自旧缓存。"
    assert technical["categories"] == [
        {
            "name": "MACD",
            "state": "anomaly",
            "direction": "bullish",
            "detail": "旧窗口内金叉。",
            "evidence_date": "2026-06-28",
        }
    ]


def test_load_dashboard_state_marks_stale_futu_news_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    path = config.data_dir / "latest" / "US" / "futu_skill_facts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.futu_skill_facts.v1",
                "generated_at": "2026-07-01T09:15:00+08:00",
                "run_date": "2026-07-01",
                "market": "US",
                "records": [
                    {
                        "schema_version": "open_trader.futu_skill_facts.v1",
                        "run_date": "2026-07-01",
                        "market": "US",
                        "symbol": "VIXY",
                        "name": "ProShares VIX Short-Term Futures ETF",
                        "news_sentiment": {
                            "status": "stale",
                            "signal": "supportive",
                            "confidence": "medium",
                            "freshness": {
                                "generated_at": "2026-06-30T09:10:00+08:00",
                                "source_window": "latest",
                            },
                            "evidence": [
                                {
                                    "title": "Old volatility digest",
                                    "summary": "旧新闻仍可展示。",
                                    "url": "https://example.com/old-vixy",
                                }
                            ],
                            "domestic_discussion": {
                                "status": "ok",
                                "keyword_counts": [{"keyword": "波动", "count": 1}],
                                "summary": "旧社区讨论。",
                                "focus": "波动率 ETF",
                                "divergence_risk": "样本旧。",
                                "credibility": "低",
                                "trading_constraint": "仅展示旧上下文。",
                                "post_count": 1,
                                "relevant_post_count": 1,
                            },
                            "blocking_reason": "旧缓存",
                            "suggested_constraint": "no_add",
                        },
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    news = state["holding_enrichment"][0]["futu_skill_facts"]["news_sentiment"]
    assert news["available"] is False
    assert news["status"] == "stale"
    assert news["signal"] == "supportive"
    assert news["confidence"] == "medium"
    assert news["evidence"][0]["url"] == "https://example.com/old-vixy"
    assert news["domestic_discussion"]["summary"] == "旧社区讨论。"
    assert news["blocking_reason"] == "旧缓存"
    assert news["suggested_constraint"] == "no_add"


def test_load_dashboard_state_marks_missing_agent_sections_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    unavailable = {"available": False, "error": ""}
    assert vixy["agent_report"] == unavailable
    assert vixy["strategy"] == unavailable
    assert vixy["premarket_action"] == unavailable
    assert vixy["trade_action"] == unavailable


def test_load_dashboard_state_reads_large_agent_report_fields(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    raw_decision = "x" * 150_000
    write_csv(
        config.data_dir / "latest" / "trading_advice.csv",
        TRADING_ADVICE_FIELDNAMES,
        [
            {
                "run_date": "2026-06-18",
                "symbol": "VIXY",
                "market": "US",
                "asset_class": "etf",
                "portfolio_weight_hkd": "97.80%",
                "risk_flag": "overweight",
                "source": "agent",
                "advice_action": "reduce",
                "advice_summary": "Large raw decision.",
                "raw_decision": raw_decision,
                "status": "ok",
                "error": "",
                "source_status": "fresh",
                "fallback_reason": "",
                "fallback_from_date": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["agent_report"]["raw_decision"] == raw_decision


def test_load_dashboard_state_attaches_research_view(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    bundle = config.data_dir / "research_data" / "US" / "VIXY" / "2026-06-19"
    bundle.mkdir(parents=True)
    (bundle / "dashboard_view.json").write_text(
        json.dumps(
            {
                "schema_version": "dashboard.research_view.v1",
                "market": "US",
                "symbol": "VIXY",
                "research_date": "2026-06-19",
                "tradingagents_conclusion": {
                    "status": "present",
                    "content": "低配，当前动作为减仓。",
                },
                "user_llm_conclusion": {"status": "missing", "content": ""},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["research_view"]["available"] is True
    assert vixy["research_view"]["research_date"] == "2026-06-19"
    assert (
        vixy["research_view"]["tradingagents_conclusion"]["content"]
        == "低配，当前动作为减仓。"
    )
    assert vixy["research_view"]["user_llm_conclusion"] == {
        "status": "missing",
        "content": "",
    }


def test_load_dashboard_state_marks_missing_research_view(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["research_view"]["available"] is False
    assert vixy["research_view"]["tradingagents_conclusion"] == {
        "status": "missing",
        "content": "",
    }
def test_load_dashboard_state_degrades_invalid_kelly_lab_artifacts(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    latest = tmp_path / "data" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "kelly_strategy_templates.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_strategy_templates.v0",
                "templates": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (latest / "kelly_experiments.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_experiments.v1",
                "experiments": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    assert len(state["holding_enrichment"]) == 1
    assert state["kelly_lab"]["available"] is False
    assert state["kelly_lab"]["template_count"] == 0
    assert state["kelly_lab"]["experiment_count"] == 0
    assert state["kelly_lab"]["templates"] == []
    assert state["kelly_lab"]["experiments"] == []
    assert "Kelly Lab" in state["kelly_lab"]["error"]
    assert "kelly_strategy_templates.json schema_version" in state["kelly_lab"]["error"]
    vixy = next(row for row in state["holding_enrichment"] if row["symbol"] == "VIXY")
    assert vixy["kelly"]["available"] is False
    assert vixy["kelly"]["experiment_count"] == 0
    assert vixy["kelly"]["status"] == "missing_experiment"


def test_dashboard_attaches_plan_events_and_previous_review(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    current = dashboard_decision_plan("2026-07-13")
    previous = dashboard_decision_plan("2026-07-10")
    publish_decision_plans(
        data_dir=config.data_dir,
        run_date="2026-07-13",
        market="US",
        records=[current],
        update_latest=True,
    )
    publish_decision_plans(
        data_dir=config.data_dir,
        run_date="2026-07-10",
        market="US",
        records=[previous],
        update_latest=False,
    )
    events_path = config.data_dir / "runs/2026-07-13/US/plan_events.jsonl"
    for index in range(2):
        append_plan_event(
            events_path,
            PlanEvent(
                event_id=f"trigger-{index}",
                plan_id=str(current["plan_id"]),
                event_type="condition_triggered",
                condition_id="trend-exit",
                occurred_at=f"2026-07-13T10:0{index}:00-04:00",
                payload={"price": "42"},
            ),
        )

    holding = load_dashboard_state(config).to_dict()["holding_enrichment"][0]

    assert holding["decision_plan"]["available"] is True
    assert holding["decision_plan"]["status"] == "triggered"
    assert holding["decision_plan"]["conditions"][0]["trigger_count"] == 2
    review = holding["decision_plan"]["previous_review"]
    assert review["run_date"] == "2026-07-10"
    assert review["starting_quantity"] == "100"
    assert review["closing_quantity"] == "100"
    assert "compliance" not in review


def test_dashboard_projects_decision_plan_backtests_to_visible_summary(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    current = dashboard_decision_plan("2026-07-13")
    first = current["backtests"][0]
    first["strategy"]["equity_curve"] = [str(index) for index in range(1_000)]
    first["strategy"]["trades"] = [{"id": index} for index in range(1_000)]
    first["market_benchmark"]["equity_curve"] = [
        str(index) for index in range(1_000)
    ]
    first["market_benchmark"]["trades"] = [
        {"id": index} for index in range(1_000)
    ]
    first["signals"] = [{"date": "2026-01-01"} for _ in range(1_000)]
    publish_decision_plans(
        data_dir=config.data_dir,
        run_date="2026-07-13",
        market="US",
        records=[current],
        update_latest=True,
    )

    projected = load_dashboard_state(config).to_dict()["holding_enrichment"][0][
        "decision_plan"
    ]["backtests"][0]

    assert projected == {
        "strategy_id": "trend_pullback/v1",
        "range": "6M",
        "gate": {"passed": True},
        "strategy": {
            "total_return_pct": "8",
            "max_drawdown_pct": "6",
            "sharpe_ratio": "1.1",
            "calmar_ratio": None,
        },
        "market_benchmark": {
            "symbol": "SPY",
            "total_return_pct": "5",
        },
        "market_excess_return_pct": "3",
    }


def test_dashboard_caches_decision_plan_file_until_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    current = dashboard_decision_plan("2026-07-13")
    publish_decision_plans(
        data_dir=data_dir,
        run_date="2026-07-13",
        market="US",
        records=[current],
        update_latest=True,
    )
    real_load = dashboard_module.load_decision_plans
    calls: list[Path] = []
    latest_path = data_dir / "latest/US/decision_plans.json"

    def counted_load(path: Path) -> list[dict[str, object]]:
        calls.append(path)
        return real_load(path)

    monkeypatch.setattr(dashboard_module, "load_decision_plans", counted_load)
    dashboard_module._load_decision_plans_cached.cache_clear()

    first, _ = dashboard_module._latest_decision_plans_for_markets(
        data_dir, {"US"}
    )
    second, _ = dashboard_module._latest_decision_plans_for_markets(
        data_dir, {"US"}
    )

    assert calls.count(latest_path) == 1
    assert first == second

    updated = dashboard_decision_plan("2026-07-14")
    publish_decision_plans(
        data_dir=data_dir,
        run_date="2026-07-14",
        market="US",
        records=[updated],
        update_latest=True,
    )
    refreshed, _ = dashboard_module._latest_decision_plans_for_markets(
        data_dir, {"US"}
    )

    assert calls.count(latest_path) == 2
    assert next(iter(refreshed.values()))["run_date"] == "2026-07-14"


def test_dashboard_exposes_invalid_plan_as_failed_state(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    current_path = config.data_dir / "latest/US/decision_plans.json"
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"schema_version":"invalid"}', encoding="utf-8")

    plan = load_dashboard_state(config).to_dict()["holding_enrichment"][0]["decision_plan"]

    assert plan["available"] is False
    assert plan["error"] == "decision_plans.json 无效"
