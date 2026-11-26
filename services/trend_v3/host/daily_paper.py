"""Trusted sync adapter. No broker, approvals, planning, or order submission."""

import hashlib
import sys
from datetime import UTC, datetime

from daily_release import PIN, SERVICE_ROOT

# SERVICE_ROOT is installed from a verified immutable release, never request data.
sys.path.insert(0, str(SERVICE_ROOT / "src"))
from trend_v3.paper_authority import latest_report  # noqa: E402
from trend_v3.paper_report import (  # noqa: E402
    pending_until,
    refresh_report,
    verified_display,
)


def source_digest(root, day, revision, verification, body):
    if (
        verification.get("source", {}).get("commit") != PIN
        or verification.get("report_date") != day
        or verification.get("revision") != revision
    ):
        raise ValueError("paper source release/date/revision mismatch")
    digest = hashlib.sha256(body).hexdigest()
    if digest != verification.get("report_sha256"):
        if verified_display(root, verification) != body:
            raise ValueError("paper display does not match durable state")
    return verification["report_sha256"]


def display_digest(root, day, revision, verification, body):
    # Also verify all original frozen artifacts and latest pointer through the
    # same authority used by the scheduled paper entry, independent of digest claims.
    info = latest_report(root, day, PIN)[2]
    if info != verification or info["revision"] != revision:
        raise ValueError("paper display verification changed")
    source_digest(root, day, revision, verification, body)


def refresh_display(root, day):
    database = root / "reports/_evidence/.paper-entry/state.sqlite3"
    if database.exists():
        return refresh_report(root, day, PIN, datetime.now(UTC))
    return None


def continuing(root, day, now):
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo

    today = now.astimezone(ZoneInfo("Asia/Taipei")).date()

    report_day = date.fromisoformat(day)
    if report_day == today:
        return True
    if report_day != today - timedelta(days=1):
        return False
    closes = pending_until(root, day, PIN)
    return closes is not None and now < closes
