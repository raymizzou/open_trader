"""Independent prediction-arbitrage health check service."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .daily_premarket import build_notifier, load_env_config
from .notifications import NullNotifier


HEARTBEAT_MAX_SECONDS = 60.0
UNIVERSE_MAX_SECONDS = 300.0
DEFAULT_INTERVAL_SECONDS = 7200.0
_STATE_PATH = "api/prediction-arbitrage/state"
_HEALTHZ_PATH = "healthz"


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


def _fetch_healthz(url: str, timeout: float) -> bool:
    request = Request(f"{url.rstrip('/')}/{_HEALTHZ_PATH}", headers={"User-Agent": "OpenTrader/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.status == 200


def _llm_stats(data_dir: Path) -> tuple[int, int]:
    path = data_dir / "prediction_arbitrage" / "prediction_arbitrage.sqlite3"
    cutoff = datetime.now(UTC).isoformat(timespec="seconds")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute(
            """
            SELECT count(*), coalesce(sum(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0)
            FROM llm_usage
            WHERE kind = 'call' AND created_at >= ?
            """,
            (cutoff,),
        ).fetchone()
    return int(row[0]), int(row[1])


def _process_info(repo_root: Path) -> dict[str, object] | None:
    try:
        output = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
    except OSError:
        return None
    pid = None
    for line in output.splitlines():
        candidate, _, command = line.strip().partition(" ")
        if (
            candidate.isdigit()
            and "open_trader" in command
            and " dashboard " in command
            and "--port 8767" in command
        ):
            pid = candidate
            break
    if pid is None:
        return None
    sha = _dashboard_git_sha(repo_root)
    expected_sha = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"pid": pid, "sha": sha, "expected_sha": expected_sha}


def _dashboard_git_sha(repo_root: Path) -> str | None:
    """Read the git SHA the dashboard process actually loaded from its startup log."""

    log = Path(repo_root) / "logs" / "legacy_dashboard" / "launchd.out.log"
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return None
    matches = re.findall(r'"git_sha":\s*"([0-9a-f]{7,40})"', text)
    return matches[-1] if matches else None


def run_health_check(
    *,
    url: str,
    data_dir: Path,
    repo_root: Path,
    fetch_state: Callable[[str, float], Mapping[str, object]] = _fetch_state,
    fetch_healthz: Callable[[str, float], bool] = _fetch_healthz,
    llm_stats: Callable[[Path], tuple[int, int]] = _llm_stats,
    process_info: Callable[[Path], dict[str, object] | None] = _process_info,
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
        add("websocket", "FAIL", reason="stale websocket state")
    else:
        add("websocket", "PASS")
    try:
        healthz_ok = bool(fetch_healthz(url, timeout))
    except Exception:
        healthz_ok = False
    add("gateway", "PASS" if healthz_ok else "FAIL", value=url, reason="" if healthz_ok else "healthz unavailable")

    health = _mapping(payload.get("health"))
    heartbeat = _seconds(health.get("heartbeat_age_seconds"))
    if heartbeat is None:
        heartbeat = _age(payload.get("heartbeat_at") or payload.get("heartbeat"))
    if heartbeat is None:
        add("heartbeat", "FAIL", reason="heartbeat timestamp missing")
    elif heartbeat > HEARTBEAT_MAX_SECONDS:
        add("heartbeat", "FAIL", value=f"{heartbeat:.1f}s", reason=f"older than {HEARTBEAT_MAX_SECONDS:.0f}s")
    else:
        add("heartbeat", "PASS", value=f"{heartbeat:.1f}s")

    universe = _seconds(health.get("universe_age_seconds"))
    if universe is None:
        universe = _age(payload.get("universe_refreshed_at"))
    if universe is None:
        add("universe", "FAIL", reason="universe refresh timestamp missing")
    elif universe > UNIVERSE_MAX_SECONDS:
        add("universe", "FAIL", value=f"{universe:.1f}s", reason=f"older than {UNIVERSE_MAX_SECONDS:.0f}s")
    else:
        add("universe", "PASS", value=f"{universe:.1f}s")

    breaker = _mapping(payload.get("breaker"))
    if breaker.get("open") is True:
        add("breaker", "FAIL", reason="execution breaker open")
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
        add("cross_venue", "FAIL", value=cross_status, reason="cross-venue discovery unavailable")
    elif cross_status == "degraded":
        add("cross_venue", "WARN", value=cross_status)
    else:
        add("cross_venue", "PASS", value=cross_status)

    if health.get("universe_retry_exhausted") is True:
        add("universe_retry", "FAIL", reason="universe refresh retries exhausted")
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
        add("readiness", "FAIL", value=reason, reason="execution readiness blocked")

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
            reason="auto mode active but every attempt rejected",
        )
    else:
        add(
            "auto_eat",
            "PASS",
            value=f"mode={mode} submitted={submitted} rejected={rejected} realized={realized:.4f}",
        )

    try:
        llm_total, llm_success = llm_stats(data_dir)
    except Exception as exc:
        add("llm", "FAIL", reason=f"{type(exc).__name__}: {exc}")
        llm_total, llm_success = 0, 0
    else:
        if llm_total == 0:
            add("llm", "PASS", value="0/0", reason="no validation calls in window")
        elif llm_success == 0:
            add("llm", "FAIL", value=f"{llm_success}/{llm_total}", reason="no successful LLM validation in window")
        else:
            add("llm", "PASS", value=f"{llm_success}/{llm_total}")

    try:
        process = process_info(repo_root)
    except Exception:
        process = None
    if process is None:
        add("process", "FAIL", reason="dashboard process not found")
        pid, sha = "none", "unknown"
    else:
        pid = str(process.get("pid") or "none")
        sha = str(process.get("sha") or "unknown")
        expected_sha = str(process.get("expected_sha") or "")
        if sha != expected_sha:
            add("process", "WARN", value=f"pid={pid} sha={sha}", reason="running SHA differs from repo HEAD")
        else:
            add("process", "PASS", value=f"pid={pid} sha={sha}")

    if not notify_configured:
        add("notify", "WARN", reason="Feishu not configured")

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
        "summary": report.summary,
        "checks": [
            {"name": check.name, "status": check.status, "value": check.value, "reason": check.reason}
            for check in report.checks
        ],
    }


def format_report(report: HealthReport) -> str:
    summary = report.summary
    line = (
        f"{report.status} · heartbeat {summary.get('heartbeat_age') or '?'}s"
        f" · universe {summary.get('universe_age') or '?'}s"
        f" · LLM {summary.get('llm_success')}/{summary.get('llm_total')}"
        f" · cross_venue {summary.get('cross_venue')}"
        f" · PID {summary.get('pid')} · SHA {summary.get('sha')}"
    )
    if report.status == "PASS":
        return line
    lines = [line]
    for check in report.checks:
        if check.status == "PASS":
            continue
        detail = f" {check.value}" if check.value else ""
        reason = f" ({check.reason})" if check.reason else ""
        lines.append(f"- {check.status} {check.name}:{detail}{reason}")
    return "\n".join(lines)


def send_report(notifier: object, report: HealthReport) -> bool:
    try:
        notifier.notify(f"[预测套利健康检查] {report.status}", format_report(report))
        return True
    except Exception:
        return False


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} pid={os.getpid()} {message}", flush=True)


def run_service(
    notifier: object,
    *,
    url: str,
    data_dir: Path,
    repo_root: Path,
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
            data_dir=data_dir,
            repo_root=repo_root,
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
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--config", type=Path, default=Path("config/daily_premarket.env"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
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
            data_dir=args.data_dir,
            repo_root=args.repo,
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
        data_dir=args.data_dir,
        repo_root=args.repo,
        interval_seconds=args.interval,
        notify=not args.no_notify,
    )
