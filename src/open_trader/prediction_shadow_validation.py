"""Bounded, read-only parity validation for the isolated Prediction shadow."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from tempfile import NamedTemporaryFile
from typing import Mapping
from urllib.parse import urlsplit
from urllib.request import urlopen

from .prediction_shadow import seed_shadow_store


_LABEL = "com.open-trader.prediction-service"
_POLL_SECONDS = 5
_MAX_TIMEOUT_SECONDS = 900
_INSTALLER = "scripts/install_prediction_service_launchd.sh"
_UNINSTALLER = "scripts/uninstall_prediction_service_launchd.sh"
_DETERMINISTIC_FIELDS = (
    "venue", "venues", "strategy", "market_type", "relation_direction", "legs",
    "fee", "fees", "fee_components", "cost", "max_cost", "total_max_cost",
    "actionable", "eligibility_reason", "eligibility", "gating", "profit",
    "minimum_profit", "estimated_profit", "profit_formula",
)
_VOLATILE_FIELDS = {
    "csrf_token", "heartbeat", "heartbeat_at", "started_at", "updated_at",
    "created_at", "completed_at", "timestamp", "timestamps", "current_execution",
    "breaker", "mode", "cross_auto", "history", "events", "counters",
}
_FROZEN_EXCLUSIONS = {"csrf_token", "pid", "cwd", "git_sha", "started_at", "heartbeat", "heartbeat_at", "session"}


class _DeadlineExceeded(RuntimeError):
    pass


class _StopRequested(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineExceeded("validation deadline reached")
    return remaining


def _git_sha(repo_root: Path, *, deadline: float | None = None) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
            **({} if deadline is None else {"timeout": _remaining(deadline)}),
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _validate_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("validation URLs must be plain loopback http URLs")
    if parsed.hostname != "localhost":
        try:
            if not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError("validation URLs must use a loopback host")
        except ValueError as exc:
            if str(exc) == "validation URLs must use a loopback host":
                raise
            raise ValueError("validation URLs must use a loopback host") from exc
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ValueError("validation URL has an invalid port") from exc
    return url.rstrip("/")


def _fetch_json(url: str, timeout: float) -> dict[str, object]:
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - URL is loopback-validated.
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object from {url}")
    return payload


def _opportunities(state: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows: list[object] = []
    for value in (state.get("opportunities"), _mapping(state.get("cross_venue")).get("opportunities")):
        if isinstance(value, list):
            rows.extend(value)
    result: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("opportunity_id", row.get("id"))
        if identifier not in (None, ""):
            result[str(identifier)] = row
    return result


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _compare_live_states(
    legacy: Mapping[str, object], shadow: Mapping[str, object]
) -> list[dict[str, object]]:
    """Classify only stable shared opportunities; never compare array position."""

    differences: list[dict[str, object]] = []
    legacy_rows, shadow_rows = _opportunities(legacy), _opportunities(shadow)
    for identifier in sorted(legacy_rows.keys() - shadow_rows.keys()):
        differences.append({"classification": "sampling_difference", "opportunity_id": identifier, "side": "legacy"})
    for identifier in sorted(shadow_rows.keys() - legacy_rows.keys()):
        differences.append({"classification": "sampling_difference", "opportunity_id": identifier, "side": "shadow"})
    for identifier in sorted(legacy_rows.keys() & shadow_rows.keys()):
        for field in _DETERMINISTIC_FIELDS:
            left, right = legacy_rows[identifier].get(field), shadow_rows[identifier].get(field)
            if type(left) is not type(right) or left != right:
                differences.append({
                    "classification": "semantic_difference", "opportunity_id": identifier,
                    "field": field, "legacy": left, "shadow": right,
                })
    differences.extend(_schema_differences(legacy, shadow))
    if _volatile_projection(legacy) != _volatile_projection(shadow):
        differences.append({"classification": "isolated_state_difference"})
    return differences


def _schema_differences(legacy: object, shadow: object, field: str = "") -> list[dict[str, object]]:
    if type(legacy) is not type(shadow):
        return [{"classification": "semantic_difference", "field": field or "schema", "legacy_type": type(legacy).__name__, "shadow_type": type(shadow).__name__}]
    if isinstance(legacy, Mapping):
        differences: list[dict[str, object]] = []
        keys = set(legacy) | set(shadow)  # type: ignore[arg-type]
        for key in sorted(str(key) for key in keys):
            if key in _VOLATILE_FIELDS:
                continue
            path = f"{field}.{key}" if field else key
            if key not in legacy or key not in shadow:
                differences.append({"classification": "semantic_difference", "field": "schema", "path": path, "legacy": key in legacy, "shadow": key in shadow})
            elif key == "opportunities":
                if type(legacy[key]) is not type(shadow[key]):
                    differences.append({"classification": "semantic_difference", "field": "schema", "path": path, "legacy_type": type(legacy[key]).__name__, "shadow_type": type(shadow[key]).__name__})
            else:
                differences.extend(_schema_differences(legacy[key], shadow[key], path))
        return differences
    if isinstance(legacy, list):
        if len(legacy) != len(shadow):  # type: ignore[arg-type]
            return [{"classification": "semantic_difference", "field": "schema", "path": field, "legacy_length": len(legacy), "shadow_length": len(shadow)}]
        return [difference for left, right in zip(legacy, shadow, strict=True) for difference in _schema_differences(left, right, field)]  # type: ignore[arg-type]
    return []


def _compare_histories(
    legacy: Mapping[str, object], shadow: Mapping[str, object]
) -> list[dict[str, object]]:
    differences = _schema_differences(
        {key: value for key, value in legacy.items() if key != "items"},
        {key: value for key, value in shadow.items() if key != "items"}, "history",
    )
    if legacy.get("items") != shadow.get("items"):
        differences.append({"classification": "isolated_state_difference", "field": "history.items"})
    return differences


def _compare_frozen_payloads(legacy: object, shadow: object, field: str = "") -> list[dict[str, object]]:
    """Strict frozen-read parity, excluding only process/time/session facts."""
    if type(legacy) is not type(shadow):
        return [{"field": field or "payload", "legacy": legacy, "shadow": shadow}]
    if isinstance(legacy, Mapping):
        differences: list[dict[str, object]] = []
        for key in sorted(set(legacy) | set(shadow)):  # type: ignore[arg-type]
            name = str(key)
            if name in _FROZEN_EXCLUSIONS:
                continue
            path = f"{field}.{name}" if field else name
            if key not in legacy or key not in shadow:
                differences.append({"field": path, "legacy": key in legacy, "shadow": key in shadow})
            else:
                differences.extend(_compare_frozen_payloads(legacy[key], shadow[key], path))
        return differences
    if isinstance(legacy, list):
        if len(legacy) != len(shadow):  # type: ignore[arg-type]
            return [{"field": field, "legacy": len(legacy), "shadow": len(shadow)}]
        return [difference for left, right in zip(legacy, shadow, strict=True) for difference in _compare_frozen_payloads(left, right, field)]  # type: ignore[arg-type]
    return [] if legacy == shadow else [{"field": field, "legacy": legacy, "shadow": shadow}]


def _volatile_projection(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _volatile_projection(item)
            for key, item in value.items()
            if str(key) in _VOLATILE_FIELDS
        }
    if isinstance(value, list):
        return [_volatile_projection(item) for item in value]
    return value


def _install_shadow(
    *, repo_root: Path, runtime_root: Path, prediction_config_path: Path, deadline: float
) -> dict[str, object]:
    timeout = _remaining(deadline)
    command = [
        str(repo_root / _INSTALLER), "--runtime-root", str(runtime_root),
        "--repo-root", str(repo_root), "--python", sys.executable,
        "--config", str(prediction_config_path), "--wait-seconds", str(max(1, int(timeout))),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    evidence: dict[str, object] = {
        "command": command, "returncode": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr, "label": _LABEL,
    }
    if completed.returncode:
        raise RuntimeError(f"shadow installer failed: {completed.stderr.strip()}")
    evidence.update(_service_evidence(repo_root=repo_root, deadline=deadline))
    return evidence


def _restart_shadow(
    *, repo_root: Path, runtime_root: Path, prediction_config_path: Path, deadline: float
) -> dict[str, object]:
    return _install_shadow(
        repo_root=repo_root, runtime_root=runtime_root,
        prediction_config_path=prediction_config_path, deadline=deadline,
    )


def _service_evidence(*, repo_root: Path, deadline: float) -> dict[str, object]:
    timeout = _remaining(deadline)
    label = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{_LABEL}"], capture_output=True, text=True, check=False, timeout=timeout)
    match = re.search(r"\bpid\s*=\s*(\d+)", label.stdout)
    pid = int(match.group(1)) if match else 0
    cwd = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, check=False, timeout=_remaining(deadline)) if pid else None
    listener = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP:8769", "-sTCP:LISTEN", "-Fn"], capture_output=True, text=True, check=False, timeout=_remaining(deadline)) if pid else None
    health = _fetch_json("http://127.0.0.1:8769/healthz", _remaining(deadline))
    values = {
        "pid": pid, "cwd": next((line[1:] for line in (cwd.stdout.splitlines() if cwd else []) if line.startswith("n")), ""),
        "git_sha": str(health.get("git_sha") or ""), "label": _LABEL,
        "plist": str(Path.home() / "Library/LaunchAgents" / f"{_LABEL}.plist"),
        "listener": next((line[1:] for line in (listener.stdout.splitlines() if listener else []) if line.startswith("n")), ""),
        "health": health,
    }
    if not (pid and values["cwd"] == str(repo_root) and values["git_sha"] == _git_sha(repo_root, deadline=deadline) and values["listener"] == "127.0.0.1:8769" and health.get("status") == "running"):
        raise RuntimeError("shadow installer evidence did not verify PID/cwd/SHA/listener/health")
    return values


def _absence_evidence(*, deadline: float) -> dict[str, object]:
    plist = Path.home() / "Library/LaunchAgents" / f"{_LABEL}.plist"
    launchctl = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{_LABEL}"],
        capture_output=True, text=True, check=False, timeout=max(0.01, deadline - time.monotonic()),
    )
    listener = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-iTCP:8769", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False, timeout=max(0.01, deadline - time.monotonic()),
    )
    return {
        "label_absent": launchctl.returncode != 0 and "Could not find service" in launchctl.stderr,
        "plist_absent": not plist.exists(),
        "listener_absent": listener.returncode == 1,
        "label_check": launchctl.stderr.strip() or launchctl.stdout.strip(),
        "listener_check": listener.stdout.strip() or listener.stderr.strip(),
    }


def _uninstall_and_verify_absent(*, repo_root: Path, deadline: float) -> dict[str, object]:
    command = [str(repo_root / _UNINSTALLER)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=max(0.01, deadline - time.monotonic()))
    evidence: dict[str, object] = {
        "command": command, "returncode": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr,
    }
    if completed.returncode:
        evidence.update(_absence_evidence(deadline=deadline))
        return evidence
    evidence.update(_absence_evidence(deadline=deadline))
    return evidence


def _activity_completed_at(state: Mapping[str, object]) -> str:
    activity = _mapping(_mapping(state.get("relation_discovery")).get("activity"))
    value = activity.get("completed_at")
    return str(value) if value not in (None, "") else ""


def _provider_evidence(state: Mapping[str, object]) -> dict[str, dict[str, int]]:
    providers = _mapping(_mapping(state.get("relation_discovery")).get("llm_usage_24h_by_provider"))
    return {provider: {"calls": int(_mapping(providers.get(provider)).get("calls") or 0), **{str(key): int(value) for key, value in _mapping(providers.get(provider)).items() if isinstance(value, int)}} for provider in ("codex", "deepseek")}


def _codex_evidence(health: Mapping[str, object]) -> dict[str, dict[str, int]]:
    codex = _mapping(health.get("codex"))
    return {
        "same_venue": _counter(_mapping(codex.get("relation"))),
        "cross_venue": _counter(_mapping(codex.get("cross_venue"))),
    }


def _counter(value: Mapping[str, object]) -> dict[str, int]:
    return {"attempts": int(value.get("calls") or 0), "successes": int(value.get("successes") or 0)}


def _token_counts(state: Mapping[str, object]) -> dict[str, int]:
    usage = _mapping(_mapping(state.get("relation_discovery")).get("codex_usage_24h"))
    return {str(key): int(value) for key, value in usage.items() if "token" in str(key) and isinstance(value, int)}


def _validation_status(
    *, semantic: list[dict[str, object]], health: Mapping[str, object], activity: set[str], codex: Mapping[str, Mapping[str, int]], deepseek_calls: int, deadline: bool
) -> tuple[str, str]:
    if semantic:
        return "FAIL", "semantic parity difference"
    if health.get("first_violation") or health.get("guard_attempts"):
        return "FAIL", "read-only guard violation"
    if deepseek_calls:
        return "FAIL", "DeepSeek use observed"
    for category in ("same_venue", "cross_venue"):
        counter = codex[category]
        if counter["attempts"] < 3 or counter["successes"] < 1:
            return "BLOCKED", f"insufficient {category} Codex canary evidence"
    if len(activity) < 3:
        return "BLOCKED", "fewer than three relation discovery completions" if deadline else "relation discovery evidence incomplete"
    return "PASS", "all parity evidence complete"


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(report, handle, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def run_shadow_validation(
    *, repo_root: Path, source_data_dir: Path, runtime_root: Path,
    prediction_config_path: Path, legacy_url: str, shadow_url: str,
    timeout_seconds: int = _MAX_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Run the retained, bounded live validator; it never mutates production."""

    deadline = time.monotonic() + timeout_seconds
    if not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {_MAX_TIMEOUT_SECONDS}")
    legacy_url, shadow_url = _validate_loopback_url(legacy_url), _validate_loopback_url(shadow_url)
    runtime_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "status": "FAIL", "reason": "validation did not start", "started_at": _now(),
        "urls": {"legacy": legacy_url, "shadow": shadow_url}, "git_sha": _git_sha(repo_root, deadline=deadline),
        "seed": {}, "cycles": [], "allowed_differences": [], "semantic_differences": [],
        "codex": {"baseline": {"same_venue": {"attempts": 0, "successes": 0}, "cross_venue": {"attempts": 0, "successes": 0}}, "current": {}, "delta": {}}, "token_counts": {}, "guard_attempts": [], "restart": {}, "shutdown": {},
        "provider_evidence": {"baseline": {"codex": {"calls": 0}, "deepseek": {"calls": 0}}, "current": {"codex": {}, "deepseek": {}}, "delta": {"codex": {"calls": 0}, "deepseek": {"calls": 0}}},
    }
    status, reason = "FAIL", "validation did not start"
    previous_handlers: dict[int, object] = {}
    def stop_handler(signum: int, _frame: object) -> None:
        raise _StopRequested(f"received signal {signum}")
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop_handler)
        _remaining(deadline)
        report["seed"] = seed_shadow_store(
            source_data_dir=source_data_dir, shadow_data_dir=runtime_root / "data"
        )
        _remaining(deadline)
        report["install"] = _install_shadow(
            repo_root=repo_root, runtime_root=runtime_root,
            prediction_config_path=prediction_config_path, deadline=deadline,
        )
        completed: set[str] = set()
        semantic: list[dict[str, object]] = []
        allowed: list[dict[str, object]] = []
        last_health: dict[str, object] = {}
        while time.monotonic() < deadline:
            legacy_health = _fetch_json(f"{legacy_url}/healthz", _remaining(deadline))
            shadow_health = _fetch_json(f"{shadow_url}/healthz", _remaining(deadline))
            legacy_state = _fetch_json(f"{legacy_url}/api/prediction-arbitrage/state", _remaining(deadline))
            shadow_state = _fetch_json(f"{shadow_url}/api/prediction-arbitrage/state", _remaining(deadline))
            legacy_history = _fetch_json(f"{legacy_url}/api/prediction-arbitrage/history?kind=signals&limit=100&offset=0", _remaining(deadline))
            shadow_history = _fetch_json(f"{shadow_url}/api/prediction-arbitrage/history?kind=signals&limit=100&offset=0", _remaining(deadline))
            differences = _compare_live_states(legacy_state, shadow_state)
            history_differences = _compare_histories(legacy_history, shadow_history)
            differences.extend(history_differences)
            semantic.extend(item for item in differences if item["classification"] == "semantic_difference")
            allowed.extend(item for item in differences if item["classification"] != "semantic_difference")
            completion = _activity_completed_at(shadow_state)
            if completion:
                completed.add(completion)
            last_health = shadow_health
            report["cycles"].append({
                "at": _now(), "legacy_health": legacy_health, "shadow_health": shadow_health,
                "legacy_history": legacy_history, "shadow_history": shadow_history,
                "differences": differences, "relation_completed_at": completion,
            })
            codex = _codex_evidence(shadow_health)
            report["codex"] = {"baseline": report["codex"]["baseline"], "current": codex, "delta": codex}  # type: ignore[index]
            report["token_counts"] = _token_counts(shadow_state)
            report["guard_attempts"] = list(_mapping(shadow_health).get("guard_attempts") or [])
            provider = _provider_evidence(shadow_state)
            report["provider_evidence"] = {"baseline": {"codex": {"calls": 0}, "deepseek": {"calls": 0}}, "current": provider, "delta": provider}
            report["allowed_differences"], report["semantic_differences"] = allowed, semantic
            status, reason = _validation_status(
                semantic=semantic, health=shadow_health, activity=completed,
                codex=_mapping(_mapping(report["codex"]).get("delta")),
                deepseek_calls=int(_mapping(provider.get("deepseek")).get("calls") or 0), deadline=False,
            )
            if status in {"PASS", "FAIL"}:
                break
            time.sleep(min(_POLL_SECONDS, _remaining(deadline)))
        else:
            status, reason = _validation_status(
                semantic=semantic, health=last_health, activity=completed,
                codex=_mapping(_mapping(report["codex"]).get("delta")),
                deepseek_calls=int(_mapping(_mapping(report["provider_evidence"]).get("delta")).get("deepseek", {}).get("calls") or 0), deadline=True,
            )
        if status == "PASS":
            report["restart"] = _restart_shadow(
                repo_root=repo_root, runtime_root=runtime_root,
                prediction_config_path=prediction_config_path, deadline=deadline,
            )
            restart_health = _fetch_json(f"{shadow_url}/healthz", _remaining(deadline))
            restart_state = _fetch_json(f"{shadow_url}/api/prediction-arbitrage/state", _remaining(deadline))
            restart_differences = _compare_live_states(
                _fetch_json(f"{legacy_url}/api/prediction-arbitrage/state", _remaining(deadline)), restart_state
            )
            report["restart"].update({"health": restart_health, "differences": restart_differences})
            restart_semantic = [item for item in restart_differences if item["classification"] == "semantic_difference"]
            if restart_semantic or restart_health.get("first_violation"):
                status, reason = "FAIL", "restart parity or read-only proof failed"
    except (KeyboardInterrupt, _StopRequested):
        status, reason = "BLOCKED", "validation interrupted"
    except (_DeadlineExceeded, subprocess.TimeoutExpired):
        status, reason = "BLOCKED", "validation deadline reached"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        status, reason = "BLOCKED", f"live evidence unavailable: {exc}"
    except Exception as exc:
        status, reason = "FAIL", f"validation failed: {exc}"
    finally:
        try:
            shutdown = _uninstall_and_verify_absent(repo_root=repo_root, deadline=deadline)
        except Exception as exc:
            shutdown = {"error": str(exc), "label_absent": False, "plist_absent": False, "listener_absent": False}
        report["shutdown"] = shutdown
        if not all(shutdown.get(key) is True for key in ("label_absent", "plist_absent", "listener_absent")):
            status, reason = "FAIL", "shadow cleanup evidence incomplete"
        report["status"], report["reason"], report["ended_at"] = status, reason, _now()
        _write_report(runtime_root / "shadow-validation.json", report)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open_trader prediction-shadow-validate")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--config", dest="prediction_config_path", type=Path, required=True)
    parser.add_argument("--legacy-url", default="http://127.0.0.1:8767")
    parser.add_argument("--shadow-url", default="http://127.0.0.1:8769")
    parser.add_argument("--timeout-seconds", type=int, default=_MAX_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        report = run_shadow_validation(**vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[str(report["status"])]
