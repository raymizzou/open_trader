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
    history_kind = "executions" if scenario == "success" else "incidents" if scenario == "incident" else "signals"
    opportunity = {
        "opportunity_id": "opp-ceasefire",
        "event_id": "event-ceasefire",
        "title": "以色列与伊朗停火是否持续至 2026 年 8 月 31 日？",
        "venue": "Polymarket",
        "market_type": "普通二元",
        "fee_status": "免手续费",
        "updated_at": "1 秒前更新",
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
        "actionable": scenario in {"ready", "confirmation", "executing", "success"},
    }
    events = [
        {"event_id": "event-ceasefire", "title": "以色列与伊朗停火是否持续至 8 月 31 日？", "volume_24h": "9700000", "markets": "1 个普通二元市场", "profit": "1.20", "actionable": opportunity["actionable"], "details": [["停火持续至 8 月 31 日？", "普通二元 · 免手续费 · 已订阅"], ["当前执行条件", "20 组 · 最多 $18.80 · 可参与"]], "opportunities": [opportunity]},
        {"event_id": "event-btc", "title": "比特币会在 8 月突破 $150,000？", "volume_24h": "12800000", "markets": "2 个市场", "profit": "4.60", "actionable": False, "opportunities": [{"title": "主要二元市场", "volume_24h": "12800000", "actionable": False, "reason": "已订阅 · 收费市场不可参与"}]},
        {"event_id": "event-fed", "title": "2026 年 9 月美联储是否降息？", "volume_24h": "15400000", "markets": "8 个市场", "profit": "3.80", "actionable": False, "opportunities": [{"title": "多结果事件市场", "volume_24h": "15400000", "actionable": False, "reason": "已订阅 · Negative Risk 暂不可参与"}]},
        {"event_id": "event-eth", "title": "以太坊会在 9 月前突破 $6,000？", "volume_24h": "7100000", "markets": "2 个市场", "profit": "2.10", "actionable": False, "opportunities": [{"title": "主要二元市场", "volume_24h": "7100000", "actionable": False, "reason": "已订阅 · $20 上限内净利润不足 $1"}]},
        {"event_id": "event-senate", "title": "2026 年美国参议院控制权", "volume_24h": "6800000", "markets": "12 个市场", "profit": "1.40", "actionable": False, "opportunities": [{"title": "多结果事件市场", "volume_24h": "6800000", "actionable": False, "reason": "已订阅 · Negative Risk 暂不可参与"}]},
        {"event_id": "event-fed-chair", "title": "下一任美联储主席人选", "volume_24h": "5900000", "markets": "14 个市场", "profit": "1.40", "actionable": False, "opportunities": [{"title": "多结果事件市场", "volume_24h": "5900000", "markets": "14 个市场", "actionable": False, "reason": "已订阅 · Negative Risk 暂不可参与"}]},
    ]
    event_labels = {
        "event-ceasefire": ("预计净利润", "可参与"),
        "event-btc": ("毛利润上限", "仅监控 · 收费市场"),
        "event-fed": ("毛利润上限", "仅监控 · Negative Risk"),
        "event-eth": ("毛利润上限", "仅监控 · 净利润不足"),
        "event-senate": ("毛利润上限", "仅监控 · Negative Risk"),
        "event-fed-chair": ("毛利润上限", "仅监控 · Negative Risk"),
    }
    events = [
        {**event, "profit_label": event_labels.get(str(event.get("event_id")), ("预计净利润", "仅监控"))[0], "status": event_labels.get(str(event.get("event_id")), ("预计净利润", "仅监控"))[1]}
        for event in events
    ]
    if scenario == "quiet":
        opportunity = {**opportunity, "actionable": False}
        events[0] = {**events[0], "actionable": False, "opportunities": [opportunity]}
    if scenario == "loading":
        return {"status": "loading", "readiness": {"status": "checking", "wallet_address": "0x7A4E…91C2", "balance": "50.00", "geoblock": "允许交易", "first_live_order": "待首单"}, "wallet": {"masked_address": "0x7A4E…91C2"}, "masked_wallet": "0x7A4E…91C2", "events": events, "opportunities": [opportunity], "histories": {"signals": _prediction_history("signals")}, "event_count": 20, "market_count": 331, "token_count": 662, "breaker": {"open": True}, "heartbeat_at": "刚刚", "csrf_token": "fixture-csrf"}
    if scenario == "degraded":
        return {"status": "degraded", "stale": True, "readiness": {"status": "degraded", "wallet_address": "0x7A4E…91C2", "balance": "50.00", "geoblock": "检查失败", "first_live_order": "待首单"}, "wallet": {"masked_address": "0x7A4E…91C2"}, "masked_wallet": "0x7A4E…91C2", "events": events, "opportunities": [opportunity], "histories": {"signals": _prediction_history("signals")}, "signals_24h": 3, "event_count": 20, "market_count": 331, "token_count": 662, "breaker": {"open": True}, "heartbeat_at": "刚刚", "csrf_token": "fixture-csrf"}
    payload: dict[str, object] = {
        "status": "healthy" if scenario not in {"executing", "success", "incident"} else scenario,
        "readiness": {"status": "ready", "wallet_address": "0x7A4E…91C2", "balance": "$49.40" if scenario == "incident" else "$51.20" if scenario == "success" else "$50.00", "geoblock": "允许交易", "first_live_order": "已验证" if scenario == "success" else "事故熔断" if scenario == "incident" else "待首单"},
        "wallet": {"masked_address": "0x7A4E…91C2"},
        "masked_wallet": "0x7A4E…91C2",
        "balances": {"p_usd": "50.00", "allowance": "50.00"},
        "policy_limits": {"max_wallet_balance": "50", "max_normal_cost": "20", "max_emergency_loss": "2", "min_estimated_profit": "1"},
        "heartbeat_at": "刚刚",
        "event_count": 20,
        "market_count": 331,
        "token_count": 662,
        "signals_24h": 3,
        "events": events,
        "opportunities": [] if scenario == "quiet" else [opportunity],
        "histories": {history_kind: _prediction_history(history_kind)},
        "breaker": {"open": scenario == "incident", "status": "locked" if scenario == "incident" else "ready"},
        "csrf_token": "fixture-csrf",
    }
    if scenario == "executing":
        payload["current_execution"] = {"execution_id": "exec-fixture", "status": "executing", "event_title": "停火持续至 8 月 31 日？"}
    elif scenario == "success":
        payload["current_execution"] = {"execution_id": "exec-fixture", "status": "completed", "event_title": opportunity["title"], "realized_profit": "1.20", "actual_cost": "18.80", "merge_value": "20.00", "completed_at": "14:36:12"}
    elif scenario == "incident":
        payload["breaker"] = {"open": True, "status": "locked", "incident": {"incident_id": "incident-fixture", "reason": "YES 成交、NO 被拒；系统已卖回 YES，当前不平衡持仓为 0，实际损失 $0.60。macOS 与飞书已通知。", "unbalanced_positions": "0", "open_orders": "0", "balance": "$49.40 pUSD", "notification_status": "macOS 与飞书已发送"}}
        payload["status"] = "incident"
    return payload


