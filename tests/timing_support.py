"""Real-time supervision for tests that deliberately contend on real locks."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


_NODE_ENV = "OPEN_TRADER_SUPERVISED_TEST_NODE"


def supervised_process(command: list[str], *, timeout: float, env=None, cwd=None, check=False, combine_stderr=True):
    """Capture a subprocess with a watchdog and reclaim its whole process group.

    The watchdog belongs to the test supervisor, never to a business clock or
    the lock owner. Killing the group also handles executor shutdown deadlocks.
    """
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if combine_stderr else subprocess.PIPE, text=True,
        start_new_session=True, env=env, cwd=cwd,
    )
    timed_out = False
    try:
        try:
            output, error = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # SIGKILL makes cleanup independent of Python threads and signal handlers.
        tail, error_tail = process.communicate(timeout=5)
    if timed_out:
        raise AssertionError(f"test subprocess exceeded {timeout}s watchdog:\n{tail}{error_tail or ''}")
    result = subprocess.CompletedProcess(command, process.returncode, output, error)
    if check:
        result.check_returncode()
    return result


def run_test_in_subprocess(request: pytest.FixtureRequest, *, timeout: float = 30) -> bool:
    """Return True in the supervising parent, False in the test-body child.

    Call at the beginning of a real threaded test as
    ``if run_test_in_subprocess(request): return``. No test is skipped: the
    child executes the exact node and its result/assertions propagate here.
    """
    node = request.node.nodeid
    if os.environ.get(_NODE_ENV) == node:
        return False
    result = supervised_process(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", node],
        timeout=timeout,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, _NODE_ENV: node, "PYTEST_ADDOPTS": ""},
    )
    assert result.returncode == 0, result.stdout
    return True
