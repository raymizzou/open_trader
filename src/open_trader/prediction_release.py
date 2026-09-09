from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from typing import Mapping


RELEASE_SCHEMA = "open_trader.prediction_service.release.v1"
RUNTIME_SCHEMA = "open_trader.prediction_service.runtime.v1"
RUNTIME_STATES = {"maintenance", "ready", "failed", "stopped"}
PREFLIGHT_SCHEMA = "open_trader.prediction_service.preflight.v1"


@dataclass(frozen=True)
class PredictionReleaseManifest:
    schema_version: str
    reader_generation: int
    contract_generation: int


def load_prediction_release_manifest(path: Path) -> PredictionReleaseManifest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction release manifest is unreadable: {path}") from exc
    required = {"schema_version", "reader_generation", "contract_generation"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("prediction release manifest has invalid keys")
    if payload["schema_version"] != RELEASE_SCHEMA:
        raise ValueError("prediction release manifest has invalid schema")
    for key in ("reader_generation", "contract_generation"):
        if type(payload[key]) is not int or payload[key] < 1:
            raise ValueError(f"prediction release manifest has invalid {key}")
    return PredictionReleaseManifest(
        schema_version=RELEASE_SCHEMA,
        reader_generation=payload["reader_generation"],
        contract_generation=payload["contract_generation"],
    )


def load_prediction_runtime_record(path: Path) -> dict[str, object] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"prediction runtime record is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != RUNTIME_SCHEMA:
        raise ValueError("prediction runtime record has invalid schema")
    if not isinstance(payload.get("state"), str) or payload["state"] not in RUNTIME_STATES:
        raise ValueError("prediction runtime record has invalid state")
    return payload


def write_prediction_runtime_record(
    path: Path, payload: Mapping[str, object]
) -> None:
    state = payload.get("state")
    if not isinstance(state, str) or state not in RUNTIME_STATES:
        raise ValueError("prediction runtime state is invalid")
    record = {**dict(payload), "schema_version": RUNTIME_SCHEMA}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def inspect_prediction_release_checkout(
    checkout: Path, manifest_path: Path | None = None
) -> dict[str, object]:
    """Return independently verified release identity for a clean checkout."""
    checkout = Path(checkout).resolve()
    try:
        sha = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"release checkout cannot be verified: {checkout}") from exc
    if status:
        raise ValueError(f"release root is dirty: {checkout}")
    manifest = (checkout / "ops" / "prediction-service-release.json") if manifest_path is None else Path(manifest_path).resolve()
    try:
        relative = manifest.relative_to(checkout).as_posix()
    except ValueError as exc:
        raise ValueError("release manifest must be tracked by checkout") from exc
    try:
        subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", relative],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "cat-file", "-e", f"HEAD:{relative}"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("release manifest must be tracked by checkout") from exc
    release = load_prediction_release_manifest(manifest)
    return {
        "checkout": str(checkout),
        "git_sha": sha,
        "source_state": "clean",
        "reader_generation": release.reader_generation,
        "contract_generation": release.contract_generation,
        "manifest": str(manifest),
    }


