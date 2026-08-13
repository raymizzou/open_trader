#!/usr/bin/env python3
"""Strict Account identity proof used by the prediction cutover script.

This is deliberately a small, dependency-free contract helper.  The shell
cutover captures launchd, listener, health, and controller-status observations;
this module validates their canonical shape once and emits the sanitized
Account evidence shape consumed by ready-evidence validation.
"""

from __future__ import annotations

from datetime import datetime
import json
import plistlib
from pathlib import Path
import re
import sys
from typing import Any


HEALTH_KEYS = {
    "schema_version",
    "module",
    "status",
    "mode",
    "pid",
    "started_at",
    "api_git_sha",
    "worker_git_sha",
    "release_match",
    "source",
}
CONTROLLER_KEYS = {"pid", "cwd", "argv", "git_sha", "heartbeat_at"}
API_KEYS = {
    "pid",
    "cwd",
    "argv",
    "git_sha",
    "listener",
    "health_status",
    "health_mode",
    "health_pid",
    "api_git_sha",
    "worker_git_sha",
    "release_match",
}


def _expected_argv(
    kind: str,
    *,
    python: str,
    runtime: str,
    tiger_config_dir: str,
) -> list[str]:
    data_dir = str(Path(runtime) / "data")
    daily_config = str(Path(runtime) / "config" / "daily_premarket.env")
    if kind == "controller":
        return [
            python,
            "-m",
            "open_trader",
            "account-sync-worker",
            "--config",
            daily_config,
            "--data-dir",
            data_dir,
            "--reports-dir",
            str(Path(runtime) / "reports"),
            "--portfolio",
            str(Path(runtime) / "data" / "latest" / "portfolio.csv"),
            "--tiger-config-dir",
            tiger_config_dir,
        ]
    if kind == "api":
        return [
            python,
            "-m",
            "open_trader",
            "account-api",
            "--data-dir",
            data_dir,
            "--mode",
            "production",
            "--config",
            daily_config,
        ]
    raise ValueError(f"unknown Account process kind: {kind}")


