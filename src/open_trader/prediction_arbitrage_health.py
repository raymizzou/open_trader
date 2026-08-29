"""Independent prediction-arbitrage health check service."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .daily_premarket import build_notifier, load_env_config
from .notifications import NullNotifier, beijing_clock


HEARTBEAT_MAX_SECONDS = 60.0
UNIVERSE_MAX_SECONDS = 300.0
DEFAULT_INTERVAL_SECONDS = 7200.0
_STATE_PATH = "api/prediction-arbitrage/state"
_HEALTHZ_PATH = "healthz"
PREDICTION_SERVICE_HEALTH_SCHEMA = "open_trader.prediction_service.health.v1"
FRONTEND_GATEWAY_HEALTH_SCHEMA = "open_trader.frontend_gateway.health.v1"

_CHECK_LABELS_ZH = {
    "endpoint": "服务地址",
    "state_status": "状态接口",
    "websocket": "实时行情通道",
    "service": "服务健康",
    "heartbeat": "心跳",
    "universe": "行情刷新",
    "breaker": "熔断器",
    "cross_venue": "跨市场扫描",
    "universe_retry": "行情重试",
    "relation_catalog": "关系目录",
    "readiness": "下单就绪",
    "auto_eat": "自动吃单",
    "llm": "LLM 校验",
    "process": "进程",
    "notify": "通知配置",
    "thread": "监控线程",
}

_STATUS_VALUE_ZH = {
    "unavailable": "不可用",
    "error": "错误",
    "degraded": "降级",
}


def _check_label(name: str) -> str:
    return _CHECK_LABELS_ZH.get(name, name)


def human_age(seconds: object) -> str:
    """Render an age in seconds as a coarse human-readable Chinese string."""

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "未知"
    value = float(seconds)
    if value < 120:
        return f"{int(round(value))} 秒前"
    if value < 3600:
        return f"{value / 60:.1f} 分钟前"
    return f"{value / 3600:.1f} 小时前"


def _sha7(sha: object) -> str:
    text = str(sha or "")
    if not text.strip() or text == "unknown":
        return "未知"
    return text[:7]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    value: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str
    checks: tuple[Check, ...]
    summary: dict[str, object]
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    url: str = ""


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, Mapping) else {}


def _seconds(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _age(timestamp: object) -> float | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds(), 0.0)


def _fetch_state(url: str, timeout: float) -> Mapping[str, object]:
    request = Request(f"{url.rstrip('/')}/{_STATE_PATH}", headers={"User-Agent": "OpenTrader/1.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("state payload must be an object")
    return payload


def _fetch_healthz(url: str, timeout: float) -> Mapping[str, object]:
    request = Request(f"{url.rstrip('/')}/{_HEALTHZ_PATH}", headers={"User-Agent": "OpenTrader/1.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("health payload must be an object")
    return payload


def validate_prediction_service_health(payload: object) -> tuple[bool, str]:
    """Accept only the production Prediction Service health contract."""

    if not isinstance(payload, Mapping):
        return False, "health payload must be an object"
    expected = {
        "schema_version": PREDICTION_SERVICE_HEALTH_SCHEMA,
        "module": "prediction_service",
        "status": "running",
        "mode": "production",
        "mutations": "enabled",
        "source_state": "clean",
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            return False, f"health {field} mismatch"
    if payload.get("production_owner") is not True:
        return False, "health production_owner mismatch"
    pid = payload.get("pid")
    if type(pid) is not int or pid <= 0:
        return False, "health pid is invalid"
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip() or not Path(cwd).is_absolute():
        return False, "health cwd is invalid"
    git_sha = payload.get("git_sha")
    if not isinstance(git_sha, str) or not git_sha.strip():
        return False, "health git_sha is invalid"
    return True, ""


def validate_frontend_gateway_health(payload: object) -> tuple[bool, str]:
    """Accept only the Gateway health identity and healthy upstream."""

    if not isinstance(payload, Mapping):
        return False, "health payload must be an object"
    for field, value in {
        "schema_version": FRONTEND_GATEWAY_HEALTH_SCHEMA,
        "module": "frontend_gateway",
        "upstream_status": "ok",
        "prediction_route_mode": "service",
        "prediction_upstream_status": "ok",
    }.items():
        if payload.get(field) != value:
            return False, f"health {field} mismatch"
    return True, ""


def run_health_check(
    *,
    url: str,
    fetch_state: Callable[[str, float], Mapping[str, object]] = _fetch_state,
    fetch_healthz: Callable[[str, float], Mapping[str, object]] = _fetch_healthz,
    timeout: float = 10.0,
    notify_configured: bool = True,
) -> HealthReport:
    """Run every component check and aggregate to PASS/WARN/FAIL."""

    checks: list[Check] = []

    def add(name: str, status: str, value: str = "", reason: str = "") -> None:
        checks.append(Check(name=name, status=status, value=value, reason=reason))

    try:
        payload = fetch_state(url, timeout)
    except Exception as exc:
        payload = {}
        add("endpoint", "FAIL", value=url, reason=f"{type(exc).__name__}: {exc}")
    state_status = str(payload.get("status") or "unavailable")
    if state_status in {"unavailable", "error"}:
        add("state_status", "FAIL", value=state_status)
    else:
        add("state_status", "PASS", value=state_status)
    if payload.get("stale") is True:
        add("websocket", "FAIL", reason="实时行情状态已停滞")
    else:
        add("websocket", "PASS")
    thread = payload.get("thread")
    thread_status = str(_mapping(thread).get("status") or "")
    if isinstance(thread, Mapping) and thread_status == "running":
        add("thread", "PASS")
    elif thread_status == "gave_up":
        add("thread", "FAIL", reason="监控线程已停止重启")
    else:
        add("thread", "FAIL", reason="监控线程状态缺失")
    try:
        healthz_payload = fetch_healthz(url, timeout)
    except Exception:
        healthz_payload = {}
    healthz = _mapping(healthz_payload)
    healthz_ok, healthz_reason = validate_prediction_service_health(healthz_payload)
    add(
        "service",
        "PASS" if healthz_ok else "FAIL",
        value=url,
        reason="" if healthz_ok else healthz_reason,
    )

    health = _mapping(payload.get("health"))
    heartbeat = _seconds(health.get("heartbeat_age_seconds"))
    if heartbeat is None:
        heartbeat = _age(payload.get("heartbeat_at") or payload.get("heartbeat"))
    if heartbeat is None:
        add("heartbeat", "FAIL", reason="心跳时间戳缺失")
    elif heartbeat > HEARTBEAT_MAX_SECONDS:
        add(
            "heartbeat",
            "FAIL",
            value=f"已停滞 {human_age(heartbeat)}",
            reason=f"超过 {HEARTBEAT_MAX_SECONDS:.0f} 秒阈值",
        )
    else:
        add("heartbeat", "PASS", value=f"{heartbeat:.1f}s")

    universe = _seconds(health.get("universe_age_seconds"))
    if universe is None:
        universe = _age(payload.get("universe_refreshed_at"))
    if universe is None:
        add("universe", "FAIL", reason="行情刷新时间戳缺失")
    elif universe > UNIVERSE_MAX_SECONDS:
        add(
            "universe",
            "FAIL",
            value=f"已停滞 {human_age(universe)}",
            reason=f"超过 {UNIVERSE_MAX_SECONDS:.0f} 秒阈值",
        )
    else:
        add("universe", "PASS", value=f"{universe:.1f}s")

    breaker = _mapping(payload.get("breaker"))
    if breaker.get("open") is True:
        add("breaker", "FAIL", reason="执行熔断器已打开")
    else:
        add("breaker", "PASS")

    cross_venue = _mapping(payload.get("cross_venue"))
    cross_status = str(cross_venue.get("status") or "unavailable")
    funnel = _mapping(cross_venue.get("funnel"))
    cross_value = (
        f"matched={funnel.get('matched_pairs')} "
        f"monitored={funnel.get('monitored_pairs')} "
        f"approved={funnel.get('codex_approved_pairs')}"
    )
    if cross_status in {"unavailable", "error"}:
        add("cross_venue", "FAIL", value=cross_status, reason="跨市场发现不可用")
    elif cross_status == "degraded":
        add("cross_venue", "WARN", value=cross_status)
    else:
        add("cross_venue", "PASS", value=cross_status)

    if health.get("universe_retry_exhausted") is True:
        add("universe_retry", "FAIL", reason="行情刷新重试已耗尽")
    else:
        add("universe_retry", "PASS")

    relation = _mapping(payload.get("relation_discovery"))
    relation_status = str(relation.get("status") or "")
    catalog_status = str(_mapping(relation.get("catalog")).get("status") or "")
    if relation_status in {"degraded", "unavailable"} or catalog_status in {"degraded", "unavailable"}:
        add("relation_catalog", "WARN", value=f"discovery={relation_status or '-'} catalog={catalog_status or '-'}")
    else:
        add("relation_catalog", "PASS", value=f"discovery={relation_status or '-'} catalog={catalog_status or '-'}")

    readiness = _mapping(payload.get("readiness"))
    if readiness.get("ready") is True:
        add("readiness", "PASS")
    else:
        reason = str(readiness.get("reason") or readiness.get("status") or "unavailable")
        add("readiness", "FAIL", value=reason, reason="下单就绪被阻塞")

    auto_eat = _mapping(payload.get("auto_eat_stats"))
    mode = str(auto_eat.get("mode") or "observe_only")
    submitted = int(auto_eat.get("today_submitted") or 0)
    attempts = int(auto_eat.get("today_attempts") or 0)
    rejected = max(attempts - submitted, 0)
    realized = float(auto_eat.get("realized_pnl") or 0.0)
    if mode == "auto" and attempts > 0 and submitted == 0:
        add(
            "auto_eat",
            "WARN",
            value=f"mode={mode} submitted={submitted} rejected={rejected} realized={realized:.4f}",
            reason="自动模式启用但所有尝试均被拒绝",
        )
    else:
        add(
            "auto_eat",
            "PASS",
            value=f"mode={mode} submitted={submitted} rejected={rejected} realized={realized:.4f}",
        )

    llm_usage = _mapping(payload.get("llm_usage_24h"))
    if not llm_usage:
        add("llm", "FAIL", reason="服务 LLM 用量数据不可用")
        llm_total, llm_success = 0, 0
    else:
        try:
            llm_total = int(llm_usage.get("calls", 0) or 0)
            llm_success = int(llm_usage.get("successes", 0) or 0)
        except (TypeError, ValueError) as exc:
            add("llm", "FAIL", reason=f"服务 LLM 用量数据无效：{exc}")
            llm_total, llm_success = 0, 0
        else:
            if llm_total == 0:
                add("llm", "PASS", value="0/0", reason="窗口内无校验调用")
            elif llm_success == 0:
                add("llm", "FAIL", value=f"{llm_success}/{llm_total}", reason="窗口内无成功的 LLM 校验")
            else:
                add("llm", "PASS", value=f"{llm_success}/{llm_total}")

    pid_value = healthz.get("pid")
    pid = str(pid_value) if type(pid_value) is int and pid_value > 0 else "未知"
    sha = str(healthz.get("git_sha") or "unknown")
    if not healthz_ok:
        add("process", "FAIL", reason=healthz_reason or "预测服务身份不可用")
    else:
        add("process", "PASS", value=f"pid={pid} sha={sha}")

    if not notify_configured:
        add("notify", "WARN", reason="飞书通知未配置")

    status = "PASS"
    for check in checks:
        if check.status == "FAIL":
            status = "FAIL"
            break
        if check.status == "WARN":
            status = "WARN"
    return HealthReport(
        status=status,
        checks=tuple(checks),
        url=url,
        summary={
            "heartbeat_age": heartbeat,
            "universe_age": universe,
            "llm_total": llm_total,
            "llm_success": llm_success,
            "cross_venue": cross_status,
            "pid": pid,
            "sha": sha,
            "validation_mode": mode,
            "auto_eat_submitted": submitted,
            "auto_eat_rejected": rejected,
            "auto_eat_realized_pnl": realized,
        },
    )


def report_to_dict(report: HealthReport) -> dict[str, object]:
    return {
        "status": report.status,
        "checked_at": report.checked_at.isoformat(),
        "summary": report.summary,
        "checks": [
            {"name": check.name, "status": check.status, "value": check.value, "reason": check.reason}
            for check in report.checks
        ],
    }


def format_report(report: HealthReport) -> str:
    summary = report.summary
    heartbeat_age = summary.get("heartbeat_age")
    universe_age = summary.get("universe_age")
    if report.status == "PASS":
        return "\n".join(
            (
                f"心跳 {human_age(heartbeat_age)} · 行情刷新 {human_age(universe_age)}",
                (
                    f"LLM 校验 24h：{summary.get('llm_success')}/{summary.get('llm_total')} 成功"
                    f" · 自动吃单今日 {summary.get('auto_eat_submitted')} 笔"
                ),
                f"PID {summary.get('pid')} · 版本 {_sha7(summary.get('sha'))}",
            )
        )
    lines: list[str] = []
    if report.status == "FAIL":
        for check in report.checks:
            if check.status != "FAIL":
                continue
            lines.append(_format_check_line(check))
        for check in report.checks:
            if check.status != "WARN":
                continue
            lines.append(_format_check_line(check))
        passed = sum(1 for check in report.checks if check.status == "PASS")
        lines.append(f"其余 {passed} 项通过（心跳 {human_age(heartbeat_age)}）")
        lines.append(
            f"Dashboard：{report.url} · PID {summary.get('pid')} · 版本 {_sha7(summary.get('sha'))}"
        )
        return "\n".join(lines)
    for check in report.checks:
        if check.status != "WARN":
            continue
        lines.append(_format_check_line(check))
    lines.append(f"其余各项通过 · 心跳 {human_age(heartbeat_age)} · PID {summary.get('pid')}")
    return "\n".join(lines)


def _format_check_line(check: Check) -> str:
    value = _STATUS_VALUE_ZH.get(check.value, check.value)
    if check.name == "llm" and check.value:
        value = f"24h {check.value} 成功"
    detail = f"：{value}" if value else ""
    reason = f"（{check.reason}）" if check.reason else ""
    return f"· {_check_label(check.name)}{detail}{reason}"


def send_report(notifier: object, report: HealthReport) -> bool:
    clock = beijing_clock(report.checked_at) or "未知"
    if report.status == "PASS":
        title = f"✅ 预测套利正常（{clock}）"
    elif report.status == "FAIL":
        failed = sum(1 for check in report.checks if check.status == "FAIL")
        title = f"❌ 预测套利异常：{failed} 项失败需处理（{clock}）"
    else:
        first_warn = next((check for check in report.checks if check.status == "WARN"), None)
        label = _check_label(first_warn.name) if first_warn is not None else "警告"
        title = f"⚠️ 预测套利有警告：{label}（{clock}）"
    try:
        notifier.notify(title, format_report(report))
        return True
    except Exception:
        return False


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} pid={os.getpid()} {message}", flush=True)


def run_service(
    notifier: object,
    *,
    url: str,
    interval_seconds: float,
    once: bool = False,
    notify: bool = True,
) -> int:
    notify_configured = notify and not isinstance(notifier, NullNotifier)
    if not once:
        first = datetime.now(UTC).timestamp() + interval_seconds
        _log(
            f"health service started interval={interval_seconds:.0f}s "
            f"first_check_at={datetime.fromtimestamp(first, UTC).isoformat(timespec='seconds')}"
        )
        time.sleep(interval_seconds)
    while True:
        report = run_health_check(
            url=url,
            notify_configured=notify_configured,
        )
        _log(format_report(report).replace("\n", " | "))
        if notify_configured and not send_report(notifier, report):
            _log(f"feishu delivery failed status={report.status}")
        if once:
            return 0 if report.status == "PASS" else (1 if report.status == "WARN" else 2)
        time.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader prediction-arb health-check")
    parser.add_argument("--url", default="http://127.0.0.1:8769")
    parser.add_argument("--config", type=Path, default=Path("config/daily_premarket.env"))
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_env_config(args.config)
        notifier = NullNotifier() if args.no_notify else build_notifier(config)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"health: BLOCKED\nresult: BLOCKED\nerror: {exc}", file=sys.stderr)
        return 2
    if args.once:
        report = run_health_check(
            url=args.url,
            notify_configured=not args.no_notify and not isinstance(notifier, NullNotifier),
        )
        if args.json:
            print(json.dumps(report_to_dict(report), ensure_ascii=False))
        else:
            print(format_report(report))
        if not args.no_notify and not isinstance(notifier, NullNotifier):
            if not send_report(notifier, report):
                _log(f"feishu delivery failed status={report.status}")
        return 0 if report.status == "PASS" else (1 if report.status == "WARN" else 2)
    return run_service(
        notifier,
        url=args.url,
        interval_seconds=args.interval,
        notify=not args.no_notify,
    )
