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
    BROKER_LABELS,
    BROKER_SOURCE_KINDS,
    DashboardConfig,
    _futu_skill_signal_detail,
    load_dashboard_state,
)
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


def test_dashboard_excludes_zero_quantity_closed_positions(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    closed = {field: "" for field in PORTFOLIO_FIELDNAMES}
    closed.update(
        {
            "market": "US",
            "asset_class": "etf",
            "symbol": "CLOSED",
            "currency": "USD",
            "total_quantity": "0",
            "market_value": "0",
            "market_value_hkd": "0",
            "portfolio_weight_hkd": "0.00%",
            "brokers": "futu",
        }
    )
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [closed])
    write_csv(
        config.data_dir / "runs" / "2026-07-16" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-07-16-futu-live",
                "broker": "futu",
                "account_alias": "futu_main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "CLOSED",
                "currency": "USD",
                "quantity": "0",
                "market_value": "0",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    assert state["holdings"] == []
    assert state["broker_positions"] == []
    futu = next(row for row in state["broker_summaries"] if row["broker"] == "futu")
    assert futu["holding_count"] == 0


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
    )


def test_dashboard_config_defaults_simulate_account_ids_to_zero(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)

    assert config.trend_review_cn_simulate_acc_id == 0
    assert config.trend_review_us_simulate_acc_id == 0
    assert config.trend_review_hk_simulate_acc_id == 0


def test_dashboard_ignores_zero_quantity_closed_positions(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    rows = []
    for symbol, quantity, market_value in (
        ("AAPL", "1", "100"),
        ("RAM", "0", "0"),
    ):
        row = {field: "" for field in PORTFOLIO_FIELDNAMES}
        row.update({
            "market": "US",
            "asset_class": "stock",
            "symbol": symbol,
            "name": symbol,
            "currency": "USD",
            "total_quantity": quantity,
            "last_price": market_value,
            "market_value": market_value,
            "market_value_hkd": market_value,
            "fx_to_hkd": "1",
            "brokers": "futu",
        })
        rows.append(row)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)

    payload = load_dashboard_state(config).to_dict()

    assert [row["symbol"] for row in payload["holdings"]] == ["AAPL"]
    assert payload["summary"]["holding_count"] == 1
    assert [row["symbol"] for row in payload["backtest_universe"]["holdings"]] == [
        "AAPL"
    ]


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
        config.data_dir,
        config.reports_dir,
        broker="tiger",
        artifact="2026-07-16.json",
    )

    assert report["report_date"] == "2026-07-17"
    assert report["buy_actions"][0]["execution"]["status"] == "missed"
    assert report["audit"]["artifact"] == "2026-07-16.json"
    assert report["report_sha256"] == _report_hash(payload)
    assert report["strategy_version"] == "v1"


@pytest.mark.parametrize("artifact", ["../secret.json", "/tmp/secret.json"])
def test_historical_report_rejects_unsafe_artifact_paths(
    tmp_path: Path, artifact: str,
) -> None:
    from open_trader.dashboard import load_historical_trend_report

    config = dashboard_config(tmp_path)
    with pytest.raises(ValueError, match="unsafe trend report artifact"):
        load_historical_trend_report(
            config.data_dir,
            config.reports_dir,
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
            config.data_dir,
            config.reports_dir,
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
            config.data_dir,
            config.reports_dir,
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
            config.data_dir,
            config.reports_dir,
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
                tmp_path / "data",
                tmp_path / "reports",
                broker="unknown",
                artifact="report.json",
            )


def trend_review_projection(market: str, broker: str) -> dict[str, object]:
    return {
        "schema_version": "open_trader.trend_review.projection.v1",
        "available": True,
        "market": market,
        "market_label": {"CN": "A 股", "US": "美股", "HK": "港股"}[market],
        "broker": broker,
        "strategy_snapshot": {
            "strategy_id": f"trend_animals_warm_to_hot/{market}/v1",
            "strategy_name": f"{market} 短线右侧趋势",
            "strategy_version": "v1",
            "process_version": "abc1234",
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


def test_dashboard_loads_only_strict_market_matched_trend_reviews(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [])
    latest = config.data_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for market, broker in (("CN", "eastmoney"), ("US", "tiger"), ("HK", "phillips")):
        (latest / f"trend_review_{market.lower()}.json").write_text(
            json.dumps(trend_review_projection(market, broker)), encoding="utf-8"
        )

    reviews = load_dashboard_state(config).to_dict()["trend_reviews"]

    assert set(reviews) == {"eastmoney", "tiger", "phillips"}
    assert reviews["eastmoney"]["market"] == "CN"
    assert reviews["tiger"]["strategy_snapshot"]["strategy_version"] == "v1"
    assert reviews["phillips"]["metrics"]["calmar"]["actual"]["value"] == "9.4"
    assert "batch" not in reviews["tiger"]
    assert "batch_path" not in reviews["tiger"]


def test_dashboard_accepts_strict_v2_trend_review_projection(tmp_path: Path) -> None:
    path = tmp_path / "data/latest/trend_review_us.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(trend_review_projection_v2("US", "tiger")), encoding="utf-8"
    )

    review = dashboard_module._load_trend_reviews(tmp_path / "data")["tiger"]

    assert review["available"] is True
    assert review["metrics"]["calmar"]["actual"]["value"] == "9.4"


@pytest.mark.parametrize(
    ("mutation", "broker"),
    [
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
    ],
)
def test_dashboard_rejects_invalid_trend_review_projection(
    tmp_path: Path, mutation: object, broker: str
) -> None:
    payload = trend_review_projection("US", "tiger")
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
        dashboard_module, "_shanghai_date", lambda: date(2026, 7, 15)
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

    assert set(reports) == {"tiger", "phillips", "eastmoney", "futu"}
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


def test_dashboard_projects_complete_cn_candidate_audit_only_for_eastmoney(
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

    assert [item["symbol"] for item in reports["eastmoney"]["audit"]["candidates"]] == [
        "688046", "600000",
    ]
    assert reports["tiger"]["audit"]["candidates"] == top_ten


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


def test_dashboard_hk_friday_report_is_current_then_stale_over_weekend(
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

    assert friday["data_status"] == "current"
    assert friday["report_date"] == "2026-07-20"
    assert friday["option_attention"] == current_attention
    assert saturday["data_status"] == "stale"


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


def test_dashboard_projects_futu_attention_from_tiger_us_and_phillips_hk(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    stale_us = [option_attention("QQQ")]
    current_hk = [
        option_attention("00700", market="HK", source_broker="辉立")
    ]
    for directory, market, broker, execution_date, attention in (
        ("trend_us_tiger", "US", "tiger", "2026-07-14", stale_us),
        ("trend_hk_phillips", "HK", "phillips", "2026-07-15", current_hk),
    ):
        path = config.reports_dir / directory / f"{execution_date}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "execution_date": execution_date,
            "as_of_date": execution_date,
            "generated_at": f"{execution_date}T18:00:00+08:00",
            "account": serialized_trend_account(fresh=True),
            "metadata": {"market": market, "broker": broker},
            "strategy_judgments": {
                "formal_actions": [],
                "holding_decisions": [],
                "top10_candidates": [],
            },
            "option_attention": attention,
        }), encoding="utf-8")

    reports = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )

    assert reports["futu"] == {
        "available": True,
        "broker": "futu",
        "broker_label": "富途",
        "market": "US_HK",
        "market_label": "美股 / 港股",
        "status_text": "期权关注",
        "attention_markets": [
            {
                "market": "US",
                "market_label": "美股",
                "data_status": "stale",
                "data_date": "2026-07-14",
                "items": stale_us,
            },
            {
                "market": "HK",
                "market_label": "港股",
                "data_status": "current",
                "data_date": "2026-07-15",
                "items": current_hk,
            },
        ],
    }


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
    }
    return payload


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
    write_trend_api_stats(config.data_dir, stats)

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    trade_stats = report["risk_summary"]["trade_stats"]
    assert trade_stats["available"] is True
    assert trade_stats["strategy_id"] == "trend_animals_warm_to_hot/CN/v2"
    assert trade_stats["opening_strategy_version"] == "v2"
    assert trade_stats["statistics_cutoff_at"] == "2026-07-20T11:59:59+08:00"
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