def managed_release_identity_error(facts: Mapping[str, object]) -> str | None:
    """Explain why independently observed launchd evidence is not managed."""
    if facts.get("label_path") != facts.get("expected_plist_path"):
        return "managed launchd plist path is not verified"
    if facts.get("launchd_cwd") != facts.get("process_cwd"):
        return "managed launchd cwd disagrees with process cwd"
    expected_cwd = facts.get("expected_cwd")
    if expected_cwd and facts.get("process_cwd") != expected_cwd:
        return "managed process cwd is not verified"
    release_manifest = facts.get("release_manifest")
    process_cwd = facts.get("process_cwd")
    if not release_manifest or not process_cwd:
        return "managed launchd release manifest is not verified"
    try:
        manifest = Path(str(release_manifest)).resolve()
        checkout = Path(str(process_cwd)).resolve()
        manifest.relative_to(checkout)
        if not manifest.is_file():
            return "managed launchd release manifest is not verified"
    except (OSError, RuntimeError, ValueError):
        return "managed launchd release manifest is not verified"
    try:
        pid = int(facts["pid"])
        listener_pid = int(facts["listener_pid"])
    except (KeyError, TypeError, ValueError):
        return "managed PID evidence is not verified"
    if facts.get("listener_count") != 1 or listener_pid != pid:
        return "managed listener PID disagrees with managed PID"
    if facts.get("listener_addr") != "127.0.0.1:8769":
        return "managed listener is not verified"
    health = facts.get("health")
    if not isinstance(health, Mapping):
        return "managed health is unavailable"
    if health.get("pid") != pid:
        return "managed health PID disagrees with managed PID"
    if health.get("cwd") != facts.get("process_cwd"):
        return "managed health cwd is not verified"
    if (
        health.get("schema_version") != "open_trader.prediction_service.health.v1"
        or health.get("module") != "prediction_service"
        or health.get("status") != "running"
        or health.get("mode") != "production"
        or health.get("production_owner") is not True
        or health.get("mutations") != "enabled"
        or health.get("release_schema_version") != RELEASE_SCHEMA
        or not isinstance(health.get("git_sha"), str)
        or not isinstance(health.get("started_at"), str)
        or type(health.get("reader_generation")) is not int
        or type(health.get("contract_generation")) is not int
        or not health.get("started_at")
    ):
        return "managed health is not production-ready"
    arguments = facts.get("arguments")
    if not isinstance(arguments, list):
        return "managed launchd arguments are not verified"
    args = [str(item) for item in arguments]

    def value(flag: str) -> str | None:
        indexes = [index for index, item in enumerate(args) if item == flag]
        if len(indexes) != 1 or indexes[0] + 1 >= len(args):
            return None
        return args[indexes[0] + 1]

    if "-m" not in args:
        return "managed launchd arguments are not verified"
    module_index = args.index("-m")
    if args[module_index + 1:module_index + 3] != ["open_trader", "prediction-service"]:
        return "managed launchd arguments are not verified"
    if value("--mode") != "production":
        return "managed launchd arguments use a non-production mode"
    if value("--data-dir") != facts.get("expected_data_dir"):
        return "managed launchd data root is not verified"
    if value("--host") != "127.0.0.1" or value("--port") != "8769":
        return "managed launchd listener arguments are not verified"
    loaded_manifest = value("--release-manifest")
    try:
        loaded_manifest_path = Path(loaded_manifest).resolve() if loaded_manifest else None
    except (OSError, RuntimeError):
        loaded_manifest_path = None
    if loaded_manifest_path != manifest:
        return "managed launchd release manifest is not verified"
    lock_owner_pids = facts.get("lock_owner_pids")
    if lock_owner_pids is None:
        lock_owner_pid = facts.get("lock_owner_pid")
        lock_owner_pids = [] if lock_owner_pid is None else [lock_owner_pid]
    try:
        observed_lock_pids = {int(item) for item in lock_owner_pids}
    except (TypeError, ValueError):
        return "prediction runtime lock owner is not verified"
    if observed_lock_pids != {pid}:
        return "prediction runtime lock owner is not verified"
    return None


def ready_evidence_from_health(
    health: Mapping[str, object], *, pid: int, cwd: str, listener: str,
    stdout: str, stderr: str,
) -> dict[str, object]:
    """Build the persisted observation without trusting the prior record."""
    return {
        "pid": pid,
        "cwd": cwd,
        "listener": listener,
        "health_schema": health.get("schema_version"),
        "health_module": health.get("module"),
        "health_status": health.get("status"),
        "mode": health.get("mode"),
        "production_owner": health.get("production_owner"),
        "mutations": health.get("mutations"),
        "git_sha": health.get("git_sha"),
        "release_schema_version": health.get("release_schema_version"),
        "reader_generation": health.get("reader_generation"),
        "contract_generation": health.get("contract_generation"),
        "process_started_at": health.get("started_at"),
        "logs": {"stdout": stdout, "stderr": stderr},
    }


def archive_prediction_runtime_record(
    path: Path, audit_dir: Path, *, reason: str,
) -> Path:
    """Durably preserve exact record bytes before recovery replaces them."""
    path = Path(path)
    audit_dir = Path(audit_dir)
    raw = path.read_bytes()
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    safe_reason = "".join(character if character.isalnum() or character in "-_" else "_" for character in reason)
    target = audit_dir / f"{path.name}.recovery-{stamp}-{safe_reason}-{os.getpid()}.bak"
    audit_dir.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with NamedTemporaryFile("wb", dir=audit_dir, prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return target
