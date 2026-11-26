"""Trusted date-bound display sync. No agent dispatch, data API or order submission."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_lifecycle import atomic_json
from daily_paper import continuing, refresh_display
from daily_support import SyncDenied, retry_sync, sync_allowed
from daily_sync import (
    PIN,
    VerifiedCopy,
    folder_identity,
    latest_report,
    metadata,
    read_file,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def host_sync_once(home, day, launch, now=None):
    # Validate date before constructing any path.
    if datetime.fromisoformat(day).date().isoformat() != day:
        raise ValueError("invalid date")
    root = home / ".local/share/trend-animals"
    state = home / ".config/trend-animals/daily-state"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / f"{day}.sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            refresh_display(root, day)
            source, digest, revision = latest_report(root, day)
            folder = home / "Library/Mobile Documents/com~apple~CloudDocs/trend_v3"
            identity = folder_identity(folder)
            if not sync_allowed(identity, None, day, launch):
                raise SyncDenied("fresh host share identity/permission denied")
            keys = ("path", "device", "inode", "owner", "role")
            pinned = state / f"{day}.share.json"
            launch_path = state / f"{day}.launch.json"
            anchor = None
            if launch_path.exists():
                record = json.loads(read_file(launch_path))
                anchor = record.get("cloud_identity")
            if pinned.exists():
                anchor = json.loads(read_file(pinned))
            if anchor and any(identity.get(k) != anchor.get(k) for k in keys):
                raise SyncDenied("approved share identity changed")
            if not pinned.exists():
                atomic_json(pinned, {k: identity[k] for k in keys})

            def authorize():
                current = folder_identity(folder)
                return (
                    sync_allowed(current, None, day, launch)
                    and all(
                        current.get(k) == identity[k]
                        for k in ("path", "device", "inode", "owner", "role")
                    )
                    and latest_report(root, day)[1:] == (digest, revision)
                )

            def uploaded(path):
                value = metadata(path)
                return (
                    value.get("uploaded") is True
                    and value.get("uploading") is False
                    and value.get("upload_error") is False
                )

            allowed = set()
            prior = state / f"{day}.upload.json"
            if prior.exists():
                previous = json.loads(read_file(prior))
                if previous.get("day") == day and previous.get("identity") == identity:
                    allowed.add(previous.get("sha256"))
            # Known historical verified report hashes are safe replacement predecessors.
            for p in (root / "reports/_evidence" / day).glob(
                "v*/report-verification.json"
            ):
                v = json.loads(read_file(p))
                if (
                    v.get("report_date") == day
                    and v.get("status") == "reader_verified"
                    and v.get("source", {}).get("commit") == PIN
                ):
                    allowed.add(v.get("report_sha256"))
            result = retry_sync(
                VerifiedCopy(
                    source, folder / source.name, authorize, uploaded, digest, allowed
                ),
                sleep=time.sleep,
            )
            if not authorize():
                raise ValueError("report/share changed after upload check")
            verification = json.loads(
                read_file(
                    root
                    / "reports/_evidence"
                    / day
                    / revision
                    / "report-verification.json"
                )
            )
            result.update(
                source_sha256=verification["report_sha256"],
                day=day,
                launch=launch,
                revision=revision,
                identity=identity,
                timestamp=time.time() if now is None else now,
                source="host Foundation",
                other_device_receipt="UNVERIFIED",
            )
            atomic_json(state / f"{day}.upload.json", result)
            atomic_json(
                state / f"{day}.host-status.json",
                dict(
                    status="uploaded" if result["uploaded"] else "pending",
                    day=day,
                    launch=launch,
                    timestamp=result["timestamp"],
                ),
            )
            return result
        except Exception as error:
            stamp = time.time() if now is None else now
            atomic_json(
                state / f"{day}.upload.json",
                dict(day=day, launch=launch, uploaded=False, timestamp=stamp),
            )
            atomic_json(
                state / f"{day}.host-status.json",
                dict(
                    day=day,
                    launch=launch,
                    status="denied" if isinstance(error, SyncDenied) else "pending",
                    reason=type(error).__name__,
                    timestamp=stamp,
                ),
            )
            raise


def companion(home, day, launch, window=86400, interval=60):
    state = home / ".config/trend-animals/daily-state"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = home / ".local/share/trend-animals"
    with (state / f"{day}.host.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "already running"
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            launch_path = state / f"{day}.launch.json"
            if launch_path.exists():
                launch = json.loads(read_file(launch_path))["launch"]
            try:
                host_sync_once(home, day, launch)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass
            # Copy the last terminal update before stopping after midnight.
            # The original report identity and launch remain fixed.
            if not continuing(root, day, datetime.now(TAIPEI)):
                break
            atomic_json(
                state / f"{day}.companion.json",
                dict(
                    day=day,
                    launch=launch,
                    pid=os.getpid(),
                    status="waiting",
                    next_check=time.time() + interval,
                    deadline=deadline,
                ),
            )
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        atomic_json(
            state / f"{day}.companion.json",
            dict(day=day, launch=launch, status="stopped", timestamp=time.time()),
        )
    return "stopped"


def ensure_companion(home, day, launch):
    helper = home / ".config/trend-animals/daily_host.py"
    if not helper.is_file():
        raise ValueError("trusted host helper is missing")
    # Always fixed executable and trusted source; never execute a report/request path.
    return subprocess.Popen(
        [
            "/opt/homebrew/opt/python@3.12/libexec/bin/python3",
            str(helper),
            "--day",
            day,
            "--launch",
            launch,
        ],
        cwd=str(helper.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ).pid


def main():
    home = Path.home()
    if Path(__file__).resolve().parent != home / ".config/trend-animals":
        print(
            "host helper must be installed in trusted config directory", file=sys.stderr
        )
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day")
    parser.add_argument("--launch")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--boot", action="store_true")
    args = parser.parse_args()
    day = args.day or datetime.now(TAIPEI).date().isoformat()
    launch = args.launch
    if not launch:
        path = home / ".config/trend-animals/daily-state" / f"{day}.launch.json"
        launch = (
            json.loads(read_file(path))["launch"]
            if path.exists()
            else "host-recovery-" + day
        )
    try:
        if args.once or args.boot:
            result = host_sync_once(home, day, launch)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["uploaded"] else 2
        companion(home, day, launch)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print(json.dumps(dict(day=day, status="sync pending")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