def test_dashboard_futu_projection_keeps_both_unavailable_market_rows(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)

    futu = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["futu"]

    assert futu["available"] is False
    assert futu["attention_markets"] == [
        {
            "market": "US",
            "market_label": "美股",
            "data_status": "unavailable",
            "data_date": "",
            "items": [],
        },
        {
            "market": "HK",
            "market_label": "港股",
            "data_status": "unavailable",
            "data_date": "",
            "items": [],
        },
    ]


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


def test_dashboard_trend_report_skips_future_candidate(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    payload = {
        "as_of_date": "2026-07-15",
        "account": serialized_trend_account(fresh=True),
        "metadata": {"market": "US", "broker": "tiger"},
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [],
    }
    for execution_date in ("2026-07-15", "2026-07-16"):
        (reports_dir / f"{execution_date}.json").write_text(json.dumps({
            **payload,
            "execution_date": execution_date,
            "generated_at": f"{execution_date}T18:00:00+08:00",
        }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is True
    assert report["report_date"] == "2026-07-15"
    assert report["data_status"] == "current"


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
    assert report["buy_actions"] == [stale_buy]
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


def test_dashboard_preserves_cn_statement_values_when_backtest_cache_exists(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    row = {field: "" for field in PORTFOLIO_FIELDNAMES}
    row.update(
        {
            "market": "CN",
            "asset_class": "stock",
            "symbol": "600025",
            "name": "华能水电",
            "currency": "CNY",
            "total_quantity": "6000",
            "last_price": "9.62",
            "market_value": "57720",
            "market_value_hkd": "62337.60",
            "cost_value": "53346",
            "fx_to_hkd": "1.08",
            "brokers": "eastmoney",
        }
    )
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])
    write_csv(
        config.data_dir / "prices/CN/600025.csv",
        ["date", "open", "high", "low", "close", "volume"],
        [
            {
                "date": "2026-07-10",
                "open": "9.8",
                "high": "10.1",
                "low": "9.7",
                "close": "10.00",
                "volume": "123456",
            }
        ],
    )

    state = load_dashboard_state(config)
    holding = state.holdings[0]
    assert holding["last_price"] == "9.62"
    assert holding["market_value"] == "57720"
    assert state.summary["portfolio_value_hkd"] == "62337.60"


def test_dashboard_preserves_all_statement_weights_when_cn_cache_exists(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = []
    for values in [
        {
            "market": "CN", "asset_class": "stock", "symbol": "600001",
            "currency": "CNY", "total_quantity": "1", "cost_value": "100",
            "fx_to_hkd": "1", "market_value_hkd": "100",
            "portfolio_weight_hkd": "10.00%",
        },
        {
            "market": "CN", "asset_class": "stock", "symbol": "600002",
            "currency": "CNY", "total_quantity": "1", "cost_value": "200",
            "fx_to_hkd": "1", "market_value_hkd": "200",
            "portfolio_weight_hkd": "20.00%",
        },
        {
            "market": "US", "asset_class": "stock", "symbol": "AAPL",
            "currency": "HKD", "market_value_hkd": "400",
            "portfolio_weight_hkd": "40.00%",
        },
        {
            "market": "CASH", "asset_class": "cash", "symbol": "HKD_CASH",
            "currency": "HKD", "market_value_hkd": "100",
            "portfolio_weight_hkd": "30.00%",
        },
    ]:
        row = {field: "" for field in PORTFOLIO_FIELDNAMES}
        row.update(values)
        rows.append(row)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)
    original = config.portfolio_path.read_bytes()
    for symbol, close in [("600001", "201"), ("600002", "302")]:
        write_csv(
            config.data_dir / f"prices/CN/{symbol}.csv",
            ["date", "close"],
            [{"date": "2026-07-10", "close": close}],
        )

    payload = load_dashboard_state(config).to_dict()
    displayed_rows = payload["holdings"] + payload["cash_rows"]

    assert payload["summary"]["portfolio_value_hkd"] == "800.00"
    assert sum(Decimal(row["market_value_hkd"]) for row in displayed_rows) == Decimal(
        "800.00"
    )
    assert {row["symbol"]: row["portfolio_weight_hkd"] for row in displayed_rows} == {
        "600001": "10.00%",
        "600002": "20.00%",
        "AAPL": "40.00%",
        "HKD_CASH": "30.00%",
    }
    assert sum(
        Decimal(row["portfolio_weight_hkd"].rstrip("%")) for row in displayed_rows
    ) == Decimal("100.00")
    assert config.portfolio_path.read_bytes() == original


def test_dashboard_preserves_statement_when_complete_weights_are_invalid(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = []
    for values in [
        {
            "market": "CN", "asset_class": "stock", "symbol": "600001",
            "currency": "CNY", "total_quantity": "1", "last_price": "100",
            "market_value": "100", "cost_value": "80", "fx_to_hkd": "1",
            "market_value_hkd": "100", "unrealized_pnl": "20",
            "unrealized_pnl_pct": "25.00%", "portfolio_weight_hkd": "10.00%",
        },
        {
            "market": "US", "asset_class": "stock", "symbol": "AAPL",
            "currency": "USD", "market_value_hkd": "bad",
            "portfolio_weight_hkd": "90.00%",
        },
    ]:
        row = {field: "" for field in PORTFOLIO_FIELDNAMES}
        row.update(values)
        rows.append(row)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)
    original_file = config.portfolio_path.read_bytes()
    write_csv(
        config.data_dir / "prices/CN/600001.csv",
        ["date", "close"],
        [{"date": "2026-07-10", "close": "200"}],
    )

    state = load_dashboard_state(config)

    assert [
        {key: holding[key] for key in original}
        for holding, original in zip(state.holdings, rows)
    ] == rows
    assert state.summary["portfolio_value_hkd"] == "100.00"
    assert config.portfolio_path.read_bytes() == original_file


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close", "bad"),
        ("close", "0"),
        ("close", "-1"),
        ("total_quantity", "bad"),
        ("total_quantity", "-1"),
        ("cost_value", ""),
        ("cost_value", "NaN"),
        ("cost_value", "-1"),
        ("fx_to_hkd", ""),
        ("fx_to_hkd", "NaN"),
        ("fx_to_hkd", "0"),
        ("fx_to_hkd", "-1"),
    ],
)
def test_dashboard_cn_cache_inputs_never_replace_statement_row_and_summary(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    config = dashboard_config(tmp_path)
    row = {fieldname: "" for fieldname in PORTFOLIO_FIELDNAMES}
    row.update(
        {
            "market": "CN",
            "asset_class": "stock",
            "symbol": "600025",
            "currency": "CNY",
            "total_quantity": "6000",
            "last_price": "9.62",
            "market_value": "57720",
            "market_value_hkd": "62337.60",
            "cost_value": "53346",
            "unrealized_pnl": "4374",
            "unrealized_pnl_pct": "8.20%",
            "fx_to_hkd": "1.08",
            "brokers": "eastmoney",
        }
    )
    close = "10.00"
    if field == "close":
        close = value
    else:
        row[field] = value
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [row])
    write_csv(
        config.data_dir / "prices/CN/600025.csv",
        ["date", "close"],
        [{"date": "2026-07-10", "close": close}],
    )

    state = load_dashboard_state(config)

    assert {key: state.holdings[0][key] for key in row} == row
    assert state.summary["portfolio_value_hkd"] == "62337.60"


