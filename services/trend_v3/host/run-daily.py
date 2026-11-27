#!/opt/homebrew/opt/python@3.12/libexec/bin/python3
"""Dispatch the Trend V3 report to the existing local trend_v3 Herdr space."""

import argparse
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_lifecycle import cleanup_old_tabs, record_launch
from daily_paper import latest_report
from daily_release import PIN, SERVICE_ROOT, SPACE_ID
from daily_support import sync_allowed
from daily_sync import folder_identity

TAIPEI = ZoneInfo("Asia/Taipei")
HERDR_BUNDLE = Path("/Applications/Herdr.app")
HERDR_STARTUP_SECONDS = 12


class DispatchError(Exception):
    """A safe operator-facing error. Never include Herdr output or argv."""


class HerdrNotExecuted(DispatchError):
    """The OS rejected execution before the Herdr command could run."""


def herdr(app, operation, *args, timeout=10):
    try:
        result = subprocess.run(
            [app, *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DispatchError(f"{operation} timed out") from None
    except OSError as error:
        if error.filename == str(app):
            raise HerdrNotExecuted(
                f"{operation}: could not execute herdr-app"
            ) from None
        raise DispatchError(f"{operation}: local CLI I/O failed") from None
    if result.returncode:
        raise DispatchError(f"{operation} failed (exit {result.returncode})")
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise DispatchError(f"{operation}: invalid JSON reply") from None
    if not isinstance(data, dict) or "error" in data:
        raise DispatchError(f"{operation}: invalid Herdr reply")
    return data


def gui_command(operation, argv):
    try:
        return subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=5,
        ).returncode
    except subprocess.TimeoutExpired:
        raise DispatchError(f"{operation} timed out") from None
    except OSError:
        raise DispatchError(f"{operation}: could not execute system command") from None


def read_inventory(app, timeout=10):
    inventory = herdr(app, "Herdr inventory", "ls", timeout=timeout)
    if not isinstance(inventory.get("spaces"), list):
        raise DispatchError("Herdr inventory: missing spaces")
    return inventory


def inventory_with_startup(app):
    try:
        return read_inventory(app)
    except HerdrNotExecuted:
        raise
    except DispatchError as error:
        process_status = gui_command(
            "Herdr process check", ["/usr/bin/pgrep", "-x", "Herdr"]
        )
        if process_status == 0:
            raise DispatchError(f"{error}; Herdr is already running") from None
        if process_status != 1:
            raise DispatchError(
                f"{error}; could not verify that Herdr is closed"
            ) from None

    if not os.access(HERDR_BUNDLE / "Contents/MacOS/Herdr", os.X_OK):
        raise DispatchError(
            "verified Herdr application is missing or is not executable"
        )
    status = gui_command(
        "Herdr background start", ["/usr/bin/open", "-g", "-a", str(HERDR_BUNDLE)]
    )
    if status:
        raise DispatchError(f"Herdr background start failed (exit {status})")

    deadline = time.monotonic() + HERDR_STARTUP_SECONDS
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            return read_inventory(app, timeout=min(2, remaining))
        except DispatchError:
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise DispatchError(
        f"Herdr startup timed out after {HERDR_STARTUP_SECONDS}s waiting for inventory"
    )


def verified_space(inventory, home):
    try:
        spaces = inventory["spaces"]
        [space] = [space for space in spaces if space["id"] == SPACE_ID]
        if (
            space["name"] != "trend_v3"
            or space["folder"] != str(home / ".local/share/trend-animals")
            or space["machine"] is not None
            or not isinstance(space["tabs"], list)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DispatchError(
            "verified local trend_v3 space is absent or changed"
        ) from None
    return space


def cloud_report_folder(home):
    folder = home / "Library/Mobile Documents/com~apple~CloudDocs/trend_v3"
    try:
        if (
            stat.S_ISDIR(folder.lstat().st_mode)
            and folder.resolve(strict=True) == folder
            and os.access(folder, os.R_OK | os.W_OK | os.X_OK)
        ):
            return folder
    except (OSError, RuntimeError):
        pass
    return None


def dispatch(home, root, day, tab_name, attempted, smoke):
    app = home / ".local/bin/herdr-app"
    codex = home / ".local/bin/codex"
    sop = home / ".config/trend-animals/daily-report-sop.md"
    report = root / ("rehearsal" if smoke else "reports") / f"{day}.html"
    reuse_identity = None
    try:
        report_stat = report.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(report_stat.st_mode):
            raise DispatchError("report path is occupied by a non-regular file")
        if smoke:
            return "existing report; launch skipped"
        try:
            _, _, verification = latest_report(root, day, PIN, source_only=True)
            reuse_identity = (verification["report_sha256"], verification["revision"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass

    inventory = inventory_with_startup(app)
    space = verified_space(inventory, home)
    if any(tab["name"] == tab_name for tab in space["tabs"]):
        return "existing tab; launch skipped"
    if any(
        tab["name"] == tab_name for item in inventory["spaces"] for tab in item["tabs"]
    ):
        raise DispatchError(
            "matching tab is in another space; inspect Herdr before retrying"
        )
    if attempted.exists():
        raise DispatchError(
            "an earlier dispatch may have started; same-day retry blocked; see SOP"
        )
    if not os.access(codex, os.X_OK):
        raise DispatchError("Codex executable is missing or cannot be executed")
    if not smoke and (
        not sop.is_file()
        or set(re.findall(r"skill-releases/([0-9a-f]{40})/", sop.read_text())) != {PIN}
    ):
        raise DispatchError("daily report SOP is missing")
    report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    permission_args = ["--yolo"]
    launch = uuid.uuid4().hex
    if smoke:
        permission_args = [
            "--sandbox",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "--ask-for-approval",
            "never",
        ]
        prompt = (
            f"Run an offline REHEARSAL for {day} (Asia/Taipei) using only "
            f"invented UUP data. "
            "Use no APIs, keys, external sources, holdings, trades, "
            "communications or other agents. "
            "Create only a private, self-contained HTML sample with REHEARSAL "
            "visible in the "
            "title and body, a simulated price, signal and rank, and zero "
            "market data fees. "
            f"Use no remote resources. Atomically save it with mode 0600 to {report}. "
            "Reply with that path and stay interactive for Ray."
        )
    else:
        prompt = (
            f"Read {sop} and {SERVICE_ROOT}/.agents/skills/trend-v3-daily-"
            f"report/SKILL.md. "
            "After trusted upload, run the same-release paper_cli plan in this"
            " original tab. "
            "Display its exact delivery card and wait for Ray. Only an "
            "explicit original-tab reply "
            f"permits paper_cli --report-date {day} cycle PLAN. Keep the original "
            "plan IDs and quantities. Preopen approvals wait for their opening. "
            "Fresh approvals after opening may submit during that item's verified "
            "market trade date after current checks, using the original report date "
            "across Taipei midnight. Keep independent confirmations available while "
            "a supervised handle waits. Read actual broker receipts. "
            "A missed preopen approval never authorizes automatic catch-up. "
            "Never manufacture approval or repeat planning on reader "
            "refresh/reconnect. "
            f"Initialize the scheduled daily session for {day} (Asia/Taipei). "
            "Ray's schedule here overrides all earlier scheduling text in the "
            "pinned skill. "
            "Keep the pinned data, evidence and reader rules. "
            f"This tab owns today's report at {report}. "
            "Start at 08:30 Asia/Taipei every calendar day, including weekends. "
            "If a current verified skill-generated report for this date "
            "already exists, "
            "reuse it without opening WeChat, fetching data or regenerating it; "
            "only the authorized iCloud copy may need recovery. "
            "When fresh board data are needed, check the applicable permission "
            "and use Computer Use in official WeChat to first "
            "read a visible 趋势动物Pro mini-program page. If the main WeChat "
            "window appears first, use Window → 趋势动物Pro. E17's bulk-history "
            "stop does not block checking access for this single daily read. "
            "Read within existing authorization and honor actual restrictions "
            "on scheduled use. Stop for account warnings or login and security "
            "prompts. Leave login to Ray. "
            "Follow the pinned visual skill to 大类资产 → 金融资产. Use "
            "overlapping scrolls to the group tail and record "
            "every displayed class name and exact strength, including zero. "
            "Keep the displayed update time, observation time and data date "
            "separate. In private run evidence, record the actual attempt or "
            "applicable prohibition, the specific blocking step if any, the "
            "observed rows and scores, and whether coverage was complete, "
            "partial or unavailable. "
            "Before any board-data API fallback, preserve the verified visual "
            "rows. Use fallback for only unreadable or missing board fields "
            "after that attempt or prohibition. With a complete visual board, "
            "make zero board-data API calls. Status and pricing metadata supply "
            "no board rows. Check board fallback pricing only when needed after "
            "the visual step. Paid or unknown-cost calls still require Ray's "
            "approval. "
            "If a fresh report is needed, run only one authorized daily pass "
            "and render one full HTML from available evidence. "
            "Missing markets or data do not trigger a coverage check, delay, "
            "or hourly retry. "
            "If the start was missed, run the pending daily pass once. Follow "
            "the skill for per-instrument outcomes. "
            "Keep the existing billing and no-replay safeguards. Release locks"
            " while waiting. "
            "Stay interactive for Ray. Read private inputs only as directed by"
            " the SOP. Keep "
            "local writes to reports and their evidence/cache. Return the link"
            " to the verified iCloud HTML or report a file or sync error. "
            "No other agents."
        )
        cloud = cloud_report_folder(home)
        proof = None
        if cloud is not None:
            try:
                identity = folder_identity(cloud)
                if identity.get("permission") == "read-write" and sync_allowed(
                    identity, None, day, launch
                ):
                    proof = dict(
                        identity, day=day, launch=launch, timestamp=time.time()
                    )
            except (OSError, ValueError, ConnectionError, subprocess.SubprocessError):
                pass
        helper = home / ".config/trend-animals/daily_sync.py"
        evidence = report.parent / "_evidence" / day
        launched_at = datetime.now(TAIPEI).isoformat()
        closeout = [
            sys.executable,
            str(SERVICE_ROOT / ".agents/skills/trend-v3-daily-report/collection.py"),
            "closeout", str(evidence / "closeout-context.json"),
            "--day", day, "--launch", launch,
            "--launched-at", launched_at,
            "--state", str(evidence / "daily-completion.json"),
            "--proof", str(evidence / "board-acquisition.json"),
        ]
        if reuse_identity:
            digest, revision = reuse_identity
            closeout.extend(("--reuse-sha256", digest, "--reuse-revision", revision))
        prompt += (
            f" Original immutable host launch ID: {launch}. Original host "
            f"folder proof JSON: " + json.dumps(proof, sort_keys=True) + ". "
            "The trusted host sync-only launchd job performs fixed sync-only "
            "work on the host, "
            "checks fresh Foundation identity/permission and latest verified "
            "report hashes, and "
            "publishes a date-bound uploaded receipt for the daily tab to "
            "verify. Only the host job may write that receipt. Do not copy "
            "on missing folder metadata or use expired proof to invent permission. "
            f"After report verification use fixed Python 3.12 and {helper} "
            f"--day {day} --revision <latest verified vNN> --launch {launch} "
            f"--proof null. "
            "This command only verifies the trusted host receipt and same "
            "current destination bytes. "
            "If pending, release locks and retry this receipt check after 60 "
            "seconds, then 5 and "
            "15 minutes at most. The fixed native host job retries "
            "independently; no collection/render/fee replay, no new "
            "daily tab. Report local bytes, host-confirmed cloud upload and "
            "other-device receipt separately. "
            "Current explicit read-only/changed identity always denies. Keep "
            "local report; no alternate "
            "destinations, auxiliary cloud files or share links. No market "
            "completeness gate. "
            "For fresh acquisition, retain private board-acquisition.json using "
            "the pinned skill's acquisition evidence contract. Prepare private "
            "closeout-context.json with mode=fresh, or mode=verified_reuse "
            "only when the verified reused report matches the host's pre-launch "
            "source digest and revision. Record that original digest as "
            "reused_sha256, separate from the current display digest. "
            "Record independent lifecycle facts in the context: "
            "session=current real Codex UUID, verified revision and sha256 if known, "
            "recheck=complete or not_scheduled, pending=false only if no "
            "tasks/timers remain, "
            "sync=uploaded only after matching trusted host receipt, "
            "next_check_execution=complete "
            "only when no check remains, turn=expected completed hc turn (read"
            " working n as n+1, done/idle n as n), last_user_at=latest actual "
            "non-context role=user response_item (or legacy user_message) "
            "timestamp from this session, and completed_at=ISO timestamp. "
            "Run the pinned closeout command when the acquisition outcome is "
            "known, even if render or sync is blocked. Refresh the context "
            "and run the same command with the same acquisition proof when "
            "later lifecycle facts settle. Run this command:\n"
            f"{shlex.join(closeout)}\n"
            "Only this command writes the receipt. Report workflow_failed or "
            "a command error explicitly to Ray in this tab. Report partial or "
            "unavailable coverage as such and keep the available HTML. "
            "Acquisition status does not report trading opportunities or set "
            "render, sync or paper results. A failed closeout never starts a replay. "
            "Read your exact terminal ID from the trusted daily-state launch "
            "receipt before reading "
            "hc identity; do not infer it from inherited environment. Later "
            "Ray work invalidates prior "
            "completion until refreshed. Preserve all waiting checks, "
            "especially existing 11/23. "
            "Stay interactive for Ray; tomorrow's cleanup may close only "
            "verified owned inactive tabs."
        )
    if not smoke:
        try:
            cleanup_old_tabs(home, root, day, app, herdr, verified_space)
        except Exception:
            # Cleanup must never suppress today's authorized single dispatch.
            pass
    args = {
        "name": tab_name,
        "space": SPACE_ID,
        "focus": False,
        "folder": str(root),
        "argv": [
            str(codex),
            "--no-daemon",
            "--model",
            "gpt-6.2-sol",
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            'service_tier="default"',
            *permission_args,
            "--cd",
            str(root),
            prompt,
        ],
    }
    # Reserve the date before requesting a side effect. Keep the reservation if
    # Herdr might have received the request or if this process is interrupted.
    with attempted.open("x") as marker:
        marker.write(datetime.now(TAIPEI).isoformat(timespec="seconds") + "\n")
        marker.flush()
        os.fsync(marker.fileno())
    try:
        herdr(app, "Herdr tab creation", "do", "tab.new", json.dumps(args))
    except HerdrNotExecuted:
        attempted.unlink()
        raise
    except DispatchError as error:
        raise DispatchError(
            f"{error}; dispatch outcome unknown; same-day retry blocked; see SOP"
        ) from None

    try:
        created = verified_space(herdr(app, "Herdr tab readback", "ls"), home)
        terminals = [
            entry["terminal"]
            for tab in created["tabs"]
            if tab["name"] == tab_name
            for entry in tab["terminals"]
            if entry.get("pane") and entry.get("terminal")
        ]
    except DispatchError as error:
        raise DispatchError(f"{error}; same-day retry blocked; see SOP") from None
    except (KeyError, TypeError, AttributeError):
        terminals = []
    if len(terminals) != 1:
        raise DispatchError(
            "visible tab readback unverified; same-day retry blocked; see SOP"
        )
    if not smoke:
        try:
            [tab] = [t for t in created["tabs"] if t["name"] == tab_name]
            record_launch(home, root, day, launch, tab, terminals[0], SPACE_ID, proof)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return (
                f"tab created; terminal={terminals[0]} "
                "(ownership unverified; future cleanup disabled)"
            )
    return f"tab created; terminal={terminals[0]} (report generation pending)"


def run(home, day, tab_name, smoke):
    root = home / ".local/share/trend-animals"
    state = root / ".dispatch"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / f"{tab_name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "dispatch already in progress; launch skipped"
        return dispatch(
            home, root, day, tab_name, state / f"{tab_name}.attempted", smoke
        )


def native_dispatch(home):
    """launchd bootstrap uses GUI APIs only; cloud I/O runs in the native local core."""
    app = home / ".local/bin/herdr-app"
    verified_space(inventory_with_startup(app), home)
    trusted = home / ".config/trend-animals"
    argv = [
        str(home / ".local/bin/hc"),
        "exec",
        "--timeout",
        "50",
        "--cwd",
        str(trusted),
        "--",
        "/opt/homebrew/opt/python@3.12/libexec/bin/python3",
        str(trusted / "run-daily.py"),
        "--native-host",
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=55
        )
    except subprocess.TimeoutExpired:
        raise DispatchError(
            "native dispatcher timed out; outcome unknown; preserve same-day "
            "reservation; see SOP"
        ) from None
    except OSError:
        raise DispatchError(
            "native dispatcher unavailable; no automatic daily replay"
        ) from None
    if result.returncode:
        raise DispatchError(
            f"native dispatcher failed (exit {result.returncode}); preserve "
            f"same-day reservation; see SOP"
        )
    try:
        return result.stdout.decode().strip()
    except UnicodeError:
        raise DispatchError(
            "native dispatcher result unavailable; preserve same-day reservation"
        ) from None


def main():
    # Host must never execute a daily-workspace-controlled launcher/helper.
    trusted = Path.home() / ".config/trend-animals"
    if Path(__file__).resolve().parent != trusted.resolve():
        print(
            "ERROR host launcher must be installed outside "
            "the daily writable workspace",
            file=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke", action="store_true", help="open an offline rehearsal tab"
    )
    parser.add_argument("--native-host", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    smoke = args.smoke
    os.umask(0o077)
    started = datetime.now(TAIPEI)
    day = started.date().isoformat()
    tab_name = f"trend507-smoke-{day}" if smoke else f"trend507-{day}"
    prefix = f"{started.isoformat(timespec='seconds')} {tab_name}"
    try:
        result = (
            run(Path.home(), day, tab_name, smoke)
            if smoke or args.native_host
            else native_dispatch(Path.home())
        )
    except DispatchError as error:
        print(f"{prefix} ERROR {error}", file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, AttributeError) as error:
        print(
            f"{prefix} ERROR local I/O or unexpected inventory "
            f"({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    print(f"{prefix} {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
