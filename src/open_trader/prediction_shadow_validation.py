"""Bounded, read-only parity validation for the isolated Prediction shadow."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import ipaddress
import json
from pathlib import Path
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


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git_sha(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
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
            if left != right:
                differences.append({
                    "classification": "semantic_difference", "opportunity_id": identifier,
                    "field": field, "legacy": left, "shadow": right,
                })
    if _volatile_projection(legacy) != _volatile_projection(shadow):
        differences.append({"classification": "isolated_state_difference"})
    return differences


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
    *, repo_root: Path, runtime_root: Path, prediction_config_path: Path
) -> dict[str, object]:
    command = [
        str(repo_root / _INSTALLER), "--runtime-root", str(runtime_root),
        "--repo-root", str(repo_root), "--python", sys.executable,
        "--config", str(prediction_config_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    evidence: dict[str, object] = {
        "command": command, "returncode": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr, "label": _LABEL,
    }
    if completed.returncode:
        raise RuntimeError(f"shadow installer failed: {completed.stderr.strip()}")
    return evidence


def _restart_shadow(
    *, repo_root: Path, runtime_root: Path, prediction_config_path: Path
) -> dict[str, object]:
    return _install_shadow(
        repo_root=repo_root, runtime_root=runtime_root,
        prediction_config_path=prediction_config_path,
    )


def _absence_evidence() -> dict[str, object]:
    plist = Path.home() / "Library/LaunchAgents" / f"{_LABEL}.plist"
    launchctl = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{__import__('os').getuid()}/{_LABEL}"],
        capture_output=True, text=True, check=False,
    )
    listener = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-iTCP:8769", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False,
    )
    return {
        "label_absent": launchctl.returncode != 0 and "Could not find service" in launchctl.stderr,
        "plist_absent": not plist.exists(),
        "listener_absent": listener.returncode == 1,
        "label_check": launchctl.stderr.strip() or launchctl.stdout.strip(),
        "listener_check": listener.stdout.strip() or listener.stderr.strip(),
    }


def _uninstall_and_verify_absent(*, repo_root: Path) -> dict[str, object]:
    command = [str(repo_root / _UNINSTALLER)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    evidence: dict[str, object] = {
        "command": command, "returncode": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr,
    }
    if completed.returncode:
        evidence.update(_absence_evidence())
        return evidence
    evidence.update(_absence_evidence())
    return evidence


def _activity_completed_at(state: Mapping[str, object]) -> str:
    activity = _mapping(_mapping(state.get("relation_discovery")).get("activity"))
    value = activity.get("completed_at")
    return str(value) if value not in (None, "") else ""


def _has_deepseek(value: object) -> bool:
    if isinstance(value, Mapping):
        return any("deepseek" in str(key).casefold() or _has_deepseek(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_deepseek(item) for item in value)
    return "deepseek" in str(value).casefold()


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
    *, semantic: list[dict[str, object]], health: Mapping[str, object], activity: set[str], codex: Mapping[str, Mapping[str, int]], deepseek: bool, deadline: bool
) -> tuple[str, str]:
    if semantic:
        return "FAIL", "semantic parity difference"
    if health.get("first_violation") or health.get("guard_attempts"):
        return "FAIL", "read-only guard violation"
    if deepseek:
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

    if not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {_MAX_TIMEOUT_SECONDS}")
    legacy_url, shadow_url = _validate_loopback_url(legacy_url), _validate_loopback_url(shadow_url)
    runtime_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "status": "FAIL", "reason": "validation did not start", "started_at": _now(),
        "urls": {"legacy": legacy_url, "shadow": shadow_url}, "git_sha": _git_sha(repo_root),
        "seed": {}, "cycles": [], "allowed_differences": [], "semantic_differences": [],
        "codex": {}, "token_counts": {}, "guard_attempts": [], "restart": {}, "shutdown": {},
    }
    status, reason = "FAIL", "validation did not start"
    try:
        report["seed"] = seed_shadow_store(
            source_data_dir=source_data_dir, shadow_data_dir=runtime_root / "data"
        )
        report["install"] = _install_shadow(
            repo_root=repo_root, runtime_root=runtime_root,
            prediction_config_path=prediction_config_path,
        )
        deadline = time.monotonic() + timeout_seconds
        completed: set[str] = set()
        semantic: list[dict[str, object]] = []
        allowed: list[dict[str, object]] = []
        last_health: dict[str, object] = {}
        while time.monotonic() < deadline:
            legacy_health = _fetch_json(f"{legacy_url}/healthz", 10)
            shadow_health = _fetch_json(f"{shadow_url}/healthz", 10)
            legacy_state = _fetch_json(f"{legacy_url}/api/prediction-arbitrage/state", 10)
            shadow_state = _fetch_json(f"{shadow_url}/api/prediction-arbitrage/state", 10)
            legacy_history = _fetch_json(f"{legacy_url}/api/prediction-arbitrage/history?kind=signals&limit=100&offset=0", 10)
            shadow_history = _fetch_json(f"{shadow_url}/api/prediction-arbitrage/history?kind=signals&limit=100&offset=0", 10)
            differences = _compare_live_states(legacy_state, shadow_state)
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
            report["codex"] = _codex_evidence(shadow_health)
            report["token_counts"] = _token_counts(shadow_state)
            report["guard_attempts"] = list(_mapping(shadow_health).get("guard_attempts") or [])
            report["allowed_differences"], report["semantic_differences"] = allowed, semantic
            status, reason = _validation_status(
                semantic=semantic, health=shadow_health, activity=completed,
                codex=_mapping(report["codex"]),
                deepseek=_has_deepseek((shadow_health, shadow_state, shadow_history)), deadline=False,
            )
            if status in {"PASS", "FAIL"}:
                break
            time.sleep(_POLL_SECONDS)
        else:
            status, reason = _validation_status(
                semantic=semantic, health=last_health, activity=completed,
                codex=_mapping(report["codex"]), deepseek=False, deadline=True,
            )
        if status == "PASS":
            report["restart"] = _restart_shadow(
                repo_root=repo_root, runtime_root=runtime_root,
                prediction_config_path=prediction_config_path,
            )
            restart_health = _fetch_json(f"{shadow_url}/healthz", 10)
            restart_state = _fetch_json(f"{shadow_url}/api/prediction-arbitrage/state", 10)
            restart_differences = _compare_live_states(
                _fetch_json(f"{legacy_url}/api/prediction-arbitrage/state", 10), restart_state
            )
            report["restart"].update({"health": restart_health, "differences": restart_differences})
            restart_semantic = [item for item in restart_differences if item["classification"] == "semantic_difference"]
            if restart_semantic or restart_health.get("first_violation"):
                status, reason = "FAIL", "restart parity or read-only proof failed"
    except KeyboardInterrupt:
        status, reason = "BLOCKED", "validation interrupted"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        status, reason = "BLOCKED", f"live evidence unavailable: {exc}"
    except Exception as exc:
        status, reason = "FAIL", f"validation failed: {exc}"
    finally:
        try:
            shutdown = _uninstall_and_verify_absent(repo_root=repo_root)
        except Exception as exc:
            shutdown = {"error": str(exc), "label_absent": False, "plist_absent": False, "listener_absent": False}
        report["shutdown"] = shutdown
        if not all(shutdown.get(key) is True for key in ("label_absent", "plist_absent", "listener_absent")):
            status, reason = "FAIL", "shadow cleanup evidence incomplete"
        report["status"], report["reason"], report["ended_at"] = status, reason, _now()
        _write_report(runtime_root / "shadow-validation.json", report)
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