def test_dashboard_exposes_eastmoney_statement_metadata() -> None:
    assert BROKER_LABELS["eastmoney"] == "东方财富"
    assert BROKER_SOURCE_KINDS["eastmoney"] == "statement"


def test_dashboard_backtest_universe_combines_holdings_and_watchlist(tmp_path: Path) -> None:
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

    assert [(row["market"], row["symbol"]) for row in payload["backtest_universe"]["holdings"]] == [
        ("US", "MSFT"), ("HK", "00700"),
    ]
    assert [(row["market"], row["symbol"]) for row in payload["backtest_universe"]["watchlist"]] == [
        ("US", "NVDA"),
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

    assert {(row["market"], row["symbol"]) for row in payload["holdings"]} == {
        ("US", "MSFT"),
        ("OTHER", "PRIVATE"),
    }


def test_dashboard_backtest_universe_rejects_unsafe_and_option_symbols(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    rows = []
    for market, symbol, asset_class, name in [
        ("US", "BRK.B", "stock", "Berkshire"),
        ("US", "SPY", "etf", "SPDR ETF"),
        ("HK", "00700", "stock", "腾讯"),
        ("US", "AAPL260116C00150000", "", ""),
        ("HK", "12345", "", "腾讯期权"),
        ("US", "../../outside", "stock", "unsafe"),
        ("US", "BAD/SYMBOL", "stock", "unsafe"),
        ("US", "BAD\\SYMBOL", "stock", "unsafe"),
        ("US", "BAD:SYMBOL", "stock", "unsafe"),
        ("US", "BAD SYMBOL", "stock", "unsafe"),
        ("HK", "123456", "stock", "unsafe"),
    ]:
        row = {field: "" for field in PORTFOLIO_FIELDNAMES}
        row.update({"market": market, "symbol": symbol, "asset_class": asset_class, "name": name})
        rows.append(row)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)

    universe = load_dashboard_state(config).to_dict()["backtest_universe"]["holdings"]

    assert [(row["market"], row["symbol"]) for row in universe] == [
        ("US", "BRK.B"), ("US", "SPY"), ("HK", "00700"),
    ]


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


def test_load_dashboard_state_merges_portfolio_details_cash_and_trade_actions(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-05"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "ProShares VIX Short-Term Futures ETF",
                "currency": "USD",
                "quantity": "40",
                "cost_price": "44.00",
                "last_price": "48.50",
                "market_value": "1940.00",
                "cost_value": "1760.00",
                "unrealized_pnl": "180.00",
                "confidence": "high",
                "notes": "",
            },
            {
                "statement_id": "2026-05-tiger",
                "broker": "tiger",
                "account_alias": "growth",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "ProShares VIX Short-Term Futures ETF",
                "currency": "USD",
                "quantity": "60",
                "cost_price": "45.67",
                "last_price": "48.50",
                "market_value": "2910.00",
                "cost_value": "2740.00",
                "unrealized_pnl": "170.00",
                "confidence": "high",
                "notes": "",
            },
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-futu",
                "broker": "futu",
                "account_alias": "main",
                "currency": "HKD",
                "cash_balance": "850.00",
                "available_balance": "850.00",
                "confidence": "high",
                "notes": "",
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
                "status": "ready",
                "reason": "trim into strength",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    assert state["portfolio_path"] == str(config.portfolio_path)
    assert state["data_dir"] == str(config.data_dir)
    assert state["reports_dir"] == str(config.reports_dir)
    assert state["poll_seconds"] == 1.5
    assert state["futu_host"] == "127.0.0.1"
    assert state["futu_port"] == 11111
    assert state["broker_detail_month"] == "2026-05"
    assert state["detail_available"] is True
    assert state["summary"]["holding_count"] == 1
    assert state["summary"]["portfolio_value_hkd"] == "38680.00"
    assert state["summary"]["holding_value_hkd"] == "37830.00"
    assert state["summary"]["cash_like_value_hkd"] == "850.00"
    assert state["summary"]["holding_weight_hkd"] == "97.80%"
    assert state["summary"]["cash_like_weight_hkd"] == "2.20%"
    assert state["summary"]["broker_count"] == 2
    assert len(state["broker_positions"]) == 2
    assert len(state["cash_details"]) == 1
    assert len(state["trade_actions"]) == 1

    holdings_by_symbol = {row["symbol"]: row for row in state["holdings"]}
    assert holdings_by_symbol["VIXY"]["broker_detail_count"] == 2
    assert [
        {
            "broker": row["broker"],
            "account_alias": row["account_alias"],
            "quantity": row["quantity"],
            "market_value": row["market_value"],
        }
        for row in holdings_by_symbol["VIXY"]["broker_details"]
    ] == [
        {
            "broker": "futu",
            "account_alias": "main",
            "quantity": "40",
            "market_value": "1940.00",
        },
        {
            "broker": "tiger",
            "account_alias": "growth",
            "quantity": "60",
            "market_value": "2910.00",
        },
    ]
    assert holdings_by_symbol["VIXY"]["trade_action"]["action"] == "TRIM"


def test_load_dashboard_state_uses_portfolio_when_monthly_details_are_absent(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    assert state["broker_detail_month"] == ""
    assert state["detail_available"] is False
    assert state["summary"]["holding_count"] == 1
    holdings_by_symbol = {row["symbol"]: row for row in state["holdings"]}
    assert "VIXY" in holdings_by_symbol
    assert holdings_by_symbol["VIXY"]["broker_detail_count"] == 0
    assert holdings_by_symbol["VIXY"]["broker_details"] == []
    assert holdings_by_symbol["VIXY"]["trade_action"] == {"available": False, "error": ""}
    assert "backtest" not in holdings_by_symbol["VIXY"]
    assert "backtest_readiness" not in holdings_by_symbol["VIXY"]


def obsolete_load_dashboard_state_attaches_latest_backtest_result(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    older_dir = config.data_dir / "backtests" / "2026-06-16-US-VIXY-trading-plan"
    older_dir.mkdir(parents=True)
    (older_dir / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.backtest_metrics.v1",
                "run_id": "2026-06-16-US-VIXY-trading-plan",
                "run_date": "2026-06-16",
                "market": "US",
                "symbol": "VIXY",
                "strategy": "trading_plan",
                "adapter": "backtrader",
                "metrics": {
                    "total_return_pct": "-2.00",
                    "win_rate_pct": "33.33",
                },
            }
        ),
        encoding="utf-8",
    )
    latest_dir = config.data_dir / "backtests" / "2026-06-18-US-VIXY-trading-plan"
    latest_dir.mkdir(parents=True)
    (latest_dir / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.backtest_metrics.v1",
                "run_id": "2026-06-18-US-VIXY-trading-plan",
                "run_date": "2026-06-18",
                "market": "US",
                "symbol": "VIXY",
                "strategy": "trading_plan",
                "adapter": "backtrader",
                "metrics": {
                    "total_return_pct": "1.17",
                    "win_rate_pct": "50.00",
                    "max_drawdown_pct": "-3.40",
                    "trade_count": "2",
                },
            }
        ),
        encoding="utf-8",
    )
    (latest_dir / "trades.csv").write_text(
        "\n".join(
            [
                "run_id,run_date,date,market,symbol,side,price,quantity,notional,fees,cash_after,reason",
                "2026-06-18-US-VIXY-trading-plan,2026-06-18,2026-06-19,US,VIXY,BUY,40.2000,621,24964.20,24.96,75010.84,entry_zone",
                "2026-06-18-US-VIXY-trading-plan,2026-06-18,2026-06-20,US,VIXY,SELL,47.9760,621,29793.10,29.79,104774.15,target_1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (latest_dir / "equity_curve.csv").write_text(
        "\n".join(
            [
                "run_id,date,cash,position_quantity,close,equity,drawdown_pct",
                "2026-06-18-US-VIXY-trading-plan,2026-06-18,100000.00,0,45.0000,100000.00,0.00",
                "2026-06-18-US-VIXY-trading-plan,2026-06-19,75010.84,621,42.0000,101092.84,0.00",
                "2026-06-18-US-VIXY-trading-plan,2026-06-20,104774.15,0,48.0000,104774.15,0.00",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report_path = config.reports_dir / "backtests" / "2026-06-18-US-VIXY-trading-plan.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("# VIXY 回测\n", encoding="utf-8")

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest"] == {
        "available": True,
        "run_id": "2026-06-18-US-VIXY-trading-plan",
        "run_date": "2026-06-18",
        "market": "US",
        "symbol": "VIXY",
        "strategy": "trading_plan",
        "adapter": "backtrader",
        "metrics": {
            "total_return_pct": "1.17",
            "win_rate_pct": "50.00",
            "max_drawdown_pct": "-3.40",
            "trade_count": "2",
        },
        "metrics_path": str(latest_dir / "metrics.json"),
        "trades_path": str(latest_dir / "trades.csv"),
        "equity_curve_path": str(latest_dir / "equity_curve.csv"),
        "trades": [
            {
                "run_id": "2026-06-18-US-VIXY-trading-plan",
                "run_date": "2026-06-18",
                "date": "2026-06-19",
                "market": "US",
                "symbol": "VIXY",
                "side": "BUY",
                "price": "40.2000",
                "quantity": "621",
                "notional": "24964.20",
                "fees": "24.96",
                "cash_after": "75010.84",
                "reason": "entry_zone",
            },
            {
                "run_id": "2026-06-18-US-VIXY-trading-plan",
                "run_date": "2026-06-18",
                "date": "2026-06-20",
                "market": "US",
                "symbol": "VIXY",
                "side": "SELL",
                "price": "47.9760",
                "quantity": "621",
                "notional": "29793.10",
                "fees": "29.79",
                "cash_after": "104774.15",
                "reason": "target_1",
            },
        ],
        "equity_curve": [
            {
                "run_id": "2026-06-18-US-VIXY-trading-plan",
                "date": "2026-06-18",
                "cash": "100000.00",
                "position_quantity": "0",
                "close": "45.0000",
                "equity": "100000.00",
                "drawdown_pct": "0.00",
            },
            {
                "run_id": "2026-06-18-US-VIXY-trading-plan",
                "date": "2026-06-19",
                "cash": "75010.84",
                "position_quantity": "621",
                "close": "42.0000",
                "equity": "101092.84",
                "drawdown_pct": "0.00",
            },
            {
                "run_id": "2026-06-18-US-VIXY-trading-plan",
                "date": "2026-06-20",
                "cash": "104774.15",
                "position_quantity": "0",
                "close": "48.0000",
                "equity": "104774.15",
                "drawdown_pct": "0.00",
            },
        ],
        "report_path": str(report_path),
        "status": "ok",
        "error": "",
    }


def obsolete_load_dashboard_state_exposes_backtest_readiness(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Overweight",
            "entry_zone_low": "40",
            "entry_zone_high": "",
            "max_weight": "",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"] == {
        "available": False,
        "status": "missing_fields",
        "run_date": "2026-06-18",
        "plan_path": str(config.data_dir / "latest" / "US" / "trading_plan.csv"),
        "prices_path": str(config.data_dir / "prices" / "US" / "VIXY.csv"),
        "prices_missing": True,
        "missing_fields": ["entry_zone_high", "max_weight"],
        "error": "missing backtest field(s): entry_zone_high, max_weight",
    }

    plan_row["entry_zone_high"] = "42"
    plan_row["max_weight"] = "25%"
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    write_csv(
        config.data_dir / "prices" / "US" / "VIXY.csv",
        ["date", "open", "high", "low", "close"],
        [{"date": "2026-06-19", "open": "41", "high": "43", "low": "40", "close": "42"}],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"] == {
        "available": True,
        "status": "ready",
        "run_date": "2026-06-18",
        "plan_path": str(config.data_dir / "latest" / "US" / "trading_plan.csv"),
        "prices_path": str(config.data_dir / "prices" / "US" / "VIXY.csv"),
        "prices_missing": False,
        "missing_fields": [],
        "error": "",
    }


def obsolete_load_dashboard_state_marks_sell_side_backtest_ready(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    plan_row = {field: "" for field in TRADING_PLAN_FIELDNAMES}
    plan_row.update(
        {
            "run_date": "2026-06-18",
            "symbol": "VIXY",
            "market": "US",
            "rating": "Underweight",
            "entry_zone_low": "30",
            "entry_zone_high": "50",
            "target_1": "35",
            "max_weight": "",
            "status": "active",
        }
    )
    write_csv(
        config.data_dir / "latest" / "US" / "trading_plan.csv",
        TRADING_PLAN_FIELDNAMES,
        [plan_row],
    )
    write_csv(
        config.data_dir / "prices" / "US" / "VIXY.csv",
        ["date", "open", "high", "low", "close"],
        [{"date": "2026-06-19", "open": "41", "high": "43", "low": "34", "close": "35"}],
    )

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["backtest_readiness"] == {
        "available": True,
        "status": "ready",
        "run_date": "2026-06-18",
        "plan_path": str(config.data_dir / "latest" / "US" / "trading_plan.csv"),
        "prices_path": str(config.data_dir / "prices" / "US" / "VIXY.csv"),
        "prices_missing": False,
        "missing_fields": [],
        "error": "",
    }


def test_load_dashboard_state_excludes_cash_like_rows_from_holdings(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = [
        portfolio_rows()[0],
        {
            **portfolio_rows()[0],
            "sort_group": "3",
            "market": "HK",
            "asset_class": "money_market_fund",
            "symbol": "HK0000951506.HKD",
            "name": "华泰港元货币市场基金A",
            "currency": "HKD",
            "market_value_hkd": "597524.58",
            "portfolio_weight_hkd": "35.14%",
            "brokers": "tiger",
            "ai_eligible": "false",
            "analysis_symbol": "",
        },
        {
            **portfolio_rows()[1],
            "symbol": "FUTU_UNMAPPED_ASSETS",
            "name": "富途未明细账户资产",
            "market_value_hkd": "849884.06",
            "portfolio_weight_hkd": "49.98%",
        },
        {
            **portfolio_rows()[1],
            "symbol": "USD_CASH",
            "name": "USD Cash",
            "market_value_hkd": "-87760.17",
            "portfolio_weight_hkd": "-5.16%",
        },
    ]
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)

    state = load_dashboard_state(config).to_dict()

    assert state["summary"]["holding_count"] == 1
    assert state["summary"]["portfolio_value_hkd"] == "1397478.47"
    assert state["summary"]["holding_value_hkd"] == "37830.00"
    assert state["summary"]["cash_like_value_hkd"] == "1359648.47"
    assert state["summary"]["holding_weight_hkd"] == "2.71%"
    assert state["summary"]["cash_like_weight_hkd"] == "97.29%"
    assert [row["symbol"] for row in state["holdings"]] == ["VIXY"]


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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    holdings = {row["symbol"]: row for row in state["holdings"]}
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

    dram_holding = next(row for row in state["holdings"] if row["symbol"] == "DRAM")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    technical = load_dashboard_state(config).to_dict()["holdings"][0]["technical_facts"]

    assert technical["available"] is False
    assert technical["status"] == "stale_run_date"
    assert technical["error"] == "technical facts run date does not match latest advice"


def test_load_dashboard_state_marks_missing_technical_facts_file_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = state["holdings"][0]
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

    futu = load_dashboard_state(config).to_dict()["holdings"][0]["futu_skill_facts"]

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

    futu = load_dashboard_state(config).to_dict()["holdings"][0]["futu_skill_facts"]

    assert futu["technical_anomaly"]["available"] is False
    assert futu["technical_anomaly"]["status"] == "stale_run_date"
    assert futu["technical_anomaly"]["error"] == "Futu facts run date does not match latest advice"


def test_load_dashboard_state_marks_missing_anomaly_modules_unavailable(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()
    vixy = state["holdings"][0]

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

    technical = state["holdings"][0]["futu_skill_facts"]["technical_anomaly"]
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

    technical = state["holdings"][0]["futu_skill_facts"]["technical_anomaly"]
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

    technical = state["holdings"][0]["futu_skill_facts"]["technical_anomaly"]
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

    news = state["holdings"][0]["futu_skill_facts"]["news_sentiment"]
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["research_view"]["available"] is False
    assert vixy["research_view"]["tradingagents_conclusion"] == {
        "status": "missing",
        "content": "",
    }


def test_load_dashboard_state_prefers_latest_daily_sync_details(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "runs" / "2026-05" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-futu",
                "broker": "futu",
                "account_alias": "old",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "165",
                "cost_price": "",
                "last_price": "24.41",
                "market_value": "4027.65",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "high",
                "notes": "",
            }
        ],
    )
    write_csv(
        config.data_dir / "runs" / "2026-06-19" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu-live",
                "broker": "futu",
                "account_alias": "live",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "100",
                "cost_price": "42.62",
                "last_price": "21.93",
                "market_value": "2193.00",
                "cost_value": "4261.60",
                "unrealized_pnl": "-2068.60",
                "confidence": "high",
                "notes": "Futu live account position",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    assert state["broker_detail_month"] == "2026-06-19"
    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["broker_details"][0]["account_alias"] == "live"
    assert vixy["broker_details"][0]["quantity"] == "100"


def test_load_dashboard_state_keeps_latest_details_for_each_broker(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    base = {
        "account_alias": "main",
        "market": "HK",
        "asset_class": "stock",
        "symbol": "00001",
        "name": "Holding",
        "currency": "HKD",
        "quantity": "1",
        "cost_price": "",
        "last_price": "100",
        "market_value": "100",
        "cost_value": "",
        "unrealized_pnl": "",
        "confidence": "high",
        "notes": "",
    }
    write_csv(
        config.data_dir / "runs" / "2026-06" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [{**base, "statement_id": "2026-06-phillips", "broker": "phillips"}],
    )
    write_csv(
        config.data_dir / "runs" / "2026-07" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [{**base, "statement_id": "2026-07-eastmoney", "broker": "eastmoney"}],
    )

    state = load_dashboard_state(config).to_dict()
    summaries = {row["broker"]: row for row in state["broker_summaries"]}

    assert summaries["phillips"]["portfolio_value_hkd"] == "100.00"
    assert summaries["eastmoney"]["portfolio_value_hkd"] == "100.00"


def test_load_dashboard_state_builds_broker_summaries_from_detail_rows(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "ProShares VIX Short-Term Futures ETF",
                "currency": "USD",
                "quantity": "40",
                "cost_price": "44.00",
                "last_price": "48.50",
                "market_value": "1940.00",
                "cost_value": "1760.00",
                "unrealized_pnl": "180.00",
                "confidence": "high",
                "notes": "",
            },
            {
                "statement_id": "2026-06-19-tiger",
                "broker": "tiger",
                "account_alias": "growth",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "ProShares VIX Short-Term Futures ETF",
                "currency": "USD",
                "quantity": "60",
                "cost_price": "45.67",
                "last_price": "48.50",
                "market_value": "2910.00",
                "cost_value": "2740.00",
                "unrealized_pnl": "170.00",
                "confidence": "high",
                "notes": "",
            },
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "currency": "HKD",
                "cash_balance": "850.00",
                "available_balance": "850.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    holdings_by_symbol = {row["symbol"]: row for row in state["holdings"]}
    vixy_details = {
        row["broker"]: row for row in holdings_by_symbol["VIXY"]["broker_details"]
    }
    assert vixy_details["futu"]["market_value_hkd"] == "15132.00"
    assert vixy_details["tiger"]["market_value_hkd"] == "22698.00"

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["futu"]["label"] == "富途"
    assert summaries["futu"]["source_kind"] == "live_account"
    assert summaries["futu"]["holding_value_hkd"] == "15132.00"
    assert summaries["futu"]["cash_like_value_hkd"] == "850.00"
    assert summaries["futu"]["portfolio_value_hkd"] == "15982.00"
    assert summaries["futu"]["holding_count"] == 1
    assert summaries["tiger"]["label"] == "老虎"
    assert summaries["tiger"]["holding_value_hkd"] == "22698.00"
    assert summaries["tiger"]["cash_like_value_hkd"] == "0.00"
    assert summaries["tiger"]["portfolio_value_hkd"] == "22698.00"
    assert summaries["tiger"]["holding_count"] == 1
    assert summaries["phillips"]["label"] == "辉立"
    assert summaries["phillips"]["portfolio_value_hkd"] == ""
    assert summaries["phillips"]["source_kind"] == "statement"
    assert summaries["phillips"]["detail_available"] is False

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["display_text"] == "仅月结单明细"
    assert statuses["futu"]["status"] == "non_realtime"
    assert statuses["tiger"]["display_text"] == "仅月结单明细"
    assert statuses["tiger"]["status"] == "non_realtime"
    assert statuses["phillips"]["display_text"] == "暂无月结单明细"
    assert statuses["phillips"]["status"] == "non_realtime"


def test_broker_summary_counts_cash_classified_positions_as_cash() -> None:
    futu = dashboard_module._build_broker_summary(
        "futu",
        [],
        [{
            "broker": "futu",
            "market": "CASH",
            "asset_class": "cash",
            "currency": "HKD",
            "market_value": "900",
        }],
        [{
            "broker": "futu",
            "currency": "HKD",
            "cash_balance": "100",
        }],
        {},
    )

    assert futu["holding_value_hkd"] == "0.00"
    assert futu["cash_like_value_hkd"] == "1000.00"
    assert futu["portfolio_value_hkd"] == "1000.00"
    assert futu["holding_count"] == 0


def test_tiger_broker_summary_counts_money_market_fund_as_cash_like(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-07-16"
    base_position = {
        "statement_id": "2026-07-16-tiger-live",
        "broker": "tiger",
        "account_alias": "tiger_5683",
        "market": "US",
        "asset_class": "stock",
        "symbol": "MSFT",
        "name": "Microsoft",
        "currency": "USD",
        "quantity": "1",
        "cost_price": "90",
        "last_price": "100",
        "market_value": "100",
        "fx_to_hkd": "7.84",
        "cost_value": "90",
        "unrealized_pnl": "10",
        "confidence": "high",
        "notes": "",
    }
    write_csv(
        run_dir / "extracted_positions.csv",
        [*POSITION_FIELDNAMES, "fx_to_hkd"],
        [
            base_position,
            {
                **base_position,
                "market": "HK",
                "asset_class": "fund",
                "symbol": "ORDINARY_FUND",
                "name": "环球股票基金",
                "currency": "HKD",
                "market_value": "200",
                "fx_to_hkd": "1",
            },
            {
                **base_position,
                "market": "HK",
                "asset_class": "money_market_fund",
                "symbol": "HK0000951506.HKD",
                "name": "华泰港元货币市场基金A",
                "currency": "HKD",
                "market_value": "1000",
                "fx_to_hkd": "1",
            },
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        [*CASH_FIELDNAMES, "fx_to_hkd"],
        [
            {
                "statement_id": "2026-07-16-tiger-live",
                "broker": "tiger",
                "account_alias": "tiger_5683",
                "currency": "USD",
                "cash_balance": "-10",
                "fx_to_hkd": "7.84",
                "available_balance": "-10",
                "confidence": "high",
                "notes": "",
            }
        ],
    )
    (run_dir / "tiger_account_snapshot.json").write_text(
        json.dumps(
            {
                "cash_records": [
                    {
                        "record_type": "account_total",
                        "currency": "USD",
                        "cash_available_for_trade": "62",
                        "fx_to_hkd": "7.84",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    summary = next(row for row in state["broker_summaries"] if row["broker"] == "tiger")
    assert summary["holding_count"] == 2
    assert summary["holding_value_hkd"] == "984.00"
    assert summary["cash_like_value_hkd"] == "921.60"
    assert summary["portfolio_value_hkd"] == "1905.60"
    assert summary["available_to_trade_hkd"] == "486.08"
    assert summary["cash_components"] == [
        {"label": "USD 现金", "value_hkd": "-78.40"},
        {"label": "华泰港元货币市场基金A", "value_hkd": "1000.00"},
    ]


def test_load_dashboard_state_exposes_cash_rows_for_dashboard_view(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    assert [row["symbol"] for row in state["cash_rows"]] == ["HKD_CASH"]
    assert state["cash_rows"][0]["market_value_hkd"] == "850.00"
    assert state["cash_rows"][0]["brokers"] == "futu"


def test_load_dashboard_state_discovers_cash_only_detail_runs(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "runs" / "2026-06-19" / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-tiger",
                "broker": "tiger",
                "account_alias": "growth",
                "currency": "USD",
                "cash_balance": "10.00",
                "available_balance": "10.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    assert state["broker_detail_month"] == "2026-06-19"
    assert state["detail_available"] is True
    assert len(state["cash_details"]) == 1
    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["tiger"]["detail_available"] is True
    assert summaries["tiger"]["holding_value_hkd"] == "0.00"
    assert summaries["tiger"]["cash_like_value_hkd"] == "78.00"
    assert summaries["tiger"]["portfolio_value_hkd"] == "78.00"
    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["tiger"]["status"] == "non_realtime"
    assert statuses["tiger"]["display_text"] == "仅月结单明细"


def test_tiger_live_detail_without_fx_does_not_guess_cash_value(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "runs" / "2026-07-16" / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-07-16-tiger-live",
                "broker": "tiger",
                "account_alias": "tiger_5683",
                "currency": "USD",
                "cash_balance": "10",
                "available_balance": "10",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    summary = next(row for row in state["broker_summaries"] if row["broker"] == "tiger")
    assert summary["cash_like_value_hkd"] == ""
    assert summary["portfolio_value_hkd"] == ""


def test_load_dashboard_state_marks_futu_and_tiger_live_only_from_live_statement_ids(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu-live",
                "broker": "futu",
                "account_alias": "main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "ProShares VIX Short-Term Futures ETF",
                "currency": "USD",
                "quantity": "40",
                "cost_price": "44.00",
                "last_price": "48.50",
                "market_value": "1940.00",
                "cost_value": "1760.00",
                "unrealized_pnl": "180.00",
                "confidence": "high",
                "notes": "",
            },
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-tiger-live",
                "broker": "tiger",
                "account_alias": "growth",
                "currency": "USD",
                "cash_balance": "10.00",
                "available_balance": "10.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["status"] == "ok"
    assert statuses["futu"]["display_text"] == "账户实时同步"
    assert statuses["tiger"]["status"] == "ok"
    assert statuses["tiger"]["display_text"] == "账户实时同步，行情走富途"


def test_load_dashboard_state_rejects_live_marker_unless_statement_id_suffix(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    row = {
        "statement_id": "2026-05-futu-live-statement-import",
        "broker": "futu",
        "account_alias": "main",
        "market": "US",
        "asset_class": "etf",
        "symbol": "VIXY",
        "name": "ProShares VIX Short-Term Futures ETF",
        "currency": "USD",
        "quantity": "40",
        "cost_price": "44.00",
        "last_price": "48.50",
        "market_value": "1940.00",
        "cost_value": "1760.00",
        "unrealized_pnl": "180.00",
        "confidence": "high",
        "notes": "",
    }
    write_csv(run_dir / "extracted_positions.csv", POSITION_FIELDNAMES, [row])

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["status"] == "non_realtime"
    assert statuses["futu"]["display_text"] == "仅月结单明细"

    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [{**row, "statement_id": "2026-06-19-futu-live"}],
    )

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["futu"]["status"] == "ok"
    assert statuses["futu"]["display_text"] == "账户实时同步"


def test_load_dashboard_state_uses_phillips_statement_id_for_source_status(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    write_csv(
        config.data_dir / "runs" / "2026-06-19" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-05-phillips",
                "broker": "phillips",
                "account_alias": "cash",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "00700",
                "name": "Tencent",
                "currency": "HKD",
                "quantity": "100",
                "cost_price": "100.00",
                "last_price": "150.00",
                "market_value": "15000.00",
                "cost_value": "10000.00",
                "unrealized_pnl": "5000.00",
                "confidence": "high",
                "notes": "",
            }
        ],
    )

    state = load_dashboard_state(config).to_dict()

    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["phillips"]["display_text"] == "2026-05 月结单导入"


def test_load_dashboard_state_prefers_newer_statement_over_newer_daily_copy(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    def phillips_row(statement_id: str, symbol: str) -> dict[str, str]:
        return {
            "statement_id": statement_id,
            "broker": "phillips",
            "account_alias": "phillips_main",
            "market": "HK",
            "asset_class": "stock",
            "symbol": symbol,
            "name": symbol,
            "currency": "HKD",
            "quantity": "1",
            "cost_price": "80",
            "last_price": "100",
            "market_value": "100",
            "cost_value": "80",
            "unrealized_pnl": "20",
            "confidence": "high",
            "notes": "",
        }

    write_csv(
        config.data_dir / "runs" / "2026-07-16" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [phillips_row("2026-07-10-phillips", "OLD")],
    )
    write_csv(
        config.data_dir / "runs" / "2026-07" / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [phillips_row("2026-07-15-phillips", "NEW")],
    )

    details, _ = dashboard_module._latest_broker_details(config.data_dir)
    details = [row for row in details if row["broker"] == "phillips"]
    assert [row["symbol"] for row in details] == ["NEW"]
    state = load_dashboard_state(config).to_dict()
    statuses = {row["broker"]: row for row in state["source_statuses"]}
    assert statuses["phillips"]["display_text"] == "2026-07-15 月结单导入"


def test_load_dashboard_state_blanks_unsupported_or_malformed_detail_money(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    run_dir = config.data_dir / "runs" / "2026-06-19"
    write_csv(
        run_dir / "extracted_positions.csv",
        POSITION_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "US",
                "asset_class": "etf",
                "symbol": "VIXY",
                "name": "VIXY",
                "currency": "USD",
                "quantity": "1",
                "cost_price": "",
                "last_price": "",
                "market_value": "10.00",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "high",
                "notes": "",
            },
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "00001",
                "name": "Unsupported Currency",
                "currency": "EUR",
                "quantity": "1",
                "cost_price": "",
                "last_price": "",
                "market_value": "100.00",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "low",
                "notes": "",
            },
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "market": "HK",
                "asset_class": "stock",
                "symbol": "00002",
                "name": "Malformed Value",
                "currency": "HKD",
                "quantity": "1",
                "cost_price": "",
                "last_price": "",
                "market_value": "not-money",
                "cost_value": "",
                "unrealized_pnl": "",
                "confidence": "low",
                "notes": "",
            },
        ],
    )
    write_csv(
        run_dir / "extracted_cash.csv",
        CASH_FIELDNAMES,
        [
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "currency": "USD",
                "cash_balance": "bad-cash",
                "available_balance": "bad-cash",
                "confidence": "low",
                "notes": "",
            },
            {
                "statement_id": "2026-06-19-futu",
                "broker": "futu",
                "account_alias": "main",
                "currency": "CNY",
                "cash_balance": "100.00",
                "available_balance": "100.00",
                "confidence": "high",
                "notes": "",
            },
        ],
    )

    state = load_dashboard_state(config).to_dict()

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["futu"]["holding_value_hkd"] == ""
    assert summaries["futu"]["cash_like_value_hkd"] == ""
    assert summaries["futu"]["portfolio_value_hkd"] == ""


def test_load_dashboard_state_uses_single_broker_portfolio_fallback(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = [
        {**portfolio_rows()[0], "brokers": "phillips", "accounts": "cash"},
        {**portfolio_rows()[1], "brokers": "phillips", "accounts": "cash"},
    ]
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)

    state = load_dashboard_state(config).to_dict()

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["phillips"]["detail_available"] is False
    assert summaries["phillips"]["holding_value_hkd"] == "37830.00"
    assert summaries["phillips"]["cash_like_value_hkd"] == "850.00"
    assert summaries["phillips"]["portfolio_value_hkd"] == "38680.00"
    assert summaries["phillips"]["holding_count"] == 1


def test_load_dashboard_state_blanks_multi_broker_portfolio_fallback(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())

    state = load_dashboard_state(config).to_dict()

    summaries = {row["broker"]: row for row in state["broker_summaries"]}
    assert summaries["futu"]["holding_value_hkd"] == ""
    assert summaries["futu"]["cash_like_value_hkd"] == ""
    assert summaries["futu"]["portfolio_value_hkd"] == ""
    assert summaries["futu"]["holding_count"] == 0
    assert summaries["tiger"]["holding_value_hkd"] == ""
    assert summaries["tiger"]["cash_like_value_hkd"] == ""
    assert summaries["tiger"]["portfolio_value_hkd"] == ""
    assert summaries["tiger"]["holding_count"] == 0


def test_load_dashboard_state_exposes_kelly_lab_and_holding_detail(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    rows = portfolio_rows()
    rows.append(
        {
            **rows[0],
            "symbol": "QQQ",
            "name": "Invesco QQQ Trust",
            "analysis_symbol": "QQQ",
        }
    )
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, rows)
    latest = tmp_path / "data" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "kelly_strategy_templates.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_strategy_templates.v1",
                "templates": [
                    {
                        "strategy_id": "trend_pullback_20d",
                        "strategy_name": "趋势回调 20D",
                        "strategy_version": "v1",
                        "entry_rule_description": "价格回调到 20 日均线附近。",
                        "exit_rule_description": "目标价、止损或 20 个交易日到期。",
                        "max_holding_days": 20,
                        "order_type": "limit",
                        "market_session": "regular",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (latest / "kelly_experiments.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_experiments.v1",
                "experiments": [
                    {
                        "experiment_id": "trend_pullback_20d_exp_20260707",
                        "experiment_name": "趋势回调 20D 第一批",
                        "strategy_id": "trend_pullback_20d",
                        "strategy_version": "v1",
                        "market": "US",
                        "start_date": "2026-07-07",
                        "paper_account": "futu_simulate",
                        "experiment_budget": "100000",
                        "budget_currency": "USD",
                        "capital_utilization_pct": "50",
                        "allocation_mode": "equal_weight",
                        "max_open_position_per_symbol": 1,
                        "status": "running",
                        "locked": True,
                        "participants": [
                            {
                                "market": "US",
                                "symbol": "VIXY",
                                "name": "ProShares VIX",
                                "source": "holding+watchlist",
                                "locked": True,
                                "per_symbol_budget": "25000",
                                "budget_currency": "USD",
                            }
                        ],
                        "stats": {
                            "completed_samples": 0,
                            "open_samples": 0,
                            "observed_win_rate": "",
                            "sample_stage": "insufficient",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (latest / "kelly_order_executions.json").write_text(
        json.dumps(
            {
                "schema_version": "open_trader.kelly_order_executions.v1",
                "environment": "DRY_RUN",
                "source": "dry_run",
                "executed_at": "2026-07-10 13:32",
                "execution_count": 1,
                "submitted_count": 0,
                "dry_run_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "executions": [
                    {
                        "intent_id": "trend_pullback_20d_exp_20260707:US:VIXY:entry",
                        "experiment_id": "trend_pullback_20d_exp_20260707",
                        "market": "US",
                        "symbol": "VIXY",
                        "futu_code": "US.VIXY",
                        "side": "buy",
                        "order_type": "NORMAL",
                        "price": "12.5",
                        "qty": "80",
                        "planned_notional": "1000",
                        "budget_currency": "USD",
                        "execution_status": "dry_run",
                        "submitted": False,
                        "futu_order_id": "",
                        "executed_at": "2026-07-10 13:32",
                        "error": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    trade_samples_payload = {
        "schema_version": "open_trader.kelly_trade_samples.v1",
        "generated_at": "2026-07-11 12:00",
        "source_orders_synced_at": "2026-07-11 11:59",
        "sample_count": 0,
        "open_position_count": 0,
        "skipped_order_count": 0,
        "stats_by_experiment": {},
        "samples": [],
        "open_positions": [],
        "diagnostics": {"skipped_orders": []},
    }
    (latest / "kelly_trade_samples.json").write_text(
        json.dumps(trade_samples_payload),
        encoding="utf-8",
    )
    (latest / "kelly_strategy_stats.json").write_text(
        json.dumps(
            build_kelly_strategy_stats_payload(
                [
                    {
                        "experiment_id": "trend_pullback_20d_exp_20260707",
                        "experiment_name": "趋势回调 20D 第一批",
                        "market": "US",
                    }
                ],
                trade_samples_payload,
                generated_at="2026-07-11 12:01",
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = load_dashboard_state(config).to_dict()

    assert state["kelly_lab"]["available"] is True
    assert state["kelly_lab"]["experiment_count"] == 1
    experiment = state["kelly_lab"]["experiments"][0]
    assert experiment["order_execution"] == {
        "status": "success",
        "environment": "DRY_RUN",
        "source": "dry_run",
        "last_executed_at": "2026-07-10 13:32",
        "execution_count": 1,
        "submitted_count": 0,
        "dry_run_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "message": "Kelly 订单执行结果已生成。",
        "executions": [
            {
                "intent_id": "trend_pullback_20d_exp_20260707:US:VIXY:entry",
                "experiment_id": "trend_pullback_20d_exp_20260707",
                "market": "US",
                "symbol": "VIXY",
                "futu_code": "US.VIXY",
                "side": "buy",
                "order_type": "NORMAL",
                "price": "12.5",
                "qty": "80",
                "planned_notional": "1000",
                "budget_currency": "USD",
                "execution_status": "dry_run",
                "submitted": False,
                "futu_order_id": "",
                "executed_at": "2026-07-10 13:32",
                "error": "",
            }
        ],
    }
    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
    assert vixy["kelly"]["available"] is True
    assert vixy["kelly"]["experiment_count"] == 1
    assert vixy["kelly"]["status"] == "available"
    assert vixy["kelly"]["message"] == "该标的已关联 Kelly 策略实验。"
    assert (
        vixy["kelly"]["experiments"][0]["experiment_id"]
        == "trend_pullback_20d_exp_20260707"
    )
    qqq = next(row for row in state["holdings"] if row["symbol"] == "QQQ")
    assert qqq["kelly"]["available"] is False
    assert qqq["kelly"]["experiment_count"] == 0
    assert qqq["kelly"]["experiments"] == []
    assert qqq["kelly"]["status"] == "missing_experiment"
    assert qqq["kelly"]["message"] == "该标的未参与任何已锁定的 Kelly 策略实验。"


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

    assert len(state["holdings"]) == 1
    assert state["kelly_lab"]["available"] is False
    assert state["kelly_lab"]["template_count"] == 0
    assert state["kelly_lab"]["experiment_count"] == 0
    assert state["kelly_lab"]["templates"] == []
    assert state["kelly_lab"]["experiments"] == []
    assert "Kelly Lab" in state["kelly_lab"]["error"]
    assert "kelly_strategy_templates.json schema_version" in state["kelly_lab"]["error"]
    vixy = next(row for row in state["holdings"] if row["symbol"] == "VIXY")
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

    holding = load_dashboard_state(config).to_dict()["holdings"][0]

    assert holding["decision_plan"]["available"] is True
    assert holding["decision_plan"]["status"] == "triggered"
    assert holding["decision_plan"]["conditions"][0]["trigger_count"] == 2
    review = holding["decision_plan"]["previous_review"]
    assert review["run_date"] == "2026-07-10"
    assert review["starting_quantity"] == "100"
    assert review["closing_quantity"] == "100"
    assert "compliance" not in review


def test_dashboard_exposes_invalid_plan_as_failed_state(tmp_path: Path) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, portfolio_rows())
    current_path = config.data_dir / "latest/US/decision_plans.json"
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"schema_version":"invalid"}', encoding="utf-8")

    plan = load_dashboard_state(config).to_dict()["holdings"][0]["decision_plan"]

    assert plan["available"] is False
    assert plan["error"] == "decision_plans.json 无效"