def _prediction_history(kind: str) -> list[dict[str, object]]:
    if kind == "signals":
        return [
            {"occurred_at": "今天 14:32:08", "event_title": "停火持续至 8 月 31 日？", "duration": "2m 14s", "peak_edge": "0.060", "quantity": "20", "status": "可执行", "profit": "1.20"},
            {"occurred_at": "今天 11:08:41", "event_title": "地缘事件市场示例 B", "duration": "18s", "peak_edge": "0.055", "quantity": "20", "status": "可执行", "profit": "1.10"},
            {"occurred_at": "昨天 22:17:04", "event_title": "地缘事件市场示例 C", "duration": "41s", "peak_edge": "0.052", "quantity": "20", "status": "可执行", "profit": "1.04"},
        ]
    if kind == "executions":
        return [{"completed_at": "今天 14:36:12", "event_title": "停火持续至 8 月 31 日？", "quantity": "20 组", "actual_cost": "18.80", "merge_value": "20.00", "status": "已合并", "realized_profit": "1.20"}]
    return [{"happened_at": "今天 14:36:09", "event_title": "停火持续至 8 月 31 日？", "reason": "YES 成交、NO 被拒", "remediation": "卖回 20 YES", "loss": "-0.60", "status": "已消除敞口 · 待解除熔断"}]


class Handler(BaseHTTPRequestHandler):
    prediction_scenario = os.environ.get("PREDICTION_FIXTURE_SCENARIO", "ready")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        requested_scenario = str(query.get("scenario", [""])[0] or "").strip()
        if requested_scenario:
            type(self).prediction_scenario = requested_scenario
        if path == "/":
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
            items = _prediction_history(kind)
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
        }:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if path.endswith("/preview"):
            if type(self).prediction_scenario == "preview-rejected":
                self._send_json({"state": "rejected", "reason": "opportunity_unavailable"})
            else:
                self._send_json({"state": "previewed", "preview_id": "preview-fixture", "opportunity": _prediction_payload("ready")["opportunities"][0]})
        elif path.endswith("/executions"):
            type(self).prediction_scenario = "success"
            self._send_json({"execution_id": "exec-fixture", "status": "executing"})
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
