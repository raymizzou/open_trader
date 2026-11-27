"""Private durable paper plans, replies, and submission attempts."""

import os
import sqlite3
import stat
from contextlib import contextmanager

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS position_history (identity TEXT, as_of TEXT,
        body TEXT NOT NULL, PRIMARY KEY(identity, as_of))""",
    """CREATE TABLE IF NOT EXISTS positions (identity TEXT PRIMARY KEY,
        body TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY, report_day TEXT NOT NULL, body TEXT NOT NULL,
        created TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS current_plan (
        report_day TEXT PRIMARY KEY, id TEXT NOT NULL REFERENCES plans(id)
    )""",
    """CREATE TABLE IF NOT EXISTS replies (
        event_id TEXT PRIMARY KEY, plan TEXT NOT NULL REFERENCES plans(id),
        body TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS approvals (
        plan TEXT NOT NULL REFERENCES plans(id), item TEXT NOT NULL,
        quantity INTEGER NOT NULL, at TEXT NOT NULL,
        PRIMARY KEY (plan, item)
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        plan TEXT NOT NULL REFERENCES plans(id), item TEXT NOT NULL,
        state TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY (plan, item)
    )""",
    """CREATE TABLE IF NOT EXISTS attempts (
        token TEXT PRIMARY KEY, economic_key TEXT NOT NULL,
        plan TEXT NOT NULL REFERENCES plans(id),
        item TEXT NOT NULL, quantity INTEGER NOT NULL, number INTEGER NOT NULL,
        state TEXT NOT NULL, order_id TEXT, filled INTEGER, filled_notional TEXT,
        reason TEXT, reconciled TEXT,
        UNIQUE (economic_key, number)
    )""",
    """CREATE TABLE IF NOT EXISTS retries (
        token TEXT PRIMARY KEY REFERENCES attempts(token),
        event_id TEXT NOT NULL REFERENCES replies(event_id),
        quantity INTEGER NOT NULL, at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS attempt_dispatches (
        token TEXT PRIMARY KEY REFERENCES attempts(token) ON DELETE CASCADE,
        mode TEXT NOT NULL CHECK(mode IN ('opening', 'late', 'retry')),
        event_id TEXT NOT NULL REFERENCES replies(event_id),
        submitted_at TEXT,
        reference_price TEXT NOT NULL
    )""",
)


@contextmanager
def transaction(path):
    # SQLite must never create the main database with the host's ambient umask.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError("paper state must be owned by the caller and private")
    finally:
        os.close(fd)

    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            connection.execute(statement)
        yield connection
        connection.commit()
    finally:
        connection.close()