def _load_object(raw: str, label: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _validate_argv(
    kind: str,
    argv: object,
    *,
    python: str,
    runtime: str,
    tiger_config_dir: str,
) -> bool:
    return isinstance(argv, list) and argv == _expected_argv(
        kind,
        python=python,
        runtime=runtime,
        tiger_config_dir=tiger_config_dir,
    )


def _plist_matches(
    path: str,
    *,
    kind: str,
    repo: str,
    python: str,
    runtime: str,
    tiger_config_dir: str,
) -> bool:
    try:
        plist = plistlib.loads(Path(path).read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    return (
        plist.get("WorkingDirectory") == repo
        and _validate_argv(
            kind,
            plist.get("ProgramArguments"),
            python=python,
            runtime=runtime,
            tiger_config_dir=tiger_config_dir,
        )
    )


def _launchd(
    raw: str,
    *,
    kind: str,
    expected_plist: str,
    repo: str,
    python: str,
    runtime: str,
    tiger_config_dir: str,
) -> dict[str, Any]:
    value = _load_object(raw, f"{kind} launchd observation")
    if set(value) != {"path", "cwd", "pid", "argv"}:
        raise ValueError("launchd observation keys are not exact")
    pid = value.get("pid")
    argv = value.get("argv")
    if (
        value.get("path") != expected_plist
        or value.get("cwd") != repo
        or type(pid) is not int
        or pid <= 0
        or not _plist_matches(
            expected_plist,
            kind=kind,
            repo=repo,
            python=python,
            runtime=runtime,
            tiger_config_dir=tiger_config_dir,
        )
        or not _validate_argv(
            kind,
            argv,
            python=python,
            runtime=runtime,
            tiger_config_dir=tiger_config_dir,
        )
    ):
        raise ValueError(f"{kind} launchd ProgramArguments are not exact")
    return {"pid": pid, "cwd": repo, "argv": argv}


def _listener(raw: str, api_pid: int) -> str:
    pids = [line[1:] for line in raw.splitlines() if re.fullmatch(r"p[1-9][0-9]*", line)]
    addresses = [line[1:] for line in raw.splitlines() if line.startswith("n")]
    if pids != [str(api_pid)] or addresses != ["127.0.0.1:8768"]:
        raise ValueError("Account API listener is not exact")
    return addresses[0]


def _health(raw: str, api_pid: int) -> dict[str, Any]:
    value = _load_object(raw, "Account API health")
    if set(value) != HEALTH_KEYS:
        raise ValueError("Account API health schema is not exact")
    if (
        value.get("schema_version") != "open_trader.account_api.health.v1"
        or value.get("module") != "account_api"
        or value.get("status") != "ok"
        or value.get("mode") != "production"
        or type(value.get("pid")) is not int
        or value["pid"] != api_pid
        or not isinstance(value.get("started_at"), str)
        or not value["started_at"]
        or not isinstance(value.get("api_git_sha"), str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", value["api_git_sha"]) is None
        or not isinstance(value.get("worker_git_sha"), str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", value["worker_git_sha"]) is None
        or value.get("release_match") is not True
        or value.get("source") != "account_sync_worker_publication"
    ):
        raise ValueError("Account API health contract is not verified")
    return value


def _status(path: str, controller_pid: int, repo: str, *, require_fresh: bool) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("controller status is not an object")
    heartbeat_at = value.get("heartbeat_at")
    heartbeat = datetime.fromisoformat(str(heartbeat_at))
    if (
        value.get("schema_version") != "open_trader.account_sync.controller.v1"
        or type(value.get("pid")) is not int
        or value["pid"] != controller_pid
        or value.get("working_directory") != repo
        or not isinstance(value.get("git_sha"), str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", value["git_sha"]) is None
        or not isinstance(heartbeat_at, str)
        or heartbeat.tzinfo is None
        or heartbeat.utcoffset() is None
        or (
            require_fresh
            and abs((datetime.now().astimezone() - heartbeat).total_seconds()) > 120
        )
    ):
        raise ValueError("controller status is not verified")
    return {
        "pid": value["pid"],
        "cwd": value["working_directory"],
        "git_sha": value["git_sha"],
        "heartbeat_at": heartbeat_at,
    }


def snapshot(
    controller_raw: str,
    api_raw: str,
    listener_raw: str,
    health_raw: str,
    status_path: str,
    *,
    controller_plist: str,
    api_plist: str,
    repo: str,
    python: str,
    runtime: str,
    tiger_config_dir: str,
    require_fresh: bool = True,
) -> dict[str, Any]:
    controller = _launchd(
        controller_raw,
        kind="controller",
        expected_plist=controller_plist,
        repo=repo,
        python=python,
        runtime=runtime,
        tiger_config_dir=tiger_config_dir,
    )
    api = _launchd(
        api_raw,
        kind="api",
        expected_plist=api_plist,
        repo=repo,
        python=python,
        runtime=runtime,
        tiger_config_dir=tiger_config_dir,
    )
    health = _health(health_raw, api["pid"])
    status = _status(status_path, controller["pid"], repo, require_fresh=require_fresh)
    if health["worker_git_sha"] != status["git_sha"]:
        raise ValueError("Account API worker SHA does not match controller status")
    return {
        "controller": {
            "pid": controller["pid"],
            "cwd": controller["cwd"],
            "argv": controller["argv"],
            "git_sha": status["git_sha"],
            "heartbeat_at": status["heartbeat_at"],
        },
        "api": {
            "pid": api["pid"],
            "cwd": api["cwd"],
            "argv": api["argv"],
            "git_sha": health["api_git_sha"],
            "listener": _listener(listener_raw, api["pid"]),
            "health_status": health["status"],
            "health_mode": health["mode"],
            "health_pid": health["pid"],
            "api_git_sha": health["api_git_sha"],
            "worker_git_sha": health["worker_git_sha"],
            "release_match": health["release_match"],
        },
    }


def validate_canonical(
    value: object,
    *,
    repo: str,
    python: str,
    runtime: str,
    tiger_config_dir: str,
    controller_plist: str,
    api_plist: str,
    require_fresh: bool,
) -> bool:
    # Persisted evidence is checked against the canonical argv contract only.
    # The plist paths are intentionally not reread here: snapshot() already
    # proved the loaded launchd argv and plist at capture time, while a later
    # plist edit must not invalidate an otherwise truthful before snapshot.
    if not isinstance(value, dict) or set(value) != {"controller", "api"}:
        return False
    controller = value.get("controller")
    api = value.get("api")
    if not isinstance(controller, dict) or set(controller) != CONTROLLER_KEYS:
        return False
    if not isinstance(api, dict) or set(api) != API_KEYS:
        return False
    try:
        heartbeat = datetime.fromisoformat(str(controller["heartbeat_at"]))
        if (
            type(controller.get("pid")) is not int
            or controller["pid"] <= 0
            or controller.get("cwd") != repo
            or not _validate_argv(
                "controller",
                controller.get("argv"),
                python=python,
                runtime=runtime,
                tiger_config_dir=tiger_config_dir,
            )
            or not isinstance(controller["git_sha"], str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", controller["git_sha"]) is None
            or heartbeat.tzinfo is None
            or heartbeat.utcoffset() is None
            or (
                require_fresh
                and abs((datetime.now().astimezone() - heartbeat).total_seconds()) > 120
            )
            or type(api.get("pid")) is not int
            or api["pid"] <= 0
            or api.get("cwd") != repo
            or not _validate_argv(
                "api",
                api.get("argv"),
                python=python,
                runtime=runtime,
                tiger_config_dir=tiger_config_dir,
            )
            or api["listener"] != "127.0.0.1:8768"
            or api["health_status"] != "ok"
            or api["health_mode"] != "production"
            or api["health_pid"] != api["pid"]
            or not isinstance(api["git_sha"], str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", api["git_sha"]) is None
            or api["api_git_sha"] != api["git_sha"]
            or api["worker_git_sha"] != controller["git_sha"]
            or api["release_match"] is not True
        ):
            return False
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return True


def main(argv: list[str]) -> int:
    operation = argv[0] if argv else ""
    try:
        if operation == "snapshot":
            (
                _, controller_raw, api_raw, listener_raw, health_raw, status_path,
                controller_plist, api_plist, repo, python, runtime, tiger_config_dir,
            ) = argv
            value = snapshot(
                controller_raw,
                api_raw,
                listener_raw,
                health_raw,
                status_path,
                controller_plist=controller_plist,
                api_plist=api_plist,
                repo=repo,
                python=python,
                runtime=runtime,
                tiger_config_dir=tiger_config_dir,
            )
            print(json.dumps(value, separators=(",", ":")))
            return 0
        if operation == "validate":
            (
                _, raw, controller_plist, api_plist, repo, python, runtime,
                tiger_config_dir, fresh_raw,
            ) = argv
            value = json.loads(raw)
            return int(
                not validate_canonical(
                    value,
                    repo=repo,
                    python=python,
                    runtime=runtime,
                    tiger_config_dir=tiger_config_dir,
                    controller_plist=controller_plist,
                    api_plist=api_plist,
                    require_fresh=fresh_raw == "1",
                )
            )
    except (ValueError, KeyError, OSError, json.JSONDecodeError):
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
