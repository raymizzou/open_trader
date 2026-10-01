"""Observe real kernel lock contention without sleeps or global monkeypatches."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import threading
from types import SimpleNamespace


def observe_flock_contention(monkeypatch, module, path: Path | None = None) -> threading.Event:
    """Signal only after a nonblocking probe actually finds the owner still held.

    A blocking acquisition retains its real blocking contract after the probe.
    Only the selected module binding is replaced, never the shared fcntl module.
    """
    contended = threading.Event()

    def flock(fd, operation):
        descriptor = fd if isinstance(fd, int) else fd.fileno()
        selected = path is None
        if path is not None and path.exists():
            expected, actual = path.stat(), os.fstat(descriptor)
            selected = (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
        if operation & fcntl.LOCK_EX and selected:
            try:
                result = fcntl.flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                contended.set()
                if operation & fcntl.LOCK_NB:
                    raise
                return fcntl.flock(fd, operation)
            return result
        return fcntl.flock(fd, operation)

    monkeypatch.setattr(module, "fcntl", SimpleNamespace(
        **{name: getattr(fcntl, name) for name in dir(fcntl) if name != "flock"},
        flock=flock,
    ))
    return contended
