from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "src" / "open_trader" / "dashboard_static"
FIXTURE_PATH = Path(__file__).with_name("fixtures") / "kelly-dashboard.json"


def _prediction_payload(scenario: str) -> dict[str, object]:
    if scenario == "preview-rejected":
        scenario = "ready"
    elif scenario == "preview-incomplete":
        scenario = "ready"
    elif scenario == "reset-denied":
        scenario = "incident"
    if scenario == "reset":
        scenario = "incident"
    elif scenario == "history-signals":
        scenario = "ready"
    elif scenario == "history-executions":
        scenario = "success"
    elif scenario == "history-incidents":
        scenario = "incident"
    history_kind = "executions" if scenario in {"success", "success-incomplete"} else "incidents" if scenario in {"incident", "incident-incomplete"} else "signals"
    opportunity = {
        "opportunity_id": "opp-ceasefire",
        "event_id": "event-ceasefire",
        "title": "以色列与伊朗停火是否持续至 2026 年 8 月 31 日？",
        "venue": "Polymarket",
        "market_type": "standard_binary",
        "fee_status": "fee_free",
        "updated_at": "2026-07-28T08:18:41Z",
        "yes_price": "0.450",
        "no_price": "0.490",
        "quantity": "20",
        "yes_cost": "9.00",
        "no_cost": "9.80",
        "max_cost": "18.80",
        "merge_value": "20.00",
        "profit": "1.20",
        "minimum_profit": "1.20",
        "volume_24h": "9700000",
        "actionable": scenario in {"ready", "confirmation", "executing", "success", "success-incomplete", "incomplete"},
    }
    events = [
        {"event_id": "event-ceasefire", "title": "Will the Israel-Iran ceasefire continue through August 31, 2026?", "title_zh": "以色列与伊朗停火是否持续至 8 月 31 日？", "volume_24h": "9700000", "markets": "1 个普通二元市场", "profit": "1.20", "actionable": opportunity["actionable"], "details": [["停火持续至 8 月 31 日？", "普通二元 · 免手续费 · 已订阅"], ["当前执行条件", "20 组 · 最多 $18.80 · 可参与"]], "opportunities": [opportunity]},
        {"event_id": "event-btc", "title": "比特币会在 8 月突破 $150,000？", "volume_24h": "12800000", "markets": "2 个市场", "profit": "4.60", "actionable": False, "opportunities": [{"title": "主要二元市场", "volume_24h": "12800000", "actionable": False, "reason": "已订阅 · 收费市场不可参与"}]},
        {"event_id": "event-fed", "title": "2026 年 9 月美联储是否降息？", "volume_24h": "15400000", "markets": "8 个市场", "profit": "3.80", "actionable": False, "opportunities": [{"title": "多结果事件市场", "volume_24h": "15400000", "actionable": False, "reason": "已订阅 · Negative Risk 暂不可参与"}]},
        {"event_id": "event-eth", "title": "以太坊会在 9 月前突破 $6,000？", "volume_24h": "7100000", "markets": "2 个市场", "profit": "2.10", "actionable": False, "opportunities": [{"title": "主要二元市场", "volume_24h": "7100000", "actionable": False, "reason": "已订阅 · 净利润未达到策略门槛"}]},
        {"event_id": "event-senate", "title": "2026 年美国参议院控制权", "volume_24h": "6800000", "markets": "12 个市场", "profit": "1.40", "actionable": False, "opportunities": [{"title": "多结果事件市场", "volume_24h": "6800000", "actionable": False, "reason": "已订阅 · Negative Risk 暂不可参与"}]},
        {"event_id": "event-fed-chair", "title": "下一任美联储主席人选", "volume_24h": "5900000", "markets": "14 个市场", "profit": "1.40", "actionable": False, "opportunities": [{"title": "多结果事件市场", "volume_24h": "5900000", "markets": "14 个市场", "actionable": False, "reason": "已订阅 · Negative Risk 暂不可参与"}]},
    ]
    event_labels = {
        "event-ceasefire": ("净利润", "可参与"),
        "event-btc": ("毛利润上限", "收费市场不可参与"),
        "event-fed": ("毛利润上限", "Negative Risk 暂不可参与"),
        "event-eth": ("毛利润上限", "净利润不足"),
        "event-senate": ("毛利润上限", "Negative Risk 暂不可参与"),
        "event-fed-chair": ("毛利润上限", "Negative Risk 暂不可参与"),
    }
    events = [
        {**event, "profit_label": event_labels.get(str(event.get("event_id")), ("净利润", "暂不可参与"))[0], "status": event_labels.get(str(event.get("event_id")), ("净利润", "暂不可参与"))[1]}
        for event in events
    ]
    cross_legs = [
        {"exchange": "predict.fun", "market_id": "predict-market-fixture", "condition_id": "predict-condition-fixture", "outcome": "YES", "token_id": "predict-yes-fixture", "official_url": "https://predict.fun/markets/predict-market-fixture"},
        {"exchange": "polymarket", "market_id": "poly-market-fixture", "condition_id": "poly-condition-fixture", "outcome": "NO", "token_id": "poly-no-fixture", "official_url": "https://polymarket.com/event/poly-market-fixture"},
    ]
    cross_opportunity = {
        "opportunity_id": "cross-opportunity-observe-only-fixture",
        "event_id": "cross-event-actionable-fixture",
        "title": "Will Bitcoin close above $100,000 on December 31, 2026?",
        "title_zh": "比特币会在 2026 年 12 月 31 日收于 $100,000 以上吗？",
        "market_type": "cross_venue_yes_no",
        "execution_mode": "observe_only",
        "legs": [
            {**cross_legs[0], "max_price": "0.470", "max_cost": "2.35", "maximum_fee": "0.02", "fee_asset": "USDT", "net_quantity": "5", "settlement_asset": "USDT"},
            {**cross_legs[1], "max_price": "0.490", "max_cost": "2.45", "maximum_fee": "0.00", "fee_asset": "pUSD", "net_quantity": "5", "settlement_asset": "pUSD"},
        ],
        "quantity": "5",
        "net_quantity": "5",
        "total_max_cost": "4.80",
        "minimum_payout": "5.00",
        "minimum_profit": "0.20",
        "annualized_yield": "0.201",
        "canonical_cutoff": "2026-12-31T23:59:00Z",
        "resolution_at": "2026-12-31T23:59:00Z",
        "clear_signal": True,
        "funnel_stage": 5,
        "codex_approval": {
            "decision": "APPROVE",
            "summary": "两所规则确认同一截止时间，YES/NO 方向直接互补。",
            "evidence": [
                {"exchange": "predict.fun", "field": "cutoff", "quote": "at 23:59 UTC on December 31, 2026"},
                {"exchange": "polymarket", "field": "cutoff", "quote": "at 23:59 UTC on December 31, 2026"},
            ],
        },
        "venue_balances": [
            {"exchange": "predict.fun", "wallet": "0xcE23…f435", "asset": "USDT", "available": "12.34"},
            {"exchange": "polymarket", "wallet": "0x7A4E…91C2", "asset": "pUSD", "available": "50.00"},
        ],
        "unsettled": {"current": "35.20", "after": "40.00", "limit": "100"},
        "data_timestamps": {"predict.fun": "2026-08-03T15:40:00Z", "polymarket": "2026-08-03T15:40:01Z", "codex_reviewed_at": "2026-08-03T15:41:00Z"},
        "volume_24h": "320000",
        "actionable": True,
    }
    cross_below_threshold = {
        **cross_opportunity,
        "opportunity_id": "cross-opportunity-below-threshold-fixture",
        "event_id": "cross-event-below-threshold-fixture",
        "title": "Will Ethereum close above $6,000 on December 31, 2026?",
        "title_zh": "以太坊会在 2026 年 12 月 31 日收于 $6,000 以上吗？",
        "legs": [
            {**cross_opportunity["legs"][0], "token_id": "predict-yes-below-threshold"},
            {**cross_opportunity["legs"][1], "token_id": "poly-no-below-threshold"},
        ],
        "minimum_profit": "0.08",
        "annualized_yield": "0.149",
        "funnel_stage": 4,
        "clear_signal": False,
        "eligibility_reason": "annualized_yield_below_minimum",
        "actionable": False,
    }
    manual_cross_opportunity = {
        **cross_opportunity,
        "opportunity_id": "cross-opportunity-actionable-fixture",
        "event_id": "cross-event-actionable-fixture",
        "execution_mode": "manual_confirm",
        "manual_only": True,
        "manual_reason": "UNRESOLVED_UNCERTAINTY",
    }
    if scenario == "quiet":
        opportunity = {**opportunity, "actionable": False}
        events[0] = {**events[0], "actionable": False, "opportunities": [opportunity]}
    if scenario == "incomplete":
        opportunity = {
            key: value
            for key, value in opportunity.items()
            if key not in {"no_price", "profit", "minimum_profit"}
        }
        opportunity["actionable"] = True
        events[0] = {**events[0], "actionable": True, "opportunities": [opportunity]}
    if scenario == "loading":
        return {"status": "loading", "health": {"status": "loading", "degraded_reasons": ["universe_unavailable"]}, "failure_reason": "universe_unavailable", "readiness": {"status": "unavailable", "reason": "readiness_unavailable"}, "venues": [{"venue": "polymarket", "rest": "unavailable", "ws": "unavailable", "wallet": "-", "balance": {"asset": "pUSD", "value": None}, "mode": "只读"}, {"venue": "predict.fun", "rest": "unavailable", "ws": "unavailable", "wallet": "-", "balance": {"asset": "USDT", "value": None}, "mode": "只读"}], "events": [], "opportunities": [], "histories": {"signals": []}, "breaker": {"open": True}, "csrf_token": "fixture-csrf"}
    if scenario == "degraded":
        return {"status": "degraded", "health": {"status": "degraded", "degraded_reasons": ["heartbeat_stale"]}, "failure_reason": "heartbeat_stale", "stale": True, "readiness": {"status": "degraded", "wallet_address": "0x7A4E…91C2", "balance": "50.00", "geoblock": "blocked", "relayer": "ready"}, "wallet": {"masked_address": "0x7A4E…91C2"}, "masked_wallet": "0x7A4E…91C2", "venues": [{"venue": "polymarket", "rest": "degraded", "ws": "stale", "wallet": "0x7A4E…91C2", "balance": {"asset": "pUSD", "value": "50.00"}, "mode": "只读"}, {"venue": "predict.fun", "rest": "unavailable", "ws": "unavailable", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": None}, "mode": "只读"}], "events": events, "opportunities": [opportunity], "histories": {"signals": _prediction_history("signals")}, "signals_24h": 3, "event_count": 20, "market_count": 331, "token_count": 662, "breaker": {"open": True}, "heartbeat_at": "2026-07-28T08:17:40Z", "csrf_token": "fixture-csrf"}
    if scenario in {"unavailable", "unknown"}:
        status = "unavailable" if scenario == "unavailable" else "mystery"
        reason = "configuration_unavailable" if scenario == "unavailable" else "status_unknown"
        return {"status": status, "health": {"status": status, "degraded_reasons": [reason]}, "failure_reason": reason, "readiness": {"status": "unavailable", "reason": reason}, "venues": [{"venue": "polymarket", "rest": "unavailable", "ws": "unavailable", "wallet": "-", "balance": {"asset": "pUSD", "value": None}, "mode": "只读"}, {"venue": "predict.fun", "rest": "unavailable", "ws": "unavailable", "wallet": "-", "balance": {"asset": "USDT", "value": None}, "mode": "只读"}], "events": [], "opportunities": [], "histories": {"signals": []}, "breaker": {"open": True}, "csrf_token": "fixture-csrf"}
    payload: dict[str, object] = {
        "status": "healthy",
        "health": {"status": "healthy", "degraded_reasons": []},
        "readiness": {"status": "ready", "wallet_address": "0x7A4E…91C2", "balance": "49.40" if scenario in {"incident", "incident-incomplete"} else "51.20" if scenario in {"success", "success-incomplete"} else "50.00", "geoblock": "allowed", "relayer": "ready"},
        "wallet": {"masked_address": "0x7A4E…91C2"},
        "masked_wallet": "0x7A4E…91C2",
        "balances": {"p_usd": "50.00", "allowance": "50.00"},
        "venues": [
            {"venue": "polymarket", "rest": "ready", "ws": "ready", "wallet": "0x7A4E…91C2", "balance": {"asset": "pUSD", "value": "50.00"}, "mode": "可以交易", "last_success": "2026-08-02T01:00:00Z"},
            {"venue": "predict.fun", "rest": "ready", "ws": "ready", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": "12.34"}, "allowance": {"asset": "USDT", "value": "20.00", "spender": "0xSpender…C0DE"}, "mode": "可以交易", "last_success": "2026-08-02T00:59:58Z"},
        ],
        "privy_signer": {"address": "0xBnbSigner…BEEF", "mode": "可以交易", "bnb": {"current": "0.012", "required": "0.004", "minimum": "0.006"}, "copy_text": "BNB top-up to 0xBnbSigner…BEEF on BNB Smart Chain", "official_links": [{"label": "Predict.fun", "url": "https://predict.fun/"}, {"label": "BNB Chain", "url": "https://www.bnbchain.org/"}]},
        "cross_venue": {
            "mode": "manual_confirm" if scenario == "cross-manual-confirm" else "observe_only",
            "funnel": {"matched_pairs": 12, "monitored_pairs": 8, "codex_approved_pairs": 5, "arbitrage_space_pairs": 2, "clear_signal_pairs": 1, "manual_eligible_pairs": 3, "manual_pending_pairs": 1, "retained_at": "2026-08-03T15:39:00Z"},
            "unsettled": {"current": "35.20", "limit": "100"},
            "breaker": {"open": False, "scope": "cross_venue"},
        },
        "cross_auto": {
            "configured_mode": "observe_only",
            "effective_mode": "observe_only",
            "armed": False,
            "pause_reason": "not_armed",
            "notification_ready": True,
            "daily_principal": {"current": "0", "limit": "100"},
            "latest_attempt": None,
        },
        "policy_limits": {"max_wallet_balance": "65", "max_normal_cost": "20", "max_emergency_loss": "2", "max_cross_unsettled_principal": "100", "min_estimated_profit": "1"},
        "heartbeat_at": "2026-08-01T02:00:00Z",
        "event_count": 20,
        "market_count": 331,
        "token_count": 662,
        "signals_24h": 3,
        "events": events,
        "opportunities": [] if scenario == "quiet" else [opportunity, manual_cross_opportunity if scenario == "cross-manual-confirm" else cross_opportunity, cross_below_threshold],
        "histories": {history_kind: _prediction_history_for_scenario(history_kind, scenario)},
        "breaker": {"open": scenario == "incident", "status": "locked" if scenario == "incident" else "ready"},
        "csrf_token": "fixture-csrf",
    }
    if scenario == "ready-zero-allowance":
        payload["venues"][1] = {**payload["venues"][1], "allowance": {"asset": "USDT", "value": "0", "spender": "0xSpender…C0DE"}, "mode": "可以交易"}
    if scenario in {"signer-bnb-low", "cross-signal-bnb-low"}:
        payload["privy_signer"] = {**payload["privy_signer"], "mode": "只读", "bnb": {"current": "0.001", "required": "0.004", "minimum": "0.006"}}
        if scenario == "signer-bnb-low":
            payload["opportunities"] = []
            payload["events"] = []
            payload["cross_venue"] = {**payload["cross_venue"], "funnel": {**payload["cross_venue"]["funnel"], "clear_signal_pairs": 0}}
        else:
            payload["venues"][1] = {**payload["venues"][1], "mode": "只读", "reason": "insufficient_signer_bnb"}
    if scenario in {"residual-allowance", "cleanup-success", "cleanup-failure"}:
        payload["venues"][1] = {**payload["venues"][1], "mode": "熔断只读", "allowance": {"asset": "USDT", "value": "2.40", "spender": "0xSpender…C0DE"}}
        payload["predict_allowance_cleanup"] = {"owner": "0xcE23…f435", "spender": "0xSpender…C0DE", "before_allowance": "2.40", "after_allowance": "0", "gas_effect": "消耗 Privy signer BNB，不转移 USDT"}
        payload["cross_venue"]["breaker"] = {"open": True, "scope": "predict_allowance", "reason": "residual_allowance"}
    if scenario == "cross-stale-stage4":
        stale = {**manual_cross_opportunity, "clear_signal": False, "funnel_stage": 4, "actionable": False, "eligibility_reason": "books_stale"}
        payload["stale"] = True
        payload["events"] = [{**stale, "opportunities": [stale]}]
        payload["opportunities"] = [stale]
        payload["cross_venue"] = {**payload["cross_venue"], "funnel": {"matched_pairs": 12, "monitored_pairs": 8, "codex_approved_pairs": 5, "arbitrage_space_pairs": 2, "clear_signal_pairs": 0, "manual_eligible_pairs": 3, "manual_pending_pairs": 0, "retained_at": "2026-08-03T15:39:00Z"}}
    if scenario == "cross-empty-scan":
        payload["events"] = []
        payload["opportunities"] = []
        payload["signals_24h"] = 0
        payload["cross_venue"] = {**payload["cross_venue"], "funnel": {"matched_pairs": 0, "monitored_pairs": 0, "codex_approved_pairs": 0, "arbitrage_space_pairs": 0, "clear_signal_pairs": 0, "manual_eligible_pairs": 0, "manual_pending_pairs": 0}}
    if scenario in {
        "cross-auto-armed", "cross-auto-paused", "cross-auto-daily-cap",
        "cross-auto-same-pair", "cross-auto-notification-blocked", "cross-auto-manual-only",
        "cross-auto-paused-manual-only", "cross-auto-submitted",
    }:
        paused = scenario in {"cross-auto-paused", "cross-auto-notification-blocked", "cross-auto-paused-manual-only"}
        reason = {
            "cross-auto-paused": ("operator_paused", "操作员已暂停自动下单", "-", "-", "both", True),
            "cross-auto-daily-cap": ("cross_auto_daily_principal_cap", "自动新本金已达当日上限", "100", "100", "both", False),
            "cross-auto-same-pair": ("cross_pair_unsettled", "同一标的仍有未结算执行", "1", "1", "both", False),
            "cross-auto-notification-blocked": ("notification_config_unavailable", "通知通道不可用，已暂停自动下单", "0", "1", "feishu", True),
        }.get(scenario)
        payload["cross_venue"] = {**payload["cross_venue"], "mode": "auto_submit"}
        auto_opportunity = {
            **cross_opportunity,
            "opportunity_id": "cross-opportunity-auto-fixture",
            "event_id": "cross-event-auto-fixture",
            "execution_mode": "auto_submit",
            "manual_only": False,
        }
        if scenario in {"cross-auto-manual-only", "cross-auto-paused-manual-only"}:
            payload["opportunities"] = [manual_cross_opportunity]
        else:
            payload["opportunities"] = [auto_opportunity]
        payload["cross_auto"] = {
            "configured_mode": "auto_submit",
            "effective_mode": "observe_only" if paused else "auto_submit",
            "armed": not paused,
            "pause_reason": "notification_delivery_failed" if scenario == "cross-auto-notification-blocked" else "operator_paused" if scenario in {"cross-auto-paused", "cross-auto-paused-manual-only"} else "",
            "notification_ready": scenario != "cross-auto-notification-blocked",
            "daily_principal": {"current": "100" if scenario == "cross-auto-daily-cap" else "5", "limit": "100"},
            "latest_attempt": (
                {
                    "decision": "submitted", "reason_code": "submitted",
                    "reason_zh": "已提交双边订单，等待对账", "current": None,
                    "limit": None, "venue": "跨市场",
                    "created_at": "2026-08-08T01:00:00Z",
                    "updated_at": "2026-08-08T01:01:00Z",
                    "operator_action_required": False,
                }
                if scenario == "cross-auto-submitted"
                else None if reason is None else {
                    "decision": "rejected", "reason_code": reason[0],
                    "reason_zh": reason[1], "current": reason[2],
                    "limit": reason[3], "venue": reason[4],
                    "created_at": "2026-08-08T01:00:00Z",
                    "updated_at": "2026-08-08T01:02:00Z",
                    "operator_action_required": reason[5],
                }
            ),
        }
    if scenario == "first-canary-cap5":
        payload["policy_limits"] = {**payload["policy_limits"], "max_normal_cost": "5", "canary_status": "first_live_trade"}
    if scenario == "completed-canary-cap20":
        payload["policy_limits"] = {**payload["policy_limits"], "max_normal_cost": "20", "canary_status": "completed"}
    if scenario == "post-approval-cleared":
        payload["histories"] = {"signals": [{**_prediction_history("signals")[1], "status": "未下单 · 授权已清零", "actionable_now": False}]}
    if scenario == "cross-grouped-history":
        payload["histories"] = {"executions": [{
            "completed_at": "今天 15:42:00", "event_title": "跨所比特币阈值", "market_type": "cross_venue_yes_no",
            "legs": cross_opportunity["legs"], "state": "holding_to_resolution", "status": "待兑付", "quantity": "5", "actual_cost": "4.80",
            "lifecycle": [
                {"phase": "授权", "receipt": "0xapprove-fixture", "status": "成功"},
                {"phase": "双腿订单", "receipt": "0xorders-fixture", "status": "全部成交"},
                {"phase": "对账", "receipt": "0xreconcile-fixture", "status": "余额一致"},
                {"phase": "授权清零", "receipt": "0xcleanup-fixture", "status": "完成"},
            ],
        }]}
    if scenario == "predict-pending":
        payload["venues"] = [
            payload["venues"][0],
            {"venue": "predict.fun", "rest": "pending", "ws": "pending", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": None}, "mode": "API Key 待分配", "reason": "api_key_pending", "last_success": "2026-08-02T00:59:58Z"},
        ]
    if scenario == "predict-degraded":
        payload["venues"] = [
            payload["venues"][0],
            {"venue": "predict.fun", "rest": "unavailable", "ws": "unavailable", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": None}, "mode": "只读", "reason": "predict_construction_failed", "last_success": "2026-08-02T00:59:58Z"},
        ]
    if scenario == "predict-not-configured":
        payload["venues"] = [
            payload["venues"][0],
            {"venue": "predict.fun", "rest": "unavailable", "ws": "unavailable", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": None}, "mode": "只读", "reason": "predict_not_configured", "last_success": "2026-08-02T00:59:58Z"},
        ]
    if scenario == "cross-venue-unavailable":
        payload["venues"] = [
            payload["venues"][0],
            {"venue": "predict.fun", "rest": "unavailable", "ws": "unavailable", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": None}, "mode": "只读", "reason": "cross_venue_unavailable", "last_success": "2026-08-02T00:59:58Z"},
        ]
    if scenario == "predict-stale":
        payload["venues"] = [
            payload["venues"][0],
            {"venue": "predict.fun", "rest": "stale", "ws": "stale", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": None}, "mode": "只读", "reason": "predict_stale", "last_success": "2026-08-02T00:59:58Z"},
        ]
    if scenario == "predict-auth-blocked":
        payload["venues"] = [
            payload["venues"][0],
            {"venue": "predict.fun", "rest": "unavailable", "ws": "unavailable", "wallet": "0xcE23…f435", "balance": {"asset": "USDT", "value": None}, "mode": "只读", "reason": "predict_auth_blocked", "last_success": "2026-08-02T00:59:58Z"},
        ]
    if scenario == "threshold":
        approved_threshold = {
            "opportunity_id": "threshold-approved",
            "relation_id": "threshold-approved",
            "event_id": "threshold-event",
            "market_type": "threshold_hedge",
            "question_a": "Will Bitcoin be above $100,000 on September 30, 2026?",
            "question_b": "Will Bitcoin be above $90,000 on September 30, 2026?",
            "relation": "A_IMPLIES_B",
            "condition_id_a": "0x7b000000000000000000000000000000000000091a",
            "condition_id_b": "0xc400000000000000000000000000000000000e02",
            "buy_legs": [
                {"label": "A", "outcome": "NO", "condition_id": "0x7b000000000000000000000000000000000000091a", "token_id": "token-a", "quantity": "20", "max_price": "0.42", "max_cost": "8.40"},
                {"label": "B", "outcome": "YES", "condition_id": "0xc400000000000000000000000000000000000e02", "token_id": "token-b", "quantity": "20", "max_price": "0.55", "max_cost": "11.00"},
            ],
            "quantity": "20",
            "total_max_cost": "19.46",
            "minimum_payout": "20.00",
            "minimum_profit": "0.54",
            "annualized_yield": "0.2155",
            "remaining_days": "47",
            "resolution_at": "2026-09-14T00:00:00Z",
            "depth_status": "pass",
            "max_executable_quantity": "2000",
            "max_executable_cost": "1995.00",
            "policy_quantity": "20",
            "policy_cost": "19.46",
            "volume_24h": "415000",
            "confirmed_at": "2026-07-29T06:32:05Z",
            "llm_status": "approved",
            "llm_decision": "APPROVE",
            "llm_summary": "高阈值事件蕴含低阈值事件，危险反例被排除。",
            "llm_reason_codes": [],
            "llm_evidence": [
                {"market": "A", "field": "threshold", "quote": "above $100,000"},
                {"market": "B", "field": "threshold", "quote": "above $90,000"},
            ],
            "llm_uncertainties": [],
            "codex_model": "deepseek-v4-flash-max",
            "actionable": True,
        }
        rejected_threshold = {
            **approved_threshold,
            "opportunity_id": "threshold-rejected",
            "relation_id": "threshold-rejected",
            "event_id": "threshold-event-rejected",
            "question_a": "Will ETH be above $6,000 on December 31, 2026?",
            "question_b": "Will ETH be above $5,000 on December 31, 2026?",
            "condition_id_a": "condition-eth-a",
            "condition_id_b": "condition-eth-b",
            "buy_legs": [
                {"label": "A", "outcome": "NO", "condition_id": "condition-eth-a", "token_id": "token-eth-a", "quantity": "20", "max_price": "0.42", "max_cost": "8.40"},
                {"label": "B", "outcome": "YES", "condition_id": "condition-eth-b", "token_id": "token-eth-b", "quantity": "20", "max_price": "0.55", "max_cost": "11.00"},
            ],
            "annualized_yield": "0.154",
            "volume_24h": "92000",
            "llm_status": "llm_rejected",
            "llm_decision": "REJECT",
            "llm_summary": "两份规则的特殊结算条款不一致。",
            "llm_reason_codes": ["SPECIAL_SETTLEMENT_MISMATCH"],
            "llm_evidence": [{"market": "A", "field": "special_settlement", "quote": "50-50"}],
            "llm_uncertainties": ["特殊结算可能破坏覆盖关系"],
            "actionable": False,
        }
        payload["opportunities"] = [opportunity, approved_threshold, rejected_threshold]
        payload["relation_discovery"] = {
            "status": "healthy",
            "catalog": {"status": "healthy"},
            "scan_logs": [
                {"phase": "full_scan", "events": 428, "candidates": 14},
                {"phase": "books", "positive": 2, "actionable": 1},
            ],
            "codex_usage_24h": {
                "calls": 18,
                "successes": 17,
                "failures": 0,
                "cache_hits": 11,
                "input_tokens": 41200,
                "output_tokens": 8600,
            },
            "llm_usage_24h_by_provider": {
                "codex": {
                    "calls": 12,
                    "successes": 12,
                    "failures": 0,
                    "cache_hits": 9,
                    "input_tokens": 32100,
                    "output_tokens": 6100,
                },
                "deepseek": {
                    "calls": 6,
                    "successes": 5,
                    "failures": 1,
                    "cache_hits": 2,
                    "input_tokens": 9100,
                    "output_tokens": 2500,
                },
            },
            "annualized_distribution": {
                "current": {"count": 2, "median": "0.185", "p90": "0.2155"},
                "7d": {"count": 86, "median": "0.21", "p90": "0.34"},
                "30d": {"count": 302, "median": "0.19", "p90": "0.31"},
            },
        }
    if scenario == "cross-submitting":
        payload["current_execution"] = {"execution_id": "cross-exec-fixture", "status": "submitting", "market_type": "cross_venue_yes_no", "event_title": cross_opportunity["title"], "legs": cross_opportunity["legs"]}
    elif scenario == "cross-reconciling":
        payload["current_execution"] = {"execution_id": "cross-exec-fixture", "status": "reconciling", "market_type": "cross_venue_yes_no", "event_title": cross_opportunity["title"], "legs": cross_opportunity["legs"]}
    elif scenario == "cross-holding":
        payload["current_execution"] = {"execution_id": "cross-exec-fixture", "status": "holding_to_resolution", "market_type": "cross_venue_yes_no", "event_title": cross_opportunity["title"], "legs": cross_opportunity["legs"], "redemption_status": "pending"}
    elif scenario == "cross-dust":
        payload["breaker"] = {"open": True, "status": "locked", "incident": {"incident_id": "cross-dust-fixture", "happened_at": "2026-08-02T01:05:00Z", "event_title": cross_opportunity["title"], "reason": "cross_dust", "remediation": "残余头寸待人工处理", "loss": "-0.80", "state": "dust_incident", "legs": cross_opportunity["legs"]}}
    elif scenario == "cross-breaker":
        payload["cross_venue"]["breaker"] = {"open": True, "scope": "cross_venue", "reason": "cross_circuit_breaker_open"}
        payload["breaker"] = {"open": True, "status": "locked", "incident": {"incident_id": "cross-breaker-fixture", "happened_at": "2026-08-02T01:06:00Z", "event_title": cross_opportunity["title"], "reason": "cross_circuit_breaker_open", "remediation": "已停止新的跨所订单", "loss": "-0.80", "state": "directional_incident", "legs": cross_opportunity["legs"]}}
    elif scenario == "executing":
        payload["current_execution"] = {"execution_id": "exec-fixture", "status": "reconciling", "event_title": "停火持续至 8 月 31 日？"}
    elif scenario == "success":
        payload["current_execution"] = {"execution_id": "exec-fixture", "status": "completed", "event_title": opportunity["title"], "quantity": "20", "realized_profit": "1.20", "actual_cost": "18.80", "merge_value": "20.00", "completed_at": "14:36:12"}
    elif scenario == "success-incomplete":
        payload["current_execution"] = {"execution_id": "exec-fixture", "status": "completed"}
    elif scenario == "incident":
        payload["breaker"] = {"open": True, "status": "locked", "incident": {"incident_id": "incident-fixture", "happened_at": "2026-07-28T06:36:09Z", "event_title": opportunity["title"], "reason": "YES 成交、NO 被拒", "loss": "-0.60"}}
    elif scenario == "incident-incomplete":
        payload["breaker"] = {"open": True, "status": "locked", "incident": {"incident_id": "incident-fixture"}}
    return payload


def _prediction_history(kind: str) -> list[dict[str, object]]:
    if kind == "signals":
        return [
            {"signal_id": "signal-ceasefire", "opportunity_id": "opp-ceasefire", "occurred_at": "2026-08-01T01:59:00Z", "event_title": "Will the Israel-Iran ceasefire continue through August 31, 2026?", "event_title_zh": "以色列与伊朗停火是否持续至 2026 年 8 月 31 日？", "duration": "2m 14s", "volume_24h": "9700000", "initial_profit": "0.30", "live_profit": "0.38", "actionable_now": True, "notification_state": "sent"},
            {"signal_id": "cross-signal-fixture", "opportunity_id": "cross-opportunity-observe-only-fixture", "market_type": "cross_venue_yes_no", "execution_mode": "observe_only", "occurred_at": "2026-08-01T01:57:00Z", "event_title": "Will Bitcoin close above $100,000 on December 31, 2026?", "event_title_zh": "比特币会在 2026 年 12 月 31 日收于 $100,000 以上吗？", "legs": [{"exchange": "predict.fun", "outcome": "YES", "token_id": "predict-yes-fixture"}, {"exchange": "polymarket", "outcome": "NO", "token_id": "poly-no-fixture"}], "duration": "38s", "volume_24h": "12800000", "initial_profit": "0.20", "live_profit": "0.20", "annualized_yield": "0.201", "actionable_now": True, "notification_state": "sent"},
            {"signal_id": "signal-fed", "opportunity_id": "opp-fed", "occurred_at": "2026-08-01T01:55:00Z", "event_title": "Will the Fed cut rates in September 2026?", "event_title_zh": "2026 年 9 月美联储是否降息？", "duration": "18s", "volume_24h": "15400000", "initial_profit": "0.20", "live_profit": "0.24", "actionable_now": False, "notification_state": "failed"},
            {"signal_id": "signal-closed", "opportunity_id": "opp-closed", "occurred_at": "2026-08-01T01:50:00Z", "ended_at": "2026-08-01T01:50:41Z", "event_title": "Will Ethereum exceed $6,000 before September?", "event_title_zh": "以太坊会在 9 月前突破 $6,000？", "duration": "41s", "volume_24h": "7100000", "initial_profit": "0.15", "live_profit": "0.18", "actionable_now": False, "notification_state": "sent"},
            {"signal_id": "signal-threshold", "opportunity_id": "threshold-approved", "market_id": "threshold-approved", "market_type": "threshold_hedge", "occurred_at": "2026-08-01T01:48:00Z", "event_title": "Will Bitcoin be above $90,000 on December 31, 2026? / Will Bitcoin be above $100,000 on December 31, 2026?", "event_title_zh": "比特币在 12 月 31 日是否高于 9 万美元？ / 比特币在 12 月 31 日是否高于 10 万美元？", "volume_24h": "6800000", "minimum_profit": "0.54", "total_max_cost": "19.46", "maximum_fee": "0.12", "annualized_yield": "0.0053", "remaining_days": "152.6", "resolution_at": "2026-12-31T00:00:00Z", "eligibility_reason": "annualized_yield_below_minimum", "actionable_now": False, "notification_state": "not_sent"},
        ]
    if kind == "executions":
        legs = [{"exchange": "predict.fun", "outcome": "YES"}, {"exchange": "polymarket", "outcome": "NO"}]
        return [
            {"completed_at": "今天 14:36:12", "event_title": "停火持续至 8 月 31 日？", "quantity": "20 组", "actual_cost": "18.80", "merge_value": "20.00", "status": "已合并", "realized_profit": "1.20"},
            {"completed_at": "今天 14:40:00", "event_title": "跨所比特币阈值", "market_type": "cross_venue_yes_no", "legs": legs, "state": "submitting", "status": "submitting", "quantity": "5", "actual_cost": "4.80"},
            {"completed_at": "今天 14:41:00", "event_title": "跨所比特币阈值", "market_type": "cross_venue_yes_no", "legs": legs, "state": "reconciling", "status": "reconciling", "quantity": "5", "actual_cost": "4.80"},
            {"completed_at": "今天 14:42:00", "event_title": "跨所比特币阈值", "market_type": "cross_venue_yes_no", "legs": legs, "state": "holding_to_resolution", "status": "待兑付", "redemption_status": "pending", "quantity": "5", "actual_cost": "4.80"},
        ]
    return [
        {"happened_at": "今天 14:36:09", "event_title": "停火持续至 8 月 31 日？", "reason": "YES 成交、NO 被拒", "remediation": "卖回 20 YES", "loss": "-0.60", "status": "已消除敞口 · 待解除熔断"},
        {"happened_at": "今天 14:43:00", "event_title": "跨所比特币阈值", "market_type": "cross_venue_yes_no", "legs": [{"exchange": "predict.fun", "outcome": "YES"}, {"exchange": "polymarket", "outcome": "NO"}], "reason": "cross_dust", "remediation": "残余头寸待人工处理", "loss": "-0.80", "status": "dust_incident"},
        {"happened_at": "今天 14:44:00", "event_title": "跨所比特币阈值", "market_type": "cross_venue_yes_no", "legs": [{"exchange": "predict.fun", "outcome": "YES"}, {"exchange": "polymarket", "outcome": "NO"}], "reason": "cross_circuit_breaker_open", "remediation": "已停止新的跨所订单", "loss": "-0.80", "status": "directional_incident"},
    ]


def _prediction_history_for_scenario(kind: str, scenario: str) -> list[dict[str, object]]:
    items = _prediction_history(kind)
    if scenario == "cross-grouped-history" and kind == "executions":
        return [{
            "completed_at": "今天 15:42:00", "event_title": "跨所比特币阈值", "market_type": "cross_venue_yes_no",
            "legs": [{"exchange": "predict.fun", "outcome": "YES"}, {"exchange": "polymarket", "outcome": "NO"}],
            "state": "holding_to_resolution", "status": "待兑付", "quantity": "5", "actual_cost": "4.80",
            "lifecycle": [
                {"phase": "授权", "receipt": "0xapprove-fixture", "status": "成功"},
                {"phase": "双腿订单", "receipt": "0xorders-fixture", "status": "全部成交"},
                {"phase": "对账", "receipt": "0xreconcile-fixture", "status": "余额一致"},
                {"phase": "授权清零", "receipt": "0xcleanup-fixture", "status": "完成"},
            ],
        }]
    if scenario == "post-approval-cleared" and kind == "signals":
        return [{**items[1], "status": "未下单 · 授权已清零", "actionable_now": False}]
    if kind != "signals":
        return items
    if scenario == "cross-manual-confirm":
        return [
            {
                **item,
                "opportunity_id": "cross-opportunity-actionable-fixture",
                "execution_mode": "manual_confirm",
            }
            if item.get("market_type") == "cross_venue_yes_no"
            else item
            for item in items
        ]
    if scenario == "cross-observe-only":
        return [
            {
                **item,
                "event_title": "Observe-only cross venue Bitcoin signal",
                "event_title_zh": "跨所只观察信号",
            }
            if item.get("market_type") == "cross_venue_yes_no"
            else item
            for item in items
        ]
    if scenario == "cross-history-stale-manual":
        return [
            {
                **item,
                "event_title": "Stale manual cross venue signal",
                "event_title_zh": "历史陈旧手动信号",
                "execution_mode": "manual_confirm",
            }
            if item.get("market_type") == "cross_venue_yes_no"
            else item
            for item in items
        ]
    return items


class Handler(BaseHTTPRequestHandler):
    prediction_scenario = os.environ.get("PREDICTION_FIXTURE_SCENARIO", "ready")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path == "/":
            requested_scenario = str(query.get("prediction_state", [""])[0] or "").strip()
            if requested_scenario:
                type(self).prediction_scenario = requested_scenario
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/static/dashboard.css":
            self._send_file(STATIC_DIR / "dashboard.css", "text/css; charset=utf-8")
            return
        if path == "/static/dashboard.js":
            self._send_file(STATIC_DIR / "dashboard.js", "application/javascript; charset=utf-8")
            return
        if path == "/api/dashboard":
            self._send_json(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
            return
        if path == "/api/quotes":
            self._send_json(
                {
                    "status": "ok",
                    "requested_count": 0,
                    "quote_count": 0,
                    "missing_count": 0,
                    "fetched_at": "2026-07-07T15:30:00+08:00",
                    "last_success_at": "2026-07-07T15:30:00+08:00",
                    "stale": False,
                    "quotes": {},
                    "diagnostic": {},
                }
            )
            return
        if path == "/api/prediction-arbitrage/state":
            self._send_json(_prediction_payload(type(self).prediction_scenario))
            return
        if path == "/api/prediction-arbitrage/history":
            kind = str(query.get("kind", ["signals"])[0] or "signals")
            if type(self).prediction_scenario == "signal-error" and kind == "signals":
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.end_headers()
                return
            items = _prediction_history_for_scenario(kind, type(self).prediction_scenario)
            if type(self).prediction_scenario == "signal-closed" and kind == "signals":
                items = [{**items[0], "ended_at": "2026-08-01T02:00:10Z", "actionable_now": False, "live_profit": None}, *items[1:]]
            if type(self).prediction_scenario in {"degraded", "unavailable", "unknown"} and kind == "signals":
                items = [{**item, "actionable_now": False} for item in items]
            self._send_json({"kind": kind, "items": items, "total": len(items), "limit": 100, "offset": 0, "has_more": False})
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()
        self.wfile.write(b"not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path not in {
            "/api/prediction-arbitrage/preview",
            "/api/prediction-arbitrage/executions",
            "/api/prediction-arbitrage/circuit-breaker/reset",
            "/api/prediction-arbitrage/predict-allowance/cleanup",
            "/api/prediction-arbitrage/cross-auto/pause",
        }:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if path.endswith("/preview"):
            if type(self).prediction_scenario == "preview-rejected":
                self._send_json({"state": "rejected", "reason": "opportunity_unavailable"})
            elif type(self).prediction_scenario == "preview-incomplete":
                self._send_json({
                    "state": "previewed",
                    "preview_id": "preview-fixture",
                    "question": "以色列与伊朗停火是否持续至 2026 年 8 月 31 日？",
                    "market_type": "standard_binary",
                    "fee_status": "fee_free",
                    "quantity": "20",
                    "yes_max_price": "0.450",
                    "no_max_price": "0.490",
                    "yes_max_cost": "9.00",
                    "no_max_cost": "9.80",
                    "total_max_cost": "18.80",
                    "merge_value": "20.00",
                    "minimum_profit": "1.20",
                })
            elif body.get("opportunity_id") == "cross-opportunity-actionable-fixture":
                self._send_json({
                    "state": "previewed",
                    "preview_id": "cross-preview-fixture",
                    "execution_id": "cross-execution-fixture",
                    "opportunity_id": "cross-opportunity-actionable-fixture",
                    "question": "Will Bitcoin close above $100,000 on December 31, 2026?",
                    "market_type": "cross_venue_yes_no",
                    "intent_type": "cross_venue",
                    "pair_id": "cross-pair-fixture",
                    "direction": "PREDICT_YES_POLYMARKET_NO",
                    "net_quantity": "5",
                    "buy_legs": [
                        {"exchange": "predict.fun", "market_id": "predict-market-fixture", "condition_id": "predict-condition-fixture", "outcome": "YES", "token_id": "predict-yes-fixture", "settlement_asset": "USDT", "requested_quantity": "5", "net_quantity": "5", "max_price": "0.470", "max_cost": "2.35", "maximum_fee": "0.02", "fee_asset": "USDT", "official_url": "https://predict.fun/markets/predict-market-fixture", "quote_at": "2026-08-03T15:40:00Z"},
                        {"exchange": "polymarket", "market_id": "poly-market-fixture", "condition_id": "poly-condition-fixture", "outcome": "NO", "token_id": "poly-no-fixture", "settlement_asset": "pUSD", "requested_quantity": "5", "net_quantity": "5", "max_price": "0.490", "max_cost": "2.45", "maximum_fee": "0.00", "fee_asset": "pUSD", "official_url": "https://polymarket.com/event/poly-market-fixture", "quote_at": "2026-08-03T15:40:01Z"},
                    ],
                    "total_max_cost": "4.80",
                    "maximum_total_cost": "4.80",
                    "minimum_payout": "5.00",
                    "minimum_profit": "0.20",
                    "annualized_yield": "0.201",
                    "canonical_cutoff": "2099-12-31T23:59:00Z",
                    "codex_approval": {"decision": "APPROVE", "summary": "两所规则确认同一截止时间，YES/NO 方向直接互补。", "reviewed_at": "2026-08-03T15:41:00Z", "direct_outcome_mapping": {"predict_yes": "YES", "predict_no": "NO", "polymarket_yes": "YES", "polymarket_no": "NO"}, "evidence": [{"exchange": "predict.fun", "field": "cutoff", "quote": "at 23:59 UTC on December 31, 2099"}, {"exchange": "polymarket", "field": "cutoff", "quote": "at 23:59 UTC on December 31, 2099"}]},
                    "balances": {"predict.fun": {"asset": "USDT", "wallet_address": "0xcE23…f435", "available_balance": "12.34", "allowance_ready": True}, "polymarket": {"asset": "pUSD", "wallet_address": "0x7A4E…91C2", "available_balance": "50.00", "allowance": "50.00"}},
                    "unsettled": {"current": "35.20", "after": "40.00", "limit": "100"},
                    "policy_limits": {"max_normal_cost": "5" if type(self).prediction_scenario == "first-canary-cap5" else "20", "max_emergency_loss": "2", "max_cross_unsettled_principal": "100"},
                    "expires_at": "2026-08-03T12:00:00Z",
                })
            else:
                opportunity = dict(_prediction_payload("ready")["opportunities"][0])
                self._send_json({
                    "state": "previewed",
                    "preview_id": "preview-fixture",
                    "question": opportunity["title"],
                    "market_type": opportunity["market_type"],
                    "fee_status": opportunity["fee_status"],
                    "quantity": opportunity["quantity"],
                    "yes_max_price": opportunity["yes_price"],
                    "no_max_price": opportunity["no_price"],
                    "yes_max_cost": opportunity["yes_cost"],
                    "no_max_cost": opportunity["no_cost"],
                    "total_max_cost": opportunity["max_cost"],
                    "merge_value": opportunity["merge_value"],
                    "minimum_profit": opportunity["profit"],
                    "available_balance": "50.00",
                    "wallet_address": "0x7A4E1234567890ABCDEF91C2",
                    "policy_limits": {"max_wallet_balance": "65", "max_normal_cost": "20", "max_emergency_loss": "2", "min_estimated_profit": "1"},
                })
        elif path.endswith("/executions"):
            type(self).prediction_scenario = "success"
            self._send_json({"execution_id": "exec-fixture", "status": "executing"})
        elif path.endswith("/predict-allowance/cleanup"):
            if body != {"confirm": True}:
                self.send_response(HTTPStatus.BAD_REQUEST)
                self.end_headers()
                return
            if type(self).prediction_scenario == "cleanup-failure":
                self._send_json({"state": "rejected", "reason": "gas_unavailable", "before_allowance": "2.40", "after_allowance": "2.40", "usdt_moved": False})
            else:
                type(self).prediction_scenario = "cleanup-success"
                self._send_json({"state": "ready", "before_allowance": "2.40", "after_allowance": "0", "usdt_moved": False})
        elif path.endswith("/cross-auto/pause"):
            if body != {"confirm": True}:
                self.send_response(HTTPStatus.BAD_REQUEST)
                self.end_headers()
                return
            type(self).prediction_scenario = "cross-auto-paused"
            self._send_json({"armed": False, "reason": "operator_paused"})
        else:
            if type(self).prediction_scenario == "reset-denied":
                self._send_json({"state": "rejected", "reason": "incident_unresolved"})
            else:
                type(self).prediction_scenario = "ready"
                self._send_json({"status": "reset", "incident_id": body.get("incident_id", "incident-fixture")})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fixture_dashboard_url: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
