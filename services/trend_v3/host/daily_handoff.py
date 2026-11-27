"""Notify the verified original report session after a trusted upload."""

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from daily_lifecycle import atomic_json
from daily_release import SERVICE_ROOT, SPACE_ID

sys.path.insert(0, str(SERVICE_ROOT / "src"))
from trend_v3.paper_authority import (  # noqa: E402
    SESSION_ID,
    handoff_identity,
    native_tab_matches,
    native_terminal,
    original_prompt,
    plan_display,
    plan_displayed_at,
    read_json,
)
from trend_v3.paper_store import handoff_plan  # noqa: E402


def original_owner(home, root, launch):
    terminal = launch["terminal"]
    info = native_terminal(home, terminal)
    inventory = subprocess.run(
        [str(home / ".local/bin/herdr-app"), "ls"],
        capture_output=True,
        check=True,
        timeout=5,
    )
    spaces = json.loads(inventory.stdout)["spaces"]
    [space] = [item for item in spaces if item["id"] == SPACE_ID]
    [tab] = [item for item in space["tabs"] if item["id"] == launch["tab"]]
    [pane] = tab["terminals"]
    if (
        launch["space"] != SPACE_ID
        or space["name"] != "trend_v3"
        or space["folder"] != str(root)
        or space["machine"] is not None
        or tab["name"] != launch["name"]
        or pane["terminal"] != terminal
        or not pane.get("pane")
        or not native_tab_matches(info, launch)
        or info.get("address") != terminal
    ):
        raise ValueError("original approval tab is unavailable")
    files = subprocess.run(
        ["/usr/sbin/lsof", "-a", "-p", str(info["foreground"]["pid"]), "-Fn"],
        capture_output=True,
        check=True,
        timeout=5,
    )
    sessions = home / ".codex/sessions"
    paths = {
        Path(line[1:])
        for line in files.stdout.decode().splitlines()
        if line.startswith("n")
        and Path(line[1:]).is_relative_to(sessions)
        and line.endswith(".jsonl")
    }
    [path] = paths
    session = path.stem[-36:]
    if (
        not SESSION_ID.fullmatch(session)
        or launch.get("session") not in (None, session)
        or not original_prompt(home, session, root, launch["launch"], launch["day"])
    ):
        raise ValueError("original approval session is unavailable")
    return session, info


def check_handoff(home, root, launch, upload, verification, authorize):
    """Record actual native display independently of upload and wake results."""
    day, revision = upload["day"], upload["revision"]
    state = home / ".config/trend-animals/daily-state"
    path = state / f"{day}.paper-handoff.json"
    binding = handoff_identity(day, launch["launch"], verification)
    if upload["source_sha256"] != binding["source_sha256"]:
        return
    previous = read_json(path) if path.exists() else {}
    if previous.get("token") != binding["token"]:
        previous = binding | {"status": "pending"}
        atomic_json(path, previous)
    elif previous.get("status") == "displayed":
        return

    stamp = upload["timestamp"]
    try:
        session, info = original_owner(home, root, launch)
        if previous.get("session") not in (None, session):
            return
        scope = {"date": day, "id": session, "owner": launch["tab"]}
        database = root / "reports/_evidence/.paper-entry/state.sqlite3"
        plan, created = handoff_plan(database, scope, verification)
        if plan is not None and created is not None:
            at = plan_displayed_at(
                home,
                root,
                session,
                plan,
                created,
                datetime.fromtimestamp(stamp, UTC),
            )
            if at is not None and authorize():
                atomic_json(
                    path,
                    binding
                    | {
                        "status": "displayed",
                        "session": session,
                        "plan": plan["id"],
                        "displayed_at": at.isoformat(),
                        "display_sha256": sha256(
                            plan_display(plan).encode()
                        ).hexdigest(),
                    },
                )
                return
        if info["status"]["state"] not in ("idle", "done") or not authorize():
            return
        if stamp - previous.get("requested_at", stamp - 120) < 120:
            return
        now_info = native_terminal(home, launch["terminal"])
        if not native_tab_matches(now_info, launch) or any(
            now_info.get(key) != info.get(key)
            for key in ("turn", "foreground", "status", "address")
        ):
            return
        token = binding["token"]
        message = (
            f"Trend V3 host delivery {day} {revision} {token}. "
            "This is a host planning notice. Open the pinned daily report skill's "
            "original-tab handoff section. Verify the latest revision and host "
            f"receipt, then run paper_cli plan --handoff {token} and show its exact "
            "display here. "
            "This notice grants no approval and asks for no order or cycle."
        )
        atomic_json(
            path,
            binding
            | {
                "status": "requested",
                "session": session,
                "requested_at": stamp,
            },
        )
        subprocess.run(
            [
                str(home / ".local/bin/hc"),
                "send",
                "--json",
                launch["terminal"],
                message,
            ],
            capture_output=True,
            check=True,
            timeout=8,
        )
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        return
